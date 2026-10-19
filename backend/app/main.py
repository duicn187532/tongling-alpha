import asyncio
import ipaddress
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
    File,
    Form,
)
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from .db import (
    Session,
    Workspace,
    AppJob,
    ClientSession,
    ResourceLease,
    Project,
    Source,
    Link,
    Chunk,
    Assertion,
    Requirement,
    Meeting,
    Revision,
    TechnicalReview,
    DeliveryPackage,
    Change,
    Run,
    Activity,
    Interruption,
    Event,
    Connector,
    ROOT,
    serialize,
    log,
    now,
)
from .evidence import (
    ingest,
    parse_file,
    FileUploadProvider,
    scan_folder,
    mark_stale,
)
from .intelligence import (
    settings,
    validate_endpoint,
    extract,
    check_connection,
    DEFAULT_SETTINGS,
)
from .lifecycle import (
    create_draft,
    create_technical_amendment,
    baseline,
    interruption,
    resolve_interruptions,
    content_hash,
    re_unsafe,
)
from .workflow import launch, plan
from . import meeting_service, project_hub
from . import workspace_files
from . import activity
from . import prd_templates, enrichment, v7_api, retrieval, project_lifecycle, knowledge_api, knowledge_management, client_gateway, job_queue



def _recover_startup_state():
    from .db import ResearchRun

    with Session() as db:
        queued_agents = list(db.scalars(select(AppJob).where(
            AppJob.kind == "AGENT", AppJob.status == "QUEUED")))
        queued_agent_runs = {job.payload.get("run_id") for job in queued_agents}
        queued_agent_projects = {job.project_id for job in queued_agents}
        for research in db.scalars(
            select(ResearchRun).where(ResearchRun.status.in_(["RUNNING", "QUEUED"]))
        ):
            research.status = "INTERRUPTED"
            research.data = {
                **research.data,
                "errors": research.data.get("errors", [])
                + ["服務重啟中斷搜尋，已保留取得的來源，可重新搜尋。"],
            }
        for task in db.scalars(
            select(Activity).where(Activity.status.in_(["RUNNING", "QUEUED"]))
        ):
            if task.id in queued_agent_runs:
                continue
            task.status, task.detail, task.finished_at = (
                "INTERRUPTED",
                "服務已重新啟動，本次工作中斷。請重新執行。",
                now(),
            )
        db.commit()
    with Session() as db:
        if not db.scalar(select(Workspace)):
            db.add(Workspace(name="我的工作空間", settings=DEFAULT_SETTINGS))
        for run in db.scalars(select(Run).where(Run.status == "RUNNING")):
            if run.id in queued_agent_runs:
                continue
            run.status, run.error = "INTERRUPTED", "程式重新啟動；可從檢查點繼續"
        for project in db.scalars(select(Project).where(Project.runtime == "RUNNING")):
            if project.id in queued_agent_projects:
                continue
            project.runtime = "PAUSED"
            project.epoch += 1
        for project in db.scalars(select(Project).where(Project.status == "ACTIVE")):
            report = (project.state or {}).get("change_analysis") or {}
            if report.get("model_status") == "PENDING":
                project.state = {**project.state, "change_analysis": {
                    **report, "model_status": "ERROR",
                    "model_error": "服務重啟中斷提案生成；基礎分析仍可審閱，亦可重新執行分析。"}}
        for meeting in db.scalars(select(Meeting).where(Meeting.status == "ANALYZING")):
            meeting.status, meeting.analysis_error = (
                "ERROR",
                "上次分析因服務重啟中斷；逐字稿已保存，請重新分析。",
            )
        db.commit()


@asynccontextmanager
async def lifespan(app):
    # The gateway shares the database, but only the host owns workers and may
    # recover interrupted work. Restarting a gateway must not pause a live Agent.
    if not client_gateway.IS_GATEWAY:
        _recover_startup_state()
    if not client_gateway.IS_GATEWAY:
        enrichment.start()
        job_queue.start()
    yield
    if not client_gateway.IS_GATEWAY:
        job_queue.shutdown()
        enrichment.shutdown()


app = FastAPI(title="彤靈談心 API", version="0.7.6", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_control(request, call_next):
    async def served():
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    if client_gateway.IS_GATEWAY:
        hostname = request.url.hostname or ""
        public_url = client_gateway.tunnel_url()
        public_host = urlparse(public_url).hostname
        if (hostname not in {"localhost", "127.0.0.1", "testserver", public_host}
                and not client_gateway.private_ip_host(hostname)):
            return JSONResponse({"detail": "不允許的客戶端網址"}, status_code=400)
        path = request.url.path
        if not path.startswith("/api/"):
            return JSONResponse({"detail": "找不到頁面"}, status_code=404)
        if not client_gateway.client_route_allowed(request.method, path):
            return JSONResponse({"detail": "此操作只供彤靈主機管理"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "跨來源寫入不被允許"}, status_code=403)
        if path not in {"/api/health", "/api/pair", "/api/session/context", "/api/session/demo"}:
            client_id = client_gateway.session_client(request.cookies.get("tongling_session", ""))
            if not client_id:
                demo = client_gateway.access_config()["mode"] == "demo"
                return JSONResponse({"detail": "展示連線已失效，正在重新連線" if demo
                                     else "請輸入主機提供的配對碼"}, status_code=401)
            context_token = client_gateway.CURRENT_CLIENT.set(client_id)
            try:
                return await served()
            finally:
                client_gateway.CURRENT_CLIENT.reset(context_token)
        return await served()
    hostname = request.url.hostname or ""
    host_allowed = hostname in {"localhost", "testserver"}
    if not host_allowed:
        try:
            address = ipaddress.ip_address(hostname)
            host_allowed = address.is_loopback
        except ValueError:
            pass
    if not host_allowed:
        return JSONResponse({"detail": "不允許的主機名稱"}, status_code=400)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if (
            origin
            and urlparse(origin).netloc != request.headers.get("host")
            and origin not in {"http://localhost:5173", "http://127.0.0.1:5173"}
        ):
            return JSONResponse({"detail": "跨來源寫入不被允許"}, status_code=403)
    return await served()


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


def get_db():
    with Session() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def get(db, model, identifier):
    row = db.get(model, identifier)
    if not row:
        raise HTTPException(404, "找不到資料")
    if isinstance(row, Project) and row.status != "ACTIVE":
        raise HTTPException(404, "專案已移至垃圾桶")
    owner = client_gateway.CURRENT_CLIENT.get()
    if owner:
        project = row if isinstance(row, Project) else db.get(Project, getattr(row, "project_id", None))
        if project and not client_gateway.project_is_owned(project, owner):
            raise HTTPException(404, "找不到資料")
        if isinstance(row, Source) and not project:
            linked = db.scalars(select(Project).join(Link, Link.project_id == Project.id).where(Link.source_id == row.id))
            if not any(client_gateway.project_is_owned(p, owner) for p in linked):
                raise HTTPException(404, "找不到資料")
    pid = getattr(row, "project_id", None)
    if pid and (not db.get(Project, pid) or db.get(Project, pid).status != "ACTIVE"):
        raise HTTPException(404, "專案已移至垃圾桶")
    return row


def public_revision(row):
    result = serialize(row)
    result.pop("docx_path", None)
    result["projection"] = prd_templates.project(
        row.content, row.presentation or prd_templates.builtins()[0], row.version
    )
    return result


def public_project(row):
    result = serialize(row)
    if client_gateway.IS_GATEWAY:
        result["state"].pop("client_owner_id", None)
    return result


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=100000)


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    title: str = "需求補充"
    kind: Literal[
        "REQUIREMENT_TEXT",
        "USER_UPLOAD",
        "INTERNAL_POLICY",
        "TECH_SPEC",
        "COMPETITOR",
        "BUSINESS_KNOWLEDGE",
    ] = "REQUIREMENT_TEXT"


class ModeInput(BaseModel):
    mode: Literal["AGENT", "MANUAL"]


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)


class TechnicalAssistInput(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=20)


class AttachInput(BaseModel):
    source_id: str


class DecisionInput(BaseModel):
    action: Literal["ACCEPT", "MODIFY", "REJECT", "PENDING"]
    statement: str = ""
    rationale: str = ""
    category: Literal["FUNCTIONAL", "BUSINESS_RULE", "OPEN_ISSUE", "DECISION_CONTEXT", "SCOPE_EXCLUSION"] = "FUNCTIONAL"


class ProposalReviewInput(BaseModel):
    action: Literal["APPROVE", "DEFER"]
    fingerprint: str
    rationale: str = ""


class GuidanceInput(BaseModel):
    brief: str = Field(min_length=1, max_length=4000)


class BackgroundJobInput(BaseModel):
    kind: Literal["GUIDANCE", "RAG", "PRD"]
    project_id: str | None = None
    brief: str = Field(default="", max_length=4000)
    query: str = Field(default="", max_length=1000)
    automatic: bool = False


class QueueSettingsInput(BaseModel):
    model_concurrency: int = Field(default=2, ge=1, le=4)
    prd_concurrency: int = Field(default=1, ge=1, le=3)
    retrieval_concurrency: int = Field(default=4, ge=1, le=8)
    agent_concurrency: int = Field(default=4, ge=1, le=8)
    queue_capacity: int = Field(default=40, ge=1, le=200)
    per_user_jobs: int = Field(default=3, ge=1, le=10)
    job_timeout_seconds: int = Field(default=900, ge=30, le=3600)
    max_retries: int = Field(default=1, ge=0, le=3)


class DelegationInput(BaseModel):
    mode: Literal["GUIDED", "AUTO_DECIDE_SAFE"]


class ChangeInput(BaseModel):
    action: Literal["REVISION_REQUIRED", "NO_PRD_CHANGE", "CANCELLED"]
    rationale: str = Field(min_length=1)


class FolderInput(BaseModel):
    path: str
    name: str = "本機資料夾"
    project_id: str | None = None


class MeetingInput(BaseModel):
    title: str = Field(min_length=1)
    meeting_date: date
    started_at: str = ""
    timezone: str = "Asia/Taipei"
    participants: list[str] = Field(default_factory=list)
    transcript: str = Field(default="", max_length=200000)


class MeetingConfirmInput(BaseModel):
    selected_indices: list[int] | None = None


class SettingsInput(BaseModel):
    retrieval_version: Literal["v1", "v2"] = "v1"
    retrieval_v2: dict = Field(default_factory=dict)
    provider: Literal["mock", "ollama", "openai_compatible"] = "mock"
    model: str = ""
    endpoint: str = "http://127.0.0.1:11434"
    local_only: bool = True
    embedding_model_path: str = ""
    top_k: int = Field(default=8, ge=1, le=30)
    prd_template: str = "FINANCIAL_STANDARD"
    organization: dict = Field(default_factory=dict)
    graph_retrieval_enabled: bool = False
    graph_document_top_k: int = Field(default=3, ge=1, le=20)
    graph_hop_limit: int = Field(default=2, ge=1, le=3)
    reranker_enabled: bool = False
    case_relevance_threshold: float = Field(default=0.45, ge=0, le=1)
    key_evidence_threshold: float = Field(default=0.75, ge=0, le=1)
    unknown_threshold: float = Field(default=0.2, ge=0, le=1)
    model_roles: dict = Field(default_factory=dict)
    task_routes: dict = Field(default_factory=dict)
    model_concurrency: int = Field(default=2, ge=1, le=4)
    model_router: dict = Field(default_factory=dict)
    research_provider: Literal["google_news", "tavily", "brave"] = "google_news"
    research_enabled: bool = False
    research_topic: str = Field(default="", max_length=180)
    research_limit: int = Field(default=5, ge=1, le=10)
    knowledge_policy: dict = Field(default_factory=dict)
    demo_queue: dict = Field(default_factory=dict)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version, "gateway": client_gateway.IS_GATEWAY}


def _client_address(request):
    return request.client.host if request.client else ""


@app.get("/api/session/context")
def session_context(request: Request):
    if client_gateway.IS_GATEWAY:
        info = client_gateway.session_info(request.cookies.get("tongling_session", ""))
        mode = client_gateway.access_config()["mode"]
        blocked = client_gateway.demo_block_reason(_client_address(request), request.url.hostname) if mode == "demo" else ""
        return {"role": "CLIENT", "authenticated": bool(info), "mode": mode,
                "session_kind": info[1] if info else None,
                "demo_available": mode == "demo" and not blocked, "demo_reason": blocked}
    return {"role": "HOST", "authenticated": True}


@app.post("/api/session/demo")
def start_demo_session(request: Request, response: Response):
    """Demo mode skips the code, never the session: each browser gets its own owner id."""
    if not client_gateway.IS_GATEWAY:
        raise HTTPException(404, "找不到頁面")
    info = client_gateway.session_info(request.cookies.get("tongling_session", ""))
    if info:
        return {"paired": True, "session_kind": info[1], "reused": True}
    address = _client_address(request)
    reason = client_gateway.demo_block_reason(address, request.url.hostname)
    if reason:
        raise HTTPException(403, reason)
    client_id = request.cookies.get("tongling_client_id") or secrets.token_urlsafe(32)
    try:
        token = client_gateway.issue_demo_session(address, client_id)
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from exc
    secure = request.url.scheme == "https"
    response.set_cookie("tongling_client_id", client_id, httponly=True, samesite="lax", secure=secure, max_age=30 * 86400)
    response.set_cookie("tongling_session", token, httponly=True, samesite="lax", secure=secure,
                        max_age=client_gateway.DEMO_SESSION_TTL)
    return {"paired": True, "session_kind": "DEMO", "reused": False}


@app.get("/api/client/workspace-config")
def client_workspace_config(db=Depends(get_db)):
    """Only non-administrative display hints may leave the host."""
    if not client_gateway.IS_GATEWAY:
        raise HTTPException(404, "找不到頁面")
    config = settings(db)
    return {"provider": config.get("provider", "mock"),
            "retrieval_version": config.get("retrieval_version", "v1"),
            "research_topic": config.get("research_topic", ""),
            "organization": {"industry": (config.get("organization") or {}).get("industry", "")}}


@app.get("/api/admin/client-access")
def client_access(request: Request, db=Depends(get_db)):
    _host_admin(request)
    return {**client_gateway.pairing_details(), "url": client_gateway.tunnel_url(),
            **client_gateway.access_config(db), "demo_session_hours": client_gateway.DEMO_SESSION_TTL // 3600,
            "demo_max_sessions": client_gateway.DEMO_MAX_ACTIVE}


class ClientAccessModeInput(BaseModel):
    mode: Literal["pairing", "demo"]
    demo_network: Literal["LAN", "PUBLIC"] = "LAN"


@app.put("/api/admin/client-access/mode")
def save_client_access_mode(body: ClientAccessModeInput, request: Request, db=Depends(get_db)):
    # Only the loopback-bound host process may change who can enter; the gateway
    # process refuses this path before it reaches any handler.
    _host_admin(request)
    return client_gateway.save_access_config(db, body.mode, body.demo_network)


@app.get("/api/admin/client-sessions")
def client_sessions(request: Request, db=Depends(get_db)):
    _host_admin(request)
    return [{"id": row.id, "client_label": row.client_id[:8], "kind": row.kind,
             "created_at": row.created_at, "expires_at": row.expires_at,
             "last_seen_at": row.last_seen_at, "revoked": bool(row.revoked_at)}
            for row in db.scalars(select(ClientSession).order_by(ClientSession.created_at.desc()).limit(100))]


@app.post("/api/admin/client-sessions/{session_id}/revoke")
def revoke_client_session(session_id: str, request: Request, db=Depends(get_db)):
    _host_admin(request)
    row = db.get(ClientSession, session_id)
    if not row:
        raise HTTPException(404, "找不到配對紀錄")
    row.revoked_at = now()
    return {"ok": True}


@app.post("/api/pair")
async def pair(request: Request, response: Response):
    if not client_gateway.IS_GATEWAY:
        raise HTTPException(404, "主機工作台不需要配對")
    body = await request.json()
    address = _client_address(request)
    recent = [t for t in client_gateway.ATTEMPTS.get(address, []) if time.time() - t < 60]
    client_gateway.ATTEMPTS[address] = recent + [time.time()]
    if len(recent) >= 5:
        raise HTTPException(429, "配對嘗試過多，請稍後再試")
    if not client_gateway.check_pairing(body.get("code", "")):
        raise HTTPException(401, "配對碼無效或已過期，請向主機索取新碼")
    client_id = request.cookies.get("tongling_client_id") or secrets.token_urlsafe(32)
    token = client_gateway.new_session(client_id)
    response.set_cookie("tongling_client_id", client_id, httponly=True, samesite="lax", secure=True, max_age=30 * 86400)
    response.set_cookie("tongling_session", token, httponly=True, samesite="lax", secure=True, max_age=client_gateway.SESSION_TTL)
    return {"paired": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    client_gateway.revoke_session(request.cookies.get("tongling_session", ""))
    response.delete_cookie("tongling_session")
    return {"ok": True}


@app.get("/api/workspace")
def workspace(db=Depends(get_db)):
    return serialize(db.scalar(select(Workspace)))


class FolderSettingInput(BaseModel):
    path: str = Field(min_length=1, max_length=2000)


@app.get("/api/workspace/folder")
def workspace_folder(request: Request, db=Depends(get_db)):
    row = workspace_files.get_folder(db)
    local = request.client.host in {"127.0.0.1", "::1", "testclient"}
    return {
        "configured": bool(row),
        "can_edit": local,
        "path": (row.path if row else str(ROOT / "workspace_files"))
        if local
        else "已由主機管理",
        "files": len(row.inventory) if row else 0,
        "errors": row.errors if row else [],
        "id": row.id if row else None,
    }


@app.put("/api/workspace/folder")
def save_workspace_folder(
    body: FolderSettingInput, request: Request, db=Depends(get_db)
):
    if request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "請從 Runtime 主機設定專用資料夾")
    row = workspace_files.configure_folder(db, body.path)
    return {
        "configured": True,
        "path": row.path,
        "id": row.id,
        "files": len(row.inventory),
        "can_edit": True,
    }


@app.post("/api/workspace/folder/scan")
def scan_workspace_folder(db=Depends(get_db)):
    row = workspace_files.get_folder(db)
    if not row:
        raise ValueError("請先在設定頁保存專用資料夾路徑")
    report = scan_folder(db, row)
    return {
        **report,
        "files": len(row.inventory),
        "message": f"讀取完成：新增或更新 {report['changed']} 份、移除 {report['removed']} 份、錯誤 {len(report['errors'])} 份。內容已加入共用知識庫。",
    }


@app.post("/api/demo/install-files")
def install_demo_files(db=Depends(get_db)):
    workspace_files.install_samples(db)
    return {
        "ok": True,
        "message": "測試文件已放入專用資料夾的「彤靈範例」子資料夾；既有檔案不會被覆蓋。",
    }


@app.post("/api/demo/apply")
def apply_demo(db=Depends(get_db)):
    return workspace_files.load_demo(db)


@app.post("/api/projects/{pid}/demo/revision-meeting")
def demo_revision_meeting(pid: str, db=Depends(get_db)):
    return serialize(workspace_files.add_revision_meeting(db, get(db, Project, pid)))


@app.get("/api/demo/files.zip")
def demo_files():
    import io
    import zipfile

    workspace_files.manifest()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in workspace_files.PACK.rglob("*"):
            if path.is_file() and not path.is_symlink():
                archive.write(path, path.relative_to(workspace_files.PACK))
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="TongLing_Test_Documents.zip"'
        },
    )


@app.put("/api/workspace")
def update_workspace(body: ProjectInput, db=Depends(get_db)):
    row = db.scalar(select(Workspace))
    row.name = body.name
    return serialize(row)


@app.get("/api/settings")
def read_settings(db=Depends(get_db)):
    return settings(db)


@app.put("/api/settings")
def save_settings(body: SettingsInput, db=Depends(get_db)):
    config = body.model_dump()
    config["demo_queue"] = job_queue.validate(config["demo_queue"])
    from .knowledge_federation import validate_policy
    config["knowledge_policy"] = validate_policy(config["knowledge_policy"])
    from .retrieval_v2 import RetrievalConfig
    RetrievalConfig(**config["retrieval_v2"])
    validate_endpoint(config)
    if (
        not config["unknown_threshold"]
        <= config["case_relevance_threshold"]
        <= config["key_evidence_threshold"]
    ):
        raise ValueError("門檻必須為 unknown ≤ relevance ≤ key")
    from .model_runtime import route, TASK_ROLES, validate_router

    validate_router(config)
    for task in TASK_ROLES:
        route(config, task)
    row = db.scalar(select(Workspace))
    # Keys owned by other screens (client access mode) survive a model-settings save.
    row.settings = {**(row.settings or {}), **config}
    return config


def _host_admin(request):
    if client_gateway.IS_GATEWAY or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "僅供主機本機管理")


@app.get("/api/admin/queue/settings")
def queue_settings(request: Request, db=Depends(get_db)):
    _host_admin(request)
    return {**job_queue.config(db), "model_concurrency": settings(db).get("model_concurrency", 2)}


@app.put("/api/admin/queue/settings")
def save_queue_settings(body: QueueSettingsInput, request: Request, db=Depends(get_db)):
    _host_admin(request)
    data = body.model_dump()
    model_limit = data.pop("model_concurrency")
    row = db.scalar(select(Workspace))
    row.settings = {**row.settings, "model_concurrency": model_limit,
                    "demo_queue": job_queue.validate(data)}
    return {**data, "model_concurrency": model_limit}


@app.get("/api/admin/queue/status")
def queue_status(request: Request, db=Depends(get_db)):
    _host_admin(request)
    import shutil
    import subprocess
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    counts = {name: db.scalar(select(func.count()).select_from(AppJob).where(AppJob.status == name)) or 0
              for name in ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")}
    active_sessions = db.scalar(select(func.count(func.distinct(ClientSession.client_id))).where(
        ClientSession.revoked_at.is_(None), ClientSession.expires_at > int(time.time()),
        ClientSession.last_seen_at >= int(time.time()) - 300)) or 0
    recent_errors = db.scalar(select(func.count()).select_from(AppJob).where(
        AppJob.status == "FAILED", AppJob.finished_at >= cutoff)) or 0
    llm_active = db.scalar(select(func.count()).select_from(ResourceLease).where(
        ResourceLease.resource.in_(["LLM", "LLM_HEAVY"]), ResourceLease.expires_at > now())) or 0
    active_jobs = [{"job_id": row.id, "kind": row.kind, "status": row.status,
                    "session": "主機" if row.session_id == "host" else row.session_id[-6:],
                    "project_id": row.project_id, "progress": row.progress,
                    "created_at": row.created_at}
                   for row in db.scalars(select(AppJob).where(
                       AppJob.status.in_(["QUEUED", "RUNNING"])
                   ).order_by(AppJob.created_at).limit(50))]
    gpu = {"status": "UNAVAILABLE"}
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2, check=True)
            used, total, utilization = [int(value.strip()) for value in result.stdout.splitlines()[0].split(",")]
            gpu = {"status": "AVAILABLE", "memory_used_mb": used, "memory_total_mb": total,
                   "utilization_percent": utilization}
        except Exception:
            gpu = {"status": "UNAVAILABLE"}
    return {"active_sessions": active_sessions, "jobs": counts, "active_jobs": active_jobs,
            "errors_last_24h": recent_errors, "llm_active": llm_active,
            "gpu": gpu, "settings": {**job_queue.config(db), "model_concurrency": settings(db).get("model_concurrency", 2)}}


@app.get("/api/admin/model-router/preview")
def model_router_preview(request: Request, db=Depends(get_db)):
    if client_gateway.IS_GATEWAY or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "僅供主機本機瀏覽器查看")
    from .model_runtime import TASK_ROLES, route, model_info, router_config, recent_traces, config_snapshot_id
    from collections import Counter
    config = settings(db)
    rows = []
    checks = {}
    for task in TASK_ROLES:
        selected = route(config, task)
        item = {"task": task, "role": selected["_route_role"], "model": selected["model"],
                "provider": selected["provider"], "status": "READY", "reason": ""}
        if selected["provider"] == "ollama":
            key = (selected["endpoint"], selected["model"])
            if key not in checks:
                try:
                    checks[key] = model_info(selected)
                except Exception as exc:
                    checks[key] = exc
            info = checks[key]
            if isinstance(info, Exception):
                item.update(status="UNAVAILABLE", reason=f"模型查驗失敗：{type(info).__name__}")
            else:
                item["digest"] = info["digest"]
                if "completion" not in info["capabilities"]:
                    item.update(status="UNAVAILABLE", reason="模型沒有文字生成功能")
                policy = (router_config(config).get("policy_by_task") or {}).get(task, {})
                think = policy.get("think", False)
                if router_config(config)["mode"] == "active" and think is not False and think not in info["thinking"]:
                    item.update(status="UNAVAILABLE", reason="模型不支援此任務設定的 thinking 值")
        elif selected["provider"] == "mock":
            item.update(status="RULE_ONLY", reason="離線規則模式，不執行模型")
        rows.append(item)
    traces = recent_traces(500)
    totals = Counter((r.get("task"), r.get("status")) for r in traces)
    groups = {}
    for record in traces:
        key = (record.get("task"), record.get("mode"), record.get("model"))
        groups.setdefault(key, []).append(record)
    def percentile(values, fraction):
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))] if ordered else None
    comparison = []
    for (task, mode, model), samples in groups.items():
        latencies = [x["latency_ms"] for x in samples if isinstance(x.get("latency_ms"), (int, float))]
        comparison.append({"task": task, "mode": mode, "model": model, "n": len(samples),
                           "completed": sum(x.get("status") == "SUCCEEDED" for x in samples),
                           "p50_ms": percentile(latencies, 0.5), "p95_ms": percentile(latencies, 0.95)})
    return {"mode": router_config(config)["mode"], "snapshot_id": config_snapshot_id(config),
            "routes": rows, "evaluation": [{"task": task, "status": status, "count": count}
                                            for (task, status), count in totals.items()],
            "comparison": comparison, "sample_count": len(traces), "traces": traces[-30:]}


@app.post("/api/settings/providers/test")
def test_provider(db=Depends(get_db)):
    return check_connection(settings(db))


@app.post("/api/settings/providers/test-generation")
def test_generation(db=Depends(get_db)):
    task_id = activity.create(db, "模型生成測試", settings(db))
    db.commit()
    with activity.tracking(task_id):
        result = generation_impl(db)
        activity.finish(task_id, "SUCCEEDED", result["message"])
        return result


def generation_impl(db):
    import httpx

    config = settings(db)
    check_connection(config)
    start = time.monotonic()
    try:
        result = extract(
            [{"id": "connection-test", "text": "登入需求：使用者必須輸入驗證碼。"}],
            config,
            timeout_seconds=30,
            max_tokens=256,
        )
    except httpx.TimeoutException as exc:
        raise ValueError(
            "生成測試逾時（每次請求 30 秒，結構修復最多一次）。服務可連通，但模型可能正在載入或忙碌；請稍後重試。"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"模型生成回傳 HTTP {exc.response.status_code}，請檢查模型狀態及 Ollama 日誌。"
        ) from exc
    except httpx.ConnectError as exc:
        raise ValueError("生成期間模型服務中斷，請確認 Ollama 仍在執行。") from exc
    if not result:
        raise ValueError("模型已回應，但沒有抽取出測試需求。請重試或更換模型。")
    return {
        "ok": True,
        "provider": config["provider"],
        "assertions": len(result),
        "latency_ms": round((time.monotonic() - start) * 1000),
        "message": f"生成成功：{result[0].statement}（證據引用與 JSON 格式均有效）",
    }


v7_api.register(app, get_db, get)
knowledge_api.register(app, get_db, get)
knowledge_management.register(app, get_db)
project_hub.register(app, get_db, get)
from . import prd_editor, prd_quality  # noqa: E402
prd_editor.register(app, get_db, get)
prd_quality.register(app, get_db, get)


@app.get("/api/projects")
def projects(db=Depends(get_db)):
    return [
        public_project(p)
        for p in db.scalars(
            select(Project)
            .where(Project.status == "ACTIVE")
            .order_by(Project.created_at.desc())
        )
        if not client_gateway.CURRENT_CLIENT.get() or client_gateway.project_is_owned(p)
    ]


@app.post("/api/projects")
def create_project(body: ProjectInput, db=Depends(get_db)):
    ws = db.scalar(select(Workspace))
    p = Project(
        workspace_id=ws.id,
        name=body.name.strip(),
        description=body.description.strip(),
        state={"step": 1, "progress": 0, "analysis_stale": True, "dirty_sources": [],
               **({"client_owner_id": client_gateway.CURRENT_CLIENT.get(),
                   "knowledge_policy": {"mode": "CUSTOM", "scopes": ["PROJECT_LOCAL", "LOCAL_PRIVATE"], "allow_remote_query": False}}
                  if client_gateway.CURRENT_CLIENT.get() else {})},
    )
    db.add(p)
    db.flush()
    if body.description.strip():
        ingest(
            db,
            "需求起點",
            [{"text": body.description}],
            kind="REQUIREMENT_TEXT",
            project=p,
        )
    log(db, p.id, "PROJECT", "建立專案")
    return public_project(p)


@app.post("/api/demo/sample-requirement")
def sample_requirement(db=Depends(get_db)):
    """A fictional requirement to try the flow with; nothing is saved."""
    from .sample_requirements import generate
    config = settings(db)
    db.rollback()  # the model call must not hold a transaction open
    return generate(config)


@app.post("/api/agent/guidance")
def agent_guidance(body: GuidanceInput, db=Depends(get_db)):
    from .agent_guidance import suggest
    return suggest(body.brief, settings(db))


@app.post("/api/jobs", status_code=202)
def submit_job(body: BackgroundJobInput, request: Request, db=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    project = get(db, Project, body.project_id) if body.project_id else None
    if body.kind in {"RAG", "PRD"} and not project:
        raise ValueError("此工作需要專案")
    if body.kind == "GUIDANCE" and not body.brief.strip():
        raise ValueError("請輸入需求起點")
    if body.kind == "RAG" and not body.automatic and not body.query.strip():
        raise ValueError("請輸入檢索關鍵字")
    payload = {"brief": body.brief.strip()} if body.kind == "GUIDANCE" else (
        {"query": body.query.strip(), "automatic": body.automatic} if body.kind == "RAG" else {})
    key = request.headers.get("Idempotency-Key")
    if key and (len(key) > 160 or len(key) < 8):
        raise ValueError("Idempotency-Key 長度必須介於 8–160")
    job = job_queue.submit(db, body.kind, project=project, payload=payload, idempotency_key=key)
    return job_queue.public(job)


@app.get("/api/jobs")
def list_jobs(project_id: str | None = None, db=Depends(get_db)):
    owner = job_queue.owner_id()
    query = select(AppJob).where(AppJob.session_id == owner).order_by(AppJob.created_at.desc()).limit(30)
    if project_id:
        get(db, Project, project_id)
        query = query.where(AppJob.project_id == project_id)
    return [job_queue.public(row, include_result=False) for row in db.scalars(query)]


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str, db=Depends(get_db)):
    job = db.get(AppJob, jid)
    if not job or job.session_id != job_queue.owner_id():
        raise HTTPException(404, "找不到工作")
    if job.status == "QUEUED":
        job.status, job.finished_at = "CANCELLED", now()
    elif job.status == "RUNNING" and job.kind == "PRD":
        raise HTTPException(409, "PRD 已開始寫入；完成後可檢視草稿")
    elif job.status == "RUNNING":
        job.cancel_requested = 1
    if job.kind == "AGENT" and job.project_id and job.status in {"RUNNING", "CANCELLED"}:
        project = db.get(Project, job.project_id)
        activity.cancel_project(db, project.id)
        project.epoch += 1
        project.runtime = "PAUSED"
    return job_queue.public(job)


@app.post("/api/jobs/{jid}/dismiss")
def dismiss_job(jid: str, db=Depends(get_db)):
    job = db.get(AppJob, jid)
    if not job or job.session_id != job_queue.owner_id():
        raise HTTPException(404, "找不到工作")
    if job.status not in job_queue.TERMINAL:
        raise HTTPException(409, "工作仍在執行，完成後才能清除")
    job.dismissed_at = now()
    return job_queue.public(job, include_result=False)


@app.post("/api/activities/{aid}/dismiss")
def dismiss_activity(aid: str, db=Depends(get_db)):
    row = get(db, Activity, aid)
    if row.project_id is None and client_gateway.CURRENT_CLIENT.get():
        raise HTTPException(404, "找不到資料")
    if row.status in {"RUNNING", "QUEUED"}:
        raise HTTPException(409, "工作仍在執行，完成後才能清除")
    row.dismissed_at = now()
    return serialize(row)


@app.post("/api/jobs/{jid}/resume", status_code=202)
def resume_job(jid: str, db=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    original = db.get(AppJob, jid)
    if not original or original.session_id != job_queue.owner_id():
        raise HTTPException(404, "找不到工作")
    if original.status not in job_queue.TERMINAL:
        raise ValueError("工作仍在排隊或執行中")
    project = get(db, Project, original.project_id) if original.project_id else None
    if original.kind == "AGENT":
        row = launch(db, project)
        resumed = db.get(AppJob, row.checkpoint["job_id"])
    else:
        resumed = job_queue.submit(db, original.kind, project=project, payload=original.payload)
    return job_queue.public(resumed)


@app.post("/api/projects/{pid}/agent/delegation")
def agent_delegation(pid: str, body: DelegationInput, db=Depends(get_db)):
    project = get(db, Project, pid)
    if project.runtime == "RUNNING":
        raise ValueError("Agent 正在執行，請等本輪完成再變更代理授權")
    project.state = {**project.state, "delegation_mode": body.mode,
                     "delegated_at": now() if body.mode == "AUTO_DECIDE_SAFE" else None}
    log(db, pid, "DELEGATION", "已授權 Agent 代理明確且無衝突的需求決策" if body.mode == "AUTO_DECIDE_SAFE" else "改為逐項人工確認")
    return {"mode": body.mode, "safeguard": "開放議題、衝突與正式 PRD 定版仍須人工確認"}


@app.get("/api/projects/{pid}")
def project_detail(pid: str, db=Depends(get_db)):
    p = get(db, Project, pid)
    result = public_project(p)
    result["resume_plan"] = plan(p)
    result["activities"] = [
        serialize(a)
        for a in db.scalars(
            select(Activity)
            .where(Activity.project_id == pid)
            .order_by(Activity.created_at.desc())
            .limit(12)
        )
    ]
    latest = db.scalar(
        select(Run)
        .where(Run.project_id == pid)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    result["latest_run"] = serialize(latest) if latest else None
    result["sources"] = [
        serialize(s)
        for s in db.scalars(
            select(Source)
            .join(Link, Link.source_id == Source.id)
            .where(Link.project_id == pid)
        )
    ]
    result["requirements"] = [
        serialize(r)
        for r in db.scalars(
            select(Requirement)
            .where(Requirement.project_id == pid)
            .order_by(Requirement.topic)
        )
    ]
    result["meetings"] = [
        serialize(m)
        for m in db.scalars(
            select(Meeting)
            .where(Meeting.project_id == pid)
            .order_by(Meeting.meeting_date, Meeting.started_at)
        )
    ]
    result["revisions"] = [
        public_revision(r)
        for r in db.scalars(
            select(Revision)
            .where(Revision.project_id == pid)
            .order_by(Revision.created_at.desc())
        )
    ]
    result["changes"] = [
        serialize(c)
        for c in db.scalars(
            select(Change)
            .where(Change.project_id == pid)
            .order_by(Change.created_at.desc())
        )
    ]
    result["interruptions"] = [
        serialize(i)
        for i in db.scalars(
            select(Interruption).where(
                Interruption.project_id == pid, Interruption.status != "RESOLVED"
            )
        )
    ]
    result["events"] = list(
        reversed(
            [
                serialize(e)
                for e in db.scalars(
                    select(Event)
                    .where(Event.project_id == pid)
                    .order_by(Event.id.desc())
                    .limit(100)
                )
            ]
        )
    )
    return result


@app.get("/api/projects/{pid}/pulse")
def project_pulse(pid: str, db=Depends(get_db)):
    """Small status update; full project data is fetched only after a new event."""
    p = get(db, Project, pid)
    latest = db.scalar(
        select(Run).where(Run.project_id == pid).order_by(Run.created_at.desc()).limit(1)
    )
    last_event_id = db.scalar(
        select(Event.id).where(Event.project_id == pid).order_by(Event.id.desc()).limit(1)
    ) or 0
    tasks = list(db.scalars(
        select(Activity).where(Activity.project_id == pid)
        .order_by(Activity.created_at.desc()).limit(12)
    ))
    return {
        "id": pid,
        "runtime": p.runtime,
        "state": {key: p.state.get(key) for key in (
            "step", "progress", "analysis_stale", "draft_id"
        )},
        "epoch": p.epoch,
        "last_event_id": last_event_id,
        "latest_run": {"error": latest.error, "status": latest.status} if latest else None,
        "activities": [serialize(task) for task in tasks],
    }


@app.post("/api/projects/{pid}/requirement-text")
def add_text(pid: str, body: TextInput, db=Depends(get_db)):
    p = get(db, Project, pid)
    return serialize(
        ingest(db, body.title, [{"text": body.text}], kind=body.kind, project=p)
    )


@app.post("/api/projects/{pid}/sources/upload")
def upload(
    pid: str,
    file: UploadFile = File(...),
    kind: str = Form("USER_UPLOAD"),
    db=Depends(get_db),
):
    p = get(db, Project, pid)
    return upload_common(file, kind, db, p)


@app.post("/api/knowledge/upload")
def knowledge_upload(
    file: UploadFile = File(...),
    kind: str = Form("BUSINESS_KNOWLEDGE"),
    db=Depends(get_db),
):
    return upload_common(file, kind, db)


def upload_common(file, kind, db, project=None):
    if kind not in {
        "USER_UPLOAD",
        "BUSINESS_KNOWLEDGE",
        "INTERNAL_POLICY",
        "TECH_SPEC",
        "COMPETITOR",
        "HISTORICAL_PRD",
        "REQUIREMENT_DOCUMENT",
    }:
        raise ValueError("不支援的來源分類")
    data = file.file.read(30 * 1024 * 1024 + 1)
    name = Path(file.filename or "upload.txt").name
    blocks = parse_file(name, data)
    # Wireframes and slide pictures are read by a vision model (AI-11).
    from .vision import image_blocks
    blocks += image_blocks(name, data, settings(db))
    stored = FileUploadProvider().store(name, data)
    return serialize(
        ingest(db, name, blocks, "FILE_UPLOAD", kind, project, {"object_id": stored})
    )


@app.get("/api/sources/{sid}")
def source_detail(sid: str, project_id: str | None = None, db=Depends(get_db)):
    if client_gateway.CURRENT_CLIENT.get() and project_id:
        from .knowledge_federation import local_source_ids, policy_for
        get(db, Project, project_id)
        if sid not in local_source_ids(db, project_id, policy_for(db, project_id, settings(db))):
            raise HTTPException(404, "找不到可用來源")
        source = db.get(Source, sid)
        if not source:
            raise HTTPException(404, "找不到資料")
    else:
        source = get(db, Source, sid)
    row = serialize(source)
    row["chunks"] = [
        serialize(c) for c in db.scalars(select(Chunk).where(Chunk.source_id == sid))
    ]
    return row


@app.post("/api/projects/{pid}/knowledge/search")
def knowledge_search(pid: str, body: SearchInput, db=Depends(get_db)):
    get(db, Project, pid)
    config = settings(db)
    task_id = activity.create(db, "知識檢索與來源相關性評估", config, pid)
    db.commit()
    with activity.tracking(task_id):
        activity.stage("檢索與評估來源", "正在合併候選文件並檢查相關性、權威與版本")
        hits = retrieval.retrieve(db, body.query, pid, config)
        activity.finish(
            task_id, "SUCCEEDED", f"已完成評估，找到 {len(hits)} 份候選文件。"
        )
        return hits


@app.post("/api/projects/{pid}/knowledge/debug-search")
def knowledge_debug_search(pid: str, body: SearchInput, request: Request, db=Depends(get_db)):
    if request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "檢索除錯只供本機使用")
    from .retrieval_v2 import RetrievalPipelineV2
    get(db, Project, pid)
    return RetrievalPipelineV2().search(db, body.query, pid, settings(db))


@app.post("/api/projects/{pid}/knowledge/attach")
def attach(pid: str, body: AttachInput, db=Depends(get_db)):
    p = get(db, Project, pid)
    if client_gateway.CURRENT_CLIENT.get():
        from .knowledge_federation import local_source_ids, policy_for
        if body.source_id not in local_source_ids(db, pid, policy_for(db, pid, settings(db))):
            raise HTTPException(404, "找不到可用來源")
        s = db.get(Source, body.source_id)
    else:
        s = get(db, Source, body.source_id)
    if not s:
        raise HTTPException(404, "找不到可用來源")
    if not db.get(Link, (pid, s.id)):
        db.add(Link(project_id=pid, source_id=s.id))
        mark_stale(db, p, s.id, f"採用知識來源：{s.title}")
        return {"ok": True, "source_id": s.id, "added": True, "message": f"已加入專案依據：{s.title}"}
    return {"ok": True, "source_id": s.id, "added": False, "message": f"已是專案依據：{s.title}"}


class RemoteAttachInput(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)
    remote_evidence_id: str = Field(min_length=1, max_length=300)
    query: str = Field(default="", max_length=200)


@app.post("/api/projects/{pid}/knowledge/attach-remote")
def attach_remote(pid: str, body: RemoteAttachInput, db=Depends(get_db)):
    """A company-server hit becomes a project source only through the host,
    which fetches it itself; the browser never supplies evidence text."""
    from .knowledge_federation import import_remote_evidence
    p = get(db, Project, pid)
    if p.runtime == "RUNNING":
        raise HTTPException(409, "Agent 正在執行，請等本輪完成再加入依據")
    source, created = import_remote_evidence(db, p, body.provider_id, body.remote_evidence_id,
                                             settings(db), body.query)
    if created:
        mark_stale(db, p, source.id, f"採用公司檔案服務器來源：{source.title}")
    return {"ok": True, "source_id": source.id, "added": created,
            "message": f"已加入專案依據：{source.title}" if created else f"已是專案依據：{source.title}"}


@app.delete("/api/projects/{pid}/sources/{sid}")
def remove_project_source(pid: str, sid: str, db=Depends(get_db)):
    """Unlink a source from this project; the library copy stays for audit."""
    from .intelligence import apply_analysis
    db.execute(text("BEGIN IMMEDIATE"))
    p = get(db, Project, pid)
    if p.runtime == "RUNNING":
        raise HTTPException(409, "Agent 正在執行，請先暫停再移除依據")
    link = db.get(Link, (pid, sid))
    if not link:
        raise HTTPException(404, "此來源不是本案依據")
    source = db.get(Source, sid)
    db.delete(link)
    db.flush()
    activity.cancel_project(db, pid)
    state = dict(p.state)
    pending = [x for x in state.get("dirty_sources", []) if x != sid]
    state["dirty_sources"] = pending
    state["draft_id"] = None
    title = source.title if source else sid
    if state.get("analysis_stale") and pending:
        # Other evidence still waits for analysis; retire this one in that run.
        state["removed_sources"] = sorted(set(state.get("removed_sources", [])) | {sid})
        p.state = state
    else:
        p.state = state
        apply_analysis(db, p, {sid: []})
    p.epoch += 1
    p.runtime = "READY"
    log(db, pid, "SOURCE_REMOVED", f"移除專案依據：{title}", {"source_id": sid})
    return {"ok": True, "message": f"已移除依據：{title}。由它產生的需求已重新整理。"}


@app.post("/api/connectors")
def connector_create(body: FolderInput, request: Request, db=Depends(get_db)):
    if request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "本機資料夾必須從 Runtime 主機新增")
    path = Path(body.path).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("不是資料夾")
    if body.project_id:
        get(db, Project, body.project_id)
    row = Connector(name=body.name, path=str(path), project_id=body.project_id)
    db.add(row)
    db.flush()
    return {"id": row.id, "name": row.name}


@app.get("/api/connectors")
def connectors(db=Depends(get_db)):
    return [
        {
            "id": c.id,
            "name": c.name,
            "project_id": c.project_id,
            "files": len(c.inventory),
            "errors": c.errors,
        }
        for c in db.scalars(select(Connector))
    ]


@app.post("/api/connectors/{cid}/scan")
def connector_scan(cid: str, db=Depends(get_db)):
    return scan_folder(db, get(db, Connector, cid))


@app.post("/api/projects/{pid}/workflow/mode")
def switch_mode(pid: str, body: ModeInput, db=Depends(get_db)):
    p = get(db, Project, pid)
    activity.cancel_project(db, pid)
    p.mode, p.epoch = body.mode, p.epoch + 1
    if p.runtime == "RUNNING":
        p.runtime = "PAUSED"
    log(db, pid, "MODE", f"切換 {body.mode}；保留目前成果")
    return serialize(p)


@app.post("/api/projects/{pid}/agent/pause")
def pause(pid: str, db=Depends(get_db)):
    p = get(db, Project, pid)
    activity.cancel_project(db, pid)
    p.epoch += 1
    p.runtime = "PAUSED"
    log(db, pid, "PAUSE", "已暫停，保存檢查點")
    return serialize(p)


@app.post("/api/projects/{pid}/analysis/run")
def analyze_project(pid: str, db=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    run = launch(db, get(db, Project, pid), force_analysis=True)
    return {**serialize(run), "job_id": run.checkpoint.get("job_id")}


@app.post("/api/projects/{pid}/agent/continue")
def run(pid: str, db=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    run = launch(db, get(db, Project, pid))
    return {**serialize(run), "job_id": run.checkpoint.get("job_id")}


@app.get("/api/jobs/{rid}")
def job(rid: str, db=Depends(get_db)):
    queued = db.get(AppJob, rid)
    if queued:
        if queued.session_id != job_queue.owner_id():
            raise HTTPException(404, "找不到工作")
        return job_queue.public(queued)
    return serialize(get(db, Run, rid))


@app.get("/api/activities")
def global_activities(db=Depends(get_db)):
    return [
        serialize(a)
        for a in db.scalars(
            select(Activity)
            .where(Activity.project_id.is_(None))
            .order_by(Activity.created_at.desc())
            .limit(4)
        )
    ]


def initial_event_cursor(db, pid: str, after: int, last_event_id: int) -> int:
    cursor = max(after, last_event_id)
    if cursor:
        return cursor
    return db.scalar(
        select(Event.id).where(Event.project_id == pid)
        .order_by(Event.id.desc()).limit(1)
    ) or 0


@app.get("/api/projects/{pid}/events")
async def event_stream(pid: str, request: Request, after: int = 0):
    with Session() as db:
        get(db, Project, pid)
        cursor = initial_event_cursor(
            db, pid, after, int(request.headers.get("last-event-id", "0"))
        )

    def fetch(after_id):
        with Session() as db:
            return [serialize(row) for row in db.scalars(
                select(Event)
                .where(Event.project_id == pid, Event.id > after_id)
                .order_by(Event.id)
            )]

    async def stream():
        nonlocal cursor
        # A fresh connection observes future changes. Replaying the entire
        # history caused one full project download for every old event.
        while not await request.is_disconnected():
            # SQLite calls block; keep them off the event loop shared by all streams.
            rows = await run_in_threadpool(fetch, cursor)
            for row in rows:
                cursor = row["id"]
                yield f"id: {row['id']}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/interruptions/{iid}/dismiss")
def dismiss(iid: str, db=Depends(get_db)):
    row = get(db, Interruption, iid)
    if row.status != "RESOLVED":
        row.status = "DISMISSED"
    return serialize(row)


class ModifiedCriteria(BaseModel):
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)


def criteria_placeholder(statement):
    return [f"待 PM 依修改後需求確認可驗證條件：{statement.strip().rstrip('。')}"]


def modified_criteria(rid, statement):
    """Acceptance criteria for the PM's modified wording, generated in a
    read-only session so the model call never runs under the write lock.
    Returns (change_analysis fingerprint, criteria), or None when there is no
    proposal to update."""
    from .model_runtime import structured
    with Session() as read_db:
        r = get(read_db, Requirement, rid)
        p = get(read_db, Project, r.project_id)
        report = (p.state or {}).get("change_analysis") or {}
        proposal = next((item for item in report.get("proposals", []) if item["requirement_id"] == rid), None)
        # Skip the model when the save will be refused or the wording is the
        # AI's own suggestion (its criteria already fit).
        if (not proposal or p.runtime == "RUNNING" or p.state.get("analysis_stale")
                or report.get("model_status") == "PENDING"
                or statement.strip() == (proposal.get("proposed_requirement") or {}).get("statement")):
            return None
        config = {**settings(read_db), "_project_id": p.id}
        payload = {"topic": r.topic, "modified_requirement": statement,
                   "current_state": proposal.get("current_state", ""),
                   "evidence": [f.get("statement", "")[:400] for f in r.evidence[:6]]}
    fallback = {"acceptance_criteria": criteria_placeholder(statement)}
    try:
        result = structured(
            config, "CHANGE_PROPOSAL",
            "你是金融業資深 PM。依 PM 修改後的需求，寫出 1–3 條可驗證的繁體中文驗收條件，"
            "每條一行，格式為「前提：…；操作：…；結果：…」，涵蓋主要成功情境與最重要的例外，不寫空泛條件。"
            "只能依修改後需求的內容撰寫：修改後需求未提及或已排除的行為不得出現；"
            "不得新增需求以外的系統、欄位、核准或數值。只回傳 acceptance_criteria。",
            payload, ModifiedCriteria, lambda: fallback, timeout_seconds=90, max_tokens=800)
        criteria = [c.strip() for c in result.acceptance_criteria if c.strip()] or fallback["acceptance_criteria"]
    except Exception:
        criteria = fallback["acceptance_criteria"]
    return report.get("fingerprint"), criteria


@app.put("/api/requirements/{rid}/decision")
def decision(rid: str, body: DecisionInput, db=Depends(get_db)):
    # A modified requirement needs criteria that match its new wording; the
    # model call happens before BEGIN IMMEDIATE so it never holds the lock.
    generated = modified_criteria(rid, body.statement) if body.action == "MODIFY" and body.statement.strip() else None
    db.execute(text("BEGIN IMMEDIATE"))
    r = get(db, Requirement, rid)
    p = get(db, Project, r.project_id)
    if p.runtime == "RUNNING":
        raise ValueError("AI 工作仍在執行，請等待完成或暫停後再保存決策")
    if (p.state.get("change_analysis") or {}).get("model_status") == "PENDING":
        raise ValueError("AI 正在整理變更提案，請稍候再保存決策")
    # Adopting or rejecting the AI proposal needs no reason; only a modified
    # requirement must carry its new wording (the note is optional).
    if body.action == "MODIFY" and not body.statement.strip():
        raise ValueError("請填入修改後的需求，或先請 AI 依你的說明整理")
    if body.action == "PENDING" and body.category == "OPEN_ISSUE" and not body.rationale.strip():
        body.rationale = "PM 保留待確認，列為待決事項"
    if p.state.get("analysis_stale"):
        raise ValueError("請先更新分析，再進行決策")
    report = dict(p.state.get("change_analysis") or {})
    proposals = [dict(item) for item in report.get("proposals", [])]
    proposal = next((item for item in proposals if item["requirement_id"] == rid), {})
    suggested = (proposal.get("proposed_requirement") or {}).get("statement") or r.statement
    criteria = None
    if proposal:
        offered = dict(proposal.get("proposed_requirement") or {})
        if body.action == "MODIFY" and generated:
            fingerprint, criteria = generated
            if fingerprint != report.get("fingerprint"):
                criteria = criteria_placeholder(body.statement)
            # Keep the AI's own criteria so adopting its wording later restores them.
            offered.setdefault("suggested_acceptance_criteria", offered.get("acceptance_criteria", []))
            offered["acceptance_criteria"] = criteria
        elif "suggested_acceptance_criteria" in offered:
            offered["acceptance_criteria"] = offered.pop("suggested_acceptance_criteria")
        proposal["proposed_requirement"] = offered
        # The fingerprint stays as is: criteria are not part of it, so the
        # approved review still holds.
        report["proposals"] = proposals
    r.decision = {
        **body.model_dump(),
        "category": "SCOPE_EXCLUSION" if r.status == "EXCLUDED" else body.category,
        # Adopting means adopting the AI's formal wording shown on the card.
        "statement": body.statement if body.action == "MODIFY" else suggested if body.action == "ACCEPT" else r.statement,
        **({"acceptance_criteria": criteria} if criteria else {}),
        "decided_at": now(),
    }
    project_lifecycle.memory_for_decision(db, r)
    p.epoch += 1
    p.state = {**p.state, **({"change_analysis": report} if report else {}), "draft_id": None, "step": 6}
    p.runtime = "WAITING_HUMAN"
    if p.current_revision_id and not db.scalar(
        select(Change).where(
            Change.project_id == p.id, Change.status.in_(["OPEN", "REVIEWED"])
        )
    ):
        db.add(
            Change(
                project_id=p.id,
                source_ref=r.id,
                summary=f"人工修改：{r.topic}",
                status="REVIEWED",
                impact={
                    "affected_requirement_ids": [r.id],
                    "affected_prd_sections": ["requirements"],
                },
                resolution={"rationale": body.rationale},
            )
        )
    log(
        db,
        p.id,
        "DECISION",
        f"{body.action} · {r.topic}",
        {"requirement_id": rid, "decision": r.decision},
    )
    return serialize(r)


class RewriteInput(BaseModel):
    note: str = Field(min_length=1, max_length=1000)
    statement: str = Field(default="", max_length=2000)


class RewrittenRequirement(BaseModel):
    statement: str = Field(min_length=1, max_length=2000)


@app.post("/api/requirements/{rid}/rewrite")
def rewrite_requirement(rid: str, body: RewriteInput, db=Depends(get_db)):
    """Turn the PM's change note into formal requirement wording for review.
    Nothing is saved; the PM confirms the text with a MODIFY decision."""
    from .model_runtime import structured
    r = get(db, Requirement, rid)
    p = get(db, Project, r.project_id)
    proposal = next((item for item in (p.state.get("change_analysis") or {}).get("proposals", [])
                     if item["requirement_id"] == rid), {})
    base = body.statement.strip() or proposal.get("proposed_requirement", {}).get("statement") or r.statement
    fallback = {"statement": f"{base.rstrip('。')}；{body.note.strip().rstrip('。')}。"}
    config = {**settings(db), "_project_id": p.id}
    try:
        result = structured(
            config, "CHANGE_PROPOSAL",
            "你是金融業資深 PM。依 PM 的修改說明重寫一條正式、可驗證的繁體中文需求。"
            "PM 說明優先：原需求中與說明矛盾的文字必須刪除或改寫，不可並列保留；"
            "其餘未被修改的內容保留。不得新增說明以外的系統、欄位、核准或數值。只回傳 statement。",
            {"topic": r.topic, "current_requirement": base, "pm_change_note": body.note,
             "evidence": [f.get("statement", "")[:400] for f in r.evidence[:6]]},
            RewrittenRequirement, lambda: fallback, timeout_seconds=90, max_tokens=800)
        return {"statement": result.statement, "method": "RULES" if config.get("provider") == "mock" else "MODEL"}
    except Exception as exc:
        return {"statement": fallback["statement"], "method": "RULES",
                "warning": f"模型整理失敗，已先合併你的說明：{type(exc).__name__}"}


@app.post("/api/projects/{pid}/change-analysis/review")
def review_change_analysis(pid: str, body: ProposalReviewInput, db=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    p = get(db, Project, pid)
    if p.runtime == "RUNNING" or p.state.get("analysis_stale"):
        raise ValueError("請等分析完成後審閱最新提案")
    report = p.state.get("change_analysis") or {}
    if not report or body.fingerprint != report.get("fingerprint"):
        raise ValueError("提案版本已變動，請重新檢視分析報告")
    if report.get("model_status") == "PENDING":
        raise ValueError("AI 正在整理變更提案，請稍候再審閱")
    from .change_analysis import unresolved
    rows = list(db.scalars(select(Requirement).where(Requirement.project_id == pid)))
    if body.action == "APPROVE":
        invalid_holds = [row for row in rows if row.decision.get("action") == "PENDING" and
                         not (row.decision.get("category") == "OPEN_ISSUE" and row.decision.get("rationale"))]
        if invalid_holds:
            raise ValueError("保留待確認的項目須分類為待決事項並寫明原因")
        questions = unresolved(report, rows)
        if questions:
            raise ValueError(f"尚有 {len(questions)} 項關鍵問題待決策；可逐項決策或保留待確認")
        proposals = {item["requirement_id"]: item for item in report["proposals"]}
        names = {item["requirement_id"]: item["name"] for item in report["proposals"]}
        for row in rows:
            if row.decision.get("action") in {"ACCEPT", "MODIFY", "REJECT", "PENDING"}:
                continue
            proposal = proposals.get(row.id, {})
            status = proposal.get("status")
            if status in {"TRUE_CONFLICT", "UNSUPPORTED"}:
                continue
            decided = {"decided_at": now(), "decided_by": "PM_PROPOSAL_REVIEW"}
            if status == "CONTEXT":
                row.decision = {"action": "ACCEPT", "category": "DECISION_CONTEXT", "statement": row.statement,
                                "rationale": "現行規範，作為本次需求的背景依據", **decided}
            elif proposal.get("duplicate_of"):
                row.decision = {"action": "REJECT", "category": "FUNCTIONAL", "statement": row.statement,
                                "rationale": f"與「{names.get(proposal['duplicate_of'], '其他提案')}」重複，已合併", **decided}
            elif status == "UNKNOWN":
                row.decision = {"action": "PENDING", "category": "OPEN_ISSUE", "statement": row.statement,
                                "rationale": "資訊不足，列為待決事項", **decided}
            else:
                row.decision = {"action": "ACCEPT", "category": "FUNCTIONAL",
                                "statement": proposal.get("proposed_requirement", {}).get("statement") or row.statement,
                                "rationale": "PM 整體審閱變更提案；此項納入 PRD 草稿，尚非正式業務基準。", **decided}
        p.state = {**p.state, "change_review": {"status": "APPROVED", "fingerprint": body.fingerprint,
                                                 "rationale": body.rationale, "reviewed_at": now()}, "draft_id": None}
        resolve_interruptions(db, p, ["REQUIREMENT_REVIEW"])
        log(db, pid, "PROPOSAL_REVIEW", "PM 已整體審閱變更提案；正式定版仍需另行核准")
    else:
        p.state = {**p.state, "change_review": {"status": "DEFERRED", "fingerprint": body.fingerprint,
                                                 "rationale": body.rationale, "reviewed_at": now()}}
        log(db, pid, "PROPOSAL_REVIEW", "PM 保留變更提案待確認")
    return {"change_analysis": {**report, "review": p.state["change_review"]},
            "requirements": [serialize(r) for r in rows]}


@app.post("/api/change-requests/{cid}/resolve")
def resolve_change(cid: str, body: ChangeInput, db=Depends(get_db)):
    c = get(db, Change, cid)
    if c.status not in {"OPEN", "REVIEWED"}:
        raise ValueError("此變更已處理")
    if not c.impact:
        raise ValueError("請先執行分析取得變更影響")
    c.status = "REVIEWED" if body.action == "REVISION_REQUIRED" else body.action
    c.resolution = {**body.model_dump(), "reviewed_at": now()}
    p = get(db, Project, c.project_id)
    if body.action in {"NO_PRD_CHANGE", "CANCELLED"}:
        link = db.get(Link, (p.id, c.source_ref))
        if link:
            link.role = "REFERENCE"
        for assertion in db.scalars(
            select(Assertion).where(
                Assertion.project_id == p.id, Assertion.source_id == c.source_ref
            )
        ):
            db.delete(assertion)
        parent = db.get(Revision, p.current_revision_id)
        old = {r["id"]: r for r in parent.content["requirements"]} if parent else {}
        for rid in c.impact.get("affected_requirement_ids", []):
            req = db.get(Requirement, rid)
            if not req:
                continue
            if rid in old:
                snapshot = old[rid]
                req.statement, req.status = snapshot["statement"], snapshot["status"]
                req.evidence, req.decision = snapshot["evidence"], snapshot["decision"]
            else:
                req.decision = {
                    "action": "REJECT",
                    "rationale": body.rationale,
                    "decided_at": now(),
                }
        resolve_interruptions(db, p, ["CHANGE_REVIEW"])
    p.epoch += 1
    p.state = {**p.state, "draft_id": None}
    log(
        db,
        p.id,
        "CHANGE",
        f"變更審查：{body.action}",
        {"change_id": cid, "rationale": body.rationale},
    )
    return serialize(c)


@app.post("/api/projects/{pid}/prd/revisions")
def draft(pid: str, db=Depends(get_db)):
    return public_revision(create_draft(db, get(db, Project, pid)))


@app.post("/api/prd-revisions/{rid}/technical-amendment")
def technical_amendment(rid: str, db=Depends(get_db)):
    parent = get(db, Revision, rid)
    return public_revision(create_technical_amendment(db, get(db, Project, parent.project_id), parent))


@app.post("/api/prd-revisions/{rid}/baseline")
def approve_revision(rid: str, db=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    r = get(db, Revision, rid)
    return public_revision(baseline(db, get(db, Project, r.project_id), r))


@app.get("/api/prd-revisions/{rid}/diff")
def revision_diff(rid: str, db=Depends(get_db)):
    return get(db, Revision, rid).diff


@app.get("/api/prd-revisions/{rid}/delivery-readiness")
def delivery_readiness(rid: str, db=Depends(get_db)):
    from .technical_delivery import readiness
    return readiness(db, get(db, Revision, rid))


@app.get("/api/prd-revisions/{rid}/technical-model")
def technical_model(rid: str, db=Depends(get_db)):
    from .technical_delivery import TechnicalModel, reviews
    r = get(db, Revision, rid)
    return {"model": r.content.get("technical_model") or TechnicalModel().model_dump(),
            "diagrams": [d for d in r.content.get("diagrams", []) if d.get("generated_from") == "technical_model"],
            "reviews": reviews(db, r), "content_hash": r.content_hash}


@app.post("/api/prd-revisions/{rid}/assist/{kind}")
def revision_assist(rid: str, kind: Literal["prd", "architecture"],
                    body: TechnicalAssistInput | None = None, db=Depends(get_db)):
    from .model_assist import suggest_revision, linked_source_signature
    r = get(db, Revision, rid)
    project = get(db, Project, r.project_id)
    expected_hash = r.content_hash
    expected_sources = linked_source_signature(db, project.id)
    config = settings(db)
    from .model_runtime import enabled
    if not enabled(config, "prd_assist" if kind == "prd" else "architecture_routing"):
        raise ValueError("請先在主機設定頁啟用此項 AI 建議")
    task_id = activity.create(db, "PRD 品質候選" if kind == "prd" else "架構影響候選", config, project.id)
    db.commit()
    with activity.tracking(task_id):
        activity.stage("檢索並分析技術來源" if kind == "architecture" else "模型生成中",
                       "正在比對已確認需求與來源；候選需由人工審閱")
        result = suggest_revision(db, r, config, kind,
                                  body.source_ids if kind == "architecture" and body and body.source_ids else None)
        activity.stage("驗證來源引用", "確認候選只引用本專案可見文件")
        db.refresh(r)
        db.refresh(project)
        if r.content_hash != expected_hash or linked_source_signature(db, project.id) != expected_sources:
            raise HTTPException(409, "PRD 或來源在生成期間已有更新，請重新執行建議")
        saved = dict(project.state.get("model_assist") or {})
        saved[rid + ":" + kind] = {"revision_hash": r.content_hash,
                                     "source_signature": expected_sources,
                                     "result": result, "generated_at": now()}
        project.state = {**project.state, "model_assist": saved}
        db.commit()
        count = len(result["candidates"]) + sum(len(v) for v in (result.get("technical_model") or {}).values())
        activity.finish(task_id, "SUCCEEDED", f"已產生 {count} 項候選，可供人工審閱")
        return saved[rid + ":" + kind]


@app.get("/api/prd-revisions/{rid}/assist/{kind}")
def read_revision_assist(rid: str, kind: Literal["prd", "architecture"], db=Depends(get_db)):
    from .model_assist import linked_source_signature
    r = get(db, Revision, rid)
    project = get(db, Project, r.project_id)
    result = (project.state.get("model_assist") or {}).get(rid + ":" + kind)
    return result if (result and result.get("revision_hash") == r.content_hash
                      and result.get("source_signature") == linked_source_signature(db, project.id)) else None


@app.put("/api/prd-revisions/{rid}/technical-model")
def save_technical_model(rid: str, body: dict, db=Depends(get_db)):
    import copy
    from .technical_delivery import TechnicalModel, validate_model
    r = get(db, Revision, rid)
    if r.status not in {"DRAFT", "IN_REVIEW"}:
        raise ValueError("已定版內容不可修改；請建立新草稿")
    if body.get("expected_hash") != r.content_hash:
        raise HTTPException(409, "草稿內容已有更新，請重新整理後再儲存")
    model = TechnicalModel.model_validate(body.get("model"))
    validate_model(db, r, model)
    content = copy.deepcopy(r.content)
    content["technical_model"] = model.model_dump()
    content["diagrams"] = [d for d in content.get("diagrams", []) if d.get("generated_from") != "technical_model"]
    r.content, r.content_hash = content, content_hash(content)
    log(db, r.project_id, "TECHNICAL_MODEL", "已更新技術模型；舊審閱及技術圖表失效", {"revision_id": rid})
    return public_revision(r)


@app.post("/api/prd-revisions/{rid}/technical-diagrams")
def generate_technical_diagrams(rid: str, db=Depends(get_db)):
    import copy
    from .technical_delivery import TechnicalModel, validate_model, diagrams, render_diagrams
    r = get(db, Revision, rid)
    if r.status not in {"DRAFT", "IN_REVIEW"}:
        raise ValueError("已定版圖表不可修改")
    model = TechnicalModel.model_validate(r.content.get("technical_model") or {})
    validate_model(db, r, model)
    if not model.business_rules or not model.system_components or not model.technical_impact:
        raise ValueError("須先補齊商業規則、系統清單與技術影響判斷，才能生成三張圖")
    if not model.interface_contracts and not any(x.area == "INTERFACE" and not x.affected for x in model.technical_impact):
        raise ValueError("須提供介面契約，或明確記錄介面無異動及其來源")
    specs = diagrams(model)
    render_diagrams(r.id, specs)
    content = copy.deepcopy(r.content)
    content["diagrams"] = [d for d in content.get("diagrams", []) if d.get("type") not in {x["type"] for x in specs}] + specs
    r.content, r.content_hash = content, content_hash(content)
    log(db, r.project_id, "TECHNICAL_DIAGRAMS", "已從技術模型生成並渲染三張圖", {"revision_id": rid})
    return public_revision(r)


class TechnicalReviewInput(BaseModel):
    role: Literal["PM", "SA", "QA"]
    reviewer: str = Field(min_length=2, max_length=120)
    note: str = Field(min_length=3, max_length=2000)
    expected_hash: str = Field(min_length=64, max_length=64)


@app.post("/api/prd-revisions/{rid}/technical-reviews")
def add_technical_review(rid: str, body: TechnicalReviewInput, db=Depends(get_db)):
    r = get(db, Revision, rid)
    if r.status not in {"DRAFT", "IN_REVIEW", "BASELINED"}:
        raise ValueError("此版本不可新增審閱")
    if body.expected_hash != r.content_hash:
        raise HTTPException(409, "內容版本已變更，請重新審閱")
    if client_gateway.CURRENT_CLIENT.get() and body.role != "PM":
        raise HTTPException(403, "SA、QA 技術審閱須在主機端完成")
    db.add(TechnicalReview(revision_id=rid, content_hash=r.content_hash,
                           role=body.role, reviewer=body.reviewer.strip(), note=body.note.strip()))
    db.flush()
    from .technical_delivery import readiness
    return readiness(db, r)


@app.post("/api/prd-revisions/{rid}/delivery-package")
def create_delivery_package(rid: str, db=Depends(get_db)):
    from .technical_delivery import package
    r = get(db, Revision, rid)
    row = package(db, r)
    db.flush()
    return {"id": row.id, "manifest": row.manifest}


@app.get("/api/prd-revisions/{rid}/delivery-package")
def download_delivery_package(rid: str, db=Depends(get_db)):
    r = get(db, Revision, rid)
    row = db.scalar(select(DeliveryPackage).where(DeliveryPackage.revision_id == r.id))
    if not row or not Path(row.path).is_file():
        raise HTTPException(404, "此版本尚無 IT 交付包")
    return FileResponse(row.path, filename=f"TongLing_{r.version}_IT_HANDOFF.zip", media_type="application/zip")


@app.get("/api/prd-revisions/{rid}/download")
def download(rid: str, db=Depends(get_db)):
    r = get(db, Revision, rid)
    if r.status not in {"BASELINED", "SUPERSEDED"}:
        raise ValueError("請先審閱並建立正式版本")
    from .db import DATA
    local_export = DATA / "exports" / r.id / "PRD.docx"
    export_path = local_export if local_export.exists() else Path(r.docx_path) if r.docx_path else None
    if not export_path or not export_path.is_file():
        raise HTTPException(404, "輸出檔案不存在，請檢查資料備份")
    return FileResponse(
        export_path,
        filename=f"TongLing_{r.version}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


class DiagramInput(BaseModel):
    mermaid: str = Field(min_length=1, max_length=20000)
    title: str = Field(min_length=1, max_length=120)


@app.put("/api/prd-revisions/{rid}/diagrams/{index}")
def edit_diagram(rid: str, index: int, body: DiagramInput, db=Depends(get_db)):
    r = get(db, Revision, rid)
    if r.status not in {"DRAFT", "IN_REVIEW"}:
        raise ValueError("不可改寫已定版圖表")
    if re_unsafe(body.mermaid):
        raise ValueError("圖表包含不允許的指令")
    import copy

    content = copy.deepcopy(r.content)
    if not 0 <= index < len(content["diagrams"]):
        raise HTTPException(404)
    if content["diagrams"][index].get("generated_from") == "technical_model":
        raise ValueError("技術圖表須由模型重新生成，不能直接改寫")
    content["diagrams"][index] = {
        **content["diagrams"][index],
        **body.model_dump(),
        "warning": "人工編輯，請確認與來源一致",
    }
    r.content, r.content_hash = content, content_hash(content)
    log(
        db,
        r.project_id,
        "DIAGRAM",
        "人工編輯圖表",
        {"revision_id": rid, "index": index},
    )
    return public_revision(r)


@app.post("/api/projects/{pid}/meetings")
def create_meeting(pid: str, body: MeetingInput, db=Depends(get_db)):
    get(db, Project, pid)
    try:
        ZoneInfo(body.timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("不支援的時區")
    if body.started_at:
        try:
            started = (datetime.fromisoformat(body.started_at) if "T" in body.started_at
                       else datetime.fromisoformat(f"{body.meeting_date.isoformat()}T{body.started_at}").replace(tzinfo=ZoneInfo(body.timezone)))
        except ValueError as exc:
            raise ValueError("請輸入有效的開始時間，例如 14:00") from exc
        if not started.tzinfo or started.date() != body.meeting_date:
            raise ValueError("開始時間須含時區，且與會議日期相符")
        body.started_at = started.isoformat()
    same = db.scalar(
        select(Meeting).where(
            Meeting.project_id == pid,
            Meeting.meeting_date == body.meeting_date.isoformat(),
        )
    )
    if same and (not same.started_at or not body.started_at):
        raise ValueError("同日多場會議必須都填寫開始時間以確認先後")
    row = Meeting(
        project_id=pid,
        **{**body.model_dump(), "meeting_date": body.meeting_date.isoformat()},
    )
    db.add(row)
    db.flush()
    log(db, pid, "MEETING", f"新增會議：{body.meeting_date} {body.title}")
    return serialize(row)


@app.post("/api/meetings/{mid}/transcript")
def meeting_transcript(mid: str, file: UploadFile = File(...), db=Depends(get_db)):
    row = get(db, Meeting, mid)
    if row.status == "CONFIRMED":
        raise ValueError("已確認紀錄不可覆寫，請建立新的會議")
    blocks = parse_file(
        file.filename or "transcript.txt", file.file.read(30 * 1024 * 1024 + 1)
    )
    row.transcript = "\n".join(b["text"] for b in blocks)
    row.status, row.events = "IMPORTED", []
    row.analysis_error = ""
    return serialize(row)


@app.patch("/api/meetings/{mid}/transcript")
def edit_meeting_transcript(mid: str, body: TextInput, db=Depends(get_db)):
    row = get(db, Meeting, mid)
    if row.status == "CONFIRMED":
        raise ValueError("已確認紀錄不可覆寫，請新增後續會議")
    row.transcript, row.events, row.status, row.analysis_error = (
        body.text,
        [],
        "IMPORTED",
        "",
    )
    log(db, row.project_id, "MEETING_EDIT", f"已更新會議「{row.title}」逐字稿")
    return serialize(row)


@app.post("/api/meetings/{mid}/manual-record")
def manual_record(mid: str, body: TextInput, db=Depends(get_db)):
    row = get(db, Meeting, mid)
    if row.status == "CONFIRMED":
        raise ValueError("已確認紀錄不可覆寫")
    facts = extract([{"id": f"{mid}:manual", "text": body.text}], {"provider": "mock"})
    if not facts:
        raise ValueError("請填寫完整的會議決議或待辦，不能只有空白或標題")
    row.events = [{**f.model_dump(), "origin": "HUMAN"} for f in facts]
    row.transcript = row.transcript + "\n\n人工整理紀錄：\n" + body.text
    row.status, row.analysis_error = "ANALYZED", ""
    log(
        db,
        row.project_id,
        "MEETING_MANUAL",
        f"人工整理會議「{row.title}」紀錄，等待最後確認",
    )
    return serialize(row)


@app.get("/api/meetings/{mid}")
def get_meeting(mid: str, db=Depends(get_db)):
    return serialize(get(db, Meeting, mid))


@app.post("/api/meetings/{mid}/analyze")
def analyze_meeting(mid: str, db=Depends(get_db)):
    row = get(db, Meeting, mid)
    if row.status == "CONFIRMED":
        raise ValueError("已確認會議不可覆寫")
    if not row.transcript.strip():
        raise ValueError("請先輸入逐字稿")
    if row.status == "ANALYZING":
        return serialize(row)
    config = settings(db)
    row.events, row.status, row.analysis_error = [], "ANALYZING", ""
    task_id = activity.create(db, "會議分析：" + row.title, config, row.project_id)
    log(
        db,
        row.project_id,
        "MEETING_ANALYZING",
        f"正在分析會議「{row.title}」，可繼續操作其他內容",
    )
    db.commit()
    meeting_service.workers.submit(
        meeting_service.analyze, row.id, row.transcript, config, task_id
    )
    return serialize(row)


@app.post("/api/meetings/{mid}/confirm")
def confirm_meeting(mid: str, body: MeetingConfirmInput | None = None, db=Depends(get_db)):
    row = get(db, Meeting, mid)
    if row.status == "CONFIRMED":
        return serialize(row)
    if row.status != "ANALYZED" or not row.events:
        raise ValueError(
            "目前沒有可確認的紀錄。請補充逐字稿後重新分析，或人工整理會議紀錄。"
        )
    if body and body.selected_indices is not None:
        indices = body.selected_indices
        if not indices or len(indices) != len(set(indices)) or any(index < 0 or index >= len(row.events) for index in indices):
            raise ValueError("請至少選擇一項有效的會議紀錄")
        excluded = len(row.events) - len(indices)
        row.events = [row.events[index] for index in indices]
    else:
        excluded = 0
    p = get(db, Project, row.project_id)
    source = ingest(
        db,
        f"{row.meeting_date} {row.title}",
        [{"text": x["statement"]} for x in row.events],
        kind="MEETING_RECORD",
        project=p,
        meta={"meeting_id": row.id, "meeting_date": row.meeting_date},
    )
    row.status, row.record_source_id = "CONFIRMED", source.id
    p.state = {**p.state, "waiting_meeting": False}
    resolve_interruptions(db, p, ["WAITING_MEETING"])
    log(db, p.id, "MEETING_CONFIRMED", "人工確認會議紀錄", {"meeting_id": row.id, "excluded_count": excluded})
    return serialize(row)


@app.post("/api/projects/{pid}/wait-meeting")
def wait_meeting(pid: str, db=Depends(get_db)):
    p = get(db, Project, pid)
    p.epoch += 1
    p.runtime, p.state = (
        "WAITING_MEETING",
        {**p.state, "waiting_meeting": True, "step": 4},
    )
    interruption(
        db,
        p,
        "WAITING_MEETING",
        "等待會議紀錄",
        "請安排決策會議並加入紀錄；關閉提醒不會取消等待。",
    )
    return serialize(p)


@app.get("/api/meetings/{mid}/ics")
def meeting_ics(mid: str, db=Depends(get_db)):
    m = get(db, Meeting, mid)

    def escape(s):
        return (
            s.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    start = m.meeting_date.replace("-", "")
    if m.started_at:
        stamp = datetime.fromisoformat(m.started_at).astimezone(timezone.utc)
        start_field = f"DTSTART:{stamp.strftime('%Y%m%dT%H%M%SZ')}"
    else:
        start_field = f"DTSTART;VALUE=DATE:{start}"
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//TongLing//Personal//ZH",
            "BEGIN:VEVENT",
            f"UID:{m.id}@tongling.local",
            f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            start_field,
            f"SUMMARY:{escape(m.title)}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    return Response(
        ics,
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="meeting.ics"'},
    )
