"""Golden-case evaluation: fixed inputs, measured PRD outcomes.

The same case run twice should land on the same key requirements without
fragmenting them. The harness drives the public API, so it measures what a
PM would see: proposals to review, the PRD draft and its quality hints.
"""

import json
import time
from pathlib import Path

CASES = Path(__file__).parent / "cases"
PLACEHOLDER = "待 PM"


def load_cases():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(CASES.glob("*.json"))]


def _wait(client, pid, timeout):
    """Wait until analysis and any model-written proposals are done."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/projects/{pid}").json()
        report = detail["state"].get("change_analysis") or {}
        if detail["runtime"] != "RUNNING" and not detail["state"].get("analysis_stale") \
                and report.get("model_status") not in (None, "PENDING"):
            return detail
        time.sleep(1)
    raise TimeoutError("分析未在時限內完成")


def run_case(client, case, timeout=900):
    """Analyze a case end to end and return measurements (no pass/fail)."""
    started = time.monotonic()
    pid = client.post("/api/projects", json={"name": case["name"], "description": case["brief"]}).json()["id"]
    client.post(f"/api/projects/{pid}/workflow/mode", json={"mode": "MANUAL"})
    for source in case["sources"]:
        response = client.post(f"/api/projects/{pid}/requirement-text",
                               json={"title": source["title"], "text": source["text"], "kind": source["kind"]})
        assert response.status_code == 200, response.text
    assert client.post(f"/api/projects/{pid}/analysis/run").status_code == 200
    detail = _wait(client, pid, timeout)
    report = detail["state"]["change_analysis"]
    review = [p for p in report["proposals"] if p.get("needs_review", True)]
    reviewed = client.post(f"/api/projects/{pid}/change-analysis/review",
                           json={"action": "APPROVE", "fingerprint": report["fingerprint"]})
    draft = client.post(f"/api/projects/{pid}/prd/revisions").json() if reviewed.status_code == 200 else None
    quality = client.get(f"/api/prd-revisions/{draft['id']}/quality").json() if draft else None
    requirements = draft["content"]["requirements"] if draft else []
    criteria = draft["content"].get("acceptance_criteria", []) if draft else []
    with_criteria = {c.get("requirement_id") for c in criteria
                     if isinstance(c, dict) and PLACEHOLDER not in c.get("description", "")}
    text = "\n".join([p["target_state"] for p in review] + [r["statement"] for r in requirements])
    return {
        "case": case["id"],
        "seconds": round(time.monotonic() - started, 1),
        "model_status": report.get("model_status"),
        "requirements_total": len(report["proposals"]),
        "review_items": len(review),
        "critical_questions": len(report.get("critical_questions", [])),
        "review_topics": sorted(p["name"] for p in review),
        "key_points": {kp["name"]: any(word in text for word in kp["any"]) for kp in case["expect"]["key_points"]},
        "prd_requirements": len(requirements),
        "criteria_coverage": round(sum(r["id"] in with_criteria for r in requirements) / max(1, len(requirements)), 2),
        "quality_score": quality["score"] if quality else None,
        "quality_counts": quality["counts"] if quality else None,
        "review_rejected": reviewed.status_code != 200 and reviewed.text[:200],
    }


def judge(case, run):
    """Advisory verdict per expectation; used to compare runs, not to gate."""
    low, high = case["expect"]["review_items"]["min"], case["expect"]["review_items"]["max"]
    covered = sum(run["key_points"].values())
    return {
        "review_items_in_range": low <= run["review_items"] <= high,
        "fragmented": run["review_items"] > high,
        "key_point_coverage": f"{covered}/{len(run['key_points'])}",
    }


def stability(runs):
    """Topic and key-point agreement across repeated runs of one case."""
    if len(runs) < 2:
        return None
    topics = [set(r["review_topics"]) for r in runs]
    union = set().union(*topics)
    shared = set.intersection(*topics)
    points = [tuple(sorted(r["key_points"].items())) for r in runs]
    return {"topic_agreement": round(len(shared) / max(1, len(union)), 2),
            "key_points_identical": len(set(points)) == 1,
            "review_item_counts": [r["review_items"] for r in runs]}
