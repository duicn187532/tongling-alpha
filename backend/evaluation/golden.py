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


def _confirm_meeting(client, pid, meeting, timeout):
    """Add a meeting, let the model sort it, and confirm every item as a PM would."""
    row = client.post(f"/api/projects/{pid}/meetings", json=meeting).json()
    assert client.post(f"/api/meetings/{row['id']}/analyze").status_code == 200
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.get(f"/api/meetings/{row['id']}").json()
        if row["status"] != "ANALYZING":
            break
        time.sleep(1)
    if row["status"] == "ANALYZED":
        client.post(f"/api/meetings/{row['id']}/confirm", json={})


def run_case(client, case, timeout=900):
    """Analyze a case end to end and return measurements (no pass/fail)."""
    started = time.monotonic()
    pid = client.post("/api/projects", json={"name": case["name"], "description": case["brief"]}).json()["id"]
    client.post(f"/api/projects/{pid}/workflow/mode", json={"mode": "MANUAL"})
    for source in case["sources"]:
        response = client.post(f"/api/projects/{pid}/requirement-text",
                               json={"title": source["title"], "text": source["text"], "kind": source["kind"]})
        assert response.status_code == 200, response.text
    for meeting in case.get("meetings", []):
        _confirm_meeting(client, pid, meeting, timeout)
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
    # Key points may be met as a new requirement or as a retained current rule.
    content = draft["content"] if draft else {}
    retained = [x.get("description", "") for x in content.get("current_state", []) if isinstance(x, dict)]
    text = "\n".join([p["target_state"] for p in review] + [r["statement"] for r in requirements] + retained)
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
        "uncovered": [i["text"] for i in (quality or {}).get("issues", []) if i["code"] == "UNCOVERED"],
        "evidence_gaps": detail["state"].get("evidence_contract", {}).get("gaps", []),
        "review_rejected": reviewed.status_code != 200 and reviewed.text[:200],
    }


def judge(case, run):
    """Advisory verdict per expectation; used to compare runs, not to gate."""
    low, high = case["expect"]["review_items"]["min"], case["expect"]["review_items"]["max"]
    covered = sum(run["key_points"].values())
    verdict = {
        "review_items_in_range": low <= run["review_items"] <= high,
        "fragmented": run["review_items"] > high,
        "key_point_coverage": f"{covered}/{len(run['key_points'])}",
    }
    critical = case["expect"].get("critical_questions")
    if critical:
        verdict["critical_in_range"] = (critical.get("min", 0) <= run["critical_questions"]
                                        <= critical.get("max", 99))
    return verdict


def stability(runs):
    """Topic and key-point agreement across repeated runs of one case."""
    if len(runs) < 2:
        return None
    from app.change_analysis import _overlap
    first, second = runs[0]["review_topics"], runs[1]["review_topics"]
    # Names vary ("通知加密方式" vs "薪資通知加密方式"); count similar names as the same topic.
    matched = sum(any(_overlap(a, b) >= 0.5 for b in second) for a in first)
    points = [tuple(sorted(r["key_points"].items())) for r in runs]
    return {"topic_agreement": round(matched / max(1, len(first), len(second)), 2),
            "key_points_identical": len(set(points)) == 1,
            "review_item_counts": [r["review_items"] for r in runs]}
