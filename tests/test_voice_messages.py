import io
from pathlib import Path

from flask import Flask

import blueprints.media as media
import services.elevenlabs as elevenlabs
import services.voice_messages as voice_messages


ROOT = Path(__file__).resolve().parents[1]
FILENAME = "vm_" + ("a" * 32) + ".webm"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_scribe_transcription_uses_platform_key_in_server_header(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(payload={"text": "你好，能听清吗？"})

    monkeypatch.setattr(elevenlabs.requests, "post", fake_post)
    text = elevenlabs.transcribe_speech(
        "platform-key",
        filename="recording.webm",
        audio_bytes=b"audio",
        content_type="audio/webm",
    )

    assert text == "你好，能听清吗？"
    assert captured["headers"] == {"xi-api-key": "platform-key"}
    assert captured["data"]["model_id"] == "scribe_v2"
    assert captured["data"]["tag_audio_events"] == "true"
    assert captured["data"]["diarize"] == "false"
    assert captured["files"][0][0] == "file"


def test_scribe_localizes_meaningful_audio_events(monkeypatch):
    monkeypatch.setattr(
        elevenlabs.requests,
        "post",
        lambda *_a, **_k: FakeResponse(
            payload={"text": "(laughter) 你好 (background music) (door knocking)"}
        ),
    )

    text = elevenlabs.transcribe_speech(
        "platform-key",
        filename="recording.webm",
        audio_bytes=b"audio",
        content_type="audio/webm",
    )

    assert text == "（笑） 你好 （背景音乐） （敲门声）"


def test_scribe_spoken_text_detection_ignores_audio_events():
    assert elevenlabs.scribe_has_spoken_text("（背景嘈杂）（敲门声）") is False
    assert elevenlabs.scribe_has_spoken_text("（背景嘈杂）你好，你听得到吗？") is True
    assert elevenlabs.scribe_has_spoken_text("嗯") is True


def test_realtime_scribe_token_uses_platform_key(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(payload={"token": "sutkn_test"})

    monkeypatch.setattr(elevenlabs.requests, "post", fake_post)

    token = elevenlabs.create_realtime_scribe_token("platform-key")

    assert token == "sutkn_test"
    assert captured["url"].endswith("/single-use-token/realtime_scribe")
    assert captured["headers"] == {"xi-api-key": "platform-key"}


def test_scribe_permission_error_explains_required_permission(monkeypatch):
    monkeypatch.setattr(
        elevenlabs.requests,
        "post",
        lambda *_a, **_k: FakeResponse(status_code=403),
    )
    try:
        elevenlabs.transcribe_speech(
            "platform-key",
            filename="recording.webm",
            audio_bytes=b"audio",
            content_type="audio/webm",
        )
    except elevenlabs.ElevenLabsAPIError as error:
        assert error.code == "elevenlabs_permission_denied"
        assert "Speech-to-Text" in error.user_message
    else:
        raise AssertionError("expected permission error")


def test_voice_message_tag_accepts_parentheses_and_protects_slash_delimiter():
    tag = voice_messages.build_voice_message_tag(
        FILENAME, "你好（刚刚有点吵），你能听清吗？/可以"
    )
    parsed = voice_messages.parse_voice_message_tag(tag)

    assert parsed["filename"] == FILENAME
    assert parsed["transcript"] == "你好（刚刚有点吵），你能听清吗？／可以"
    assert voice_messages.voice_message_for_ai(tag) == (
        "[用户语音] 你好（刚刚有点吵），你能听清吗？／可以"
    )


def test_voice_upload_uses_env_platform_key_and_returns_short_tag(monkeypatch):
    captured = {}
    monkeypatch.setattr(media, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(media.core.config, "ELEVENLABS_API_KEY", "platform-env-key")
    monkeypatch.setattr(media, "new_voice_filename", lambda _ext: FILENAME)
    monkeypatch.setattr(media, "consume_voice_stt_quota", lambda *_a, **_k: True)

    def fake_transcribe(api_key, **kwargs):
        captured["api_key"] = api_key
        captured.update(kwargs)
        return "你好（有括号）"

    def fake_save(user_id, **kwargs):
        captured["user_id"] = user_id
        captured["saved"] = kwargs
        return {"duration_ms": kwargs["duration_ms"]}

    monkeypatch.setattr(media, "transcribe_speech", fake_transcribe)
    monkeypatch.setattr(media, "save_voice_message", fake_save)
    app = Flask(__name__)
    app.register_blueprint(media.media_bp)

    with app.test_client() as client:
        response = client.post(
            "/api/voice-messages",
            data={
                "file": (io.BytesIO(b"audio-data"), "recording.webm", "audio/webm"),
                "duration_ms": "1200",
                "scope_type": "chat",
                "scope_id": "rin",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert response.status_code == 200
    body = response.get_json()
    assert captured["api_key"] == "platform-env-key"
    assert captured["user_id"] == 7
    assert body["tag"] == f"[voice_message]({FILENAME})(你好（有括号）)"
    assert body["playback_url"] == f"/api/voice-media/{FILENAME}"


def test_realtime_token_route_uses_env_platform_key_and_disables_cache(monkeypatch):
    captured = {}
    monkeypatch.setattr(media, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(media.core.config, "ELEVENLABS_API_KEY", "platform-env-key")
    monkeypatch.setattr(media, "consume_voice_stt_quota", lambda *_a, **_k: True)

    def fake_create_token(api_key):
        captured["api_key"] = api_key
        return "sutkn_route_test"

    monkeypatch.setattr(media, "create_realtime_scribe_token", fake_create_token)
    app = Flask(__name__)
    app.register_blueprint(media.media_bp)

    with app.test_client() as client:
        response = client.post(
            "/api/voice-messages/realtime-token",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert response.status_code == 200
    assert captured["api_key"] == "platform-env-key"
    assert response.get_json()["token"] == "sutkn_route_test"
    assert response.headers["Cache-Control"] == "no-store"


def test_local_voice_store_requires_matching_scope_and_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_messages, "USERS_ROOT", str(tmp_path))
    monkeypatch.setattr(voice_messages, "COS_BASE_URL", "")
    record = voice_messages.save_voice_message(
        3,
        filename=FILENAME,
        audio_bytes=b"audio-data",
        mime_type="audio/webm",
        duration_ms=1200,
        transcript="测试语音",
        scope_type="chat",
        scope_id="rin",
    )
    tag = voice_messages.build_voice_message_tag(FILENAME, "测试语音")

    assert record["storage_backend"] == "local"
    assert voice_messages.get_local_voice_path(3, FILENAME)
    voice_messages.attach_voice_message(
        3, tag, scope_type="chat", scope_id="rin", message_id=42
    )
    assert voice_messages.get_voice_message(3, FILENAME)["message_id"] == 42
    assert voice_messages.delete_voice_message_for_message(
        3, tag, scope_type="chat", scope_id="rin", message_id=42
    )
    assert voice_messages.get_voice_message(3, FILENAME) is None


def test_stt_quota_is_enforced_per_user(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_messages, "USERS_ROOT", str(tmp_path))

    assert voice_messages.consume_voice_stt_quota(9, limit=2)
    assert voice_messages.consume_voice_stt_quota(9, limit=2)
    assert not voice_messages.consume_voice_stt_quota(9, limit=2)
    assert voice_messages.consume_voice_stt_quota(10, limit=2)
    assert voice_messages.consume_voice_stt_quota(9, limit=2, bucket="realtime")


def test_chat_template_contains_voice_recording_and_playback_flow():
    template = (ROOT / "templates" / "chat.html").read_text(encoding="utf-8")

    assert 'id="voiceModeBtn"' in template
    assert 'id="voiceHoldBtn"' in template
    assert 'id="voiceLivePanel"' in template
    assert 'id="voiceLiveTranscript"' in template
    assert "voice-recording-ui" in template
    assert "navigator.mediaDevices.getUserMedia" in template
    assert "'/api/voice-messages'" in template
    assert "'/api/voice-messages/realtime-token'" in template
    assert "scribe_v2_realtime" in template
    assert "input_audio_chunk" in template
    assert "/api/voice-media/${encodeURIComponent(voiceFilename)}" in template
    assert "\\[voice_message\\]" in template
