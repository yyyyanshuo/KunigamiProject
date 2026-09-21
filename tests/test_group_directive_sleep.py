import json
import sqlite3
from datetime import datetime

import pytest

import app as app_module
from core.utils import (
    is_character_available_for_chat,
    is_character_available_for_group_chat,
)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _prepare_directive_runtime(tmp_path, monkeypatch, *, group_mode, initiator_info, target_info):
    group_id = "group_ab"
    group_dir = tmp_path / "groups" / group_id
    group_dir.mkdir(parents=True)
    db_path = group_dir / "chat.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "role TEXT NOT NULL, content TEXT NOT NULL, timestamp DATETIME)"
        )

    groups_file = tmp_path / "groups.json"
    _write_json(groups_file, {
        group_id: {
            "name": "AB群",
            "members": ["initiator", "target"],
            "include_user": False,
            "group_chat_mode": group_mode,
        }
    })
    characters = {
        "initiator": {"name": "发起者", **initiator_info},
        "target": {"name": "被拉入者", **target_info},
    }
    model_calls = []

    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(app_module, "get_char_name", lambda cid: characters.get(cid, {}).get("name", cid))
    monkeypatch.setattr(app_module, "get_characters_config_for_current_user", lambda: characters)
    monkeypatch.setattr(app_module, "ensure_directive_chat", lambda directive, char_id: group_id)
    monkeypatch.setattr(app_module, "_get_groups_config_file", lambda: str(groups_file))
    monkeypatch.setattr(app_module, "get_group_dir", lambda gid: str(tmp_path / "groups" / gid))
    monkeypatch.setattr(app_module, "sync_memory_before_group_chat", lambda gid: (True, None))
    monkeypatch.setattr(app_module, "build_system_prompt_v2", lambda *args, **kwargs: "system")
    monkeypatch.setattr(app_module, "build_group_relationship_prompt", lambda *args, **kwargs: "relations")
    monkeypatch.setattr(app_module, "get_character_local_now", lambda *args, **kwargs: datetime(2026, 8, 2, 12, 0))
    monkeypatch.setattr(app_module, "get_ai_language", lambda *args, **kwargs: "zh")
    monkeypatch.setattr(app_module, "get_model_config", lambda *args, **kwargs: ("relay", "test-model"))
    monkeypatch.setattr(
        app_module,
        "process_agent_actions",
        lambda char_id, text, user_id=None: (text, None, None),
    )

    def fake_model(messages, char_id=None, **kwargs):
        model_calls.append(char_id)
        if char_id == "target":
            return "被拉入者回复 [DIRECT_END]"
        return "发起者首条消息"

    monkeypatch.setattr(app_module, "call_openrouter", fake_model)
    return db_path, model_calls


def test_group_and_single_chat_availability_use_different_mode_levels():
    sleeping_offline_character = {"deep_sleep": True, "chat_mode": "offline"}

    assert is_character_available_for_chat(sleeping_offline_character) is True
    assert is_character_available_for_group_chat(sleeping_offline_character, "online") is False
    assert is_character_available_for_group_chat(sleeping_offline_character, "offline") is True


@pytest.mark.parametrize(
    ("group_mode", "initiator_info", "target_info", "expected_calls"),
    [
        (
            "online",
            {"deep_sleep": True, "chat_mode": "online"},
            {"deep_sleep": True, "chat_mode": "online"},
            ["initiator"],
        ),
        (
            "online",
            {"deep_sleep": True, "chat_mode": "online"},
            {"deep_sleep": False, "chat_mode": "online"},
            ["initiator", "target"],
        ),
        (
            "offline",
            {"deep_sleep": True, "chat_mode": "online"},
            {"deep_sleep": True, "chat_mode": "online"},
            ["initiator", "target"],
        ),
        (
            "online",
            {"deep_sleep": False, "chat_mode": "online"},
            {"deep_sleep": True, "chat_mode": "offline"},
            ["initiator"],
        ),
    ],
)
def test_group_directive_uses_group_mode_and_first_reply_is_not_initiator(
    tmp_path,
    monkeypatch,
    group_mode,
    initiator_info,
    target_info,
    expected_calls,
):
    db_path, model_calls = _prepare_directive_runtime(
        tmp_path,
        monkeypatch,
        group_mode=group_mode,
        initiator_info=initiator_info,
        target_info=target_info,
    )

    app_module._execute_directive(
        {"type": "group", "member_ids": ["target"], "include_user": False},
        "initiator",
        "去群里继续说",
    )

    assert model_calls == expected_calls
    if len(model_calls) > 1:
        assert model_calls[1] != "initiator"
    with sqlite3.connect(db_path) as conn:
        stored_roles = [row[0] for row in conn.execute("SELECT role FROM messages ORDER BY id")]
    assert stored_roles == expected_calls


def test_two_person_directive_alternates_speakers_without_self_talk(
    tmp_path, monkeypatch
):
    db_path, model_calls = _prepare_directive_runtime(
        tmp_path,
        monkeypatch,
        group_mode="online",
        initiator_info={"deep_sleep": True, "chat_mode": "online"},
        target_info={"deep_sleep": False, "chat_mode": "online"},
    )

    def fake_model(messages, char_id=None, **kwargs):
        model_calls.append(char_id)
        return f"{char_id} 的消息"

    monkeypatch.setattr(app_module, "call_openrouter", fake_model)
    monkeypatch.setattr(app_module.random, "random", lambda: 0.0)

    app_module._execute_directive(
        {"type": "group", "member_ids": ["target"], "include_user": False},
        "initiator",
        "去群里继续说",
    )

    with sqlite3.connect(db_path) as conn:
        stored_roles = [
            row[0] for row in conn.execute("SELECT role FROM messages ORDER BY id")
        ]

    assert stored_roles == [
        "initiator",
        "target",
        "initiator",
        "target",
        "initiator",
        "target",
    ]
    assert all(left != right for left, right in zip(stored_roles, stored_roles[1:]))


def test_directive_created_group_defaults_online_and_reused_group_keeps_offline(tmp_path, monkeypatch):
    groups_file = tmp_path / "groups.json"
    _write_json(groups_file, {})
    groups_root = tmp_path / "groups"
    monkeypatch.setattr(app_module, "_get_groups_config_file", lambda: str(groups_file))
    monkeypatch.setattr(app_module, "get_group_dir", lambda gid: str(groups_root / gid))
    monkeypatch.setattr(app_module, "get_char_name", lambda cid: cid)

    directive = {"member_ids": ["target"], "include_user": False}
    group_id = app_module.ensure_directive_chat(directive, "initiator")
    config = json.loads(groups_file.read_text(encoding="utf-8"))
    assert config[group_id]["group_chat_mode"] == "online"

    config[group_id]["group_chat_mode"] = "offline"
    _write_json(groups_file, config)
    reused_id = app_module.ensure_directive_chat(directive, "initiator")
    reused = json.loads(groups_file.read_text(encoding="utf-8"))
    assert reused_id == group_id
    assert reused[group_id]["group_chat_mode"] == "offline"


def test_all_group_reply_paths_use_group_level_availability():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    app_source = (root / "app.py").read_text(encoding="utf-8-sig")
    group_source = (root / "blueprints" / "group.py").read_text(encoding="utf-8")

    assert "is_character_available_for_group_chat(cinfo, group_chat_mode)" in app_source
    assert "is_character_available_for_group_chat(cinfo, group_chat_mode)" in group_source
    assert '"group_chat_mode": "online"' in app_source
    assert '"group_chat_mode": "online"' in group_source


def test_directive_stores_initiator_message_before_memory_sync(tmp_path, monkeypatch):
    db_path, _ = _prepare_directive_runtime(
        tmp_path,
        monkeypatch,
        group_mode="online",
        initiator_info={"deep_sleep": False},
        target_info={"deep_sleep": True},
    )

    def assert_message_already_visible(group_id):
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        return True, None

    monkeypatch.setattr(app_module, "sync_memory_before_group_chat", assert_message_already_visible)

    app_module._execute_directive(
        {"type": "group", "member_ids": ["target"], "include_user": False},
        "initiator",
        "去群里继续说",
    )


def test_directive_falls_back_to_source_text_when_first_message_generation_fails(
    tmp_path, monkeypatch
):
    db_path, _ = _prepare_directive_runtime(
        tmp_path,
        monkeypatch,
        group_mode="online",
        initiator_info={"deep_sleep": False},
        target_info={"deep_sleep": True},
    )
    monkeypatch.setattr(
        app_module,
        "call_openrouter",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("upstream failed")),
    )

    app_module._execute_directive(
        {"type": "group", "member_ids": ["target"], "include_user": False},
        "initiator",
        "去群里继续说",
    )

    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert stored == [("initiator", "去群里继续说")]
