import time


def wait_meeting(client, mid):
    for _ in range(100):
        result = client.get("/api/meetings/" + mid).json()
        if result["status"] != "ANALYZING":
            return result
        time.sleep(0.03)
    raise AssertionError("Meeting worker stalled")


def test_empty_analysis_can_be_edited_and_manually_confirmed(client, project):
    meeting = client.post(
        f"/api/projects/{project['id']}/meetings",
        json={"title": "測試空紀錄", "meeting_date": "2026-10-15", "transcript": "好"},
    ).json()
    mid = meeting["id"]
    assert client.post(f"/api/meetings/{mid}/analyze").status_code == 200
    row = wait_meeting(client, mid)
    assert row["status"] == "NO_FINDINGS" and row["analysis_error"]
    assert client.post(f"/api/meetings/{mid}/confirm").status_code == 422
    assert (
        client.patch(
            f"/api/meetings/{mid}/transcript", json={"text": "登入方式：決議使用驗證碼"}
        ).status_code
        == 200
    )
    assert client.get(f"/api/meetings/{mid}").json()["status"] == "IMPORTED"
    response = client.post(
        f"/api/meetings/{mid}/manual-record", json={"text": "登入方式：決議使用驗證碼"}
    )
    assert (
        response.status_code == 200
        and response.json()["events"][0]["origin"] == "HUMAN"
    )
    assert client.post(f"/api/meetings/{mid}/confirm").json()["status"] == "CONFIRMED"
    assert (
        client.patch(
            f"/api/meetings/{mid}/transcript", json={"text": "不可覆寫"}
        ).status_code
        == 422
    )


def test_meeting_failure_is_persistent_and_retriable(client, project, monkeypatch):
    from app import meeting_service

    real = meeting_service.extract

    def failure(*args, **kwargs):
        raise ValueError("test model unavailable")

    monkeypatch.setattr(meeting_service, "extract", failure)
    m = client.post(
        f"/api/projects/{project['id']}/meetings",
        json={
            "title": "模型錯誤",
            "meeting_date": "2026-10-15",
            "transcript": "通知方式：使用推播",
        },
    ).json()
    client.post(f"/api/meetings/{m['id']}/analyze")
    row = wait_meeting(client, m["id"])
    assert (
        row["status"] == "ERROR" and "test model unavailable" in row["analysis_error"]
    )
    monkeypatch.setattr(meeting_service, "extract", real)
    client.post(f"/api/meetings/{m['id']}/analyze")
    assert wait_meeting(client, m["id"])["status"] == "ANALYZED"


def test_settings_folder_and_incremental_read(client, tmp_path, project):
    folder = tmp_path / "my-tongling-files"
    response = client.put("/api/workspace/folder", json={"path": str(folder)})
    assert response.status_code == 200, response.text
    assert folder.is_dir()
    (folder / "test.md").write_text(
        "測試資料夾讀取：保存至共用知識庫", encoding="utf-8"
    )
    assert client.post("/api/workspace/folder/scan").json()["changed"] == 1
    assert client.post("/api/workspace/folder/scan").json()["changed"] == 0
    results = client.post(
        f"/api/projects/{project['id']}/knowledge/search",
        json={"query": "測試資料夾讀取"},
    ).json()
    assert any(x["title"] == "test.md" for x in results)
    assert client.get("/api/workspace/folder").json()["path"] == str(folder.resolve())

    original = next(x for x in results if x["title"] == "test.md")
    assert (
        client.post(
            f"/api/projects/{project['id']}/knowledge/attach",
            json={"source_id": original["source_id"]},
        ).status_code
        == 200
    )
    (folder / "test.md").write_text(
        "測試資料夾讀取：更新後必須重新審查", encoding="utf-8"
    )
    assert client.post("/api/workspace/folder/scan").json()["changed"] == 1
    updated = client.get(f"/api/projects/{project['id']}").json()
    versions = [s for s in updated["sources"] if s["title"] == "test.md"]
    assert {s["status"] for s in versions} == {"ACTIVE", "SUPERSEDED"}
    assert updated["state"]["analysis_stale"]
    (folder / "test.md").unlink()
    assert client.post("/api/workspace/folder/scan").json()["removed"] == 1
    updated = client.get(f"/api/projects/{project['id']}").json()
    assert any(
        s["status"] == "REMOVED" for s in updated["sources"] if s["title"] == "test.md"
    )


def test_demo_reads_files_and_reuses_project(client, tmp_path):
    from app.workspace_files import manifest

    client.put("/api/workspace/folder", json={"path": str(tmp_path / "demo-workspace")})
    response = client.post("/api/demo/apply")
    assert response.status_code == 200, response.text
    pid = response.json()["project_id"]
    p = client.get("/api/projects/" + pid).json()
    data = manifest()
    assert len(p["sources"]) == len(data["sources"]) and len(p["meetings"]) == len(
        data["meetings"]
    )
    assert all(x["provider"] == "LOCAL_FOLDER" for x in p["sources"])
    assert all(m["status"] == "IMPORTED" for m in p["meetings"])
    assert not p["requirements"] and not p["revisions"]
    again = client.post("/api/demo/apply").json()
    assert again["existing"] and again["project_id"] == pid
    assert client.get("/api/demo/files.zip").content.startswith(b"PK")
    assert client.post(f"/api/projects/{pid}/demo/revision-meeting").status_code == 422
