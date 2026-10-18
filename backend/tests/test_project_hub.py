"""The PM hub keeps user to-dos and derives progress from existing project records."""

from app.db import Project, Session


def test_dashboard_and_personal_todos(client, project):
    pid = project["id"]
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    card = next(row for row in dashboard.json()["projects"] if row["id"] == pid)
    assert card["progress"] == 20  # The PM brief is an input, not corroborating evidence.
    assert card["milestones"][1] is False
    assert card["counts"]["sources"] >= 1
    assert card["status"] in {"PLANNING", "ATTENTION", "RUNNING"}
    added = client.post(f"/api/projects/{pid}/todos", json={"title": "確認利害關係人", "due_date": "2026-11-10"})
    assert added.status_code == 200, added.text
    todo = added.json()
    assert todo["done"] is False
    with Session() as db:
        assert any(row["id"] == todo["id"] for row in db.get(Project, pid).state["pm_todos"])
    updated = client.patch(f"/api/projects/{pid}/todos/{todo['id']}", json={"done": True})
    assert updated.status_code == 200 and updated.json()["done"] is True
    card = next(row for row in client.get("/api/dashboard").json()["projects"] if row["id"] == pid)
    assert next(row for row in card["todos"] if row["id"] == todo["id"])["done"] is True
    assert client.delete(f"/api/projects/{pid}/todos/{todo['id']}").status_code == 200
    assert client.patch(f"/api/projects/{pid}/todos/{todo['id']}", json={"done": False}).status_code == 404
