import json
import shutil
from pathlib import Path
from sqlalchemy import select
from .db import ROOT, Connector, Workspace, Project, Meeting, Source, log
from .evidence import scan_folder, parse_file

FOLDER_NAME = "彤靈專用資料夾"
PACK = ROOT / "sample_data" / "demo_pack"


def get_folder(db):
    return db.scalar(
        select(Connector).where(
            Connector.name == FOLDER_NAME, Connector.project_id.is_(None)
        )
    )


def configure_folder(db, path):
    root = Path(path).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("指定位置不是資料夾")
    root.mkdir(parents=True, exist_ok=True)
    if root == Path(root.anchor):
        raise ValueError("請指定專用子資料夾，不要直接使用整個磁碟")
    row = get_folder(db)
    if not row:
        row = Connector(name=FOLDER_NAME, path=str(root))
        db.add(row)
    elif row.path != str(root):
        row.path, row.inventory, row.errors = str(root), {}, []
    db.flush()
    return row


def manifest():
    if not (PACK / "manifest.json").exists():
        raise ValueError(
            "找不到範例文件包；可在 sample_data/demo_pack 放回文件，系統其他功能不受影響"
        )
    return json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))


def safe_file(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("範例清單檔案不存在或路徑無效")
    return path


def install_samples(db):
    data = manifest()
    folder = get_folder(db) or configure_folder(db, str(ROOT / "workspace_files"))
    destination = Path(folder.path) / "彤靈範例"
    destination.mkdir(parents=True, exist_ok=True)
    for source in PACK.rglob("*"):
        if source.is_file() and not source.is_symlink():
            target = destination / source.relative_to(PACK)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
    return destination, data


def load_demo(db):
    root, data = install_samples(db)
    for project in db.scalars(select(Project)):
        if project.state.get("demo_key") == data["id"]:
            return {
                "project_id": project.id,
                "existing": True,
                "message": "已開啟現有範例專案，保留你的修改與決策。",
            }
    description = safe_file(root, data["description_file"]).read_text(encoding="utf-8")
    ws = db.scalar(select(Workspace))
    p = Project(
        workspace_id=ws.id,
        name="Demo · " + data["title"],
        description=description,
        state={
            "step": 3,
            "progress": 15,
            "analysis_stale": True,
            "dirty_sources": [],
            "demo_key": data["id"],
        },
    )
    db.add(p)
    db.flush()
    connector = Connector(project_id=p.id, name="範例文件來源", path=str(root / "docs"))
    db.add(connector)
    db.flush()
    scan_folder(db, connector)
    for item in data["sources"]:
        record = connector.inventory.get(Path(item["path"]).name)
        if record:
            source = db.get(Source, record["source_id"])
            source.kind = item["kind"]
            source.meta = {**source.meta, "owner": item.get("owner", ""), "demo": True}
    for item in data["meetings"]:
        path = safe_file(root, item["path"])
        blocks = parse_file(path.name, path.read_bytes())
        db.add(
            Meeting(
                project_id=p.id,
                title=item["title"],
                meeting_date=item["meeting_date"],
                participants=item["participants"],
                transcript="\n".join(b["text"] for b in blocks),
            )
        )
    log(db, p.id, "DEMO", "已從專用資料夾讀入範例文件與三場會議，等待分析及人工確認")
    return {
        "project_id": p.id,
        "existing": False,
        "message": "已讀入 3 份文件與 3 場會議。可開始分析；會議紀錄仍須由你確認。",
    }


def add_revision_meeting(db, project):
    if not project.current_revision_id:
        raise ValueError("請先建立 v1.0 正式版本，再加入變更範例")
    root, data = install_samples(db)
    item = data["revision_meeting"]
    existing = db.scalar(
        select(Meeting).where(
            Meeting.project_id == project.id,
            Meeting.title == item["title"],
            Meeting.meeting_date == item["meeting_date"],
        )
    )
    if existing:
        return existing
    path = safe_file(root, item["path"])
    blocks = parse_file(path.name, path.read_bytes())
    meeting = Meeting(
        project_id=project.id,
        title=item["title"],
        meeting_date=item["meeting_date"],
        participants=item["participants"],
        transcript="\n".join(b["text"] for b in blocks),
    )
    db.add(meeting)
    db.flush()
    log(
        db,
        project.id,
        "DEMO_REVISION",
        "已讀取後續變更會議，確認會議後才建立 Change Request",
    )
    return meeting
