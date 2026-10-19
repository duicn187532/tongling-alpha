"""A one-click fictional requirement for people trying TongLing."""

import random

from fastapi.testclient import TestClient

from app import client_gateway, sample_requirements
from app.main import app


def test_offline_sample_is_a_complete_fictional_requirement(client):
    sample = client.post("/api/demo/sample-requirement").json()
    assert sample["method"] == "RULES"
    assert {sample["name"], sample["brief"]} <= {x for s in sample_requirements.FALLBACKS for x in s.values()}


def test_each_request_picks_its_own_area_and_angle(monkeypatch):
    seen = []
    monkeypatch.setattr("app.model_runtime.structured",
                        lambda config, task, instruction, payload, schema, fallback, **kw:
                        seen.append((payload["business_area"], payload["angle"])) or
                        schema(name="範例需求名稱", brief="現況說明。" * 10))
    rng = random.Random(7)
    first = sample_requirements.generate({"provider": "ollama"}, rng)
    sample_requirements.generate({"provider": "ollama"}, rng)
    assert first["method"] == "MODEL" and first["area"] == "・".join(seen[0])
    assert len(set(seen)) == 2


def test_clients_may_generate_samples(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as visitor:
        code = client_gateway.pairing_details()["code"]
        assert visitor.post("/api/pair", json={"code": code}).status_code == 200
        assert visitor.post("/api/demo/sample-requirement").status_code == 200
