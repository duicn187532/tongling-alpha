from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text
from .db import Session, Meeting, log
from .intelligence import extract
from . import activity

workers = ThreadPoolExecutor(max_workers=2)


def analyze(mid, transcript, config, task_id=None):
    if not task_id:
        return _analyze(mid, transcript, config)
    with activity.tracking(task_id):
        _analyze(mid, transcript, config)
        with Session() as db:
            row = db.get(Meeting, mid)
            status = (
                "FAILED"
                if row.status == "ERROR"
                else "SUCCEEDED"
                if row.status == "ANALYZED"
                else "WAITING"
            )
            detail = (
                row.analysis_error
                or f"已產生 {len(row.events)} 項紀錄；請閱讀後按確認，才會加入專案證據。"
            )
        activity.finish(task_id, status, detail)


def _analyze(mid, transcript, config):
    try:
        config = {**config, "_meeting_extract": True}
        events = []
        for i, pos in enumerate(range(0, len(transcript), 4000)):
            activity.stage(
                "分析會議逐字稿", f"處理段落 {i + 1}/{(len(transcript) + 3999) // 4000}"
            )
            events.extend(
                f.model_dump()
                for f in extract(
                    [{"id": f"{mid}:{i}", "text": transcript[pos : pos + 4000]}], config
                )
            )
        with Session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            meeting = db.get(Meeting, mid)
            if (
                not meeting
                or meeting.status != "ANALYZING"
                or meeting.transcript != transcript
            ):
                return
            meeting.events = events
            meeting.status = "ANALYZED" if events else "NO_FINDINGS"
            meeting.analysis_error = (
                ""
                if events
                else "沒有找到可確認的決議、變更或待辦。請補上實際討論內容，或用「人工整理紀錄」補充。"
            )
            log(
                db,
                meeting.project_id,
                "MEETING_ANALYSIS",
                f"會議「{meeting.title}」分析完成：{len(events)} 項紀錄",
                {"meeting_id": mid, "status": meeting.status},
            )
            db.commit()
    except Exception as exc:
        with Session() as db:
            meeting = db.get(Meeting, mid)
            if (
                meeting
                and meeting.status == "ANALYZING"
                and meeting.transcript == transcript
            ):
                meeting.status, meeting.analysis_error = (
                    "ERROR",
                    f"分析失敗：{str(exc)[:600]}。請檢查模型連線後重試，或人工整理紀錄。",
                )
                log(
                    db,
                    meeting.project_id,
                    "MEETING_ERROR",
                    meeting.analysis_error,
                    {"meeting_id": mid},
                )
                db.commit()
