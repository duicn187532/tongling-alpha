"""Short SQLite leases bound Ollama inference across host and gateway processes."""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text

from .db import Activity, ResourceLease, Session, now

_local = threading.local()


def _expires():
    return (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()


def acquire_llm(limit, timeout_seconds, activity_id=None, heavy=False):
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        with Session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.execute(delete(ResourceLease).where(ResourceLease.expires_at < now()))
            if activity_id:
                row = db.get(Activity, activity_id)
                if row and row.status == "CANCELLED":
                    from .activity import Cancelled
                    raise Cancelled("已取消排隊中的模型呼叫")
            count = db.scalar(select(func.count()).select_from(ResourceLease).where(ResourceLease.resource.in_(["LLM", "LLM_HEAVY"]))) or 0
            heavy_active = db.scalar(select(func.count()).select_from(ResourceLease).where(ResourceLease.resource == "LLM_HEAVY")) or 0
            if count < limit and (heavy or not heavy_active):
                lease = ResourceLease(resource="LLM_HEAVY" if heavy else "LLM", owner_id=activity_id or f"thread:{threading.get_ident()}", expires_at=_expires())
                db.add(lease)
                db.commit()
                stop = threading.Event()

                def renew():
                    while not stop.wait(15):
                        try:
                            with Session.begin() as heartbeat_db:
                                current = heartbeat_db.get(ResourceLease, lease.id)
                                if current:
                                    current.expires_at = _expires()
                        except Exception:
                            pass

                threading.Thread(target=renew, daemon=True).start()
                stack = getattr(_local, "leases", [])
                stack.append((lease.id, stop))
                _local.leases = stack
                return
            db.commit()
        time.sleep(0.15)
    raise TimeoutError("模型資源等待逾時，請稍後重試")


def release_llm():
    stack = getattr(_local, "leases", [])
    if not stack:
        return
    lease_id, stop = stack.pop()
    stop.set()
    with Session.begin() as db:
        row = db.get(ResourceLease, lease_id)
        if row:
            db.delete(row)
