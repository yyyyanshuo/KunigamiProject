import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import call_memory, memory, prompt_builder, voice_calls
from services.memory_store import load_calendar_memory, memory_file_lock
from tests.test_memory_integrity import _make_chat_db, _patch_character, _write_json


CALL_ID = "call_" + "d" * 32
OTHER_CALL_ID = "call_" + "e" * 32


@pytest.fixture
def call_env(tmp_path, monkeypatch):
    env = SimpleNamespace(
        uid=91, char_id="rin", db=tmp_path / "chat.db", prompts=tmp_path / "prompts",
        now="2026-09-29T02:00:00+00:00", zone="Asia/Shanghai", sources=[],
    )
    env.file = env.prompts / "6_memory_short.json"
    _make_chat_db(env.db, [])
    _patch_character(monkeypatch, env.db, env.prompts)
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setattr(voice_calls, "_now_iso", lambda: env.now)
    monkeypatch.setattr(call_memory, "LOCAL_TEST", True)
    monkeypatch.setattr(call_memory, "get_paths", lambda *a, **k: (str(env.db), str(env.prompts)))
    monkeypatch.setattr(memory, "memory_timezone", lambda *a: env.zone)
    monkeypatch.setattr(call_memory, "memory_timezone", lambda *a: env.zone)

    def summarize(source, *args, **kwargs):
        env.sources.append(source)
        clock = re.search(r"\[(\d{2}:\d{2})\]", source).group(1)
        return memory.MemorySummaryResult("success", parsed=[{
            "time": clock, "event": f"电话里确认了出行安排，已约定后续见面。摘要 {len(env.sources)}",
        }])

    monkeypatch.setattr(memory, "_summary_with_output_retries", summarize)
    return env


def start_call(env, *, call_id=CALL_ID, char_id=None, uid=None):
    uid = env.uid if uid is None else uid
    voice_calls.create_call(uid, call_id=call_id, char_id=char_id or env.char_id, initiator="user")
    voice_calls.accept_call(uid, call_id)


def say(env, text, role="user", *, call_id=CALL_ID, uid=None):
    return voice_calls.append_turn(env.uid if uid is None else uid, call_id, role=role, content=text)


def saved(env, day="2026-09-29"):
    return json.loads(env.file.read_text())[day]


def test_full_transcript_is_summarized_after_chat_anchor_cursor(call_env):
    env = call_env
    start_call(env)
    with sqlite3.connect(env.db) as conn:
        conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?)", (
            500, "user", voice_calls.build_voice_call_tag(CALL_ID), "2026-09-29 10:00:00",
        ))
    _write_json(env.file, {"2026-09-29": {"events": [], "last_id": 500, "timezone": env.zone}})
    for index in range(32):
        say(env, f"独立标记{index:02d}：" + "完整台词里的具体计划。" * 20,
            "user" if index % 2 == 0 else "assistant")
    voice_calls.end_call(env.uid, CALL_ID, reason="hangup")

    result = memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid)
    assert result.ok
    source = "\n".join(env.sources)
    assert len(source) > 4000
    assert all(f"独立标记{index:02d}" in source for index in range(32))
    assert "[通话] 樱:" in source and "[通话] 凛:" in source
    assert "[voice_call]" not in source
    assert saved(env)["last_id"] == 500
    assert saved(env)["call_last_id"] == 32
    count = len(env.sources)
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).status == "up_to_date"
    assert len(env.sources) == count


def test_active_call_new_turns_are_not_skipped_and_rebuild_preserves_external_events(call_env):
    env = call_env
    start_call(env)
    say(env, "通话第一件事")
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).ok
    env.now = "2026-09-29T02:01:00+00:00"
    say(env, "通话后来新增的事", "assistant")
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).ok
    assert "通话第一件事" not in env.sources[-1]
    assert "通话后来新增的事" in env.sources[-1]
    assert saved(env)["call_last_id"] == 2
    data = json.loads(env.file.read_text())
    data["2026-09-29"]["events"].append({"time": "09:00", "event": "[朋友圈] 外部记忆"})
    _write_json(env.file, data)
    result = memory.update_short_memory_for_date(env.char_id, "2026-09-29", force_reset=True, user_id=env.uid)
    assert result.ok
    assert "通话第一件事" in env.sources[-1] and "通话后来新增的事" in env.sources[-1]
    assert len(saved(env)["events"]) == 2
    assert any(e["event"] == "[朋友圈] 外部记忆" for e in saved(env)["events"])


def test_chat_and_call_ids_do_not_collide_and_sources_are_chronological(call_env):
    env = call_env
    with sqlite3.connect(env.db) as conn:
        conn.execute("INSERT INTO messages VALUES (1, 'user', '先在单聊确定日期', '2026-09-29 09:59:00')")
    start_call(env)
    say(env, "随后电话里确定地点")
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).ok
    source = env.sources[0]
    assert source.index("先在单聊确定日期") < source.index("随后电话里确定地点")
    assert saved(env)["last_id"] == saved(env)["call_last_id"] == 1


def test_call_without_chat_database_is_remembered(call_env):
    env = call_env
    env.db.unlink()
    start_call(env)
    say(env, "独立保存的通话")
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).ok
    assert "独立保存的通话" in env.sources[0]
    assert not env.db.exists()


def test_failed_batch_does_not_commit_either_cursor_and_can_retry(call_env, monkeypatch):
    env = call_env
    start_call(env)
    say(env, "上午的通话内容")
    env.now = "2026-09-29T05:00:00+00:00"
    say(env, "下午的通话内容")
    _write_json(env.file, {"2026-09-29": {
        "events": [{"time": "09:00", "event": "已有记忆"}], "last_id": 17, "timezone": env.zone,
    }})
    before = env.file.read_bytes()
    original = memory._summary_with_output_retries
    monkeypatch.setattr(memory, "SHORT_MEMORY_BATCH_MESSAGES", 1)
    outcomes = iter([memory.MemorySummaryResult("success", parsed=[{"time": "10:00", "event": "新摘要"}]),
                     memory.MemorySummaryResult("api_error", message="temporary failure")])
    monkeypatch.setattr(memory, "_summary_with_output_retries", lambda *a, **k: next(outcomes))
    result = memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid)
    assert not result.ok
    assert env.file.read_bytes() == before
    monkeypatch.setattr(memory, "_summary_with_output_retries", original)
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).ok
    assert saved(env)["last_id"] == 17 and saved(env)["call_last_id"] == 2
    assert len(saved(env)["events"]) == 3


def test_concurrent_call_summary_cannot_duplicate_events(call_env, monkeypatch):
    env = call_env
    start_call(env)
    say(env, "同一通电话")
    original = memory._summary_with_output_retries

    def concurrent_summary(*args, **kwargs):
        _write_json(env.file, {"2026-09-29": {
            "events": [{"time": "10:00", "event": "另一任务已提交"}],
            "last_id": 0, "call_last_id": 1, "timezone": env.zone,
        }})
        return original(*args, **kwargs)

    monkeypatch.setattr(memory, "_summary_with_output_retries", concurrent_summary)
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).status == "busy"
    assert [e["event"] for e in saved(env)["events"]] == ["另一任务已提交"]


def test_catchup_crosses_account_midnight_and_timezone_changes_without_duplicates(call_env):
    env = call_env
    env.zone = "Asia/Tokyo"
    env.now = "2026-09-28T14:59:00+00:00"
    start_call(env)
    say(env, "午夜前的约定")
    env.now = "2026-09-28T15:01:00+00:00"
    say(env, "午夜后的补充", "assistant")
    # Active calls aren't finalized by the hangup catch-up worker.
    assert call_memory.sync_pending_call_memories(env.uid, env.char_id) == {}
    voice_calls.end_call(env.uid, CALL_ID, reason="disconnected")
    results = call_memory.sync_pending_call_memories(env.uid, env.char_id)
    assert set(results) == {"2026-09-28", "2026-09-29"}
    assert all(result.ok for result in results.values())
    assert "[23:59]" in env.sources[0] and "午夜后的补充" not in env.sources[0]
    assert "[00:01]" in env.sources[1] and "午夜前的约定" not in env.sources[1]
    assert call_memory.sync_pending_call_memories(env.uid, env.char_id) == {}
    env.zone = "America/Los_Angeles"
    with memory_file_lock(str(env.file)):
        load_calendar_memory(str(env.file), env.zone)
    assert call_memory.sync_pending_call_memories(env.uid, env.char_id) == {}
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-28", user_id=env.uid).status == "up_to_date"
    assert len(env.sources) == 2


def test_memory_reads_are_scoped_to_user_and_character(call_env):
    env = call_env
    start_call(env, uid=92)
    say(env, "其他用户的话", uid=92)
    start_call(env, char_id="isagi", call_id=OTHER_CALL_ID)
    say(env, "其他角色的话", call_id=OTHER_CALL_ID)
    voice_calls.end_call(env.uid, OTHER_CALL_ID, reason="hangup")
    start_call(env)
    say(env, "目标角色的对话")
    assert memory.update_short_memory_for_date(env.char_id, "2026-09-29", user_id=env.uid).ok
    assert "目标角色的对话" in env.sources[0]
    assert "其他用户的话" not in env.sources[0] and "其他角色的话" not in env.sources[0]


@pytest.mark.parametrize("initiator", ["user", "assistant"])
@pytest.mark.parametrize("end_reason", ["hangup", "character_hangup"])
def test_call_lifecycle_never_schedules_immediate_memory(call_env, monkeypatch, initiator, end_reason):
    env = call_env
    queued = []
    monkeypatch.setattr(call_memory, "queue_call_memory", lambda uid, cid: queued.append((uid, cid)))
    voice_calls.create_call(env.uid, call_id=CALL_ID, char_id=env.char_id, initiator=initiator)
    assert queued == []
    voice_calls.accept_call(env.uid, CALL_ID)
    say(env, "通话内容仍然保留")
    assert queued == []
    ended = voice_calls.end_call(env.uid, CALL_ID, reason=end_reason)
    assert ended["status"] == "ended"
    voice_calls.end_call(env.uid, CALL_ID, reason=end_reason)
    assert queued == []
    assert voice_calls.list_turns(env.uid, CALL_ID)[0]["content"] == "通话内容仍然保留"
    assert env.sources == []
    # The regular catch-up still includes saved calls when explicitly run later.
    results = call_memory.sync_pending_call_memories(env.uid, env.char_id)
    assert all(result.ok for result in results.values())
    assert "通话内容仍然保留" in "\n".join(env.sources)


def test_opening_or_polling_calls_cleans_up_disconnect_without_memory_job(call_env, monkeypatch):
    env = call_env
    queued = []
    monkeypatch.setattr(call_memory, "queue_call_memory", lambda uid, cid: queued.append((uid, cid)))
    start_call(env)
    say(env, "断线前的内容")
    with sqlite3.connect(voice_calls._db_path(env.uid)) as conn:
        conn.execute("UPDATE voice_calls SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE call_id=?", (CALL_ID,))
    # Both incoming-call creation and outgoing-call creation use get_live_call.
    assert voice_calls.get_live_call(env.uid) is None
    assert voice_calls.get_call(env.uid, CALL_ID)["end_reason"] == "disconnected"
    assert voice_calls.expire_stale_active_calls(env.uid) == []
    start_call(env, call_id=OTHER_CALL_ID)
    assert voice_calls.get_call(env.uid, OTHER_CALL_ID)["status"] == "active"
    assert queued == [] and env.sources == []
    assert voice_calls.list_turns(env.uid, CALL_ID)[0]["content"] == "断线前的内容"


def test_chat_window_expands_calls_and_excludes_only_current_call(call_env, monkeypatch):
    from core import utils
    env = call_env
    start_call(env, call_id=OTHER_CALL_ID)
    say(env, "上次通话的台词", call_id=OTHER_CALL_ID)
    voice_calls.end_call(env.uid, OTHER_CALL_ID, reason="hangup")
    start_call(env)
    say(env, "本次通话的台词")
    with sqlite3.connect(env.db) as conn:
        conn.executemany("INSERT INTO messages VALUES (?, 'user', ?, '2026-09-29 10:00:00')", [
            (1, voice_calls.build_voice_call_tag(OTHER_CALL_ID)),
            (2, "普通聊天内容"), (3, voice_calls.build_voice_call_tag(CALL_ID)),
        ])
    monkeypatch.setattr(utils, "get_paths", lambda *a, **k: (str(env.db), str(env.prompts)))
    monkeypatch.setattr(prompt_builder, "_get_character_time_info", lambda *a, **k: (None, env.zone, None))
    normal = prompt_builder.extract_recent_messages_with_labels(env.char_id, user_id=env.uid)
    during = prompt_builder.extract_recent_messages_with_labels(env.char_id, user_id=env.uid, exclude_call_id=CALL_ID)
    assert "本次通话的台词" in "\n".join(item[1] for item in normal)
    during_text = "\n".join(item[1] for item in during)
    assert "本次通话的台词" not in during_text
    assert "上次通话的台词" in during_text and "普通聊天内容" in during_text
    # Keep the existing 20-row window: don't pull an old call back in separately.
    with sqlite3.connect(env.db) as conn:
        conn.executemany("INSERT INTO messages VALUES (?, 'user', '后续文字消息', '2026-09-29 11:00:00')",
                         [(i,) for i in range(4, 24)])
    assert len(prompt_builder.extract_recent_messages_with_labels(env.char_id, user_id=env.uid)) == 20
    assert "台词" not in "\n".join(item[1] for item in prompt_builder.extract_recent_messages_with_labels(env.char_id, user_id=env.uid))


def test_call_request_injects_turns_once_and_keeps_long_memory_inputs(call_env, monkeypatch):
    import app as app_module
    from blueprints import calls
    from core import utils

    env = call_env
    start_call(env)
    say(env, "电话里的上一句话")
    tag = voice_calls.build_voice_call_tag(CALL_ID)
    with sqlite3.connect(env.db) as conn:
        conn.execute("INSERT INTO messages VALUES (1, 'user', ?, '2026-09-29 10:00:00')", (tag,))
    paths = lambda *a, **k: (str(env.db), str(env.prompts))
    monkeypatch.setattr(utils, "get_paths", paths)
    monkeypatch.setattr(prompt_builder, "get_paths", paths)
    monkeypatch.setattr(prompt_builder, "_get_character_time_info", lambda *a, **k: (None, env.zone, datetime.fromisoformat(env.now)))
    monkeypatch.setattr(prompt_builder, "get_char_name", lambda *a, **k: "凛")
    monkeypatch.setattr(prompt_builder, "extract_medium_memory_with_timeline_ts", lambda *a, **k: [])
    monkeypatch.setattr(prompt_builder, "extract_short_memory_with_timeline_ts", lambda *a, **k: [])
    retrieval = {}

    def long_memory(*args, **kwargs):
        retrieval.update(kwargs)
        return []

    monkeypatch.setattr(prompt_builder, "extract_long_memory_with_timeline_ts", long_memory)
    monkeypatch.setattr(calls, "build_system_prompt_v2", lambda *a, **k: prompt_builder.build_system_prompt_v2(*a, read_only=True, **k))
    monkeypatch.setattr(calls, "_current_uid", lambda: env.uid)
    monkeypatch.setattr(calls, "_recent_chat_text", lambda *a, **k: [tag])
    monkeypatch.setattr(calls, "_parse_call_reply", lambda *a, **k: {"text": "收到", "tone": "", "controls": []})
    requests = []

    def model(uid, char_id, messages):
        requests.append(messages)
        return "收到", "relay", "fake-model"

    monkeypatch.setattr(calls, "_run_call_model", model)
    with app_module.app.test_request_context(
        json={"text": "本轮新说的话", "client_turn_id": "client-turn-123"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ):
        response = calls.create_call_turn(CALL_ID)
    assert response.status_code == 200
    messages = requests[0]
    assert "电话里的上一句话" not in messages[0]["content"]
    assert "本轮新说的话" not in messages[0]["content"]
    assert [m["content"] for m in messages[1:]] == ["电话里的上一句话", "本轮新说的话"]
    assert retrieval["recent_messages"] == [tag]
    assert retrieval["user_latest_input"] == "本轮新说的话"


@pytest.mark.parametrize("reset,clear", [(False, False), (True, False), (False, True)])
def test_memory_editor_preserves_or_explicitly_resets_call_progress(call_env, monkeypatch, reset, clear):
    import app as app_module
    from blueprints import chat
    env = call_env
    coverage = [{"start": "2026-09-29 00:00:00", "end": "2026-09-30 00:00:00", "last_id": 7}]
    _write_json(env.file, {"2026-09-29": {
        "events": [{"time": "10:00", "event": "旧摘要"}], "timezone": env.zone,
        "last_id": 0, "call_last_id": 9, "call_coverage": coverage,
    }})
    monkeypatch.setattr(chat, "get_paths", lambda *a, **k: (str(env.db), str(env.prompts)))
    monkeypatch.setattr(chat, "memory_timezone", lambda *a: env.zone)
    with app_module.app.test_request_context(json={"key": "short", "content": {"2026-09-29": {
        "events": [] if clear else [{"time": "10:00", "event": "手动修正后的摘要"}],
        "last_id": 0, "reset_call_progress": reset,
    }}}):
        response = chat.save_prompt_file(env.char_id)
    assert response.status_code == 200
    assert saved(env)["call_last_id"] == (0 if reset or clear else 9)
    assert saved(env)["call_coverage"] == ([] if reset or clear else coverage)
    assert "reset_call_progress" not in saved(env)


def test_queue_deduplicates_jobs_and_releases_background_context(call_env, monkeypatch):
    from core.context import get_current_user_id
    env = call_env
    submitted = []
    synced = []
    monkeypatch.setattr(call_memory, "LOCAL_TEST", False)
    monkeypatch.setattr(call_memory, "_executor", SimpleNamespace(submit=lambda *args: submitted.append(args)))
    monkeypatch.setattr(call_memory, "sync_pending_call_memories", lambda uid, cid: synced.append((get_current_user_id(), cid)))
    call_memory.queue_call_memory(env.uid, env.char_id)
    call_memory.queue_call_memory(env.uid, env.char_id)
    assert len(submitted) == 1
    worker, *args = submitted.pop()
    worker(*args)
    assert synced == [(env.uid, env.char_id)]
    assert get_current_user_id() is None
    call_memory.queue_call_memory(env.uid, env.char_id)
    assert len(submitted) == 1
    worker, *args = submitted.pop()
    worker(*args)


def test_no_call_data_or_unanswered_calls_do_not_generate_memories(call_env):
    env = call_env
    assert voice_calls.list_memory_turns(env.uid, env.char_id) == []
    assert not Path(voice_calls._db_path(env.uid)).exists()
    voice_calls.create_call(env.uid, call_id=CALL_ID, char_id=env.char_id, initiator="user")
    voice_calls.end_call(env.uid, CALL_ID, reason="canceled")
    assert call_memory.sync_pending_call_memories(env.uid, env.char_id) == {}
    assert env.sources == []
