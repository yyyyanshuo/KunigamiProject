import json
from datetime import datetime

import pytest
from flask import Flask

import agent_utils
import blueprints.chat as chat
import core.config as config
import core.utils as utils
from services import prompt_builder


@pytest.fixture
def index_config(tmp_path, monkeypatch):
    paths = {}
    for uid in ("alice", "bob"):
        path = tmp_path / f"{uid}.json"
        path.write_text(json.dumps({
            "hero": {"emotion": 0.5, "moments_index": 1.5},
            "friend": {"emotion": 1, "moments_index": 1},
        }), encoding="utf-8")
        paths[uid] = path

    def config_path(user_id=None):
        return str(paths[user_id or "alice"])

    for module in (utils, chat, prompt_builder):
        monkeypatch.setattr(module, "_get_characters_config_file", config_path)
    monkeypatch.setattr(chat, "_load_user_settings", lambda: {})
    app = Flask(__name__)
    app.register_blueprint(chat.chat_bp)
    return paths, app.test_client()


@pytest.mark.parametrize("emotion_locked,moments_locked", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_locked_actions_are_cleaned_and_independent(
    index_config, emotion_locked, moments_locked, capsys
):
    paths, client = index_config
    response = client.post("/api/hero/update_meta", json={
        "emotion_locked": emotion_locked, "moments_index_locked": moments_locked,
    })
    assert response.status_code == 200
    cleaned, delta, directive, events = agent_utils.process_agent_actions(
        "hero", "正文[SET_EMOTION: 9][SET_PERSONALITY: 4]", "alice", return_events=True
    )
    assert cleaned == "正文"
    assert (delta, directive, events) == (None, None, [])
    data = json.loads(paths["alice"].read_text())["hero"]
    assert data["emotion"] == (0.5 if emotion_locked else 9)
    assert data["moments_index"] == (1.5 if moments_locked else 4)
    output = capsys.readouterr().out
    assert ("情绪指数设置为" in output) is not emotion_locked
    assert ("性格指数设置为" in output) is not moments_locked


def test_legacy_config_manual_edits_unlock_and_scope(index_config):
    paths, client = index_config
    original_bob = paths["bob"].read_bytes()
    original_friend = json.loads(paths["alice"].read_text())["friend"]
    details = client.get("/api/hero/config").get_json()
    assert details["emotion_locked"] is False
    assert details["moments_index_locked"] is False
    assert agent_utils._update_persona_param("hero", "emotion", 3, "alice") is True

    assert client.post("/api/hero/update_meta", json={
        "emotion_locked": True, "moments_index_locked": True,
    }).status_code == 200
    # User edits, including the contacts batch endpoint, are still permitted.
    assert client.post("/api/hero/update_meta", json={"emotion": 0}).status_code == 200
    assert client.post("/api/hero/update_meta", json={"moments_index": 2.5}).status_code == 200
    details = client.get("/api/hero/config").get_json()
    assert (details["emotion"], details["moments_index"]) == (0, 2.5)
    assert details["emotion_locked"] is True
    assert details["moments_index_locked"] is True

    assert client.post("/api/hero/update_meta", json={"emotion_locked": False}).status_code == 200
    assert agent_utils._update_persona_param("hero", "emotion", 4, "alice") is True
    assert agent_utils._update_persona_param("hero", "personality", 8, "alice") is False
    assert json.loads(paths["alice"].read_text())["friend"] == original_friend
    assert paths["bob"].read_bytes() == original_bob
    assert agent_utils._update_persona_param("hero", "personality", 6, "bob") is True
    assert agent_utils._update_persona_param("friend", "personality", 7, "alice") is True
    assert client.post("/api/hero/update_meta", json={"moments_index_locked": False}).status_code == 200
    assert agent_utils._update_persona_param("hero", "personality", 8, "alice") is True


@pytest.mark.parametrize("field", ["emotion_locked", "moments_index_locked"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_lock_api_rejects_non_booleans_without_saving(index_config, field, value):
    paths, client = index_config
    before = paths["alice"].read_bytes()
    assert client.post("/api/hero/update_meta", json={field: value, "emotion": 9}).status_code == 400
    assert paths["alice"].read_bytes() == before


def test_ai_reads_lock_after_waiting_for_config_transaction(index_config, monkeypatch):
    from contextlib import contextmanager

    paths, client = index_config
    real_lock = agent_utils.memory_file_lock

    @contextmanager
    def lock_after_user_save(path):
        # Simulate a user save winning the race before the AI acquires the lock.
        assert client.post("/api/hero/update_meta", json={"emotion_locked": True}).status_code == 200
        with real_lock(path):
            yield

    monkeypatch.setattr(agent_utils, "memory_file_lock", lock_after_user_save)
    assert agent_utils._update_persona_param("hero", "emotion", 20, "alice") is False
    assert json.loads(paths["alice"].read_text())["hero"]["emotion"] == 0.5


def test_failed_lock_save_reports_error_and_preserves_config(index_config, monkeypatch):
    paths, client = index_config
    before = paths["alice"].read_bytes()

    def fail_write(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(chat, "atomic_write_json", fail_write)
    response = client.post("/api/hero/update_meta", json={"emotion_locked": True})
    assert response.status_code == 500
    assert paths["alice"].read_bytes() == before
    assert not paths["alice"].with_suffix(".json.lock").exists()


@pytest.mark.parametrize("lang", ["zh", "ja", "en"])
@pytest.mark.parametrize("brief", [False, True])
@pytest.mark.parametrize("emotion_locked,moments_locked", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_rules_only_remove_locked_instructions(
    index_config, lang, brief, emotion_locked, moments_locked
):
    _, client = index_config
    source = (getattr(config, f"GLOBAL_SYSTEM_RULES_{lang.upper()}_AGENT_BRIEF")
              if brief else config.get_global_system_rules(lang))
    assert client.post("/api/hero/update_meta", json={
        "emotion_locked": emotion_locked, "moments_index_locked": moments_locked,
    }).status_code == 200
    result = prompt_builder._filter_locked_index_rules(source, "hero", "alice")
    assert ("[SET_EMOTION:" in result) is not emotion_locked
    assert ("[SET_PERSONALITY:" in result) is not moments_locked
    assert ("[MOOD:" in result) == ("[MOOD:" in source)
    assert "[UPDATE_AFFINITY:" in result
    removed = len(source.splitlines()) - len(result.splitlines())
    assert removed == int(emotion_locked) + int(moments_locked)
    assert prompt_builder._filter_locked_index_rules(source, "hero", "bob") == source
    assert client.post("/api/hero/update_meta", json={
        "emotion_locked": False, "moments_index_locked": False,
    }).status_code == 200
    assert prompt_builder._filter_locked_index_rules(source, "hero", "alice") == source


@pytest.mark.parametrize("version,brief,group", [
    (1, False, False), (1, True, False),
    (2, False, False), (2, True, False), (2, False, True),
])
def test_both_prompt_builders_filter_rules_and_keep_current_values(
    index_config, tmp_path, monkeypatch, version, brief, group
):
    _, client = index_config
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    monkeypatch.setattr(prompt_builder, "get_paths", lambda *a, **kw: (str(tmp_path / "chat.db"), str(prompts_dir)))
    monkeypatch.setattr(prompt_builder, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(prompt_builder, "USERS_ROOT", str(tmp_path))
    monkeypatch.setattr(prompt_builder, "get_current_user_id", lambda: None)
    monkeypatch.setattr(prompt_builder, "get_ai_language", lambda *a, **kw: "zh")
    monkeypatch.setattr(prompt_builder, "_get_char_chat_mode", lambda *a, **kw: "online")
    monkeypatch.setattr(prompt_builder, "_get_character_time_info", lambda *a, **kw: (None, "Asia/Shanghai", datetime(2026, 9, 5, 12)))
    for name in ("get_char_age", "get_user_age"):
        monkeypatch.setattr(prompt_builder, name, lambda *a, **kw: None)
    for name in ("get_char_name", "_get_username_for_user", "get_current_username"):
        monkeypatch.setattr(prompt_builder, name, lambda *a, **kw: "")
    for name in ("extract_medium_memory_with_timeline_ts", "extract_short_memory_with_timeline_ts"):
        monkeypatch.setattr(prompt_builder, name, lambda *a, **kw: [])
    monkeypatch.setattr(prompt_builder, "normalize_map_state", lambda **kw: ({}, {}, {"locations": []}))
    monkeypatch.setattr(prompt_builder, "load_character_positions", lambda **kw: {})
    assert client.post("/api/hero/update_meta", json={
        "emotion_locked": True, "moments_index_locked": True,
    }).status_code == 200
    options = {"include_global_format": not brief, "include_long_memory": False, "user_id": "alice"}
    if version == 2:
        options.update(include_recent_messages=False, group_id="group" if group else None, read_only=True)
    builder = prompt_builder.build_system_prompt_v2 if version == 2 else prompt_builder.build_system_prompt
    result = builder("hero", **options)
    assert "[SET_EMOTION:" not in result
    assert "[SET_PERSONALITY:" not in result
    assert "emotion：0.5" in result
    assert "moments_index：1.5" in result
    assert "[UPDATE_AFFINITY:" in result
    if not brief:
        assert "[MOOD:" in result
