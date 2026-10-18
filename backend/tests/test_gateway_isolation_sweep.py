"""Every route the client gateway allows must refuse another client's ids.

The allowlist only decides which routes exist for visitors; ownership is
enforced inside each handler. This sweep catches a new route that forgets it.
"""

import itertools
import re
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import client_gateway
from app.db import Activity, Interruption, Session
from app.main import app

PLACEHOLDER = re.compile(r"\[\^/\]\+|\[0-9\]\+")
GROUP = re.compile(r"\(([^()]+)\)")


def expand(pattern):
    """Turn one allowlist regex into concrete path templates."""
    groups = GROUP.findall(pattern)
    for choice in itertools.product(*[g.split("|") for g in groups]):
        path = pattern
        for value in choice:
            path = GROUP.sub(value, path, count=1)
        yield path


def fill(template, ids):
    # "[^/]+" itself contains a slash, so mark placeholders before splitting.
    parts = template.replace("[^/]+", "{ID}").replace("[0-9]+", "0").split("/")
    for index, part in enumerate(parts):
        if part == "{ID}":
            parts[index] = ids.get(parts[index - 1], "0")
    return "/".join(parts)


def wait(client, jid):
    for _ in range(200):
        status = client.get(f"/api/jobs/{jid}").json()
        if status["status"] not in {"RUNNING", "QUEUED"}:
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_no_allowed_route_serves_another_clients_resources(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
        code = client_gateway.pairing_details()["code"]
        assert alice.post("/api/pair", json={"code": code}).status_code == 200
        assert bob.post("/api/pair", json={"code": code}).status_code == 200
        pid = alice.post("/api/projects", json={"name": "Alice 私人", "description": "必須顯示電子帳單申請成功訊息。"}).json()["id"]
        assert alice.post(f"/api/projects/{pid}/agent/delegation", json={"mode": "AUTO_DECIDE_SAFE"}).status_code == 200
        run = alice.post(f"/api/projects/{pid}/agent/continue").json()
        wait(alice, run["id"])
        detail = alice.get(f"/api/projects/{pid}").json()
        meeting = alice.post(f"/api/projects/{pid}/meetings", json={
            "title": "私人會議", "meeting_date": "2026-10-18", "transcript": "決議：保留"}).json()
        todo = alice.post(f"/api/projects/{pid}/todos", json={"title": "私人代辦"}).json()
        job = alice.post("/api/jobs", json={"kind": "RAG", "project_id": pid, "query": "帳單"}).json()
        with Session() as db:
            activity = db.scalar(select(Activity.id).where(Activity.project_id == pid))
            interruption = db.scalar(select(Interruption.id).where(Interruption.project_id == pid))
        ids = {
            "projects": pid, "requirements": detail["requirements"][0]["id"],
            "meetings": meeting["id"], "jobs": job["job_id"], "sources": detail["sources"][0]["id"],
            "prd-revisions": detail["state"]["draft_id"] or "0", "interruptions": interruption or "0",
            "todos": (todo.get("todos") or [{}])[-1].get("id", "0") if isinstance(todo, dict) else "0",
            "activities": activity or "0", "change-requests": "0",
        }
        assert ids["prd-revisions"] != "0", "fixture should reach a reviewable draft"
        leaks, checked = [], 0
        for method, pattern in client_gateway.PUBLIC_ROUTES:
            if not PLACEHOLDER.search(pattern):
                continue  # Global routes are filtered per owner and covered elsewhere.
            for template in expand(pattern):
                path = fill(template, ids)
                if not any(value in path for value in ids.values() if value != "0"):
                    continue
                response = bob.request(method, path, json={})
                checked += 1
                if response.status_code < 300:
                    leaks.append(f"{method} {path} -> {response.status_code}")
        assert checked >= 40, checked
        print(f"checked {checked} cross-client requests")
        assert leaks == []
        # The owner still reaches the same resources.
        assert alice.get(f"/api/projects/{pid}").status_code == 200
        assert alice.get(f"/api/prd-revisions/{ids['prd-revisions']}/diff").status_code in {200, 404, 422}
