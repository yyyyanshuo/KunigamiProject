import sqlite3

import pytest

import services.prompt_builder as prompt_builder
import core.utils as core_utils

from services.bedtime_diary import (
    BedtimeDiaryOutputError,
    build_bedtime_diary_trigger,
    parse_bedtime_diary_output,
)


def test_bedtime_trigger_reviews_all_domains_without_general_actions():
    prompt = build_bedtime_diary_trigger("2026-08-10", "23:40", "zh")

    assert "完整关系图谱" in prompt
    assert "当前人设" in prompt
    assert "全部计划" in prompt
    assert "System Prompt 中已有的编辑指令" in prompt
    assert "MAINTENANCE_REVIEW" in prompt
    assert "图片、语音、表情、对话转向" in prompt
    assert "ADD_PERSONA" not in prompt
    assert "DIRECT_TO_GROUP" not in prompt


def test_bedtime_output_all_keep_is_valid_and_only_returns_thoughts():
    parsed = parse_bedtime_diary_output(
        """[THOUGHTS]
今天终于把那件事想明白了。
[/THOUGHTS]

[MAINTENANCE_REVIEW: {"persona":"keep","relation":"keep","plan":"keep"}]"""
    )

    assert parsed.thoughts == "[THOUGHTS]\n今天终于把那件事想明白了。\n[/THOUGHTS]"
    assert parsed.review == {"persona": "keep", "relation": "keep", "plan": "keep"}
    assert parsed.actions == ()


def test_bedtime_output_accepts_matching_domain_actions():
    parsed = parse_bedtime_diary_output(
        """[THOUGHTS]
明天还要继续。
[/THOUGHTS]
[MAINTENANCE_REVIEW: {"persona":"change","relation":"keep","plan":"change"}]
[ADD_PERSONA: {"content":"开始更坦率地面对自己的需要"}]
[EDIT_PLAN: {"from":{"content":"以后去海边"},"set":{"date":"2026-08-20"}}]"""
    )

    assert [action.name for action in parsed.actions] == ["ADD_PERSONA", "EDIT_PLAN"]


@pytest.mark.parametrize(
    "output, message",
    [
        (
            """[THOUGHTS]日记[/THOUGHTS]
[MAINTENANCE_REVIEW: {"persona":"change","relation":"keep","plan":"keep"}]""",
            "persona=change",
        ),
        (
            """[THOUGHTS]日记[/THOUGHTS]
[MAINTENANCE_REVIEW: {"persona":"keep","relation":"keep","plan":"keep"}]
[ADD_PLAN: {"content":"以后去海边"}]""",
            "plan=keep",
        ),
        (
            """[THOUGHTS]日记 [ADD_PERSONA: {"content":"错误"}][/THOUGHTS]
[MAINTENANCE_REVIEW: {"persona":"change","relation":"keep","plan":"keep"}]
[ADD_PERSONA: {"content":"正确"}]""",
            "THOUGHTS 内",
        ),
    ],
)
def test_bedtime_output_rejects_inconsistent_or_unsafe_content(output, message):
    with pytest.raises(BedtimeDiaryOutputError, match=message):
        parse_bedtime_diary_output(output)


def test_bedtime_timeline_excludes_older_thoughts(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)"
    )
    conn.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("assistant", "[THOUGHTS]\n旧日记\n[/THOUGHTS]", "2026-08-09 23:00:00"),
    )
    conn.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("user", "今天发生的新事情", "2026-08-10 22:00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        core_utils,
        "get_paths",
        lambda *args, **kwargs: (str(db_path), str(tmp_path / "prompts")),
    )
    monkeypatch.setattr(
        prompt_builder,
        "_get_character_time_info",
        lambda *args, **kwargs: (None, "Asia/Shanghai", None),
    )
    monkeypatch.setattr(prompt_builder, "voice_message_for_ai", lambda value: value)
    monkeypatch.setattr(prompt_builder, "voice_call_for_ai", lambda *args: args[-1])

    events = prompt_builder.extract_recent_messages_with_labels(
        "hero",
        user_id="12",
        exclude_bedtime_diaries=True,
    )

    assert len(events) == 1
    assert "今天发生的新事情" in events[0][1]
    assert "旧日记" not in events[0][1]
