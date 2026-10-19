"""Meeting transcripts get meeting-specific classification guidance; documents keep the generic prompt."""

from app import intelligence, model_runtime


def captured_prompt(monkeypatch, config):
    seen = {}

    def fake_structured(config, task, system, payload, schema, *args, **kwargs):
        seen["task"], seen["system"] = task, system
        return schema(assertions=[])

    monkeypatch.setattr(model_runtime, "structured", fake_structured)
    intelligence.extract([{"id": "m:0", "text": "總結：附件密碼一律以簡訊另送。"}], config)
    return seen


def test_meeting_extraction_uses_meeting_guidance(monkeypatch):
    seen = captured_prompt(monkeypatch, {"provider": "ollama", "_meeting_extract": True})
    assert seen["task"] == "MEETING_FACT_EXTRACTION"
    assert seen["system"].startswith(intelligence.SYSTEM_PROMPT)
    assert "ACTION_ITEM when a participant explicitly commits" in seen["system"]


def test_document_extraction_keeps_generic_prompt(monkeypatch):
    seen = captured_prompt(monkeypatch, {"provider": "ollama"})
    assert seen["task"] == "SOURCE_FACT_EXTRACTION"
    assert seen["system"] == intelligence.SYSTEM_PROMPT
