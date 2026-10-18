"""Separate localhost admin and ngrok client-gateway processes.

The gateway listens on loopback port 8001. The admin process stays on 8000.
The tunnel must point only to 8001; process role does not depend on Host headers.
"""

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import func, select

from .db import DATA, ClientSession, Session, now

IS_GATEWAY = os.getenv("TONGLING_CLIENT_GATEWAY") == "1"
CURRENT_CLIENT: ContextVar[str | None] = ContextVar("tongling_client", default=None)
SESSION_TTL = 30 * 86400
DEMO_SESSION_TTL = 8 * 3600
DEMO_MAX_ACTIVE = 60
DEMO_ISSUE_LIMIT = (5, 600)  # sessions per client address per window (seconds)
ATTEMPTS: dict[str, list[float]] = {}
DEMO_ISSUES: dict[str, list[float]] = {}
ACCESS_DEFAULTS = {"mode": "pairing", "demo_network": "LAN"}
_access_cache: tuple[float, dict] = (0.0, ACCESS_DEFAULTS)
SECRET_PATH = DATA / "client_gateway_secret"
URL_PATH = DATA / "client_gateway_url.txt"


def _secret():
    try:
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


def pairing_details():
    slot = int(time.time() // 600)
    digest = hmac.new(_secret(), str(slot).encode(), hashlib.sha256).digest()
    code = base64.b32encode(digest).decode().rstrip("=")[:10]
    return {"code": code, "expires_at": (slot + 1) * 600}


def check_pairing(code):
    expected = pairing_details()["code"]
    return hmac.compare_digest(str(code).strip().upper(), expected)


def access_config(db=None):
    """Connection mode is stored with the workspace, so both processes agree."""
    global _access_cache
    if db is None:
        stamp, cached = _access_cache
        if time.monotonic() - stamp < 1:
            return cached
        with Session() as session:
            return access_config(session)
    from .db import Workspace
    row = db.scalar(select(Workspace))
    stored = ((row.settings if row else {}) or {}).get("client_access") or {}
    config = {**ACCESS_DEFAULTS, **{k: v for k, v in stored.items() if k in ACCESS_DEFAULTS}}
    if config["mode"] not in {"pairing", "demo"}:
        config["mode"] = "pairing"
    if config["demo_network"] not in {"LAN", "PUBLIC"}:
        config["demo_network"] = "LAN"
    _access_cache = (time.monotonic(), config)
    return config


def save_access_config(db, mode, demo_network):
    from .db import Workspace
    row = db.scalar(select(Workspace))
    config = {"mode": mode, "demo_network": demo_network, "changed_at": now()}
    row.settings = {**(row.settings or {}), "client_access": config}
    revoked = 0
    if mode != "demo":
        for session in db.scalars(select(ClientSession).where(
                ClientSession.kind == "DEMO", ClientSession.revoked_at.is_(None))):
            session.revoked_at = now()
            revoked += 1
    global _access_cache
    _access_cache = (0.0, ACCESS_DEFAULTS)
    return {"mode": mode, "demo_network": demo_network, "revoked_demo_sessions": revoked}


def trusted_network(address):
    """Private or loopback peers. The frontend proxy forwards the real peer."""
    if address == "testclient":
        return True
    try:
        parsed = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    return parsed.is_private or parsed.is_loopback or parsed.is_link_local


def private_ip_host(hostname):
    try:
        parsed = ipaddress.ip_address((hostname or "").strip("[]"))
    except ValueError:
        return False
    return parsed.is_private or parsed.is_loopback


def demo_block_reason(address, hostname):
    config = access_config()
    if config["mode"] != "demo":
        return "主機目前使用正式模式，請輸入配對碼"
    public_host = _public_host()
    via_tunnel = bool(public_host) and hostname == public_host
    if config["demo_network"] == "LAN" and (via_tunnel or not trusted_network(address)):
        return "展示模式僅限主機所在的區域網路；請改用區網網址或向管理者取得配對碼"
    return ""


def _public_host():
    from urllib.parse import urlparse
    return urlparse(tunnel_url()).hostname


def issue_demo_session(address, client_id):
    window_count, window = DEMO_ISSUE_LIMIT
    recent = [t for t in DEMO_ISSUES.get(address, []) if time.time() - t < window]
    if len(recent) >= window_count:
        raise PermissionError("展示連線建立過於頻繁，請稍後再試")
    with Session() as db:
        active = db.scalar(select(func.count()).select_from(ClientSession).where(
            ClientSession.kind == "DEMO", ClientSession.revoked_at.is_(None),
            ClientSession.expires_at > int(time.time()))) or 0
    if active >= DEMO_MAX_ACTIVE:
        raise PermissionError("展示連線人數已達上限，請稍後再試或向管理者索取配對碼")
    DEMO_ISSUES[address] = recent + [time.time()]
    return new_session(client_id, kind="DEMO", ttl=DEMO_SESSION_TTL)


def session_info(token):
    """Return (client_id, kind) for a live session; demo sessions die with demo mode."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with Session() as db:
        row = db.scalar(select(ClientSession).where(ClientSession.token_hash == token_hash))
        if not row or row.revoked_at or row.expires_at <= time.time():
            return None
        if row.kind == "DEMO" and access_config()["mode"] != "demo":
            return None
        timestamp = int(time.time())
        if row.last_seen_at < timestamp - 60:
            row.last_seen_at = timestamp
            db.commit()
        return row.client_id, row.kind


def session_client(token):
    info = session_info(token)
    return info[0] if info else None


def new_session(client_id, kind="PAIRED", ttl=SESSION_TTL):
    token = secrets.token_urlsafe(32)
    with Session.begin() as db:
        db.add(ClientSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                             client_id=client_id, expires_at=int(time.time()) + ttl,
                             last_seen_at=int(time.time()), kind=kind))
    return token


def revoke_session(token):
    if not token:
        return
    with Session.begin() as db:
        row = db.scalar(select(ClientSession).where(
            ClientSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        if row:
            row.revoked_at = now()


def project_is_owned(project, client_id=None):
    owner = client_id or CURRENT_CLIENT.get()
    return bool(project and (project.state or {}).get("client_owner_id") == owner)


PUBLIC_ROUTES = [
    ("GET", r"/api/health"),
    ("GET", r"/api/session/context"),
    ("POST", r"/api/pair"),
    ("POST", r"/api/session/demo"),
    ("POST", r"/api/logout"),
    ("GET", r"/api/client/workspace-config"),
    ("GET", r"/api/dashboard"),
    ("GET", r"/api/projects"),
    ("GET", r"/api/projects/trash"),
    ("POST", r"/api/projects"),
    ("POST", r"/api/projects/[^/]+/todos"),
    ("PATCH", r"/api/projects/[^/]+/todos/[^/]+"),
    ("DELETE", r"/api/projects/[^/]+/todos/[^/]+"),
    ("GET", r"/api/projects/[^/]+"),
    ("GET", r"/api/projects/[^/]+/pulse"),
    ("DELETE", r"/api/projects/[^/]+"),
    ("POST", r"/api/projects/[^/]+/restore"),
    ("POST", r"/api/projects/[^/]+/workflow/mode"),
    ("POST", r"/api/projects/[^/]+/(requirement-text|sources/upload|agent/continue|agent/pause|analysis/run|agent/delegation)"),
    ("POST", r"/api/projects/[^/]+/(knowledge/attach|knowledge/attach-remote|meetings|wait-meeting|peer-research)"),
    ("DELETE", r"/api/projects/[^/]+/sources/[^/]+"),
    ("GET", r"/api/projects/[^/]+/peer-research"),
    ("GET", r"/api/knowledge/graph/status"),
    ("POST", r"/api/retrieval/search"),
    ("GET", r"/api/projects/[^/]+/events"),
    ("GET", r"/api/jobs/[^/]+"),
    ("GET", r"/api/jobs"),
    ("POST", r"/api/jobs"),
    ("POST", r"/api/jobs/[^/]+/(cancel|resume)"),
    ("GET", r"/api/sources/[^/]+"),
    ("PUT", r"/api/requirements/[^/]+/decision"),
    ("POST", r"/api/requirements/[^/]+/rewrite"),
    ("POST", r"/api/projects/[^/]+/change-analysis/review"),
    ("POST", r"/api/change-requests/[^/]+/resolve"),
    ("POST", r"/api/interruptions/[^/]+/dismiss"),
    ("POST", r"/api/meetings/[^/]+/(transcript|manual-record|analyze|confirm)"),
    ("PATCH", r"/api/meetings/[^/]+/transcript"),
    ("GET", r"/api/meetings/[^/]+/ics"),
    ("POST", r"/api/projects/[^/]+/prd/revisions"),
    ("POST", r"/api/prd-revisions/[^/]+/baseline"),
    ("POST", r"/api/prd-revisions/[^/]+/technical-amendment"),
    ("GET", r"/api/prd-revisions/[^/]+/(download|diff|delivery-readiness)"),
    ("GET", r"/api/prd-revisions/[^/]+/(technical-model|delivery-package)"),
    ("GET", r"/api/prd-revisions/[^/]+/assist/(prd|architecture)"),
    ("POST", r"/api/prd-revisions/[^/]+/assist/(prd|architecture)"),
    ("PUT", r"/api/prd-revisions/[^/]+/technical-model"),
    ("POST", r"/api/prd-revisions/[^/]+/(technical-diagrams|technical-reviews|delivery-package)"),
    ("GET", r"/api/prd-revisions/[^/]+/(projection|export-template)"),
    ("PUT", r"/api/prd-revisions/[^/]+/presentation"),
    ("PUT", r"/api/prd-revisions/[^/]+/diagrams/[0-9]+"),
    ("POST", r"/api/agent/guidance"),
]


def client_route_allowed(method, path):
    return any(method == verb and re.fullmatch(pattern, path) for verb, pattern in PUBLIC_ROUTES)


def tunnel_url():
    return URL_PATH.read_text(encoding="utf-8-sig").strip() if URL_PATH.exists() else ""
