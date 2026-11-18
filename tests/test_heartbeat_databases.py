import datetime
import sqlite3

import pytest

import memory_jobs


@pytest.mark.parametrize("broken_kind", ["missing", "no_table", "corrupt"])
def test_bad_chat_database_does_not_block_later_characters_or_groups(
    tmp_path, monkeypatch, capsys, broken_kind
):
    import app
    import core.circuit_breaker as circuit_breaker
    import services.voice_calls as voice_calls

    paths = {}
    for name in ("bad_char", "good_char", "bad_group", "good_group"):
        path = tmp_path / name / "chat.db"
        path.parent.mkdir()
        paths[name] = path
        if name.startswith("bad"):
            if broken_kind == "no_table":
                with sqlite3.connect(path) as conn:
                    conn.execute("CREATE TABLE preserved (value TEXT)")
                    conn.execute("INSERT INTO preserved VALUES ('keep me')")
            elif broken_kind == "corrupt":
                path.write_bytes(b"invalid sqlite database")
            continue
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, timestamp TEXT)"
            )
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?)",
                [
                    (1, "member" if name.endswith("group") else "assistant", "2026-09-24 12:00:00"),
                    (2, "user", "2026-09-25 11:59:00"),
                    (3, "system", "2026-09-25 11:59:00"),
                ],
            )

    before = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    triggered = []
    cleared = []
    monkeypatch.setattr(app, "set_background_user", lambda uid: None)
    monkeypatch.setattr(app, "clear_background_user", lambda: cleared.append(True))
    monkeypatch.setattr(app, "get_characters_config_for_current_user", lambda: {
        "bad_char": {}, "good_char": {},
    })
    monkeypatch.setattr(app, "get_groups_config_for_current_user", lambda: {
        "bad_group": {"active_mode": True}, "good_group": {"active_mode": True},
    })
    monkeypatch.setattr(app, "get_paths", lambda cid: (str(paths[cid]), None))
    monkeypatch.setattr(app, "get_group_dir", lambda gid: str(paths[gid].parent))
    monkeypatch.setattr(app, "trigger_active_chat", lambda cid, user_id: triggered.append((cid, user_id)))
    monkeypatch.setattr(app, "trigger_group_active_chat", lambda gid, user_id: triggered.append((gid, user_id)) or True)
    monkeypatch.setattr(circuit_breaker, "is_user_frozen", lambda uid: False)
    monkeypatch.setattr(voice_calls, "get_live_call", lambda uid: None)
    monkeypatch.setattr(voice_calls, "get_latest_ended_call", lambda uid, char_id=None: None)
    monkeypatch.setattr(memory_jobs, "_beijing_now_naive", lambda: datetime.datetime(2026, 9, 25, 12))
    monkeypatch.setattr(memory_jobs.random, "random", lambda: 0.5)

    memory_jobs._process_single_user_active_messaging(734)

    assert triggered == [("good_char", 734), ("good_group", 734)]
    assert cleared == [True]
    assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == before
    output = capsys.readouterr().out
    assert "心跳检测出错" not in output
    if broken_kind != "missing":
        assert "用户 734 角色 bad_char" in output
        assert "用户 734 群聊 bad_group" in output
    if broken_kind == "no_table":
        assert "缺少 messages 表" in output


def test_empty_messages_table_has_no_proactive_message(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, timestamp TEXT)")
    assert memory_jobs._heartbeat_last_message(str(path), 734, "new_char") is None
    assert memory_jobs._heartbeat_last_message(str(path), 734, "new_group", group=True) is None
