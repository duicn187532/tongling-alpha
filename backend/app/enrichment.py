"""SQLite-backed, at-least-once batch loop with fenced idempotent effects."""

import re
from datetime import datetime, timezone, timedelta
from threading import Event, Thread
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, text
from .db import (
    Session,
    KnowledgeJob,
    KnowledgeItem,
    Source,
    Chunk,
    GraphEdge,
    Project,
    uid,
    now,
    serialize,
)
from .graph_store import Tags, Entities, Relations, persist_chunk, visible_sources
from .model_runtime import structured, route
from .intelligence import settings

STAGES = [
    "PARSE_CHECK",
    "TAG_GENERATION",
    "ENTITY_EXTRACTION",
    "RELATION_EXTRACTION",
    "GRAPH_UPSERT",
    "GRAPH_INDEX",
    "VALIDATION",
    "FINALIZE",
]
stop = Event()
thread = None
pool = ThreadPoolExecutor(max_workers=4)
active = set()


def later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def create(db, kind, project_id=None, source_ids=None, force=False):
    if project_id and (
        not db.get(Project, project_id)
        or db.get(Project, project_id).status != "ACTIVE"
    ):
        raise ValueError("專案不存在或已刪除")
    existing = db.scalar(
        select(KnowledgeJob).where(
            KnowledgeJob.kind == kind,
            KnowledgeJob.project_id == project_id,
            KnowledgeJob.status.in_(
                ["QUEUED", "RUNNING", "WAITING_RETRY", "PAUSING", "PAUSED"]
            ),
        )
    )
    if existing:
        if source_ids and set(source_ids) != set(existing.config.get("selected_source_ids", [])):
            raise ValueError("此範圍已有建置工作，請先完成或取消後再選其他文件")
        return existing
    visible = {s.id: s for s in visible_sources(db, project_id)}
    selected = set(source_ids or visible)
    if not selected or selected - visible.keys():
        raise ValueError("請選擇建置範圍內的有效文件")
    job = KnowledgeJob(kind=kind, project_id=project_id,
                       config={**settings(db), "selected_source_ids": sorted(selected), "force": force})
    db.add(job)
    db.flush()
    sources = [visible[sid] for sid in sorted(selected)]
    skipped = 0
    for source in sources:
        for chunk in db.scalars(select(Chunk).where(Chunk.source_id == source.id)):
            if not force and kind == "GRAPH_BUILD":
                edge = db.scalar(select(GraphEdge).where(
                    GraphEdge.source_id == source.id,
                    GraphEdge.chunk_id == chunk.id,
                    GraphEdge.relation == "CONTAINS").limit(1))
                if edge and edge.meta.get("source_hash") == source.sha256:
                    skipped += 1
                    continue
            db.add(
                KnowledgeItem(
                    job_id=job.id,
                    source_id=source.id,
                    chunk_id=chunk.id,
                    source_hash=source.sha256,
                )
            )
    db.flush()
    job.checkpoint = {
        "documents": len(sources),
        "selected_source_ids": sorted(selected),
        "skipped_unchanged": skipped,
        "build_version": job.id,
        "total_items": len(
            list(
                db.scalars(
                    select(KnowledgeItem.id).where(KnowledgeItem.job_id == job.id)
                )
            )
        ),
    }
    if not job.checkpoint["total_items"]:
        job.status = "COMPLETED"
        job.progress = 100
    return job


def status(db, job):
    items = list(
        db.scalars(select(KnowledgeItem).where(KnowledgeItem.job_id == job.id))
    )
    done = sum(i.status == "COMPLETED" for i in items)
    failed = sum(i.status == "FAILED" for i in items)
    result = serialize(job)
    result.pop("config", None)
    result.update(
        total_items=len(items),
        completed_items=done,
        failed_items=failed,
        progress_percent=job.progress,
        current_batch=done + 1 if done < len(items) else done,
        retrying=sum(i.status == "WAITING_RETRY" for i in items),
        errors=[
            {
                "item_id": i.id,
                "source_id": i.source_id,
                "error": i.error,
                "attempts": i.attempts,
            }
            for i in items
            if i.error
        ],
    )
    return result


def control(db, job, action):
    if action == "pause" and job.status in {"QUEUED", "RUNNING", "WAITING_RETRY"}:
        job.status = "PAUSING"
    elif action == "cancel":
        job.status = "CANCELLED"
        for i in db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.job_id == job.id, KnowledgeItem.status != "COMPLETED"
            )
        ):
            i.status = "CANCELLED"
            i.lease_token = ""
            i.lease_until = None
    elif action in {"resume", "retry"}:
        if job.status == "CANCELLED":
            raise ValueError("已取消工作不可恢復，請建立新工作")
        job.status = "QUEUED"
        for i in db.scalars(
            select(KnowledgeItem).where(KnowledgeItem.job_id == job.id)
        ):
            if i.status == "FAILED" and action == "retry":
                i.status = "QUEUED"
                i.attempts = 0
                i.available_at = now()
                i.error = ""
    else:
        raise ValueError("此狀態不支援指定操作")


def summary(db, job):
    items = list(
        db.scalars(select(KnowledgeItem).where(KnowledgeItem.job_id == job.id))
    )
    done = sum(i.status == "COMPLETED" for i in items)
    failed = sum(i.status == "FAILED" for i in items)
    progress = sum(
        8 if i.status in {"COMPLETED", "FAILED"} else STAGES.index(i.stage)
        for i in items
    )
    job.progress = max(job.progress, int(100 * progress / max(1, len(items) * 8)))
    job.heartbeat_at = now()
    job.checkpoint = {
        **job.checkpoint,
        "completed_count": done,
        "failed_count": failed,
        "last_completed_item": next(
            (i.id for i in reversed(items) if i.status == "COMPLETED"), None
        ),
    }
    if job.status == "CANCELLED":
        return
    if job.status == "PAUSING" and not any(i.status == "RUNNING" for i in items):
        job.status = "PAUSED"
    elif done + failed == len(items):
        job.status = "PARTIAL" if failed else "COMPLETED"
        job.stage = "FINALIZE"
    elif job.status not in {"PAUSED", "PAUSING"}:
        job.status = (
            "RUNNING"
            if any(i.status == "RUNNING" for i in items)
            else "WAITING_RETRY"
            if any(i.status == "WAITING_RETRY" for i in items)
            else "QUEUED"
        )


def claim():
    with Session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        for job in db.scalars(
            select(KnowledgeJob).where(
                KnowledgeJob.status.in_(
                    ["QUEUED", "RUNNING", "WAITING_RETRY", "PAUSING"]
                )
            )
        ):
            for item in db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.job_id == job.id,
                    KnowledgeItem.status == "RUNNING",
                    KnowledgeItem.lease_until < now(),
                )
            ):
                item.status = "QUEUED"
                item.lease_token = ""
            summary(db, job)
            if job.status in {"PAUSED", "PAUSING", "COMPLETED", "PARTIAL"}:
                continue
            item = db.scalar(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.job_id == job.id,
                    KnowledgeItem.status.in_(["QUEUED", "WAITING_RETRY"]),
                    KnowledgeItem.available_at <= now(),
                )
                .order_by(KnowledgeItem.created_at)
                .limit(1)
            )
            if item:
                item.status = "RUNNING"
                item.lease_token = uid()
                item.lease_until = later(30)
                job.status = "RUNNING"
                job.heartbeat_at = now()
                result = (item.id, item.lease_token)
                db.commit()
                return result
        db.commit()


def checkpoint(item_id, token, stage, result=None):
    with Session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        item = db.get(KnowledgeItem, item_id)
        if not item or item.lease_token != token:
            return False
        job = db.get(KnowledgeJob, item.job_id)
        if job.status == "CANCELLED":
            return False
        item.stage = stage
        if result is not None:
            item.result = result
        job.stage = stage
        if job.status in {"PAUSING", "PAUSED"}:
            item.status = "QUEUED"
            item.lease_token = ""
            item.lease_until = None
            summary(db, job)
            db.commit()
            return False
        item.lease_until = later(30)
        summary(db, job)
        db.commit()
        return True


def process(item_id, token):
    pulse_stop = Event()

    def heartbeat():
        while not pulse_stop.wait(3):
            with Session() as db:
                i = db.get(KnowledgeItem, item_id)
                if not i or i.lease_token != token:
                    return
                i.lease_until = later(30)
                db.get(KnowledgeJob, i.job_id).heartbeat_at = now()
                db.commit()

    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        with Session() as db:
            item = db.get(KnowledgeItem, item_id)
            job = db.get(KnowledgeJob, item.job_id)
            chunk = db.get(Chunk, item.chunk_id)
            source = db.get(Source, item.source_id)
            config = {**job.config, "_project_id": job.project_id}
            result = dict(item.result)
            kind = job.kind
            stage = item.stage
            if not chunk or not chunk.text.strip():
                raise ValueError("文件段落為空或已移除")
            if source.status != "ACTIVE" or source.sha256 != item.source_hash:
                raise ValueError("來源已更新或移除，請對最新來源建立工作")
            content = chunk.text
            title = source.title
        tasks = [
            (
                "TAG_GENERATION",
                "TAG_EXTRACTION",
                Tags,
                "抽取 3–8 個精準標籤及 taxonomy 類別，避免泛用詞。",
                lambda: {
                    "tags": [
                        {"tag": v[:80], "tag_type": "PROJECT_TOPIC", "confidence": 0.6}
                        for v in re.findall(
                            r"[A-Za-z][\w.-]{2,}|[\u3400-\u9fff]{2,12}",
                            title + " " + content,
                        )[:6]
                    ]
                },
            ),
            (
                "ENTITY_EXTRACTION",
                "ENTITY_EXTRACTION",
                Entities,
                "抽取原文實際出現的實體；kind 限 SYSTEM, PRODUCT, API_OR_MESSAGE, DATA_OBJECT, ACTOR, ENTITY, BUSINESS_RULE, POLICY, RISK_OR_CONTROL；quote 必須原文逐字引用。",
                lambda: {
                    "entities": [
                        {
                            "name": v["tag"],
                            "kind": "ENTITY",
                            "quote": v["tag"],
                            "confidence": 0.5,
                        }
                        for v in result.get("tags", [])
                        if v["tag"] in content
                    ]
                },
            ),
            (
                "RELATION_EXTRACTION",
                "RELATION_EXTRACTION",
                Relations,
                "從已抽取 entities 找原文直接支持的關係；沒有足夠證據就回傳空陣列。origin/target 必須為已有實體名稱，quote 必須逐字引用。",
                lambda: {"relations": []},
            ),
        ]
        for step, task, schema, instruction, fallback in tasks:
            if kind == "TAG_GENERATION" and step != "TAG_GENERATION":
                continue
            if STAGES.index(stage) > STAGES.index(step):
                continue
            if not checkpoint(item_id, token, step, result):
                return
            output = structured(
                config,
                task,
                instruction,
                {
                    "title": title,
                    "text": content,
                    "entities": result.get("entities", []),
                },
                schema,
                fallback,
            ).model_dump()
            result.update(output)
            following = STAGES[STAGES.index(step) + 1]
            if kind == "TAG_GENERATION":
                following = "GRAPH_UPSERT"
            if not checkpoint(item_id, token, following, result):
                return
            stage = following
        with Session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            item = db.get(KnowledgeItem, item_id)
            job = db.get(KnowledgeJob, item.job_id)
            if item.lease_token != token or job.status == "CANCELLED":
                return
            if job.status in {"PAUSED", "PAUSING"}:
                item.status = "QUEUED"
                item.lease_token = ""
                item.lease_until = None
                summary(db, job)
                db.commit()
                return
            source = db.get(Source, item.source_id)
            chunk = db.get(Chunk, item.chunk_id)
            if source.status != "ACTIVE" or source.sha256 != item.source_hash:
                raise ValueError("來源已更新，停止套用過期圖譜")
            selected = route(
                config,
                "TAG_EXTRACTION" if kind == "TAG_GENERATION" else "RELATION_EXTRACTION",
            )
            persist_chunk(
                db,
                source,
                chunk,
                result,
                job.id,
                selected["provider"] + "/" + selected["model"],
            )
            item.result = result
            item.status = "COMPLETED"
            item.stage = "FINALIZE"
            item.error = ""
            item.lease_token = ""
            item.lease_until = None
            summary(db, job)
            db.commit()
    except Exception as exc:
        with Session() as db:
            item = db.get(KnowledgeItem, item_id)
            if item and item.lease_token == token:
                item.attempts += 1
                item.error = (str(exc) or type(exc).__name__)[:1500]
                item.status = "WAITING_RETRY" if item.attempts < 3 else "FAILED"
                item.available_at = later(min(30, 2**item.attempts))
                item.lease_token = ""
                item.lease_until = None
                summary(db, db.get(KnowledgeJob, item.job_id))
                db.commit()
    finally:
        pulse_stop.set()


def recover():
    with Session() as db:
        for item in db.scalars(
            select(KnowledgeItem).where(KnowledgeItem.status == "RUNNING")
        ):
            item.status = "QUEUED"
            item.lease_token = ""
            item.lease_until = None
        for job in db.scalars(
            select(KnowledgeJob).where(KnowledgeJob.status.in_(["RUNNING", "PAUSING"]))
        ):
            job.status = "PAUSED" if job.status == "PAUSING" else "QUEUED"
        db.commit()


def start():
    global thread
    if thread and thread.is_alive():
        return
    recover()
    stop.clear()

    def loop():
        while not stop.wait(0.35):
            try:
                active.difference_update([f for f in active if f.done()])
                with Session() as db:
                    limit = min(
                        4, max(1, int(settings(db).get("model_concurrency", 2)))
                    )
                if len(active) < limit:
                    item = claim()
                    if item:
                        active.add(pool.submit(process, *item))
            except Exception:
                # Keep durable work queued; next tick retries DB availability.
                stop.wait(1)

    thread = Thread(target=loop, daemon=True)
    thread.start()


def shutdown():
    stop.set()
    if thread:
        thread.join(timeout=2)
