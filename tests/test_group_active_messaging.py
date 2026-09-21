from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_proactive_group_chat_sends_trigger_as_user_message():
    source = _source("app.py")
    start = source.index("def trigger_group_active_chat(")
    end = source.index("# ========================================================", start)
    handler = source[start:end]

    assert 'full_sys_prompt = sys_prompt + "\\n\\n" + rel_prompt' in handler
    assert '{"role": "system", "content": full_sys_prompt}' in handler
    assert '{"role": "user", "content": instruction.strip()}' in handler
    assert "rel_prompt + instruction" not in handler
    assert "_disable_group_active_messaging" in handler
    assert "主动消息触发熔断" in handler
    assert "message_written = True" in handler
    assert "return message_written" in handler


def test_enabled_group_with_yesterday_message_is_triggered(tmp_path, monkeypatch):
    import app
    import core.circuit_breaker as circuit_breaker
    import memory_jobs
    import services.voice_calls as voice_calls

    group_dir = tmp_path / "groups" / "stale_group"
    group_dir.mkdir(parents=True)
    with sqlite3.connect(group_dir / "chat.db") as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, timestamp TEXT)"
        )
        conn.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            ("member", "昨天的消息", "2026-08-25 12:00:00"),
        )

    triggered = []
    monkeypatch.setattr(app, "set_background_user", lambda user_id: None)
    monkeypatch.setattr(app, "clear_background_user", lambda: None)
    monkeypatch.setattr(app, "get_characters_config_for_current_user", lambda: {})
    monkeypatch.setattr(
        app,
        "get_groups_config_for_current_user",
        lambda: {"stale_group": {"active_mode": True}},
    )
    monkeypatch.setattr(app, "get_group_dir", lambda group_id: str(group_dir))
    monkeypatch.setattr(
        app,
        "trigger_group_active_chat",
        lambda group_id, user_id=None: triggered.append((group_id, user_id)) or True,
    )
    monkeypatch.setattr(circuit_breaker, "is_user_frozen", lambda user_id: False)
    monkeypatch.setattr(voice_calls, "get_live_call", lambda user_id: None)
    monkeypatch.setattr(
        voice_calls, "get_latest_ended_call", lambda user_id, char_id=None: None
    )
    monkeypatch.setattr(
        memory_jobs,
        "_beijing_now_naive",
        lambda: memory_jobs.datetime.datetime(2026, 8, 26, 12, 0),
    )
    monkeypatch.setattr(memory_jobs.random, "random", lambda: 0.999)

    memory_jobs._process_single_user_active_messaging(91)

    assert triggered == [("stale_group", 91)]


def test_new_groups_default_to_proactive_messaging_off():
    app_source = _source("app.py")
    directive_start = app_source.index("def ensure_directive_chat(")
    directive_end = app_source.index("# ---------------------- 核心：Prompt 构建系统", directive_start)
    directive_handler = app_source[directive_start:directive_end]

    group_source = _source("blueprints/group.py")
    add_start = group_source.index("def add_group():")
    add_end = group_source.index('@group_bp.route("/api/group/<group_id>/delete"', add_start)
    add_handler = group_source[add_start:add_end]

    assert '"active_mode": False' in directive_handler
    assert '"active_mode": True' not in directive_handler
    assert '"active_mode": False' in add_handler
    assert '"active_mode": True' not in add_handler
