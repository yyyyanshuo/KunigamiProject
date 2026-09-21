import services.elevenlabs as elevenlabs


class FakeResponse:
    def __init__(self, status_code=200, *, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_create_voice_sends_key_only_in_server_header(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(payload={"voice_id": "PrivateVoice123", "requires_verification": False})

    monkeypatch.setattr(elevenlabs.requests, "post", fake_post)
    result = elevenlabs.create_ivc_voice(
        "secret-user-key",
        name="Rin Voice",
        filename="voice.mp3",
        audio_bytes=b"audio",
        content_type="audio/mpeg",
    )

    assert result["voice_id"] == "PrivateVoice123"
    assert captured["headers"] == {"xi-api-key": "secret-user-key"}
    assert "secret-user-key" not in captured["url"]
    assert captured["files"][0][0] == "files"


def test_tts_sends_private_key_and_returns_audio(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(content=b"mp3")

    monkeypatch.setattr(elevenlabs.requests, "post", fake_post)
    result = elevenlabs.synthesize_speech(
        "private-key",
        voice_id="VoiceId123",
        text="hello",
        model_id="eleven_v3",
        voice_settings={"stability": 0.5},
    )

    assert result == b"mp3"
    assert captured["headers"]["xi-api-key"] == "private-key"
    assert captured["json"]["model_id"] == "eleven_v3"


def test_upstream_error_is_sanitized_and_does_not_echo_body(monkeypatch):
    def fake_post(*_args, **_kwargs):
        response = FakeResponse(status_code=401, headers={"request-id": "req-123"})
        response.text = "secret-user-key should never be forwarded"
        return response

    monkeypatch.setattr(elevenlabs.requests, "post", fake_post)

    try:
        elevenlabs.synthesize_speech(
            "secret-user-key",
            voice_id="VoiceId123",
            text="hello",
            model_id="eleven_v3",
            voice_settings={},
        )
    except elevenlabs.ElevenLabsAPIError as error:
        assert error.code == "elevenlabs_key_invalid"
        assert "secret-user-key" not in error.user_message
        assert "req-123" in error.user_message
    else:
        raise AssertionError("expected ElevenLabsAPIError")
