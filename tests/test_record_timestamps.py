from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_chat_v2_does_not_persist_character_local_time():
    source = _source("blueprints/chat.py")
    start = source.index("def chat_v2(char_id):")
    end = source.index("def regenerate_message(char_id):", start)
    handler = source[start:end]

    assert "character_now = _character_now(char_id)" in handler
    assert "ai_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')" in handler
    assert "ai_ts = (now +" not in handler
    assert "character_now.strftime('%Y-%m-%d %H:%M:%S')" not in handler


def test_chat_and_moments_templates_display_record_time_in_beijing():
    for template in ("templates/chat.html", "templates/moments.html"):
        source = _source(template)
        assert "const RECORD_TIMEZONE = 'Asia/Shanghai';" in source
        assert "timeZone: RECORD_TIMEZONE" in source
        assert "USER_TIMEZONE" not in source
        assert ".getHours()" not in source
        assert ".getFullYear()" not in source


def test_all_active_message_writers_use_explicit_beijing_clock():
    expected_markers = {
        "blueprints/chat.py": ["ai_ts = beijing_now()", "user_ts = beijing_now()"],
        "blueprints/group.py": ["ai_ts = beijing_now()", "user_ts = beijing_now()"],
        "blueprints/moments.py": ["record_ts = beijing_now()"],
        "blueprints/media.py": ["now_ts = beijing_now()", "now = beijing_now()"],
    }
    for path, markers in expected_markers.items():
        source = _source(path)
        assert "INSERT INTO messages" in source
        for marker in markers:
            assert marker in source


def test_proactive_single_chat_does_not_persist_character_local_time():
    source = _source("app.py")
    start = source.index("def trigger_active_chat(")
    end = source.index("def trigger_bedtime_diary(", start)
    handler = source[start:end]

    assert "now = get_character_local_now(char_id" in handler
    assert "ai_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')" in handler
    assert "ai_ts = now.strftime('%Y-%m-%d %H:%M:%S')" not in handler
