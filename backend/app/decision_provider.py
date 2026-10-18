"""Local Decision model adapter. Outputs are advisory and never change workflow gates."""

import time
import httpx
from sqlalchemy import select

from .db import Requirement
from .model_runtime import acquire, release, model_info, router_config, _trace, config_snapshot_id
from .intelligence import validate_endpoint


def advise_project(db, project, config):
    setting = router_config(config).get("decision") or {}
    if setting.get("mode", "off") == "off":
        return {"status": "OFF", "items": []}
    if setting.get("mode") != "advisory":
        raise ValueError("Decision 自動放行尚未完成校準")
    selected = {"provider": "ollama", "endpoint": config.get("endpoint", ""), "model": setting.get("model", "d1:latest"),
                "local_only": True}
    validate_endpoint(selected)
    info = model_info(selected)
    if "decision" not in info["capabilities"]:
        raise ValueError("CAPABILITY_MISMATCH：指定模型沒有 decision 能力")
    rows = [row for row in db.scalars(select(Requirement).where(Requirement.project_id == project.id))
            if row.decision.get("action") not in {"ACCEPT", "MODIFY", "REJECT"}][:8]
    output = []
    for row in rows:
        started = time.monotonic()
        trace = {"at": time.time(), "task": "DECISION_ADVICE", "project_id": project.id,
                 "role": "decision", "model": selected["model"], "digest": info["digest"],
                 "mode": "advisory", "think": None, "attempts": 1,
                 "snapshot_id": config_snapshot_id(config)}
        state = {
            "requirement": row.statement[:1200],
            "status": row.status,
            "evidence": [{"statement": fact.get("statement", "")[:500], "kind": fact.get("kind", ""),
                          "source_id": fact.get("source_id", "")}
                         for fact in (row.evidence or [])[:8]],
        }
        questions = {
            "human_review": {
                "type": "noul",
                "instructions": "Does this financial requirement need a human to resolve missing evidence, ambiguity or conflicts before approval?",
                "criteria": {"false": "Sufficiently clear evidence for ordinary review",
                             "true": "Missing evidence, ambiguous or conflicting; human clarification needed"},
            }
        }
        acquired = False
        try:
            acquire(config, timeout_seconds=25)
            acquired = True
            with httpx.Client(timeout=httpx.Timeout(25, connect=5), trust_env=False, follow_redirects=False) as client:
                response = client.post(selected["endpoint"].rstrip("/") + "/v1/systemone",
                                       json={"model": selected["model"], "state": state, "questions": questions})
                response.raise_for_status()
                answer = response.json().get("answers", {}).get("human_review", {})
            if answer.get("type") != "noul" or not isinstance(answer.get("noul"), (float, int)):
                raise ValueError("Decision 模型回傳格式無效")
            output.append({"requirement_id": row.id, "human_review_score": float(answer["noul"]),
                           "note": "模型分數只供人工參考，不是正確率或自動批准門檻"})
            trace["status"] = "SUCCEEDED"
        except Exception as exc:
            output.append({"requirement_id": row.id, "error": str(exc)[:200]})
            trace["status"], trace["error_code"] = "FAILED", type(exc).__name__
        finally:
            trace["latency_ms"] = round((time.monotonic() - started) * 1000)
            _trace(config, trace)
            if acquired:
                release()
    return {"status": "ADVISORY", "items": output}
