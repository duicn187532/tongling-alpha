"""Separate localhost admin and ngrok client-gateway processes.

The gateway listens on loopback port 8001. The admin process stays on 8000.
The tunnel must point only to 8001; process role does not depend on Host headers.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import select

from .db import DATA, ClientSession, Session, now

IS_GATEWAY = os.getenv("TONGLING_CLIENT_GATEWAY") == "1"
CURRENT_CLIENT: ContextVar[str | None] = ContextVar("tongling_client", default=None)
SESSION_TTL = 30 * 86400
ATTEMPTS: dict[str, list[float]] = {}
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


def session_client(token):
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with Session() as db:
        row = db.scalar(select(ClientSession).where(ClientSession.token_hash == token_hash))
        return row.client_id if row and not row.revoked_at and row.expires_at > time.time() else None


def new_session(client_id):
    token = secrets.token_urlsafe(32)
    with Session.begin() as db:
        db.add(ClientSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                             client_id=client_id, expires_at=int(time.time()) + SESSION_TTL))
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
    ("POST", r"/api/logout"),
    ("GET", r"/api/client/workspace-config"),
    ("GET", r"/api/projects"),
    ("GET", r"/api/projects/trash"),
    ("POST", r"/api/projects"),
    ("GET", r"/api/projects/[^/]+"),
    ("GET", r"/api/projects/[^/]+/pulse"),
    ("DELETE", r"/api/projects/[^/]+"),
    ("POST", r"/api/projects/[^/]+/restore"),
    ("POST", r"/api/projects/[^/]+/workflow/mode"),
    ("POST", r"/api/projects/[^/]+/(requirement-text|sources/upload|agent/continue|agent/pause|analysis/run|agent/delegation)"),
    ("POST", r"/api/projects/[^/]+/(knowledge/attach|meetings|wait-meeting|peer-research)"),
    ("GET", r"/api/projects/[^/]+/peer-research"),
    ("GET", r"/api/knowledge/graph/status"),
    ("POST", r"/api/retrieval/search"),
    ("GET", r"/api/projects/[^/]+/events"),
    ("GET", r"/api/jobs/[^/]+"),
    ("GET", r"/api/sources/[^/]+"),
    ("PUT", r"/api/requirements/[^/]+/decision"),
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
