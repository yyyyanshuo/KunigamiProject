from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_character_time_section_only_injects_character_local_time():
    source = _source("services/prompt_builder.py")
    section_start = source.index("time_info = (")
    section_end = source.index("prompt_parts.append(f\"【現在時刻】", section_start)
    time_section = source[section_start:section_end]

    assert "角色当前时区" in time_section
    assert "上述当前时间仅代表你所在地域的当地时间" in time_section
    assert "并不代表用户所在地域的时间" in time_section
    assert "用户当地时间" not in time_section
    assert "系统北京时间" not in time_section


def test_single_and_group_proactive_messages_add_user_time_system_reminder():
    source = _source("app.py")
    assert "请主动根据你的时间和位置以及用户位置推算用户当前时间" in source
    assert "用户长时间不回消息可能是在睡觉" in source

    single_start = source.index("def trigger_active_chat(")
    single_end = source.index("def trigger_bedtime_diary(", single_start)
    single_handler = source[single_start:single_end]
    assert (
        '{"role": "system", "content": PROACTIVE_USER_TIME_SYSTEM_MESSAGE}'
        in single_handler
    )

    group_start = source.index("def trigger_group_active_chat(")
    group_handler = source[group_start:]
    assert (
        '{"role": "system", "content": PROACTIVE_USER_TIME_SYSTEM_MESSAGE}'
        in group_handler
    )
