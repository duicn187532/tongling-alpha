"""Generated specifics that no source supports are flagged, supported ones are not."""

from app.faithfulness import specifics, unsourced
from test_prd_editor import draft

SOURCE = "加密演算法須符合本行資安政策（AES-256 以上）。通知寄送紀錄保存 5 年。最多重送 3 次。上線日 2027-03-01。"


def test_invented_numbers_names_and_dates_are_reported():
    text = "附件以 AES-512 加密，保存 7 年，最多重送 3 次，2027-04-01 上線，介接「薪資核心系統」與 SMS 平台"
    assert unsourced(text, SOURCE) == ["2027-04-01", "AES-512", "SMS", "薪資核心系統", "7 年"]


def test_supported_specifics_pass_in_any_spelling():
    assert unsourced("依 AES-256 加密 PDF，保存5年，2027年3月1日上線，重送最多 3 次", SOURCE) == []
    assert "512" not in specifics("AES-512")  # the number belongs to the name, reported once


def test_quality_hint_flags_unsourced_requirement_text(client, project):
    revision = draft(client, project)
    req_id = revision["content"]["requirements"][0]["id"]
    client.put(f"/api/prd-revisions/{revision['id']}/requirements/{req_id}",
               json={"statement": "通知改為 AES-512 加密附件，5 分鐘內送達"})
    issues = client.get(f"/api/prd-revisions/{revision['id']}/quality").json()["issues"]
    flagged = [i for i in issues if i["code"] == "UNSOURCED"]
    assert flagged and "AES-512" in flagged[0]["message"] and "5 分鐘" in flagged[0]["message"]
