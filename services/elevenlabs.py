"""Small, server-side-only ElevenLabs HTTP client.

API keys must never leave the server.  This module intentionally returns
sanitized errors instead of forwarding arbitrary upstream response bodies.
"""

from dataclasses import dataclass
import re

import requests


ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"

_AUDIO_EVENT_LABELS = {
    "laughter": "笑",
    "laughing": "笑",
    "laugh": "笑",
    "giggle": "轻笑",
    "giggling": "轻笑",
    "chuckle": "轻笑",
    "chuckling": "轻笑",
    "sigh": "叹气",
    "sighing": "叹气",
    "crying": "哭泣",
    "sobbing": "哭泣",
    "applause": "掌声",
    "clapping": "掌声",
    "music": "背景音乐",
    "background music": "背景音乐",
    "footsteps": "脚步声",
    "knocking": "敲门声",
    "door knocking": "敲门声",
    "cough": "咳嗽",
    "coughing": "咳嗽",
    "sneeze": "喷嚏",
    "sneezing": "喷嚏",
    "breathing": "呼吸声",
    "heavy breathing": "急促呼吸",
    "phone ringing": "电话铃声",
    "ringing": "铃声",
    "typing": "打字声",
    "rain": "雨声",
    "wind": "风声",
    "thunder": "雷声",
    "traffic": "车流声",
    "dog barking": "狗叫声",
    "barking": "狗叫声",
    "cat meowing": "猫叫声",
    "meowing": "猫叫声",
    "baby crying": "婴儿哭声",
    "background noise": "背景嘈杂",
    "noise": "背景嘈杂",
    "static": "杂音",
}


@dataclass
class ElevenLabsAPIError(RuntimeError):
    code: str
    user_message: str
    http_status: int = 502
    upstream_status: int | None = None

    def __str__(self):
        return self.user_message


def _request_id(response) -> str:
    headers = getattr(response, "headers", {}) or {}
    return str(headers.get("request-id") or headers.get("x-trace-id") or "").strip()


def _raise_for_upstream_error(response, operation: str) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    request_id = _request_id(response)
    suffix = f"（请求 ID：{request_id}）" if request_id else ""

    if status == 401:
        message = (
            "服务器配置的 ElevenLabs Speech-to-Text Key 无效，请联系管理员。"
            if operation == "stt"
            else "ElevenLabs API Key 无效，请在个人主页重新填写。"
        )
        raise ElevenLabsAPIError(
            "elevenlabs_key_invalid",
            f"{message}{suffix}",
            503 if operation == "stt" else 400,
            status,
        )
    if status == 403:
        permission_message = (
            "ElevenLabs API Key 没有 Speech-to-Text 权限，请在 ElevenLabs 后台为该 Key 开启语音转文字权限。"
            if operation == "stt"
            else "ElevenLabs 拒绝了请求，请检查 API Key 的权限或 IP 白名单。"
        )
        raise ElevenLabsAPIError(
            "elevenlabs_permission_denied",
            f"{permission_message}{suffix}",
            403,
            status,
        )
    if status == 429:
        quota_message = (
            "ElevenLabs 平台 STT 额度不足或请求过于频繁，请稍后重试。"
            if operation == "stt"
            else "ElevenLabs 额度不足或请求过于频繁，请检查个人套餐后重试。"
        )
        raise ElevenLabsAPIError(
            "elevenlabs_quota_exceeded",
            f"{quota_message}{suffix}",
            429,
            status,
        )
    if status == 422:
        if operation == "clone":
            message = "ElevenLabs 无法使用这份音频，请确认文件包含清晰、单一说话人的声音。"
        elif operation == "stt":
            message = "ElevenLabs 无法识别这段录音，请确认录音清晰且文件格式受支持。"
        else:
            message = "ElevenLabs 无法处理这次语音请求，请检查文本和 Voice ID。"
        raise ElevenLabsAPIError(
            "elevenlabs_invalid_request", f"{message}{suffix}", 400, status
        )
    if status == 400:
        if operation == "clone":
            message = "音色创建失败：可能已达到当前套餐的音色数量限制，或音频不符合要求。"
        elif operation == "stt":
            message = "语音识别请求被拒绝，请检查 Speech-to-Text 权限和录音格式。"
        else:
            message = "该 Voice ID 无法使用，请确认它属于当前 API Key 对应的 ElevenLabs 账户。"
        raise ElevenLabsAPIError(
            "elevenlabs_request_rejected", f"{message}{suffix}", 400, status
        )
    if status >= 500 or status == 0:
        raise ElevenLabsAPIError(
            "elevenlabs_unavailable",
            f"ElevenLabs 服务暂时不可用，请稍后重试。{suffix}",
            502,
            status or None,
        )
    raise ElevenLabsAPIError(
        "elevenlabs_request_failed",
        f"ElevenLabs 请求失败（状态码 {status}），请稍后重试。{suffix}",
        502,
        status,
    )


def create_ivc_voice(
    api_key: str,
    *,
    name: str,
    filename: str,
    audio_bytes: bytes,
    content_type: str,
    timeout: int = 120,
) -> dict:
    response = requests.post(
        f"{ELEVENLABS_API_BASE}/voices/add",
        headers={"xi-api-key": api_key},
        files=[("files", (filename, audio_bytes, content_type))],
        data={"name": name},
        timeout=timeout,
    )
    if response.status_code != 200:
        _raise_for_upstream_error(response, "clone")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ElevenLabsAPIError(
            "elevenlabs_invalid_response",
            "ElevenLabs 返回了无法识别的结果，请稍后重试。",
        ) from exc
    voice_id = str((payload or {}).get("voice_id") or "").strip()
    if not voice_id:
        raise ElevenLabsAPIError(
            "elevenlabs_invalid_response",
            "ElevenLabs 没有返回 Voice ID，请稍后重试。",
        )
    return {
        "voice_id": voice_id,
        "requires_verification": bool(payload.get("requires_verification", False)),
    }


def synthesize_speech(
    api_key: str,
    *,
    voice_id: str,
    text: str,
    model_id: str,
    voice_settings: dict,
    timeout: int = 60,
) -> bytes:
    response = requests.post(
        f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        _raise_for_upstream_error(response, "tts")
    content = bytes(getattr(response, "content", b"") or b"")
    if not content:
        raise ElevenLabsAPIError(
            "elevenlabs_empty_audio",
            "ElevenLabs 没有返回音频，请稍后重试。",
        )
    return content


def create_realtime_scribe_token(api_key: str, *, timeout: int = 15) -> str:
    """Create a short-lived, single-use browser token without exposing the API key."""
    response = requests.post(
        f"{ELEVENLABS_API_BASE}/single-use-token/realtime_scribe",
        headers={"xi-api-key": api_key},
        timeout=timeout,
    )
    if response.status_code != 200:
        _raise_for_upstream_error(response, "stt")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ElevenLabsAPIError(
            "elevenlabs_invalid_response",
            "ElevenLabs 没有返回有效的实时语音识别凭证，请稍后重试。",
        ) from exc
    token = str((payload or {}).get("token") or "").strip()
    if not token:
        raise ElevenLabsAPIError(
            "elevenlabs_invalid_response",
            "ElevenLabs 没有返回有效的实时语音识别凭证，请稍后重试。",
        )
    return token


def normalize_scribe_transcript(text: str) -> str:
    """Localize common Scribe audio-event tags while retaining spoken text."""
    value = str(text or "").strip()

    def replace_event(match):
        label = re.sub(r"[\s_-]+", " ", match.group("label").strip().lower())
        localized = _AUDIO_EVENT_LABELS.get(label)
        return f"（{localized}）" if localized else match.group(0)

    value = re.sub(
        r"(?P<open>[\[(])(?P<label>[A-Za-z][A-Za-z\s_-]{1,40})(?P<close>[\])])",
        replace_event,
        value,
    )
    value = re.sub(r"（(?:silence|静音)）", "", value, flags=re.IGNORECASE)
    value = re.sub(r"(（[^（）]{1,16}）)(?:\s*\1)+", r"\1", value)
    value = re.sub(r"\s+([，。！？、,.!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def scribe_has_spoken_text(text: str) -> bool:
    """Return false when a Scribe result contains only audio-event labels."""
    value = str(text or "")
    value = re.sub(r"（[^（）]{1,40}）", "", value)
    value = re.sub(r"\([^()]{1,40}\)", "", value)
    value = re.sub(r"\[[^\[\]]{1,40}\]", "", value)
    return bool(re.search(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", value))


def transcribe_speech(
    api_key: str,
    *,
    filename: str,
    audio_bytes: bytes,
    content_type: str,
    model_id: str = "scribe_v2",
    timeout: int = 120,
) -> str:
    """Transcribe one recorded voice message using ElevenLabs Scribe."""
    response = requests.post(
        f"{ELEVENLABS_API_BASE}/speech-to-text",
        headers={"xi-api-key": api_key},
        files=[("file", (filename, audio_bytes, content_type))],
        data={
            "model_id": model_id,
            "tag_audio_events": "true",
            "diarize": "false",
            "timestamps_granularity": "none",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        _raise_for_upstream_error(response, "stt")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ElevenLabsAPIError(
            "elevenlabs_invalid_response",
            "ElevenLabs 返回了无法识别的转写结果，请稍后重试。",
        ) from exc
    transcript = normalize_scribe_transcript((payload or {}).get("text"))
    if not transcript:
        raise ElevenLabsAPIError(
            "elevenlabs_empty_transcript",
            "没有识别到清晰的说话内容，请靠近麦克风后重试。",
            400,
        )
    return transcript
