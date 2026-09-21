import datetime

import memory_jobs
import services.voice_calls as voice_calls


def test_latest_activity_uses_call_end_and_normalizes_to_beijing():
    message_time = datetime.datetime(2026, 8, 19, 9, 30, 0)
    ended_call = {"ended_at": "2026-08-19T02:00:00+00:00"}

    assert memory_jobs._latest_activity_datetime(message_time, ended_call) == (
        datetime.datetime(2026, 8, 19, 10, 0, 0)
    )


def test_latest_activity_keeps_newer_chat_message():
    message_time = datetime.datetime(2026, 8, 19, 11, 0, 0)
    ended_call = {"ended_at": "2026-08-19T02:00:00+00:00"}

    assert memory_jobs._latest_activity_datetime(message_time, ended_call) == message_time


def test_active_message_clock_uses_naive_beijing_time():
    utc_time = datetime.datetime(2026, 8, 26, 13, 30, tzinfo=datetime.timezone.utc)

    result = memory_jobs._beijing_now_naive(utc_time)

    assert result == datetime.datetime(2026, 8, 26, 21, 30)
    assert result.tzinfo is None


def test_active_message_heartbeat_skips_everything_during_live_call(monkeypatch):
    import app
    import core.circuit_breaker as circuit_breaker

    triggered = []
    monkeypatch.setattr(app, "set_background_user", lambda user_id: None)
    monkeypatch.setattr(app, "clear_background_user", lambda: None)
    monkeypatch.setattr(app, "get_characters_config_for_current_user", lambda: {})
    monkeypatch.setattr(
        app, "get_groups_config_for_current_user",
        lambda: (_ for _ in ()).throw(AssertionError("groups must be skipped")),
    )
    monkeypatch.setattr(
        app, "trigger_active_chat", lambda *args, **kwargs: triggered.append("single")
    )
    monkeypatch.setattr(
        app, "trigger_group_active_chat", lambda *args, **kwargs: triggered.append("group")
    )
    monkeypatch.setattr(circuit_breaker, "is_user_frozen", lambda user_id: False)
    monkeypatch.setattr(
        voice_calls, "get_live_call",
        lambda user_id: {"status": "ringing", "call_id": "call_live"},
    )

    memory_jobs._process_single_user_active_messaging(91)

    assert triggered == []


def test_direct_active_message_entrypoints_recheck_live_call(monkeypatch):
    import app

    monkeypatch.setattr(app, "set_background_user", lambda user_id: None)
    monkeypatch.setattr(app, "get_current_user_id", lambda: 91)
    monkeypatch.setattr(
        voice_calls, "get_live_call",
        lambda user_id: {"status": "active", "call_id": "call_live"},
    )

    assert app.trigger_active_chat("rin", user_id=91) is False
    assert app.trigger_group_active_chat("blue-lock", user_id=91) is False
