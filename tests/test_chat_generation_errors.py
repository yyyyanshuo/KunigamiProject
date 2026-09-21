from pathlib import Path

from flask import Flask

from blueprints import chat as chat_blueprint
from services.ai_client import ai_error_text


ROOT = Path(__file__).resolve().parents[1]


def test_provider_error_is_converted_to_json_before_chat_storage():
    app = Flask(__name__)
    error = ai_error_text(
        "AI 生成回复超时，请重试。",
        code="ai_timeout",
        retryable=True,
        provider="gemini",
        status_code=504,
    )

    with app.app_context():
        response, status = chat_blueprint._ai_error_json_response(
            error, user_msg_id=17, model="gemini-test"
        )
        payload = response.get_json()

    assert status == 504
    assert payload == {
        "error": "ai_timeout",
        "message": "AI 生成回复超时，请重试。",
        "model": "gemini-test",
        "provider": "gemini",
        "replies": [],
        "retryable": True,
        "status_code": 504,
        "user_id": 17,
    }


def test_all_chat_generation_paths_intercept_structured_errors():
    private_source = (ROOT / "blueprints" / "chat.py").read_text(encoding="utf-8")
    group_source = (ROOT / "blueprints" / "group.py").read_text(encoding="utf-8")

    assert private_source.count("error_resp = _ai_error_json_response(") == 3
    assert "if is_ai_error_response(reply_text):" in group_source


def test_frontend_renders_retryable_failures_as_normal_bubbles():
    template = (ROOT / "templates" / "chat.html").read_text(encoding="utf-8")

    assert "function renderGenerationErrorGroup(" in template
    assert "function retryGenerationError(" in template
    assert "retryUserMessageId" in template
    assert "user_message_id: userMessageId" in template
    assert "function legacyGenerationErrorMessage(text)" in template
    assert "const isSystemPrompt = !legacyErrorMessage" in template
