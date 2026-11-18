from pathlib import Path
import json

import pytest

from core.system_messages import (
    is_system_prompt_message,
    should_suppress_reply_for_deep_sleep,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "message",
    [
        "（系统提示：请立即回复。）",
        "  （系统提示：允许包含\n多行内容。）  ",
    ],
)
def test_strict_chinese_system_prompt_format_is_recognized(message):
    assert is_system_prompt_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "",
        "（系统提示：）",
        "(系统提示：内容)",
        "（系统提示:内容）",
        "普通文字（系统提示：内容）",
        "（系统提示：内容）普通文字",
    ],
)
def test_noncanonical_system_prompt_formats_are_rejected(message):
    assert not is_system_prompt_message(message)


def test_system_prompt_bypasses_only_the_current_deep_sleep_reply_gate():
    assert should_suppress_reply_for_deep_sleep(True, "普通消息")
    assert not should_suppress_reply_for_deep_sleep(True, "（系统提示：请回复。）")
    assert not should_suppress_reply_for_deep_sleep(False, "普通消息")


def test_both_chat_routes_use_the_shared_deep_sleep_gate():
    source = (ROOT / "blueprints/chat.py").read_text(encoding="utf-8")
    assert source.count("should_suppress_reply_for_deep_sleep(is_deep_sleep, user_msg_raw)") == 2


@pytest.mark.parametrize("scope", ["private", "group"])
@pytest.mark.parametrize("force_reset", [False, True])
@pytest.mark.parametrize(
    "role, message, speaker",
    [
        ("user", "（系统提示：约定地点改为图书馆。）", "System"),
        ("user", "(System notice: Meet at the library instead.)", "System"),
        ("user", "（システム通知：待ち合わせ場所は図書館に変更。）", "System"),
        ("system", "约定地点改为图书馆。", "System"),
        ("assistant", "[THOUGHTS]我决定下次主动道歉。[/THOUGHTS]", "凛"),
        ("assistant", "明天见。[THOUGHTS]我期待明天见面。[/THOUGHTS]", "凛"),
    ],
)
def test_short_memory_remembers_system_notices_and_thoughts(
    tmp_path, monkeypatch, scope, force_reset, role, message, speaker
):
    from blueprints import group as group_blueprint
    from services import memory
    from tests.test_memory_integrity import _make_chat_db, _patch_character, _write_json

    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    stored_role = "rin" if scope == "group" and role == "assistant" else role
    _make_chat_db(db_path, [(1, stored_role, message, "2026-08-07 09:00:00")])
    _patch_character(monkeypatch, db_path, prompts_dir)
    monkeypatch.setattr(memory, "memory_timezone", lambda *_args: "Asia/Shanghai")
    monkeypatch.setattr(memory, "list_memory_turns", lambda *_args, **_kwargs: [])

    if scope == "private":
        short_file = prompts_dir / "6_memory_short.json"
        summarize = lambda: memory.update_short_memory_for_date(
            "rin", "2026-08-07", force_reset=force_reset
        )
    else:
        short_file = tmp_path / "memory_short.json"
        groups_file, chars_file = tmp_path / "groups.json", tmp_path / "characters.json"
        _write_json(groups_file, {"g1": {"name": "测试群", "members": ["rin"]}})
        _write_json(chars_file, {"rin": {"name": "凛"}})
        monkeypatch.setattr(group_blueprint, "get_group_dir", lambda _group_id: str(tmp_path))
        monkeypatch.setattr(group_blueprint, "_get_groups_config_file", lambda: str(groups_file))
        monkeypatch.setattr(group_blueprint, "_get_characters_config_file", lambda: str(chars_file))
        monkeypatch.setattr(group_blueprint, "memory_timezone", lambda: "Asia/Shanghai")
        monkeypatch.setattr(group_blueprint, "group_language", lambda _group_id: "zh")
        monkeypatch.setattr(group_blueprint, "distribute_group_memory", lambda *_args, **_kwargs: None)
        summarize = lambda: group_blueprint.update_group_short_memory(
            "g1", "2026-08-07", force_reset=force_reset
        )

    # A rebuild must also revisit rows that the previous policy already skipped.
    _write_json(short_file, {"2026-08-07": {
        "events": [], "last_id": 1 if force_reset else 0, "timezone": "Asia/Shanghai",
    }})
    sources = []
    event = {"time": "09:00", "event": "保留系统背景或角色明确的内心决定。"}

    def fake_summary(source, *_args, **_kwargs):
        sources.append(source)
        assert f"[09:00] {speaker}: {message}" in source
        return json.dumps({"events": [event]}, ensure_ascii=False)

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)
    count, events = summarize()

    assert count == 1
    assert events[0]["event"] == event["event"]
    assert len(sources) == 1
    saved = json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]
    assert saved["last_id"] == 1
    assert saved["events"][0]["event"] == event["event"]
    if not force_reset:
        count, _events = summarize()
        assert count == 0
        assert len(sources) == 1


def test_chat_template_marks_and_styles_system_prompt_messages():
    template = (ROOT / "templates/chat.html").read_text(encoding="utf-8")
    assert "function isSystemPromptMessage(text)" in template
    assert "group.classList.add('system-prompt')" in template
    assert ".message-group.system-prompt .bubble" in template


def test_compact_event_messages_use_click_action_menu():
    template = (ROOT / "templates/chat.html").read_text(encoding="utf-8")
    assert "function bindCompactMessageActions(anchor, options)" in template
    assert "function openCompactMessageActionMenu(anchor, { id, rawText, group })" in template
    assert "group.querySelectorAll('.tickle-row .compact-event-label, .recall-placeholder-row .compact-event-label')" in template
    assert "if (isSystemPrompt)" in template
    assert "const shouldAttachControls = attachControls && !isSystemPrompt" in template
    assert "addAction('编辑'" in template
    assert "addAction('复制'" in template
    assert "addAction('删除'" in template


def test_system_prompt_renders_as_one_literal_full_width_block():
    template = (ROOT / "templates/chat.html").read_text(encoding="utf-8")
    assert "function renderSystemPromptGroup({ id, sourceText, role, timestamp }, mode)" in template
    assert "bubble.textContent = String(sourceText).trim()" in template
    assert "return renderSystemPromptGroup({ id, sourceText, role, timestamp }, mode)" in template
    assert ".message-group.system-prompt .bubble" in template
    assert "border-radius: 12px !important" in template
