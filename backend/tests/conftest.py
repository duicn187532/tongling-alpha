import os
import tempfile
from pathlib import Path
import pytest
import sys

# CI runs pytest from the repo root; the evaluation package lives in backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_root = Path(tempfile.mkdtemp(prefix="tongling-tests-"))
os.environ["DATA_DIR"] = str(_root)
os.environ["DATABASE_URL"] = "sqlite:///" + str(_root / "tests.db")

from alembic.config import Config
from alembic import command
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture(scope="session")
def client():
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with TestClient(app) as client:
        yield client


@pytest.fixture
def project(client):
    response = client.post(
        "/api/projects",
        json={
            "name": "測試專案",
            "description": "登入驗證：使用一次性驗證碼\n查詢紀錄：保留查詢歷程",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()
