"""Persist stage changes; heartbeat means worker alive, not model token progress."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread
from sqlalchemy import update, select
from .db import Activity, Run, Session, now

current = ContextVar("ai_activity", default=None)


class Cancelled(Exception):
    pass


def create(db, title, config, project_id=None, task_id=None):
    routed = (config.get("model_router") or {}).get("mode") == "active"
    row = Activity(
        title=title,
        project_id=project_id,
        model=("依任務選擇模型" if routed else f"{config['provider']} / {config['model']}"),
    )
    if task_id:
        row.id = task_id
    db.add(row)
    db.flush()
    return row.id


def stage(phase, detail=None):
    task_id = current.get()
    if not task_id:
        return
    with Session() as db:
        row = db.get(Activity, task_id)
        if row.status == "CANCELLED":
            raise Cancelled("已取消；模型若仍在回傳，結果將被捨棄")
        row.phase, row.updated_at, row.heartbeat_at = phase, now(), now()
        if detail is not None:
            row.detail = detail
        db.commit()


def set_model(model):
    task_id = current.get()
    if not task_id:
        return
    with Session() as db:
        row = db.get(Activity, task_id)
        if row and row.status == "RUNNING":
            row.model, row.updated_at = model, now()
            db.commit()


def finish(task_id, status, detail):
    with Session() as db:
        row = db.get(Activity, task_id)
        if row and row.status != "CANCELLED":
            row.status, row.detail, row.finished_at = status, detail, now()
            row.phase = {"SUCCEEDED": "已完成", "WAITING": "等待人工確認",
                         "FAILED": "執行失敗", "CANCELLED": "已取消"}.get(status, status)
            row.updated_at = now()
            db.commit()


@contextmanager
def tracking(task_id):
    token = current.set(task_id)
    stop = Event()
    with Session() as db:
        row = db.get(Activity, task_id)
        if row.status == "CANCELLED":
            current.reset(token)
            raise Cancelled("已取消")
        row.status, row.heartbeat_at = "RUNNING", now()
        db.commit()

    def pulse():
        while not stop.wait(3):
            try:
                with Session() as db:
                    db.execute(
                        update(Activity)
                        .where(Activity.id == task_id, Activity.status == "RUNNING")
                        .values(heartbeat_at=now())
                    )
                    db.commit()
            except Exception:
                # A failed heartbeat remains visible as a stale timestamp.
                pass

    thread = Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    except Cancelled:
        finish(task_id, "CANCELLED", "已取消，過期結果不會套用")
        raise
    except Exception as exc:
        finish(
            task_id,
            "FAILED",
            str(exc) or f"{type(exc).__name__}：模型未在期限內回應，請檢查服務後重試",
        )
        raise
    finally:
        stop.set()
        current.reset(token)


def cancel_project(db, pid):
    db.execute(
        update(Activity)
        .where(
            Activity.id.in_(select(Run.id).where(Run.project_id == pid, Run.status == "RUNNING")),
            Activity.status.in_(["QUEUED", "RUNNING"]),
        )
        .values(
            status="CANCELLED",
            finished_at=now(),
            detail="工作已暫停、模式切換或來源更新；本次結果不會套用",
        )
    )
    db.execute(
        update(Run)
        .where(Run.project_id == pid, Run.status == "RUNNING")
        .values(status="CANCELLED")
    )
