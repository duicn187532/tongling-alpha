"""SQLite-backed, fair dispatch for demo work shared by host and client gateway.

Only the host process runs workers. Gateway requests insert jobs into the same DB.
Each model call separately acquires a resource lease; an Agent never holds one
while reading sources, waiting for I/O, or preparing its next prompt.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, text

from .db import AppJob, Project, Run, Session, now, serialize
from .intelligence import settings

DEFAULTS = {
    "prd_concurrency": 1,
    "retrieval_concurrency": 4,
    "agent_concurrency": 4,
    "queue_capacity": 40,
    "per_user_jobs": 3,
    "job_timeout_seconds": 900,
    "max_retries": 1,
}
KINDS = {"AGENT": "AGENT", "GUIDANCE": "AGENT", "RAG": "RETRIEVAL", "PRD": "PRD"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_stop = threading.Event()
_dispatcher = None
_pool = ThreadPoolExecutor(max_workers=19, thread_name_prefix="tongling-job")
_last_served = {}


def config(db):
    raw = settings(db).get("demo_queue") or {}
    return {**DEFAULTS, **raw}


def validate(raw):
    result = {**DEFAULTS, **(raw or {})}
    bounds = {
        "prd_concurrency": (1, 3),
        "retrieval_concurrency": (1, 8), "agent_concurrency": (1, 8),
        "queue_capacity": (1, 200), "per_user_jobs": (1, 10),
        "job_timeout_seconds": (30, 3600), "max_retries": (0, 3),
    }
    for key, (low, high) in bounds.items():
        value = result[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 必須介於 {low}–{high}")
    return result


def owner_id(project=None):
    from .client_gateway import CURRENT_CLIENT
    owner = CURRENT_CLIENT.get()
    if project is not None:
        project_owner = (project.state or {}).get("client_owner_id")
        if owner and project_owner != owner:
            raise HTTPException(404, "找不到專案")
        return project_owner or "host"
    return owner or "host"


def public(job, include_result=True):
    row = serialize(job)
    row["job_id"] = row["id"]
    row.pop("payload", None)
    row.pop("idempotency_key", None)
    row.pop("session_id", None)
    if not include_result:
        row.pop("result", None)
    return row


def submit(db, kind, *, project=None, payload=None, idempotency_key=None):
    if kind not in KINDS:
        raise ValueError("未知工作類型")
    owner = owner_id(project)
    key = idempotency_key or str(uuid4())
    existing = db.scalar(select(AppJob).where(AppJob.session_id == owner, AppJob.idempotency_key == key))
    if existing:
        if (existing.kind != kind or existing.project_id != (project.id if project else None)
                or existing.payload != (payload or {})):
            raise HTTPException(409, "此提交識別碼已用於不同工作")
        return existing
    for active in db.scalars(select(AppJob).where(
        AppJob.session_id == owner, AppJob.kind == kind,
        AppJob.project_id == (project.id if project else None),
        AppJob.status.in_(["QUEUED", "RUNNING"])).limit(20)):
        if active.payload == (payload or {}):
            return active
    cfg = config(db)
    queued = db.scalar(select(func.count()).select_from(AppJob).where(AppJob.status == "QUEUED")) or 0
    if queued >= cfg["queue_capacity"]:
        raise HTTPException(503, "工作佇列已滿，請稍後重試")
    mine = db.scalar(select(func.count()).select_from(AppJob).where(
        AppJob.session_id == owner, AppJob.status.in_(["QUEUED", "RUNNING"]))) or 0
    if mine >= cfg["per_user_jobs"]:
        raise HTTPException(429, "你已有過多待處理工作，請等待或取消一項")
    job = AppJob(session_id=owner, project_id=project.id if project else None,
                 kind=kind, queue_class=KINDS[kind], idempotency_key=key,
                 payload=payload or {}, max_attempts=1 + cfg["max_retries"] if kind != "AGENT" else 1)
    db.add(job)
    db.flush()
    return job


def _expired(created_at, timeout):
    return datetime.fromisoformat(created_at) + timedelta(seconds=timeout) < datetime.now(timezone.utc)


def _claim():
    with Session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        cfg = config(db)
        active = list(db.scalars(select(AppJob).where(AppJob.status == "RUNNING")))
        for job in active:
            if (job.started_at and not job.cancel_requested
                    and _expired(job.started_at, cfg["job_timeout_seconds"])):
                job.cancel_requested = 1
                job.error = "工作執行逾時，已要求停止"
                if job.kind == "AGENT" and job.project_id:
                    from . import activity
                    project = db.get(Project, job.project_id)
                    activity.cancel_project(db, project.id)
                    project.epoch += 1
                    project.runtime = "PAUSED"
        by_class = {name: sum(j.queue_class == name for j in active) for name in set(KINDS.values())}
        by_owner = {}
        for job in active:
            by_owner[job.session_id] = by_owner.get(job.session_id, 0) + 1
        queued = list(db.scalars(select(AppJob).where(
            AppJob.status == "QUEUED", AppJob.available_at <= now()
        ).order_by(AppJob.created_at).limit(200)))
        for job in queued:
            if _expired(job.created_at, cfg["job_timeout_seconds"]):
                job.status, job.error, job.finished_at = "FAILED", "佇列等待逾時", now()
        eligible = [j for j in queued if j.status == "QUEUED" and
                    by_class[j.queue_class] < cfg[{"AGENT": "agent_concurrency", "PRD": "prd_concurrency", "RETRIEVAL": "retrieval_concurrency"}[j.queue_class]] and
                    by_owner.get(j.session_id, 0) < cfg["per_user_jobs"]]
        if not eligible:
            db.commit()
            return None
        # One oldest job per user, then the least recently served user.
        first = {}
        for job in eligible:
            first.setdefault(job.session_id, job)
        chosen = min(first.values(), key=lambda j: (by_owner.get(j.session_id, 0),
                      _last_served.get(j.session_id, 0), j.created_at))
        chosen.status, chosen.started_at, chosen.heartbeat_at = "RUNNING", now(), now()
        chosen.attempts += 1
        chosen.progress = 0
        _last_served[chosen.session_id] = time.monotonic()
        job_id = chosen.id
        db.commit()
        return job_id


def _execute(job_id):
    stop_pulse = threading.Event()

    def heartbeat():
        while not stop_pulse.wait(5):
            try:
                with Session.begin() as db:
                    job = db.get(AppJob, job_id)
                    if job and job.status == "RUNNING":
                        job.heartbeat_at = now()
                        if job.kind == "AGENT" and job.project_id:
                            project = db.get(Project, job.project_id)
                            job.progress = max(job.progress, min(99, int((project.state or {}).get("progress", 0)))) if project else job.progress
            except Exception:
                pass

    pulse = threading.Thread(target=heartbeat, daemon=True)
    pulse.start()
    try:
        with Session() as db:
            job = db.get(AppJob, job_id)
            kind, payload, pid = job.kind, dict(job.payload), job.project_id
        if kind == "AGENT":
            from .workflow import execute
            execute(pid, payload["run_id"], payload["epoch"])
            with Session() as db:
                run = db.get(Run, payload["run_id"])
                project = db.get(Project, pid)
                if run.status == "FAILED":
                    raise RuntimeError(run.error or "Agent 執行失敗")
                result = {"run_id": run.id, "run_status": run.status,
                          "project_runtime": project.runtime}
        elif kind == "GUIDANCE":
            from .agent_guidance import suggest
            with Session() as db:
                result = suggest(payload["brief"], settings(db))
        elif kind == "RAG":
            from .knowledge_api import search_project
            with Session() as db:
                result = search_project(db, payload["query"], pid,
                                        automatic=payload.get("automatic", False))
                db.commit()
        elif kind == "PRD":
            from .lifecycle import create_draft
            with Session.begin() as db:
                project = db.get(Project, pid)
                revision = create_draft(db, project)
                result = {"revision_id": revision.id, "version": revision.version}
        else:
            raise RuntimeError("未知工作類型")
        with Session.begin() as db:
            job = db.get(AppJob, job_id)
            # A PRD draft is committed before returning from create_draft. Once
            # committed it must remain visible even if cancellation arrived late.
            cancelled = (job.cancel_requested or job.status == "CANCELLED") and kind != "PRD"
            if kind == "AGENT" and result.get("run_status") == "CANCELLED":
                cancelled = True
            if cancelled:
                job.status, job.finished_at = ("FAILED" if job.error else "CANCELLED"), now()
                job.result = {}
            else:
                job.status, job.progress, job.result, job.finished_at, job.error = "COMPLETED", 100, result, now(), ""
    except Exception as exc:
        with Session.begin() as db:
            job = db.get(AppJob, job_id)
            if job and job.cancel_requested:
                job.status, job.finished_at = ("FAILED" if job.error else "CANCELLED"), now()
            elif job and job.status == "RUNNING":
                job.error = f"{type(exc).__name__}: {exc}"[:1000]
                if job.attempts < job.max_attempts:
                    job.status = "QUEUED"
                    job.available_at = (datetime.now(timezone.utc) + timedelta(seconds=min(30, 2 ** job.attempts))).isoformat()
                    job.started_at = None
                else:
                    job.status, job.finished_at = "FAILED", now()
    finally:
        stop_pulse.set()


def _dispatch_loop():
    while not _stop.wait(0.2):
        try:
            job_id = _claim()
            if job_id:
                _pool.submit(_execute, job_id)
        except Exception:
            _stop.wait(1)


def start():
    global _dispatcher
    with Session.begin() as db:
        for job in db.scalars(select(AppJob).where(AppJob.status == "RUNNING")):
            if job.kind == "AGENT" or job.attempts >= job.max_attempts:
                job.status, job.error, job.finished_at = "FAILED", "服務重啟中斷；可從專案檢查點重新提交", now()
            else:
                job.status, job.available_at, job.started_at = "QUEUED", now(), None
    _stop.clear()
    _dispatcher = threading.Thread(target=_dispatch_loop, name="tongling-dispatch", daemon=True)
    _dispatcher.start()


def shutdown():
    _stop.set()
    if _dispatcher:
        _dispatcher.join(timeout=2)
