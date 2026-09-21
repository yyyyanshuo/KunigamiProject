import json
from pathlib import Path

import pytest
from flask import Flask

import blueprints.chat as chat
from services.persona_locks import (
    PERSONA_LOCK_MODEL_INSTRUCTION,
    PersonaLockError,
    ensure_model_preserved_locks,
    parse_persona_segments,
)


ROOT = Path(__file__).resolve().parents[1]


def test_persona_lock_parser_supports_multiple_blocks():
    text = "前文\n[[LOCK]]\n核心一\n[[/LOCK]]\n中间\n[[LOCK]]\n核心二\n[[/LOCK]]\n"
    segments = parse_persona_segments(text)

    assert [segment.locked for segment in segments] == [False, True, False, True]
    assert [segment.text.strip() for segment in segments if segment.locked] == ["核心一", "核心二"]


@pytest.mark.parametrize(
    "text",
    [
        "[[LOCK]]\n没有结束",
        "[[/LOCK]]\n没有开始",
        "[[LOCK]]\n[[LOCK]]\n嵌套\n[[/LOCK]]\n[[/LOCK]]",
        "[[LOCK]]\n[[/LOCK]]",
    ],
)
def test_persona_lock_parser_rejects_invalid_markup(text):
    with pytest.raises(PersonaLockError):
        parse_persona_segments(text)


def test_ai_edit_must_preserve_locks_but_user_save_may_change_them(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    persona_path = prompts_dir / "1_base_persona.json"
    persona_path.write_text(
        json.dumps({"system_prompt": "[[LOCK]]\n旧核心\n[[/LOCK]]"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(chat, "get_paths", lambda *_args, **_kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)))

    app = Flask(__name__)
    with app.test_request_context(json={
        "key": "base",
        "content": {
            "system_prompt": "[[LOCK]]\n用户改后的核心\n[[/LOCK]]",
            "visual_descriptions": {"tags": "legacy"},
            "custom_settings": {"reply_style": "legacy"},
        },
    }):
        response = chat.save_prompt_file("hero")

    assert response.get_json()["status"] == "success"
    saved = json.loads(persona_path.read_text(encoding="utf-8"))
    assert set(saved) == {"system_prompt"}
    assert "用户改后的核心" in saved["system_prompt"]

    # LOCK is an instruction to future AI edits, never an editor permission boundary.
    with app.test_request_context(json={
        "key": "base",
        "content": {"system_prompt": "用户完全重写的人设，已经自行移除标签。"},
    }):
        unlocked_response = chat.save_prompt_file("hero")

    assert unlocked_response.get_json()["status"] == "success"
    unlocked_saved = json.loads(persona_path.read_text(encoding="utf-8"))
    assert unlocked_saved["system_prompt"] == "用户完全重写的人设，已经自行移除标签。"

    with pytest.raises(PersonaLockError):
        ensure_model_preserved_locks(
            "[[LOCK]]\n用户改后的核心\n[[/LOCK]]",
            "[[LOCK]]\n模型擅自修改\n[[/LOCK]]",
        )


def test_model_instruction_knows_locks_and_user_edit_boundary():
    assert "必须读取" in PERSONA_LOCK_MODEL_INSTRUCTION
    assert "逐字保留" in PERSONA_LOCK_MODEL_INSTRUCTION
    assert "用户本人仍可" in PERSONA_LOCK_MODEL_INSTRUCTION


def test_memory_template_recognizes_and_explains_persona_locks():
    source = (ROOT / "templates" / "memory.html").read_text(encoding="utf-8")

    assert "persona-lock-card" in source
    assert "parsePersonaLockSegments" in source
    assert "AI 编辑保护" in source
    assert "这不会限制你本人" in source
    assert "编辑框仍是普通文本" in source
    assert "保存后以你的新版本为准" in source

    square_source = (ROOT / "templates" / "square" / "upload.html").read_text(encoding="utf-8")
    assert "existing_persona: document.getElementById('base-persona').value" in square_source
