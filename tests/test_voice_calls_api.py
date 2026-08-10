import io
import json

import services.voice_calls as voice_calls


CALL_ID = "call_" + ("c" * 32)


def _logged_in_client(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "get_auth_version", lambda _uid: 1)
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = 91
        session["logged_in"] = True
        session["auth_version"] = 1
    return client


def test_call_model_defaults_to_gemini_36_flash(tmp_path, monkeypatch):
    import services.ai_client as ai_client

    monkeypatch.setattr(ai_client, "USERS_ROOT", str(tmp_path))
    assert ai_client.get_model_config("call", user_id=91) == (
        "relay", "gemini-3.6-flash"
    )


def test_call_stt_token_uses_only_global_key(tmp_path, monkeypatch):
    import blueprints.calls as calls

    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(91, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(91, CALL_ID)
    monkeypatch.setattr(calls.core.config, "ELEVENLABS_API_KEY", "global-stt-key")
    monkeypatch.setattr(calls, "consume_voice_stt_quota", lambda *args, **kwargs: True)
    seen = {}

    def fake_token(key):
        seen["key"] = key
        return "single-use-token"

    monkeypatch.setattr(calls, "create_realtime_scribe_token", fake_token)
    response = _logged_in_client(monkeypatch).post(
        f"/api/calls/{CALL_ID}/stt-token",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert response.get_json()["token"] == "single-use-token"
    assert seen["key"] == "global-stt-key"


def test_call_batch_transcription_uses_global_key_without_saving_audio(
    tmp_path, monkeypatch
):
    import blueprints.calls as calls

    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(91, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(91, CALL_ID)
    monkeypatch.setattr(calls.core.config, "ELEVENLABS_API_KEY", "global-stt-key")
    monkeypatch.setattr(calls, "consume_voice_stt_quota", lambda *args, **kwargs: True)
    seen = {}

    def fake_transcribe(api_key, **kwargs):
        seen["key"] = api_key
        seen.update(kwargs)
        return "这是整句话的最终识别"

    monkeypatch.setattr(calls, "transcribe_speech", fake_transcribe)
    response = _logged_in_client(monkeypatch).post(
        f"/api/calls/{CALL_ID}/transcribe",
        data={"file": (io.BytesIO(b"recorded-audio"), "call.webm", "audio/webm")},
        headers={"X-Requested-With": "XMLHttpRequest"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["transcript"] == "这是整句话的最终识别"
    assert response.get_json()["has_speech"] is True
    assert seen["key"] == "global-stt-key"
    assert seen["audio_bytes"] == b"recorded-audio"
    assert seen["model_id"] == "scribe_v2"


def test_call_batch_transcription_marks_audio_only_result_as_no_speech(
    tmp_path, monkeypatch
):
    import blueprints.calls as calls

    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(91, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(91, CALL_ID)
    monkeypatch.setattr(calls.core.config, "ELEVENLABS_API_KEY", "global-stt-key")
    monkeypatch.setattr(calls, "consume_voice_stt_quota", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        calls, "transcribe_speech", lambda *args, **kwargs: "（背景嘈杂）（敲门声）"
    )

    response = _logged_in_client(monkeypatch).post(
        f"/api/calls/{CALL_ID}/transcribe",
        data={"file": (io.BytesIO(b"noise"), "call.webm", "audio/webm")},
        headers={"X-Requested-With": "XMLHttpRequest"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["has_speech"] is False


def test_outgoing_call_disables_tts_without_personal_key(tmp_path, monkeypatch):
    import blueprints.calls as calls

    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    monkeypatch.setattr(calls.core.config, "ELEVENLABS_API_KEY", "global-stt-key")
    monkeypatch.setattr(calls, "get_user_credential", lambda *args, **kwargs: None)
    monkeypatch.setattr(calls, "_character_exists", lambda char_id: True)
    monkeypatch.setattr(calls, "_insert_call_anchor", lambda *args, **kwargs: 1)
    response = _logged_in_client(monkeypatch).post(
        "/api/calls",
        json={"char_id": "rin"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 201
    call_id = response.get_json()["call"]["call_id"]
    detail = _logged_in_client(monkeypatch).get(f"/api/calls/{call_id}")
    assert detail.status_code == 200
    assert detail.get_json()["call"]["call_mode"] == "voice_input_text_reply"
    assert detail.get_json()["call"]["tts_enabled"] is False


def test_call_disables_tts_without_character_voice_id(tmp_path, monkeypatch):
    import blueprints.calls as calls

    config_file = tmp_path / "characters.json"
    config_file.write_text(json.dumps({"rin": {"voice_id": ""}}), encoding="utf-8")
    monkeypatch.setattr(calls, "get_user_credential", lambda *args, **kwargs: "personal-key")
    monkeypatch.setattr(
        calls, "_get_characters_config_file", lambda *args, **kwargs: str(config_file)
    )

    enabled, reason = calls._call_tts_capability(91, "rin")

    assert enabled is False
    assert reason == "voice_id_missing_or_invalid"

    config_file.write_text(
        json.dumps({"rin": {"voice_id": "voice_12345678"}}), encoding="utf-8"
    )
    assert calls._call_tts_capability(91, "rin") == (True, "")


def test_selected_call_records_are_resolved_for_current_user(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(91, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(91, CALL_ID)
    voice_calls.append_turn(91, CALL_ID, role="user", content="你听得见吗")
    voice_calls.append_turn(91, CALL_ID, role="assistant", content="听得见")
    voice_calls.end_call(91, CALL_ID, reason="hangup")

    response = _logged_in_client(monkeypatch).post(
        "/api/calls/resolve-records",
        json={"contents": [voice_calls.build_voice_call_tag(CALL_ID), "普通消息"]},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    contents = response.get_json()["contents"]
    assert "用户：你听得见吗" in contents[0]
    assert "角色：听得见" in contents[0]
    assert contents[1] == "普通消息"


def test_character_can_start_call_without_personal_key(tmp_path, monkeypatch):
    import blueprints.calls as calls

    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    monkeypatch.setattr(calls, "get_user_credential", lambda *args, **kwargs: None)
    monkeypatch.setattr(calls, "_insert_call_anchor", lambda *args, **kwargs: 1)

    call, error = calls.create_incoming_call_for_character(91, "rin")

    assert error is None
    assert call["initiator"] == "assistant"


def test_character_decision_accepts_with_natural_tone(tmp_path, monkeypatch):
    import blueprints.calls as calls

    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(91, call_id=CALL_ID, char_id="rin", initiator="user")
    monkeypatch.setattr(calls, "_recent_chat_text", lambda *args, **kwargs: [])
    prompt_options = {}

    def fake_system_prompt(*args, **kwargs):
        prompt_options.update(kwargs)
        return "角色完整人设"

    monkeypatch.setattr(calls, "build_system_prompt_v2", fake_system_prompt)
    monkeypatch.setattr(
        calls, "_run_call_model",
        lambda *args, **kwargs: (
            "[CALL_ACCEPT][CALL_TONE](压低声音（像刚醒）)喂，你说。",
            "relay", "gemini-3.6-flash",
        ),
    )
    monkeypatch.setattr(
        calls, "process_agent_actions", lambda char_id, text, uid: (text, None, None)
    )
    monkeypatch.setattr(calls, "_set_call_chat_mode", lambda *args: None)
    response = _logged_in_client(monkeypatch).post(
        f"/api/calls/{CALL_ID}/decision",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["decision"] == "accepted"
    assert payload["opening_turn"]["tone"] == "压低声音（像刚醒）"
    assert prompt_options["call_mode"] is True
    assert voice_calls.get_call(91, CALL_ID)["status"] == "active"
