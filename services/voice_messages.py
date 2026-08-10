"""Persistence and tag helpers for user-recorded voice messages."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from core.config import COS_BASE_URL, USERS_ROOT
from cos_utils import delete_from_cos, upload_to_cos


VOICE_MESSAGE_TAG_RE = re.compile(
    r"^\[voice_message\]\((?P<filename>"
    r"[A-Za-z0-9_-]+\.(?:webm|m4a|mp4|ogg|wav))\)"
    r"\((?P<transcript>[\s\S]*)\)$",
    re.IGNORECASE,
)
VOICE_FILENAME_RE = re.compile(
    r"^vm_[a-f0-9]{32}\.(?:webm|m4a|mp4|ogg|wav)$", re.IGNORECASE
)


class VoiceMessageError(ValueError):
    pass


def parse_voice_message_tag(content: str) -> dict | None:
    match = VOICE_MESSAGE_TAG_RE.fullmatch(str(content or "").strip())
    if not match:
        return None
    return {
        "filename": match.group("filename"),
        "transcript": match.group("transcript").strip(),
    }


def build_voice_message_tag(filename: str, transcript: str) -> str:
    if not VOICE_FILENAME_RE.fullmatch(str(filename or "")):
        raise VoiceMessageError("invalid voice filename")
    clean_text = re.sub(r"\s+", " ", str(transcript or "")).strip()
    # Slash is the existing message-bubble delimiter. Preserve its meaning as text.
    clean_text = clean_text.replace("/", "／")
    if not clean_text:
        raise VoiceMessageError("empty transcript")
    return f"[voice_message]({filename})({clean_text})"


def voice_message_for_ai(content: str) -> str:
    parsed = parse_voice_message_tag(content)
    if not parsed:
        return content
    return f"[用户语音] {parsed['transcript']}"


def _user_dir(user_id) -> str:
    return os.path.join(USERS_ROOT, str(int(user_id)), "voice_messages")


def _db_path(user_id) -> str:
    return os.path.join(_user_dir(user_id), "media.db")


def _local_path(user_id, filename: str) -> str:
    return os.path.join(_user_dir(user_id), filename)


def _connect(user_id) -> sqlite3.Connection:
    root = _user_dir(user_id)
    os.makedirs(root, exist_ok=True)
    conn = sqlite3.connect(_db_path(user_id))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_messages (
            filename TEXT PRIMARY KEY,
            object_key TEXT NOT NULL,
            storage_backend TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            transcript TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            message_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_stt_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket TEXT NOT NULL DEFAULT 'batch',
            created_at REAL NOT NULL
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(voice_stt_usage)").fetchall()
    }
    if "bucket" not in columns:
        conn.execute(
            "ALTER TABLE voice_stt_usage ADD COLUMN bucket TEXT NOT NULL DEFAULT 'batch'"
        )
    conn.commit()
    return conn


def consume_voice_stt_quota(
    user_id, *, limit: int = 60, window_seconds: int = 3600, bucket: str = "batch"
) -> bool:
    """Atomically consume one STT attempt from a per-user sliding window."""
    if int(limit) <= 0:
        return True
    quota_bucket = str(bucket or "batch").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", quota_bucket):
        raise VoiceMessageError("invalid STT quota bucket")
    now = time.time()
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM voice_stt_usage WHERE bucket = ? AND created_at < ?",
            (quota_bucket, now - int(window_seconds)),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM voice_stt_usage WHERE bucket = ?",
            (quota_bucket,),
        ).fetchone()[0]
        if int(count) >= int(limit):
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO voice_stt_usage (bucket, created_at) VALUES (?, ?)",
            (quota_bucket, now),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def new_voice_filename(extension: str) -> str:
    ext = str(extension or "").lower().lstrip(".")
    if ext not in {"webm", "m4a", "mp4", "ogg", "wav"}:
        raise VoiceMessageError("unsupported voice extension")
    return f"vm_{uuid.uuid4().hex}.{ext}"


def save_voice_message(
    user_id,
    *,
    filename: str,
    audio_bytes: bytes,
    mime_type: str,
    duration_ms: int,
    transcript: str,
    scope_type: str,
    scope_id: str,
) -> dict:
    if not VOICE_FILENAME_RE.fullmatch(filename):
        raise VoiceMessageError("invalid voice filename")
    local_path = _local_path(user_id, filename)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    part_path = f"{local_path}.part"
    with open(part_path, "wb") as output:
        output.write(audio_bytes)
    os.replace(part_path, local_path)

    object_key = f"users/{int(user_id)}/voices/{filename}"
    storage_backend = "local"
    if COS_BASE_URL:
        uploaded_url = upload_to_cos(local_path, object_key)
        if uploaded_url:
            storage_backend = "cos"
            try:
                os.remove(local_path)
            except OSError:
                pass

    conn = _connect(user_id)
    try:
        conn.execute(
            """
            INSERT INTO voice_messages (
                filename, object_key, storage_backend, mime_type,
                duration_ms, size_bytes, transcript, scope_type, scope_id,
                message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                filename,
                object_key,
                storage_backend,
                mime_type,
                int(duration_ms),
                len(audio_bytes),
                transcript,
                scope_type,
                scope_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_voice_message(user_id, filename)


def get_voice_message(user_id, filename: str) -> dict | None:
    if not VOICE_FILENAME_RE.fullmatch(str(filename or "")):
        return None
    conn = _connect(user_id)
    try:
        row = conn.execute(
            "SELECT * FROM voice_messages WHERE filename = ?", (filename,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def validate_voice_message_for_scope(
    user_id, content: str, *, scope_type: str, scope_id: str, message_id=None
) -> dict | None:
    parsed = parse_voice_message_tag(content)
    if not parsed:
        return None
    record = get_voice_message(user_id, parsed["filename"])
    if not record:
        raise VoiceMessageError("语音文件不存在或已失效")
    if record["scope_type"] != scope_type or record["scope_id"] != str(scope_id):
        raise VoiceMessageError("语音文件不属于当前会话")
    attached_id = record.get("message_id")
    if attached_id is not None and int(attached_id) != int(message_id or -1):
        raise VoiceMessageError("语音文件已经发送，不能重复使用")
    return record


def attach_voice_message(
    user_id, content: str, *, scope_type: str, scope_id: str, message_id: int
) -> None:
    parsed = parse_voice_message_tag(content)
    if not parsed:
        return
    validate_voice_message_for_scope(
        user_id,
        content,
        scope_type=scope_type,
        scope_id=scope_id,
        message_id=message_id,
    )
    conn = _connect(user_id)
    try:
        conn.execute(
            "UPDATE voice_messages SET message_id = ? WHERE filename = ?",
            (int(message_id), parsed["filename"]),
        )
        conn.commit()
    finally:
        conn.close()


def delete_voice_message_for_message(
    user_id, content: str, *, scope_type: str, scope_id: str, message_id: int
) -> bool:
    parsed = parse_voice_message_tag(content)
    if not parsed:
        return False
    record = get_voice_message(user_id, parsed["filename"])
    if not record:
        return False
    if (
        record["scope_type"] != scope_type
        or record["scope_id"] != str(scope_id)
        or int(record.get("message_id") or -1) != int(message_id)
    ):
        return False
    if record["storage_backend"] == "cos":
        delete_from_cos(record["object_key"])
    try:
        os.remove(_local_path(user_id, record["filename"]))
    except FileNotFoundError:
        pass
    conn = _connect(user_id)
    try:
        conn.execute(
            "DELETE FROM voice_messages WHERE filename = ?", (record["filename"],)
        )
        conn.commit()
    finally:
        conn.close()
    return True


def get_local_voice_path(user_id, filename: str) -> str | None:
    record = get_voice_message(user_id, filename)
    if not record or record["storage_backend"] != "local":
        return None
    path = _local_path(user_id, filename)
    return path if os.path.isfile(path) else None
