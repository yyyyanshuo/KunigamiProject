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
    assert "[[LOCK]]" in data["prompt"]
    assert "至少输出一个合法锁定区块" in data["prompt"]
    assert "Official character facts." in data["prompt"]
    assert "# User" in data["prompt"]
    assert "キャラクター名: 国神炼介" in data["prompt"]
    assert "作品名: 蓝色监狱" in data["prompt"]
    assert "如果是蓝色监狱的角色" not in data["prompt"]
    assert "寝室（床位顺序）" not in data["prompt"]
    assert "寝室配置" not in data["prompt"]


def test_memory_persona_editor_has_prompt_copy_action():
    source = (Path(__file__).resolve().parents[1] / "templates" / "memory.html").read_text(encoding="utf-8")

    assert "复制生成人设 Prompt" in source
    assert "openGenModal('copy')" in source
    assert "function copyPersonaGenerationPrompt" in source
    assert "prompt_only: true" in source
    assert "char_id: charId" in source
    assert "existing_persona: currentPersonaDraft()" in source
    assert "submitPersonaAction(this)" in source
    assert "visual_descriptions" not in source
    assert "visual-tags" not in source


def test_generated_persona_discards_removed_fields():
    content, persona_text = app_module._decode_generated_persona(
        '{"system_prompt":"核心人设","visual_descriptions":{"tags":"legacy"},'
        '"custom_settings":{"reply_style":"legacy"}}'
    )

    assert content == {"system_prompt": "核心人设"}
    assert persona_text == "核心人设"


def test_generate_persona_surfaces_upstream_error_instead_of_lock_error(monkeypatch):
    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(app_module, "get_ai_language", lambda user_id=None: "zh")
    monkeypatch.setattr(app_module, "get_model_config", lambda *args, **kwargs: ("relay", "test-model"))
    monkeypatch.setattr(
        app_module,
        "call_openrouter",
        lambda *args, **kwargs: "（系统提示：访问被拒绝，请检查 API Key。）",
    )

    with app_module.app.test_request_context(json={
        "char_name": "测试角色",
        "source_ip": "测试作品",
        "web_search": False,
    }):
        response, status = app_module.generate_persona()
        data = response.get_json()

    assert status == 502
    assert data["code"] == "persona_generation_upstream_error"
    assert data["error"] == "访问被拒绝，请检查 API Key。"
    assert "LOCK" not in data["error"]


def test_generate_persona_retries_once_when_model_omits_lock(monkeypatch):
    replies = iter([
        "# 角色\n第一次漏掉了标签",
        "[[LOCK]]\n# 角色\n官方设定\n[[/LOCK]]\n# 补充\n推测内容",
    ])
    calls = []
    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(app_module, "get_ai_language", lambda user_id=None: "zh")
    monkeypatch.setattr(app_module, "get_model_config", lambda *args, **kwargs: ("relay", "test-model"))

    def fake_call(messages, **kwargs):
        calls.append(messages)
        return next(replies)

    monkeypatch.setattr(app_module, "call_openrouter", fake_call)

    with app_module.app.test_request_context(json={
        "char_name": "测试角色",
        "source_ip": "测试作品",
        "web_search": False,
    }):
        response = app_module.generate_persona()
        data = response.get_json()

    assert data["status"] == "success"
    assert set(data["content"]) == {"system_prompt"}
    assert data["content"]["system_prompt"].startswith("[[LOCK]]")
    assert len(calls) == 2
    assert "上一版人设没有通过 LOCK 格式校验" in calls[1][-1]["content"]


def test_generate_persona_retries_concisely_after_max_tokens(monkeypatch):
    class TruncatedText(str):
        complete = False
        finish_reason = "MAX_TOKENS"

    replies = iter([
        TruncatedText("[[LOCK]]\n未完成的长人设"),
        "[[LOCK]]\n# 角色\n精炼且完整的官方设定\n[[/LOCK]]",
    ])
    calls = []
    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(app_module, "get_ai_language", lambda user_id=None: "zh")
    monkeypatch.setattr(app_module, "get_model_config", lambda *args, **kwargs: ("relay", "test-model"))

    def fake_call(messages, **kwargs):
        calls.append(messages)
        return next(replies)

    monkeypatch.setattr(app_module, "call_openrouter", fake_call)

    with app_module.app.test_request_context(json={
        "char_name": "测试角色",
        "source_ip": "测试作品",
        "web_search": False,
    }):
        response = app_module.generate_persona()
        data = response.get_json()

    assert data["status"] == "success"
    assert len(calls) == 2
    assert "上一次输出因长度限制被截断" in calls[1][-1]["content"]
    assert data["content"]["system_prompt"].endswith("[[/LOCK]]")
