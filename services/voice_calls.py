"""Persistent state and tag helpers for one-to-one voice calls."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from core.config import USERS_ROOT


CALL_ID_RE = re.compile(r"^call_[a-f0-9]{32}$")
VOICE_CALL_TAG_RE = re.compile(r"^\[voice_call\]\((call_[a-f0-9]{32})\)$", re.I)
CALL_CONTROL_RE = re.compile(
    r"\[(CALL_USER|CALL_ACCEPT|CALL_REJECT|END_CALL)\]", re.I
)
CALL_USER_RE = re.compile(r"\[CALL_USER\]", re.I)
LIVE_STATUSES = ("ringing", "active")
END_REASONS = {
    "rejected", "missed", "canceled", "hangup", "character_hangup",
    "disconnected", "busy", "failed",
}


class VoiceCallError(ValueError):
    pass


class VoiceCallConflict(VoiceCallError):
    pass


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex}"


def build_voice_call_tag(call_id: str) -> str:
    if not CALL_ID_RE.fullmatch(str(call_id or "")):
        raise VoiceCallError("invalid call id")
    return f"[voice_call]({call_id})"


def parse_voice_call_tag(content: str) -> str | None:
    match = VOICE_CALL_TAG_RE.fullmatch(str(content or "").strip())
    return match.group(1) if match else None


def _voice_call_transcript(
    user_id,
    content: str,
    *,
    max_turns: int,
    max_chars: int,
    newest_first_window: bool,
) -> str:
    call_id = parse_voice_call_tag(content)
    if not call_id or user_id is None:
        return content
    call = get_call(user_id, call_id)
    if not call:
        return "[语音通话记录已失效]"
    all_turns = list_turns(user_id, call_id)
    turn_limit = max(1, min(int(max_turns), 1000))
    turns = all_turns[-turn_limit:] if newest_first_window else all_turns[:turn_limit]
    lines = [
        ("用户" if turn["role"] == "user" else "角色") + "：" + turn["content"]
        for turn in turns
    ]
    if call["status"] == "active":
        status = "进行中"
    elif call["status"] == "ringing":
        status = "等待接听"
    else:
        reason = call.get("end_reason")
        ended_labels = {
            "rejected": "用户拒绝来电" if call["initiator"] == "assistant" else "角色拒绝来电",
            "missed": "用户未接听" if call["initiator"] == "assistant" else "角色未接听",
            "canceled": "用户取消拨号",
            "hangup": "用户挂断",
            "character_hangup": "角色挂断",
            "disconnected": "连接中断",
            "failed": "通话失败",
        }
        status = "已结束（" + ended_labels.get(reason, "通话结束") + "）"
    body = "\n".join(lines) if lines else "（没有产生通话台词）"
    if len(all_turns) > turn_limit:
        body += f"\n（通话过长，其余 {len(all_turns) - turn_limit} 条未显示）"
    return (f"[语音通话记录｜{status}]\n{body}")[:max(100, int(max_chars))]


def voice_call_for_ai(user_id, content: str, *, max_turns: int = 24) -> str:
    """Expand a call anchor into a recent, bounded transcript for prompt use."""
    return _voice_call_transcript(
        user_id,
        content,
        max_turns=max_turns,
        max_chars=4000,
        newest_first_window=True,
    )


def voice_call_for_record(user_id, content: str) -> str:
    """Expand a selected call anchor into a chronological export transcript."""
    return _voice_call_transcript(
        user_id,
        content,
        max_turns=500,
        max_chars=40000,
        newest_first_window=False,
    )


def _balanced_group(text: str, start: int) -> tuple[str, int] | None:
    """Read one ASCII-parenthesized group, allowing nested parentheses."""
    if start >= len(text) or text[start] != "(":
        return None
    depth = 1
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:cursor], cursor + 1
        cursor += 1
    return None


def normalize_legacy_voice_for_call(content: str) -> str:
    """Unwrap an accidental normal-chat voice tag in a call model response."""
    raw = str(content or "")
    marker = re.search(r"\[voice\]\s*", raw, flags=re.I)
    if not marker:
        return raw
    spoken_group = _balanced_group(raw, marker.end())
    if not spoken_group:
        return raw
    spoken, cursor = spoken_group
    while cursor < len(raw) and raw[cursor].isspace():
        cursor += 1
    tone_group = _balanced_group(raw, cursor)
    if tone_group:
        tone, end = tone_group
        replacement = f"[CALL_TONE]({tone}){spoken}"
    else:
        end = cursor
        replacement = spoken
    return raw[:marker.start()] + replacement + raw[end:]


def normalize_untagged_tone_for_call(content: str) -> str:
    """Recover a tone direction that the model accidentally wrote as prose."""
    raw = str(content or "")
    if re.search(r"\[CALL_TONE\]\(", raw, flags=re.I):
        return raw
    match = re.match(
        r"^(?P<prefix>(?:\s*\[(?:CALL_USER|CALL_ACCEPT|CALL_REJECT|END_CALL)\]\s*)*)"
        r"(?P<body>[\s\S]*)$",
        raw,
        flags=re.I,
    )
    prefix = match.group("prefix") if match else ""
    body = (match.group("body") if match else raw).strip()
    if not body:
        return raw

    tone = text = ""
    patterns = (
        r"^(?P<tone>[^\n]{2,120}?(?:声|口調|調子)(?:で|に))[\s:：]+(?P<text>\S[\s\S]*)$",
        r"^(?P<tone>(?:用|以|带着|压低|提高|轻声|低声)[^\n]{1,120}?(?:语气|声音|声线)(?:说|说道|地|着)?)[\s:：，,]+(?P<text>\S[\s\S]*)$",
        r"^(?P<tone>(?:in|with) [^\n]{1,120}?(?:voice|tone))[,\s:]+(?P<text>\S[\s\S]*)$",
    )
    for pattern in patterns:
        candidate = re.match(pattern, body, flags=re.I)
        if candidate:
            tone = candidate.group("tone").strip()
            text = candidate.group("text").strip()
            break

    cue_re = re.compile(
        r"声|口調|調子|囁|ささや|溜め息|ため息|苛立|優し|怒|笑|"
        r"语气|語氣|声音|聲音|声线|聲線|低声|轻声|压低|"
        r"voice|tone|whisper|sigh",
        flags=re.I,
    )
    if not tone:
        parenthetical = re.match(r"^[（(]([^（）()]{2,120})[）)]\s*(\S[\s\S]*)$", body)
        if parenthetical and cue_re.search(parenthetical.group(1)):
            tone, text = parenthetical.group(1).strip(), parenthetical.group(2).strip()
    if not tone and "\n" in body:
        first, remainder = body.split("\n", 1)
        if len(first.strip()) <= 120 and cue_re.search(first) and remainder.strip():
            tone, text = first.strip(" ：:"), remainder.strip()

    if not tone or not text:
        return raw
    tone = re.sub(r"\s+", " ", tone).replace("[", "").replace("]", "")[:100]
    return f"{prefix}[CALL_TONE]({tone}){text}"


def parse_call_model_output(content: str) -> dict:
    """Extract phone control tags and one balanced natural-language tone tag."""
    raw = str(content or "")
    controls = [match.group(1).upper() for match in CALL_CONTROL_RE.finditer(raw)]
    cleaned = CALL_CONTROL_RE.sub("", raw)
    tone = ""
    marker_match = re.search(r"\[CALL_TONE\]\(", cleaned, flags=re.I)
    if marker_match:
        start = marker_match.end()
        depth = 1
        cursor = start
        while cursor < len(cleaned) and depth:
            char = cleaned[cursor]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        if depth == 0:
            tone = re.sub(r"\s+", " ", cleaned[start:cursor - 1]).strip()
            tone = tone.replace("[", "").replace("]", "")[:100]
            cleaned = cleaned[:marker_match.start()] + cleaned[cursor:]
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" /\r\n\t")
    return {"controls": controls, "tone": tone, "text": cleaned}


def consume_call_user_tag(content: str) -> tuple[str, bool]:
    """Remove proactive-call action tags from a normal character reply."""
    raw = str(content or "")
    requested = bool(CALL_USER_RE.search(raw))
    cleaned = CALL_USER_RE.sub("", raw)
    cleaned = re.sub(r"\s*\/\s*(?=\/|$)", "", cleaned).strip(" /\r\n\t")
    return cleaned, requested


def tts_text_with_tone(text: str, tone: str) -> str:
    spoken = str(text or "").strip()
    direction = re.sub(r"\s+", " ", str(tone or "")).strip()
    direction = direction.replace("[", "").replace("]", "")[:100]
    return f"[{direction}]\n{spoken}" if direction else spoken


def normalize_call_spoken_text(text: str) -> str:
    """Turn ordinary chat slash separators into natural spoken punctuation."""
    spoken = re.sub(r"\s*/\s*", "。", str(text or ""))
    spoken = re.sub(r"。{2,}", "。", spoken)
    return re.sub(r"\s+", " ", spoken).strip(" 。\r\n\t")


def _root(user_id) -> str:
    return os.path.join(USERS_ROOT, str(int(user_id)), "voice_calls")


def _db_path(user_id) -> str:
    return os.path.join(_root(user_id), "calls.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(user_id) -> sqlite3.Connection:
    os.makedirs(_root(user_id), exist_ok=True)
    conn = sqlite3.connect(_db_path(user_id), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_calls (
            call_id TEXT PRIMARY KEY,
            char_id TEXT NOT NULL,
            initiator TEXT NOT NULL CHECK (initiator IN ('user', 'assistant')),
            status TEXT NOT NULL CHECK (status IN ('ringing', 'active', 'ended')),
            end_reason TEXT,
            anchor_message_id INTEGER,
            opening_text TEXT NOT NULL DEFAULT '',
            opening_tone TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            answered_at TEXT,
            ended_at TEXT,
            heartbeat_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_call_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            tone TEXT NOT NULL DEFAULT '',
            client_turn_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (call_id) REFERENCES voice_calls(call_id) ON DELETE CASCADE,
            UNIQUE (call_id, sequence),
            UNIQUE (call_id, client_turn_id)
        )
        """
    )
    conn.commit()
    return conn


def _row_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def create_call(
    user_id,
    *,
    call_id: str,
    char_id: str,
    initiator: str,
    anchor_message_id: int | None = None,
) -> dict:
    if not CALL_ID_RE.fullmatch(str(call_id or "")):
        raise VoiceCallError("invalid call id")
    if initiator not in {"user", "assistant"}:
        raise VoiceCallError("invalid initiator")
    now = _now_iso()
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute(
            "SELECT call_id FROM voice_calls WHERE status IN ('ringing', 'active') LIMIT 1"
        ).fetchone()
        if live:
            conn.rollback()
            raise VoiceCallConflict(str(live["call_id"]))
        conn.execute(
            """
            INSERT INTO voice_calls (
                call_id, char_id, initiator, status, end_reason,
                anchor_message_id, created_at, heartbeat_at
            ) VALUES (?, ?, ?, 'ringing', NULL, ?, ?, ?)
            """,
            (call_id, str(char_id), initiator, anchor_message_id, now, now),
        )
        conn.commit()
        return get_call(user_id, call_id)
    finally:
        conn.close()


def get_call(user_id, call_id: str) -> dict | None:
    if not CALL_ID_RE.fullmatch(str(call_id or "")):
        return None
    conn = _connect(user_id)
    try:
        return _row_dict(
            conn.execute("SELECT * FROM voice_calls WHERE call_id = ?", (call_id,)).fetchone()
        )
    finally:
        conn.close()


def get_live_call(user_id) -> dict | None:
    expire_stale_ringing_calls(user_id)
    expire_stale_active_calls(user_id)
    conn = _connect(user_id)
    try:
        return _row_dict(
            conn.execute(
                """
                SELECT * FROM voice_calls
                WHERE status IN ('ringing', 'active')
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        )
    finally:
        conn.close()


def set_opening(user_id, call_id: str, *, text: str = "", tone: str = "") -> dict:
    conn = _connect(user_id)
    try:
        conn.execute(
            "UPDATE voice_calls SET opening_text = ?, opening_tone = ? WHERE call_id = ?",
            (str(text or "")[:4000], str(tone or "")[:100], call_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_call(user_id, call_id)


def accept_call(user_id, call_id: str) -> dict:
    now = _now_iso()
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM voice_calls WHERE call_id = ?", (call_id,)).fetchone()
        if not row:
            conn.rollback()
            raise VoiceCallError("call not found")
        if row["status"] == "active":
            conn.commit()
            return dict(row)
        if row["status"] != "ringing":
            conn.rollback()
            raise VoiceCallConflict("call is not ringing")
        conn.execute(
            """
            UPDATE voice_calls
            SET status = 'active', answered_at = ?, heartbeat_at = ?
            WHERE call_id = ?
            """,
            (now, now, call_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_call(user_id, call_id)


def end_call(user_id, call_id: str, *, reason: str) -> dict:
    if reason not in END_REASONS:
        raise VoiceCallError("invalid end reason")
    now = _now_iso()
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM voice_calls WHERE call_id = ?", (call_id,)).fetchone()
        if not row:
            conn.rollback()
            raise VoiceCallError("call not found")
        if row["status"] != "ended":
            conn.execute(
                """
                UPDATE voice_calls
                SET status = 'ended', end_reason = ?, ended_at = ?, heartbeat_at = ?
                WHERE call_id = ?
                """,
                (reason, now, now, call_id),
            )
        conn.commit()
    finally:
        conn.close()
    return get_call(user_id, call_id)


def touch_call(user_id, call_id: str) -> dict:
    now = _now_iso()
    conn = _connect(user_id)
    try:
        changed = conn.execute(
            "UPDATE voice_calls SET heartbeat_at = ? WHERE call_id = ? AND status = 'active'",
            (now, call_id),
        ).rowcount
        conn.commit()
        if not changed:
            raise VoiceCallConflict("call is not active")
    finally:
        conn.close()
    return get_call(user_id, call_id)


def append_turn(
    user_id,
    call_id: str,
    *,
    role: str,
    content: str,
    tone: str = "",
    client_turn_id: str | None = None,
) -> dict:
    if role not in {"user", "assistant"}:
        raise VoiceCallError("invalid turn role")
    clean_content = re.sub(r"\s+", " ", str(content or "")).strip()
    if not clean_content or len(clean_content) > 4000:
        raise VoiceCallError("invalid turn content")
    turn_key = str(client_turn_id or "").strip()[:80] or None
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        call = conn.execute("SELECT status FROM voice_calls WHERE call_id = ?", (call_id,)).fetchone()
        if not call or call["status"] != "active":
            conn.rollback()
            raise VoiceCallConflict("call is not active")
        if turn_key:
            existing = conn.execute(
                "SELECT * FROM voice_call_turns WHERE call_id = ? AND client_turn_id = ?",
                (call_id, turn_key),
            ).fetchone()
            if existing:
                conn.commit()
                return dict(existing)
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM voice_call_turns WHERE call_id = ?",
            (call_id,),
        ).fetchone()[0]
        cursor = conn.execute(
            """
            INSERT INTO voice_call_turns (
                call_id, sequence, role, content, tone, client_turn_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (call_id, sequence, role, clean_content, str(tone or "")[:100], turn_key, _now_iso()),
        )
        conn.commit()
        return _row_dict(
            conn.execute("SELECT * FROM voice_call_turns WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )
    finally:
        conn.close()


def list_turns(user_id, call_id: str) -> list[dict]:
    conn = _connect(user_id)
    try:
        return [
            dict(row) for row in conn.execute(
                "SELECT * FROM voice_call_turns WHERE call_id = ? ORDER BY sequence ASC",
                (call_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def delete_calls_for_character(user_id, char_id: str) -> int:
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT COUNT(*) FROM voice_calls WHERE char_id = ?", (str(char_id),)
        ).fetchone()[0]
        conn.execute("DELETE FROM voice_calls WHERE char_id = ?", (str(char_id),))
        conn.commit()
        return int(rows)
    finally:
        conn.close()


def expire_stale_ringing_calls(user_id, *, timeout_seconds: int = 30) -> int:
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat(
        timespec="seconds"
    )
    now = _now_iso()
    conn = _connect(user_id)
    try:
        changed = conn.execute(
            """
            UPDATE voice_calls
            SET status = 'ended', end_reason = 'missed', ended_at = ?, heartbeat_at = ?
            WHERE status = 'ringing' AND created_at < ?
            """,
            (now, now, threshold),
        ).rowcount
        conn.commit()
        return int(changed)
    finally:
        conn.close()


def expire_stale_active_calls(user_id, *, timeout_seconds: int = 45) -> list[dict]:
    """End calls whose browser heartbeat disappeared and return affected calls."""
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat(
        timespec="seconds"
    )
    now = _now_iso()
    conn = _connect(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM voice_calls WHERE status = 'active' AND heartbeat_at < ?",
            (threshold,),
        ).fetchall()
        if rows:
            conn.execute(
                """
                UPDATE voice_calls
                SET status = 'ended', end_reason = 'disconnected', ended_at = ?, heartbeat_at = ?
                WHERE status = 'active' AND heartbeat_at < ?
                """,
                (now, now, threshold),
            )
        conn.commit()
        return [dict(row) for row in rows]
    finally:
        conn.close()
