import json
import io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask

import blueprints.media as media
import blueprints.views as views


ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")


def test_voice_announcement_is_active_for_exactly_configured_window(monkeypatch):
    monkeypatch.setattr(views, "VOICE_ANNOUNCEMENT_START", "2026-08-06T00:00:00+08:00")
    monkeypatch.setattr(views, "VOICE_ANNOUNCEMENT_END", "2026-08-13T00:00:00+08:00")

    before = views._voice_announcement_payload(datetime(2026, 8, 5, 23, 59, tzinfo=BEIJING))
    during = views._voice_announcement_payload(datetime(2026, 8, 12, 23, 59, tzinfo=BEIJING))
    after = views._voice_announcement_payload(datetime(2026, 8, 13, 0, 0, tzinfo=BEIJING))

    assert before["active"] is False
    assert during["active"] is True
    assert during["server_date"] == "2026-08-12"
    assert during["legacy_voice_deletes_on"] == "2026年08月13日"
    assert after["active"] is False


def test_clone_requires_private_key_without_forwarding_to_elevenlabs(monkeypatch):
    def fail_request(*args, **kwargs):
        raise AssertionError("missing private key must not call ElevenLabs")

    monkeypatch.setattr(media, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(media, "get_user_credential", lambda *_a, **_k: "")
    monkeypatch.setattr(media, "create_ivc_voice", fail_request)
    app = Flask(__name__)
    app.register_blueprint(media.media_bp)

    with app.test_client() as client:
        response = client.post(
            "/api/voice_clone",
            data={
                "file": (io.BytesIO(b"audio"), "voice.mp3"),
                "consent": "true",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert response.status_code == 400
    assert response.get_json()["code"] == "elevenlabs_key_missing"


def test_clone_uses_current_users_key_and_returns_voice_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(media, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(
        media,
        "get_user_credential",
        lambda uid, provider, allow_legacy=False: "private-user-key",
    )

    def fake_create(api_key, **kwargs):
        captured["api_key"] = api_key
        captured.update(kwargs)
        return {"voice_id": "AbCdEfGh12345678", "requires_verification": False}

    monkeypatch.setattr(media, "create_ivc_voice", fake_create)
    app = Flask(__name__)
    app.register_blueprint(media.media_bp)

    with app.test_client() as client:
        response = client.post(
            "/api/voice_clone",
            data={
                "file": (io.BytesIO(b"audio-data"), "voice.mp3"),
                "name": "Rin Voice",
                "consent": "true",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert response.status_code == 200
    assert response.get_json()["voice_id"] == "AbCdEfGh12345678"
    assert captured["api_key"] == "private-user-key"
    assert captured["audio_bytes"] == b"audio-data"
    assert captured["name"] == "Rin Voice"


def test_clone_requires_same_origin_header_and_voice_consent(monkeypatch):
    monkeypatch.setattr(media, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(media, "get_user_credential", lambda *_a, **_k: "private-key")
    app = Flask(__name__)
    app.register_blueprint(media.media_bp)

    with app.test_client() as client:
        no_header = client.post("/api/voice_clone")
        no_consent = client.post(
            "/api/voice_clone",
            data={"file": (io.BytesIO(b"audio"), "voice.mp3")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert no_header.status_code == 403
    assert no_header.get_json()["code"] == "request_origin_invalid"
    assert no_consent.status_code == 400
    assert no_consent.get_json()["code"] == "voice_consent_required"


def test_tts_uses_private_key_without_platform_daily_quota(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(media.core.config, "USERS_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "get_current_user_id", lambda: 8)
    monkeypatch.setattr(media, "get_user_credential", lambda *_a, **_k: "user-8-key")
    monkeypatch.setattr(media, "_resolve_voice_config", lambda *_a, **_k: "VoiceId12345678")

    def fake_synthesize(api_key, **kwargs):
        captured["api_key"] = api_key
        captured.update(kwargs)
        return b"fake-mp3"

    monkeypatch.setattr(media, "synthesize_speech", fake_synthesize)
    app = Flask(__name__)
    app.register_blueprint(media.media_bp)

    with app.test_client() as client:
        response = client.post("/api/rin/tts", json={"text": "hello"})

    assert response.status_code == 200
    assert response.data == b"fake-mp3"
    assert captured["api_key"] == "user-8-key"
    assert captured["voice_id"] == "VoiceId12345678"
    assert not (tmp_path / "8" / "configs" / "tts_usage.json").exists()


def test_templates_keep_private_voice_keys_without_square_voice_ids():
    memory = (ROOT / "templates" / "memory.html").read_text(encoding="utf-8")
    profile = (ROOT / "templates" / "profile.html").read_text(encoding="utf-8")
    tabbar = (ROOT / "templates" / "tabbar.html").read_text(encoding="utf-8")
    contacts = (ROOT / "templates" / "contacts.html").read_text(encoding="utf-8")
    square_upload = (ROOT / "templates" / "square" / "upload.html").read_text(encoding="utf-8")
    voice_server = (ROOT / "Voice" / "server.py").read_text(encoding="utf-8")

    assert "voice-clone-file" in memory
    assert "cloneVoice()" in memory
    assert "voice-clone-consent" in memory
    assert "个人 ElevenLabs API Key" in memory
    assert 'id="elevenlabs-key-input"' in profile
    assert "dataUser.credentials?.elevenlabs" in profile
    assert "https://try.elevenlabs.io/8f8ks0unlqsw" in memory
    assert tabbar.count('id="daily-donate-modal"') == 1
    assert "voice-announcement-section" in tabbar
    assert "自己的 ElevenLabs API Key" in tabbar
    assert "不需要开通 Speech-to-Text 权限" in tabbar
    assert 'id="daily-donate-modal"' not in contacts
    assert 'name="voice_id"' not in square_upload
    assert "clone_voice" not in voice_server
