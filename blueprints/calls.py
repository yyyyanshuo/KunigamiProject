"""Authenticated one-to-one voice-call state API."""

import json
import os
import re
import sqlite3
import requests

from flask import Blueprint, jsonify, redirect, render_template, request

from core.context import get_current_user_id
from core.credentials import (
    CredentialConfigurationError,
    CredentialError,
    get_user_credential,
)
from core.time_utils import beijing_now
from core.utils import _get_characters_config_file, get_paths
from services.voice_calls import (
    VoiceCallConflict,
    VoiceCallError,
    accept_call,
    append_turn,
    build_voice_call_tag,
    create_call,
    end_call,
    expire_stale_active_calls,
    expire_stale_ringing_calls,
    get_call,
    get_live_call,
    list_turns,
    new_call_id,
    normalize_call_spoken_text,
    normalize_legacy_voice_for_call,
    normalize_untagged_tone_for_call,
    parse_call_model_output,
    set_opening,
    touch_call,
    voice_call_for_record,
)
from services.ai_client import call_gemini, call_openrouter, get_model_config
from services.prompt_builder import build_system_prompt_v2
from agent_utils import process_agent_actions
import core.config
from services.elevenlabs import (
    ElevenLabsAPIError,
    create_realtime_scribe_token,
    scribe_has_spoken_text,
    transcribe_speech,
)
from services.voice_messages import consume_voice_stt_quota


calls_bp = Blueprint("calls", __name__)
CHAR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
CALL_AUDIO_MIME_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


def _ajax_error():
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "请求来源校验失败", "code": "request_origin_invalid"}), 403
    return None


def _current_uid():
    uid = get_current_user_id()
    return int(uid) if uid else None


def _call_tts_capability(uid: int, char_id: str) -> tuple[bool, str]:
    """Check optional TTS capability without blocking text-only calls."""
    try:
        key = get_user_credential(uid, "elevenlabs", allow_legacy=False)
    except (CredentialConfigurationError, CredentialError):
        return False, "elevenlabs_key_unavailable"
    if not key:
        return False, "elevenlabs_key_missing"
    path = _get_characters_config_file(user_id=uid)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            voice_id = str((json.load(handle).get(char_id) or {}).get("voice_id") or "").strip()
    except (OSError, ValueError, TypeError):
        voice_id = ""
    if not VOICE_ID_RE.fullmatch(voice_id):
        return False, "voice_id_missing_or_invalid"
    return True, ""


def _character_exists(char_id: str) -> bool:
    path = _get_characters_config_file()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return char_id in payload
    except (OSError, ValueError, TypeError):
        return False


def _character_info(uid: int, char_id: str) -> dict:
    path = _get_characters_config_file(user_id=uid)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            info = (json.load(handle).get(char_id) or {})
    except (OSError, ValueError, TypeError):
        info = {}
    return {
        "id": char_id,
        "name": info.get("name") or char_id,
        "remark": info.get("remark") or info.get("name") or char_id,
        "avatar": info.get("avatar") or f"/char_assets/{char_id}/avatar.png",
        "language": info.get("ai_language") or "ja",
    }


def _set_call_chat_mode(uid: int, char_id: str, mode: str) -> None:
    try:
        process_agent_actions(char_id, f"[SET_CHAT_MODE:{mode}]", uid)
    except Exception as exc:
        print(f"[Voice Call] failed to set chat mode {mode}: {exc}")


def _cleanup_stale_active_calls(uid: int) -> None:
    for stale in expire_stale_active_calls(uid):
        _set_call_chat_mode(uid, stale["char_id"], "online")


def create_incoming_call_for_character(uid: int, char_id: str):
    """Create a character-initiated call after a model emits [CALL_USER]."""
    if get_live_call(uid):
        return None, "call_already_live"
    call_id = new_call_id()
    anchor_id = _insert_call_anchor(uid, char_id, call_id, "assistant")
    try:
        call = create_call(
            uid, call_id=call_id, char_id=char_id, initiator="assistant",
            anchor_message_id=anchor_id,
        )
    except VoiceCallConflict:
        _delete_call_anchor(uid, char_id, anchor_id)
        return None, "call_already_live"

    try:
        path = _get_characters_config_file(user_id=uid)
        with open(path, "r", encoding="utf-8") as handle:
            char_name = (json.load(handle).get(char_id) or {}).get("name") or char_id
        from blueprints.auth import send_push_notification
        tts_enabled, _ = _call_tts_capability(uid, char_id)
        send_push_notification(
            f"{char_name} 给你打来{'语音' if tts_enabled else ''}电话",
            "点击接听或拒绝",
            url=f"/call/{call_id}",
            user_id=uid,
        )
    except Exception as exc:
        print(f"[Voice Call] push notification failed: {exc}")
    return call, None


def _ensure_chat_db(char_id: str, uid: int) -> str:
    db_path, _ = get_paths(char_id, user_id=uid)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_call_anchor(uid: int, char_id: str, call_id: str, role: str) -> int:
    db_path = _ensure_chat_db(char_id, uid)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, build_voice_call_tag(call_id), beijing_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_chat_message(uid: int, char_id: str, role: str, content: str) -> int | None:
    clean = str(content or "").strip()
    if not clean:
        return None
    db_path = _ensure_chat_db(char_id, uid)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, clean[:4000], beijing_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _recent_chat_text(uid: int, char_id: str, limit: int = 24) -> list[str]:
    db_path = _ensure_chat_db(char_id, uid)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT content FROM messages ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [str(row[0]) for row in reversed(rows)]
    finally:
        conn.close()


def _run_call_model(uid: int, char_id: str, messages: list[dict]):
    route, model = get_model_config("call", user_id=uid)
    if route == "relay":
        result = call_openrouter(
            messages, char_id=char_id, model_name=model, user_id=uid,
            max_tokens=800, temperature=0.9,
        )
    else:
        result = call_gemini(
            messages, char_id=char_id, model_name=model, user_id=uid,
            temperature=0.9,
        )
    return str(result or "").strip(), route, model


def _delete_call_anchor(uid: int, char_id: str, message_id: int) -> None:
    db_path, _ = get_paths(char_id, user_id=uid)
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        conn.commit()
    finally:
        conn.close()


def _call_or_404(uid, call_id):
    expire_stale_ringing_calls(uid)
    call = get_call(uid, call_id)
    if not call:
        return None, (jsonify({"error": "通话不存在", "code": "call_not_found"}), 404)
    return call, None


@calls_bp.route("/api/calls", methods=["POST"])
def create_outgoing_call():
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    if not CHAR_ID_RE.fullmatch(char_id) or not _character_exists(char_id):
        return jsonify({"error": "角色不存在", "code": "character_not_found"}), 404
    live = get_live_call(uid)
    if live:
        return jsonify({
            "error": "当前已有来电或通话",
            "code": "call_already_live",
            "call": live,
        }), 409
    call_id = new_call_id()
    anchor_id = _insert_call_anchor(uid, char_id, call_id, "user")
    try:
        call = create_call(
            uid,
            call_id=call_id,
            char_id=char_id,
            initiator="user",
            anchor_message_id=anchor_id,
        )
    except VoiceCallConflict:
        _delete_call_anchor(uid, char_id, anchor_id)
        return jsonify({"error": "当前已有来电或通话", "code": "call_already_live"}), 409
    return jsonify({"status": "success", "call": call}), 201


@calls_bp.route("/api/calls/pending", methods=["GET"])
def pending_call():
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    _cleanup_stale_active_calls(uid)
    return jsonify({"call": get_live_call(uid)})


@calls_bp.route("/api/calls/resolve-records", methods=["POST"])
def resolve_call_records():
    """Resolve selected call anchors to transcripts owned by the current user."""
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    data = request.get_json(silent=True) or {}
    contents = data.get("contents")
    if not isinstance(contents, list) or len(contents) > 100:
        return jsonify({"error": "消息列表格式错误", "code": "invalid_contents"}), 400
    if any(not isinstance(content, str) or len(content) > 100000 for content in contents):
        return jsonify({"error": "消息内容格式错误", "code": "invalid_content"}), 400
    return jsonify({
        "contents": [voice_call_for_record(uid, content) for content in contents]
    })


@calls_bp.route("/api/calls/<call_id>", methods=["GET"])
def call_detail(call_id):
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    _cleanup_stale_active_calls(uid)
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    call["turns"] = list_turns(uid, call_id)
    call["character"] = _character_info(uid, call["char_id"])
    tts_enabled, unavailable_reason = _call_tts_capability(uid, call["char_id"])
    call["tts_enabled"] = tts_enabled
    call["call_mode"] = "voice" if tts_enabled else "voice_input_text_reply"
    call["tts_unavailable_reason"] = unavailable_reason
    return jsonify({"call": call})


@calls_bp.route("/call/<call_id>", methods=["GET"])
def call_page(call_id):
    uid = _current_uid()
    if not uid:
        return redirect("/login")
    call = get_call(uid, call_id)
    if not call:
        return redirect("/")
    return render_template("call.html", call_id=call_id)


@calls_bp.route("/api/calls/<call_id>/decision", methods=["POST"])
def decide_outgoing_call(call_id):
    """Let the character decide whether to answer a user-initiated call."""
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["initiator"] != "user":
        return jsonify({"error": "该来电不需要角色决策", "code": "call_not_outgoing"}), 409
    if call["status"] != "ringing":
        return jsonify({"status": "success", "call": call, "decision": call["status"]})

    prompt = (
        "用户现在正在给你拨打语音电话。请严格按照你完整的人设、你与用户的关系、"
        "当前情绪、当前时间与状态，自主决定是否接听；不要为了配合系统而一律接听。\n"
        "只允许二选一：\n"
        "1. 接听：输出 [CALL_ACCEPT]。可以再输出一条接通后立刻说的话；若有台词，"
        "可在前面输出 [CALL_TONE](自然语言语气描述)。语气说明不是台词，只能写在该标签内，"
        "绝不能以普通正文形式写出“用某种声音／某种声で”等描述。\n"
        "2. 拒绝：输出 [CALL_REJECT]。后面可以写一句简短自然的拒绝原因，这句话会作为普通聊天消息发给用户。\n"
        "不要输出 [CALL_USER] 或 [END_CALL]，不要解释标签规则。"
    )
    system_prompt = build_system_prompt_v2(
        call["char_id"], include_global_format=True,
        recent_messages=_recent_chat_text(uid, call["char_id"]),
        user_id=uid,
        call_mode=True,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    try:
        raw, route, model = _run_call_model(uid, call["char_id"], messages)
    except Exception as exc:
        failed = end_call(uid, call_id, reason="failed")
        return jsonify({
            "error": "角色接听决策失败", "code": "call_decision_failed",
            "detail": str(exc), "call": failed,
        }), 502

    normalized = normalize_legacy_voice_for_call(raw)
    parsed = parse_call_model_output(normalize_untagged_tone_for_call(normalized))
    try:
        parsed["text"], _, _ = process_agent_actions(
            call["char_id"], parsed["text"], uid
        )
        parsed["text"] = normalize_call_spoken_text(parsed["text"])
    except Exception:
        pass
    decisions = {item for item in parsed["controls"] if item in {"CALL_ACCEPT", "CALL_REJECT"}}
    if len(decisions) != 1:
        failed = end_call(uid, call_id, reason="failed")
        return jsonify({
            "error": "角色没有返回有效的接听或拒绝标签",
            "code": "call_decision_invalid", "call": failed,
            "model": model, "route": route,
        }), 502

    decision = next(iter(decisions))
    if decision == "CALL_REJECT":
        if parsed["text"]:
            _insert_chat_message(uid, call["char_id"], "assistant", parsed["text"])
        rejected = end_call(uid, call_id, reason="rejected")
        return jsonify({
            "status": "success", "decision": "rejected", "call": rejected,
            "message": parsed["text"], "model": model, "route": route,
        })

    set_opening(uid, call_id, text=parsed["text"], tone=parsed["tone"])
    active = accept_call(uid, call_id)
    _set_call_chat_mode(uid, call["char_id"], "offline")
    opening_turn = None
    if parsed["text"]:
        opening_turn = append_turn(
            uid, call_id, role="assistant", content=parsed["text"],
            tone=parsed["tone"], client_turn_id="character-opening",
        )
    return jsonify({
        "status": "success", "decision": "accepted", "call": active,
        "opening_turn": opening_turn, "model": model, "route": route,
    })


@calls_bp.route("/api/calls/<call_id>/accept", methods=["POST"])
def accept_incoming_call(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["initiator"] != "assistant":
        return jsonify({"error": "该通话不是角色来电", "code": "call_not_incoming"}), 409
    try:
        call = accept_call(uid, call_id)
    except VoiceCallConflict as exc:
        return jsonify({"error": str(exc), "code": "call_state_conflict"}), 409
    _set_call_chat_mode(uid, call["char_id"], "offline")
    return jsonify({"status": "success", "call": call})


@calls_bp.route("/api/calls/<call_id>/reject", methods=["POST"])
def reject_incoming_call(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["initiator"] != "assistant" or call["status"] != "ringing":
        return jsonify({"error": "当前来电无法拒绝", "code": "call_state_conflict"}), 409
    return jsonify({"status": "success", "call": end_call(uid, call_id, reason="rejected")})


@calls_bp.route("/api/calls/<call_id>/cancel", methods=["POST"])
def cancel_outgoing_call(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["initiator"] != "user" or call["status"] != "ringing":
        return jsonify({"error": "当前通话无法取消", "code": "call_state_conflict"}), 409
    return jsonify({"status": "success", "call": end_call(uid, call_id, reason="canceled")})


@calls_bp.route("/api/calls/<call_id>/end", methods=["POST"])
def hang_up_call(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["status"] == "ringing":
        reason = "canceled" if call["initiator"] == "user" else "rejected"
    else:
        reason = "hangup"
    ended = end_call(uid, call_id, reason=reason)
    if call["status"] == "active":
        _set_call_chat_mode(uid, call["char_id"], "online")
    return jsonify({"status": "success", "call": ended})


@calls_bp.route("/api/calls/<call_id>/character-end", methods=["POST"])
def complete_character_hangup(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["status"] != "active":
        return jsonify({"status": "success", "call": call})
    ended = end_call(uid, call_id, reason="character_hangup")
    _set_call_chat_mode(uid, call["char_id"], "online")
    return jsonify({"status": "success", "call": ended})


@calls_bp.route("/api/calls/<call_id>/turn", methods=["POST"])
def create_call_turn(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["status"] != "active":
        return jsonify({"error": "通话已结束", "code": "call_not_active"}), 409

    data = request.get_json(silent=True) or {}
    transcript = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
    client_turn_id = str(data.get("client_turn_id") or "").strip()[:64]
    if not transcript or len(transcript) > 2000:
        return jsonify({"error": "通话文字为空或过长", "code": "call_turn_invalid"}), 400
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_turn_id):
        return jsonify({"error": "通话轮次标识无效", "code": "call_turn_id_invalid"}), 400

    assistant_key = f"reply-{client_turn_id}"[:80]
    existing_turns = list_turns(uid, call_id)
    existing_reply = next(
        (turn for turn in existing_turns if turn.get("client_turn_id") == assistant_key),
        None,
    )
    if existing_reply:
        from services.voice_calls import tts_text_with_tone
        return jsonify({
            "status": "success", "user_turn": None,
            "assistant_turn": existing_reply,
            "tts_text": tts_text_with_tone(
                existing_reply["content"], existing_reply.get("tone")
            ),
            "end_after_speech": False,
        })

    try:
        user_turn = append_turn(
            uid, call_id, role="user", content=transcript,
            client_turn_id=client_turn_id,
        )
    except (VoiceCallConflict, VoiceCallError) as exc:
        return jsonify({"error": str(exc), "code": "call_turn_conflict"}), 409

    turns = list_turns(uid, call_id)
    recent_chat = _recent_chat_text(uid, call["char_id"])
    system_prompt = build_system_prompt_v2(
        call["char_id"], include_global_format=True,
        recent_messages=recent_chat, user_latest_input=transcript, user_id=uid,
        call_mode=True,
    )
    call_rules = (
        "你现在正与用户进行实时一对一语音电话。只回复适合直接说出口的内容，通常 1～3 句，"
        "自然、及时，不要使用斜线分段。普通聊天里的所有特殊消息格式在通话中均不可用；"
        "不要输出图片、表情、文件、转账、[voice] 或任何其他聊天气泡标签。\n"
        "若需要控制 Eleven v3 的说话方式，可在正文前输出一次 "
        "[CALL_TONE](任意自然语言语气描述)，描述可以细致、有动作感或包含括号。正文仍写正常台词。\n"
        "语气描述绝不是台词：只能存在于 [CALL_TONE](...) 内，禁止把它作为正文前缀输出；"
        "尤其不要写成“用……的声音说”“……声で”“in a ... voice”后再接台词。\n"
        "当你根据人设和当前状态想主动挂断时，在本轮加 [END_CALL]；可以先写最后一句台词。"
        "通话中不要输出 [CALL_USER]、[CALL_ACCEPT] 或 [CALL_REJECT]。"
    )
    messages = [{"role": "system", "content": system_prompt + "\n\n" + call_rules}]
    messages.extend(
        {"role": turn["role"], "content": turn["content"]}
        for turn in turns[-24:]
    )
    try:
        raw, route, model = _run_call_model(uid, call["char_id"], messages)
    except Exception as exc:
        return jsonify({
            "error": "角色回复生成失败", "code": "call_model_failed", "detail": str(exc)
        }), 502
    normalized = normalize_legacy_voice_for_call(raw)
    parsed = parse_call_model_output(normalize_untagged_tone_for_call(normalized))
    try:
        parsed["text"], _, _ = process_agent_actions(
            call["char_id"], parsed["text"], uid
        )
        parsed["text"] = normalize_call_spoken_text(parsed["text"])
    except Exception:
        pass
    should_end = "END_CALL" in parsed["controls"]
    if not parsed["text"] and not should_end:
        return jsonify({
            "error": "角色返回了空回复", "code": "call_model_empty",
            "model": model, "route": route,
        }), 502

    assistant_turn = None
    if parsed["text"]:
        assistant_turn = append_turn(
            uid, call_id, role="assistant", content=parsed["text"],
            tone=parsed["tone"], client_turn_id=assistant_key,
        )
    touch_call(uid, call_id)
    from services.voice_calls import tts_text_with_tone
    return jsonify({
        "status": "success", "user_turn": user_turn,
        "assistant_turn": assistant_turn,
        "tts_text": tts_text_with_tone(parsed["text"], parsed["tone"]),
        "end_after_speech": should_end, "model": model, "route": route,
    })


@calls_bp.route("/api/calls/<call_id>/tts", methods=["POST"])
def call_tts(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["status"] != "active":
        return jsonify({"error": "通话已结束", "code": "call_not_active"}), 409
    from blueprints.media import _character_tts_response
    return _character_tts_response(
        call["char_id"], model_id="eleven_v3", use_emotion=False
    )


@calls_bp.route("/api/calls/<call_id>/stt-token", methods=["POST"])
def call_stt_token(call_id):
    """Issue a call-scoped Scribe token using only the server STT key."""
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["status"] != "active":
        return jsonify({"error": "通话已结束", "code": "call_not_active"}), 409
    api_key = str(core.config.ELEVENLABS_API_KEY or "").strip()
    if not api_key:
        return jsonify({
            "error": "服务器尚未配置 ElevenLabs Speech-to-Text Key",
            "code": "elevenlabs_stt_not_configured",
        }), 503
    limit = max(0, int(os.getenv("VOICE_CALL_STT_SESSIONS_PER_HOUR", "240") or 240))
    if not consume_voice_stt_quota(
        uid, limit=limit, window_seconds=3600, bucket="call-realtime"
    ):
        return jsonify({
            "error": "通话语音识别请求过于频繁，请稍后再试",
            "code": "voice_call_stt_rate_limited",
        }), 429
    try:
        token = create_realtime_scribe_token(api_key)
    except ElevenLabsAPIError as exc:
        return jsonify({"error": exc.user_message, "code": exc.code}), exc.http_status
    except requests.RequestException:
        return jsonify({
            "error": "无法连接 ElevenLabs，请稍后重试",
            "code": "elevenlabs_unreachable",
        }), 502
    response = jsonify({
        "token": token, "model_id": "scribe_v2_realtime",
        "audio_format": "pcm_16000",
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@calls_bp.route("/api/calls/<call_id>/transcribe", methods=["POST"])
def call_transcribe(call_id):
    """Batch-transcribe one in-memory utterance without persisting its audio."""
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    if call["status"] != "active":
        return jsonify({"error": "通话已结束", "code": "call_not_active"}), 409
    api_key = str(core.config.ELEVENLABS_API_KEY or "").strip()
    if not api_key:
        return jsonify({
            "error": "服务器尚未配置 ElevenLabs Speech-to-Text Key",
            "code": "elevenlabs_stt_not_configured",
        }), 503

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "没有收到录音", "code": "call_audio_missing"}), 400
    content_type = str(uploaded.mimetype or "").lower().split(";", 1)[0].strip()
    extension = CALL_AUDIO_MIME_EXTENSIONS.get(content_type)
    if not extension:
        suffix = os.path.splitext(str(uploaded.filename or ""))[1].lower().lstrip(".")
        fallback_types = {
            "webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4",
            "ogg": "audio/ogg", "wav": "audio/wav",
        }
        content_type = fallback_types.get(suffix, "")
        extension = "m4a" if suffix == "mp4" else suffix
    if not content_type or extension not in {"webm", "m4a", "ogg", "wav"}:
        return jsonify({
            "error": "当前录音格式不受支持", "code": "call_audio_type_invalid",
        }), 400

    max_bytes = max(1024, int(os.getenv("VOICE_CALL_STT_MAX_BYTES", str(10 * 1024 * 1024))))
    audio_bytes = uploaded.read(max_bytes + 1)
    if not audio_bytes:
        return jsonify({"error": "录音为空", "code": "call_audio_empty"}), 400
    if len(audio_bytes) > max_bytes:
        return jsonify({"error": "单次录音过大", "code": "call_audio_too_large"}), 413
    limit = max(0, int(os.getenv("VOICE_CALL_BATCH_STT_PER_HOUR", "240") or 240))
    if not consume_voice_stt_quota(
        uid, limit=limit, window_seconds=3600, bucket="call-batch"
    ):
        return jsonify({
            "error": "通话完整识别请求过于频繁，请稍后再试",
            "code": "voice_call_batch_stt_rate_limited",
        }), 429

    try:
        transcript = transcribe_speech(
            api_key,
            filename=f"call.{extension}",
            audio_bytes=audio_bytes,
            content_type=content_type,
            model_id="scribe_v2",
        )
    except ElevenLabsAPIError as exc:
        return jsonify({"error": exc.user_message, "code": exc.code}), exc.http_status
    except requests.RequestException:
        return jsonify({
            "error": "无法连接 ElevenLabs，请稍后重试",
            "code": "elevenlabs_unreachable",
        }), 502
    response = jsonify({
        "status": "success",
        "transcript": transcript,
        "has_speech": scribe_has_spoken_text(transcript),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@calls_bp.route("/api/calls/<call_id>/heartbeat", methods=["POST"])
def call_heartbeat(call_id):
    invalid = _ajax_error()
    if invalid:
        return invalid
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    try:
        call = touch_call(uid, call_id)
    except VoiceCallConflict as exc:
        return jsonify({"error": str(exc), "code": "call_state_conflict"}), 409
    return jsonify({"status": "success", "call": call})


@calls_bp.route("/api/calls/<call_id>/turns", methods=["GET"])
def call_turns(call_id):
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "请先登录", "code": "auth_required"}), 401
    call, error = _call_or_404(uid, call_id)
    if error:
        return error
    return jsonify({"turns": list_turns(uid, call_id)})
