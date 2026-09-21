from pathlib import Path

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


def test_private_and_group_short_memory_filter_system_prompts():
    private_source = (ROOT / "services/memory.py").read_text(encoding="utf-8")
    group_source = (ROOT / "blueprints/group.py").read_text(encoding="utf-8")
    assert "and not is_system_prompt_message(row[3])" in private_source
    assert "rows if not is_system_prompt_message(row[3])" in group_source


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
