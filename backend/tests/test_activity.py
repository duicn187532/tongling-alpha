import time
from threading import Event
from app import workflow


def settled(client, pid):
    for _ in range(200):
        p = client.get(f"/api/projects/{pid}").json()
        if p["activities"] and p["activities"][0]["status"] not in {
            "RUNNING",
            "QUEUED",
        }:
            return p
        time.sleep(0.03)
    raise AssertionError("Activity never settled")


def test_explicit_analysis_reexecutes_and_reports_result(client, project, monkeypatch):
    pid = project["id"]
    original = workflow.extract_sources
    calls = []

    def extraction(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "extract_sources", extraction)
    for _ in range(2):
        assert client.post(f"/api/projects/{pid}/analysis/run").status_code == 200
        p = settled(client, pid)
        assert p["activities"][0]["status"] == "SUCCEEDED"
        assert p["requirements"] and not p["state"]["analysis_stale"]
        assert "人工" in p["activities"][0]["detail"]
    assert len(calls) == 2


def test_running_heartbeat_pause_and_discard(client, project, monkeypatch):
    entered, release = Event(), Event()
    original = workflow.extract_sources

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "extract_sources", slow)
    pid = project["id"]
    try:
        client.post(f"/api/projects/{pid}/analysis/run")
        assert entered.wait(3)
        p = client.get(f"/api/projects/{pid}").json()
        task = p["activities"][0]
        assert p["runtime"] == "RUNNING" and task["status"] == "RUNNING"
        time.sleep(3.2)
        current = client.get(f"/api/projects/{pid}").json()["activities"][0]
        assert current["heartbeat_at"] > task["heartbeat_at"]
        client.post(f"/api/projects/{pid}/agent/pause")
        p = client.get(f"/api/projects/{pid}").json()
        assert p["runtime"] == "PAUSED" and p["activities"][0]["status"] == "CANCELLED"
    finally:
        release.set()
    time.sleep(0.15)
    assert client.get(f"/api/projects/{pid}").json()["requirements"] == []


def test_analysis_failure_is_visible_in_activity(client, project, monkeypatch):
    def failure(*args, **kwargs):
        raise ValueError("測試：模型無法回應")

    monkeypatch.setattr(workflow, "extract_sources", failure)
    pid = project["id"]
    client.post(f"/api/projects/{pid}/analysis/run")
    p = settled(client, pid)
    assert p["runtime"] == "ERROR"
    assert p["activities"][0]["status"] == "FAILED"
    assert "模型無法回應" in p["activities"][0]["detail"]
