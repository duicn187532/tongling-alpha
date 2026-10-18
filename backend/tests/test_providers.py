import httpx
import pytest
from app.intelligence import extract, check_connection


def test_connectivity_only_reads_model_list(monkeypatch):
    real_client = httpx.Client

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [{"name": "example:latest", "capabilities": ["completion"]}]
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    config = {
        "provider": "ollama",
        "model": "example",
        "endpoint": "http://localhost:11434",
        "local_only": True,
    }
    assert check_connection(config)["ok"]
    with pytest.raises(ValueError, match="找不到模型"):
        check_connection({**config, "model": "missing"})


def test_connectivity_timeout_is_actionable(monkeypatch):
    real_client = httpx.Client

    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ValueError, match="連線檢查逾時"):
        check_connection(
            {
                "provider": "ollama",
                "model": "example",
                "endpoint": "http://localhost:11434",
                "local_only": True,
            }
        )


def test_provider_repairs_invalid_output_and_checks_citations(monkeypatch):
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        raw = (
            '{"assertions":[{"topic":"登入","statement":"需要驗證","chunk_ids":["bad"]}]}'
            if len(calls) == 1
            else '{"assertions":[{"topic":"登入","statement":"需要驗證","chunk_ids":["c1"]}]}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = extract(
        [{"id": "c1", "text": "登入：需要驗證"}],
        {
            "provider": "openai_compatible",
            "endpoint": "http://127.0.0.1:1234/v1",
            "model": "test",
            "local_only": True,
        },
    )
    assert len(calls) == 2
    assert result[0].chunk_ids == ["c1"]


def test_provider_does_not_silently_fallback(monkeypatch):
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"message": {"content": "not JSON"}})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ValueError, match="驗證失敗"):
        extract(
            [{"id": "c1", "text": "登入：需要驗證"}],
            {
                "provider": "ollama",
                "endpoint": "http://127.0.0.1:11434",
                "model": "test",
                "local_only": True,
            },
        )
    assert len(calls) == 2
