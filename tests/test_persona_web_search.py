from pathlib import Path

import app as app_module


class _Response:
    status_code = 200

    def json(self):
        return {
            "organic": [
                {
                    "title": "Official character page",
                    "link": "https://example.com/official-character",
                    "snippet": "Official profile information.",
                }
            ]
        }


def test_persona_web_search_returns_normalized_sources(monkeypatch):
    monkeypatch.setattr(app_module, "SERPER_KEY", "test-key")
    app_module.PERSONA_SEARCH_CACHE.clear()
    monkeypatch.setattr(app_module.requests, "post", lambda *args, **kwargs: _Response())

    status, sources = app_module._call_serper_web_search("series character")

    assert status == "success"
    assert sources == [{
        "title": "Official character page",
        "url": "https://example.com/official-character",
        "snippet": "Official profile information.",
    }]


def test_persona_web_search_reports_missing_key(monkeypatch):
    monkeypatch.setattr(app_module, "SERPER_KEY", "")
    app_module.PERSONA_SEARCH_CACHE.clear()

    status, sources = app_module._call_serper_web_search("series character")

    assert status == "unavailable"
    assert sources == []


def test_persona_prompt_only_reuses_generation_prompt_without_calling_model(monkeypatch):
    sources = [{
        "title": "Official profile",
        "url": "https://example.com/profile",
        "snippet": "Official character facts.",
    }]
    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(app_module, "get_ai_language", lambda user_id=None: "zh")
    monkeypatch.setattr(app_module, "_call_serper_web_search", lambda query: ("success", sources))

    def fail_model_call(*args, **kwargs):
        raise AssertionError("prompt_only must not call a model")

    monkeypatch.setattr(app_module, "call_openrouter", fail_model_call)
    monkeypatch.setattr(app_module, "call_gemini", fail_model_call)

    with app_module.app.test_request_context(json={
        "char_name": "国神炼介",
        "source_ip": "蓝色监狱",
        "web_search": True,
        "prompt_only": True,
    }):
        response = app_module.generate_persona()
        data = response.get_json()

    assert data["status"] == "success"
    assert data["search_status"] == "success"
    assert data["sources"] == sources
    assert "# System" in data["prompt"]
    assert "你是一位资深的角色设定师" in data["prompt"]
    assert "Official character facts." in data["prompt"]
    assert "# User" in data["prompt"]
    assert "キャラクター名: 国神炼介" in data["prompt"]
    assert "作品名: 蓝色监狱" in data["prompt"]


def test_memory_persona_editor_has_prompt_copy_action():
    source = (Path(__file__).resolve().parents[1] / "templates" / "memory.html").read_text(encoding="utf-8")

    assert "复制生成人设 Prompt" in source
    assert "openGenModal('copy')" in source
    assert "function copyPersonaGenerationPrompt" in source
    assert "prompt_only: true" in source
    assert "submitPersonaAction(this)" in source
