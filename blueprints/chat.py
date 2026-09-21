# -*- coding: utf-8 -*-
"""Single-chat blueprint: /api/<char_id>/* routes extracted from app.py."""
import os
import time
import json
import re
import sqlite3
import shutil
import threading
import tempfile
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, request, jsonify, session, redirect, send_from_directory,
)
from PIL import Image

from core.config import COS_BASE_URL, CHARACTERS_DIR, USERS_ROOT
from core.context import get_current_user_id, set_background_user
from core.circuit_breaker import get_circuit_breaker_info
from core.system_messages import should_suppress_reply_for_deep_sleep
from core.utils import (
    get_paths, safe_save_json, _add_furigana_to_japanese,
    _get_characters_config_file, _get_read_status_file,
    _get_groups_config_file, _get_character_positions_file,
    _load_user_settings, get_group_dir,
)
from core.time_utils import (
    default_character_timezone,
    beijing_now,
    ensure_character_time_defaults,
    get_character_timezone,
    get_zone,
    get_user_timezone,
    is_valid_timezone,
    parse_hhmm,
    sleep_preview,
    utc_now,
)
from services import (
    call_gemini, call_openrouter, get_model_config, build_system_prompt_v2,
    call_ai_to_summarize, generate_long_memory_for_week, generate_medium_memory_for_date,
    update_short_memory_for_date,
)
from services.ai_client import ai_error_payload, is_ai_error_response
from services.prompt_builder import build_messages_for_chat_v2, get_ai_language
from services.memory_store import atomic_write_json, load_json_object, memory_file_lock
from services.read_state import mark_conversations_read, remove_read_state
from services.schedule import ScheduleValidationError, normalize_schedule_data
from services.persona_locks import PersonaLockError, validate_persona_locks
from services.image_tags import split_message_bubbles
from services.voice_messages import (
    VoiceMessageError,
    attach_voice_message,
    delete_voice_message_for_message,
    parse_voice_message_tag,
    validate_voice_message_for_scope,
)
from services.voice_calls import consume_call_user_tag
from agent_utils import process_agent_actions
from cos_utils import upload_to_cos, get_cos_list

chat_bp = Blueprint('chat', __name__)


def _create_character_call_if_requested(user_id, char_id, requested):
    if not requested:
        return None, None
    from blueprints.calls import create_incoming_call_for_character
    return create_incoming_call_for_character(user_id, char_id)


TRANSFER_MAX_AMOUNT = Decimal("999999999.99")
TRANSFER_RESOLVED_KINDS = {
    "accept": ("领取转账", "已领取转账", "accepted"),
    "return": ("退回转账", "已退回转账", "returned"),
}
TRANSFER_TAG_RE = re.compile(
    r"\[(?P<kind>转账|已领取转账|已退回转账):(?P<body>[^\]\r\n]+)\]"
)
TRANSFER_DECISION_RE = re.compile(
    r"^\[(?P<kind>领取转账|退回转账):(?P<amount>[^\]\r\n|]+)\]$"
)
ASSISTANT_TRANSFER_DECISION_RE = re.compile(
    r"\[(?P<kind>领取转账|退回转账):(?P<amount>[^\]\r\n|]+)\]"
)


class TransferActionError(ValueError):
    def __init__(self, message, status_code=400, code="invalid_transfer_action"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def normalize_transfer_amount(raw_amount):
    """Validate an amount with optional currency text or symbols on either side."""
    amount = re.sub(r"\s+", "", str(raw_amount or ""))
    if not amount:
        raise TransferActionError("转账金额不能为空")

    match = re.fullmatch(
        r"([^0-9]*?)((?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?)([^0-9]*)",
        amount,
    )
    if not match or any(
        not (ch.isalpha() or unicodedata.category(ch) == "Sc")
        for ch in match[1] + match[3]
    ):
        raise TransferActionError("转账金额格式无效")
    number_text = match[2]
    try:
        numeric = Decimal(number_text)
    except InvalidOperation as exc:
        raise TransferActionError("转账金额格式无效") from exc
    if numeric <= 0 or numeric > TRANSFER_MAX_AMOUNT:
        raise TransferActionError("转账金额超出允许范围")
    return amount


def parse_transfer_tag(content):
    """Return the first transfer tag in content, including its replacement range."""
    text = str(content or "")
    match = TRANSFER_TAG_RE.search(text)
    if not match:
        return None
    body_parts = match.group("body").split("|", 1)
    amount = normalize_transfer_amount(body_parts[0])
    note = body_parts[1].strip() if len(body_parts) == 2 else ""
    if len(note) > 80 or any(token in note for token in ("[", "]", "|", "/", "\n", "\r")):
        raise TransferActionError("转账备注格式无效")
    return {
        "kind": match.group("kind"),
        "amount": amount,
        "note": note,
        "start": match.start(),
        "end": match.end(),
    }


def apply_transfer_action(
    cursor, payload, user_message, *, allow_group_character_source=False
):
    """Resolve an incoming transfer and return the canonical user-visible tag."""
    action = str(payload.get("transfer_action") or "").strip().lower()
    source_id = payload.get("transfer_source_id")
    decision_match = TRANSFER_DECISION_RE.fullmatch(str(user_message or "").strip())

    if not action and source_id in (None, ""):
        if decision_match:
            raise TransferActionError("请点击转账卡片领取或退回")
        return user_message, None
    if action not in TRANSFER_RESOLVED_KINDS or source_id in (None, ""):
        raise TransferActionError("转账操作参数不完整")
    try:
        source_id = int(source_id)
    except (TypeError, ValueError) as exc:
        raise TransferActionError("转账消息 ID 无效") from exc
    if source_id <= 0 or not decision_match:
        raise TransferActionError("转账操作格式无效")

    decision_kind, resolved_kind, status = TRANSFER_RESOLVED_KINDS[action]
    if decision_match.group("kind") != decision_kind:
        raise TransferActionError("转账操作与标签不一致")

    cursor.execute("SELECT role, content FROM messages WHERE id = ?", (source_id,))
    source = cursor.fetchone()
    source_role = source[0] if source is not None else None
    valid_source = (
        source_role not in (None, "user")
        if allow_group_character_source
        else source_role == "assistant"
    )
    if not valid_source:
        raise TransferActionError("找不到对应的角色转账", 404, "transfer_not_found")

    try:
        transfer = parse_transfer_tag(source[1])
    except TransferActionError as exc:
        raise TransferActionError("原转账消息格式无效", 409, "transfer_invalid") from exc
    if not transfer:
        raise TransferActionError("该消息不是转账", 409, "transfer_invalid")
    if transfer["kind"] != "转账":
        raise TransferActionError("该转账已经处理", 409, "transfer_already_resolved")

    requested_amount = normalize_transfer_amount(decision_match.group("amount"))
    if requested_amount != transfer["amount"]:
        raise TransferActionError("转账金额与原消息不一致", 409, "transfer_amount_mismatch")

    replacement = f'[{resolved_kind}:{transfer["amount"]}'
    if transfer["note"]:
        replacement += f'|{transfer["note"]}'
    replacement += "]"
    updated_content = source[1][:transfer["start"]] + replacement + source[1][transfer["end"]:]
    cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (updated_content, source_id))

    canonical_user_message = f'[{decision_kind}:{transfer["amount"]}]'
    return canonical_user_message, {
        "source_id": source_id,
        "status": status,
        "content": updated_content,
        "amount": transfer["amount"],
        "note": transfer["note"],
    }


def apply_assistant_transfer_decision(cursor, assistant_message):
    """Resolve the newest pending user transfer with the same amount and currency."""
    text = str(assistant_message or "")
    decision = ASSISTANT_TRANSFER_DECISION_RE.search(text)
    if not decision:
        return assistant_message, None
    try:
        requested_amount = normalize_transfer_amount(decision.group("amount"))
    except TransferActionError:
        return assistant_message, None

    cursor.execute(
        "SELECT id, content FROM messages WHERE role = 'user' ORDER BY id DESC"
    )
    source_id = None
    source_content = None
    transfer = None
    for row in cursor.fetchall():
        try:
            candidate = parse_transfer_tag(row[1])
        except TransferActionError:
            continue
        if (
            candidate
            and candidate["kind"] == "转账"
            and candidate["amount"] == requested_amount
        ):
            source_id = int(row[0])
            source_content = row[1]
            transfer = candidate
            break
    if transfer is None:
        return assistant_message, None

    action = "accept" if decision.group("kind") == "领取转账" else "return"
    decision_kind, resolved_kind, status = TRANSFER_RESOLVED_KINDS[action]

    source_replacement = f'[{resolved_kind}:{transfer["amount"]}'
    if transfer["note"]:
        source_replacement += f'|{transfer["note"]}'
    source_replacement += "]"
    updated_source = (
        source_content[:transfer["start"]]
        + source_replacement
        + source_content[transfer["end"]:]
    )
    cursor.execute(
        "UPDATE messages SET content = ? WHERE id = ?",
        (updated_source, source_id),
    )

    canonical_decision = f'[{decision_kind}:{transfer["amount"]}]'
    updated_assistant = text[:decision.start()] + canonical_decision + text[decision.end():]
    return updated_assistant, {
        "source_id": source_id,
        "status": status,
        "content": updated_source,
        "amount": transfer["amount"],
        "note": transfer["note"],
    }


def _character_now(char_id):
    try:
        cfg_file = _get_characters_config_file()
        with open(cfg_file, "r", encoding="utf-8") as f:
            info = (json.load(f) or {}).get(char_id, {}) or {}
    except Exception:
        info = {}
    return utc_now().astimezone(get_zone(get_character_timezone(info)))


def normalize_relationship_graph(raw):
    """Parse and validate relationship JSON from editors or model output."""
    value = raw
    if isinstance(value, str):
        text = value.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("未找到 JSON 对象")
            value = json.loads(text[start:end + 1])
        if isinstance(value, str):
            value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("关系图谱顶层必须是 JSON 对象")

    normalized = {}
    for raw_name, raw_info in value.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if len(name) > 100:
            raise ValueError(f"关系名称过长: {name[:20]}...")
        if isinstance(raw_info, dict):
            role = str(raw_info.get("role") or "").strip()
            description = str(
                raw_info.get("description")
                if raw_info.get("description") is not None
                else raw_info.get("desc") or ""
            ).strip()
            score_value = raw_info.get("score", 1)
        else:
            role = ""
            description = str(raw_info or "").strip()
            score_value = 1
        try:
            score = float(score_value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 的 score 必须是数字")
        score = max(0.0, min(5.0, score))
        if score.is_integer():
            score = int(score)
        normalized[name] = {
            "role": role[:200],
            "score": score,
            "description": description[:5000],
        }
    return normalized


def _load_relationship_characters(user_id=None):
    cfg_file = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(cfg_file):
        return {}
    with open(cfg_file, "r", encoding="utf-8-sig") as f:
        value = json.load(f) or {}
    if not isinstance(value, dict):
        raise ValueError("角色配置格式错误")
    return value


def _relationship_aliases(char_id, info):
    aliases = []
    for value in (char_id, (info or {}).get("name"), (info or {}).get("remark")):
        text = str(value or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def _find_relationship_key(graph, char_id, info):
    if not isinstance(graph, dict):
        return None
    for alias in _relationship_aliases(char_id, info):
        if alias in graph:
            return alias
    return None


def _normalize_single_relationship(value, label):
    return normalize_relationship_graph({label: value})[label]


def _resolve_reverse_relationship_graph(char_id, raw, all_chars):
    """Resolve reverse JSON keys (id/name/remark) to source character IDs."""
    normalized = normalize_relationship_graph(raw)
    alias_map = {}
    for source_cid, info in all_chars.items():
        if source_cid == char_id:
            continue
        for alias in _relationship_aliases(source_cid, info):
            alias_map.setdefault(alias.casefold(), set()).add(source_cid)

    resolved = {}
    for source_key, relation in normalized.items():
        matches = alias_map.get(source_key.casefold(), set())
        if not matches:
            raise ValueError(f"无法识别角色“{source_key}”，请使用角色 ID、原名或备注")
        if len(matches) > 1:
            names = "、".join(sorted(matches))
            raise ValueError(f"角色“{source_key}”匹配不唯一：{names}，请改用角色 ID")
        source_cid = next(iter(matches))
        if source_cid in resolved:
            raise ValueError(f"角色 {source_cid} 在 JSON 中重复出现")
        resolved[source_cid] = relation
    return resolved


def _read_json_object(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        value = json.load(f) or {}
    if not isinstance(value, dict):
        raise ValueError(f"关系文件格式错误: {os.path.basename(os.path.dirname(path))}")
    return value


def _write_json_atomic(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _reverse_response_value(source_cid, relation, all_chars):
    value = dict(relation)
    info = all_chars.get(source_cid, {}) or {}
    value["char_name"] = info.get("name") or source_cid
    value["char_remark"] = info.get("remark") or value["char_name"]
    return value


@chat_bp.route("/api/relationship/parse", methods=["POST"])
def parse_relationship_graph_api():
    try:
        graph = normalize_relationship_graph((request.json or {}).get("content"))
        return jsonify({"status": "success", "graph": graph})
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400


def _circuit_breaker_json_response(user_msg_id=None, model=None):
    cb_info = get_circuit_breaker_info()
    if not cb_info:
        return None
    resp = {
        "replies": [],
        "circuit_breaker": cb_info,
    }
    if user_msg_id is not None:
        resp["user_id"] = user_msg_id
    if model:
        resp["model"] = model
    return jsonify(resp)


def _ai_error_json_response(reply, user_msg_id=None, model=None):
    """Convert provider failures into JSON before they can become chat messages."""
    if not is_ai_error_response(reply):
        return None
    resp = ai_error_payload(reply)
    resp["replies"] = []
    if user_msg_id is not None:
        resp["user_id"] = user_msg_id
    if model:
        resp["model"] = model
    return jsonify(resp), (getattr(reply, "status_code", 0) or 502)


def get_char_db_path(char_id) -> str:
    """获取指定角色的 DB 路径（内部复用 get_paths，确保与多用户命名空间一致）。"""
    db_path, _ = get_paths(char_id)
    return db_path


def _resolve_read_targets(items):
    characters = load_json_object(_get_characters_config_file())
    groups = load_json_object(_get_groups_config_file())
    targets = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid conversation")
        conversation_id = item.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("Invalid conversation ID")
        kind = item.get("type")
        if kind is None:  # Compatibility for old callers.
            kind = "chat" if conversation_id in characters else "group"
        if kind == "char":
            kind = "chat"
        allowed = characters if kind == "chat" else groups if kind == "group" else {}
        if conversation_id not in allowed:
            raise ValueError("Conversation not found")
        db_path = (get_paths(conversation_id)[0] if kind == "chat" else
                   os.path.join(get_group_dir(conversation_id), "chat.db"))
        targets.append((kind, conversation_id, db_path, item.get("last_message_id")))
    return targets


def mark_char_as_read(char_id, kind=None, last_message_id=None):
    targets = _resolve_read_targets([{"id": char_id, "type": kind, "last_message_id": last_message_id}])
    return mark_conversations_read(_get_read_status_file(), targets)


@chat_bp.route("/api/<char_id>/mark_read", methods=["POST"])
def mark_read_api(char_id):
    if not get_current_user_id():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.get_json(silent=True)
        if data is None and not request.data:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("Invalid request")
        results = mark_char_as_read(char_id, data.get("type"), data.get("last_message_id"))
        return jsonify({"status": "success", "conversations": results})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print(f"Mark read failed: {exc}")
        return jsonify({"error": "已读状态保存失败，请重试"}), 500


@chat_bp.route("/api/contacts/mark_read", methods=["POST"])
def mark_contacts_read_api():
    if not get_current_user_id():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.get_json(silent=True)
        items = data.get("conversations") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items or len(items) > 2000:
            raise ValueError("conversations must contain 1-2000 items")
        if any(not isinstance(item, dict) or item.get("last_message_id") is None for item in items):
            raise ValueError("last_message_id is required")
        results = mark_conversations_read(_get_read_status_file(), _resolve_read_targets(items))
        return jsonify({"status": "success", "conversations": results})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print(f"Batch mark read failed: {exc}")
        return jsonify({"error": "已读状态保存失败，请重试"}), 500



@chat_bp.route("/api/<char_id>/history", methods=["GET"])
def get_history(char_id):
    from app import init_char_db, _sticker_content_from_ai
    user_id = get_current_user_id()
    limit = request.args.get('limit', 20, type=int)
    target_id = request.args.get('target_id', type=int)
    before_id = request.args.get('before_id', type=int)
    after_id = request.args.get('after_id', type=int)

    db_path, _ = get_paths(char_id, user_id=user_id)
    if not os.path.exists(db_path): init_char_db(char_id)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    messages = []

    # A. 向上滚动 (锚点模式)
    if before_id:
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    # B. 向下轮询 (轮询模式)
    elif after_id:
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit))
        messages = [dict(row) for row in cursor.fetchall()]

    # C. 跳转定位 (精准窗口模式: 上5条 + 目标 + 下5条 = 最多11条)
    elif target_id:
        before_msgs = []
        target_msgs = []
        after_msgs = []
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id < ? ORDER BY id DESC LIMIT 5", (target_id,))
        before_msgs = [dict(row) for row in cursor.fetchall()][::-1]
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id = ?", (target_id,))
        target_msgs = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id > ? ORDER BY id ASC LIMIT 5", (target_id,))
        after_msgs = [dict(row) for row in cursor.fetchall()]
        messages = before_msgs + target_msgs + after_msgs

    # C. 默认加载
    else:
        cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    # 将 [表情]名称 解析为 [表情]path（搜索含该名称的表情→随机选一个地址）并写回 DB，刷新后不变，打开编辑时看到的也是改好的 path
    for m in messages:
        new_content = _sticker_content_from_ai(m["content"])
        if new_content != m["content"]:
            cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, m["id"]))
            m["content"] = new_content
    conn.commit()

    cursor.execute("SELECT COUNT(id) FROM messages")
    total_messages = cursor.fetchone()[0]
    conn.close()

    # 日语注音处理（不写回DB）
    if get_ai_language(char_id, user_id=user_id) == "ja":
        for m in messages:
            m["content"] = _add_furigana_to_japanese(m["content"])

    return jsonify({
        "messages": messages,
        "total": total_messages
    })


@chat_bp.route("/api/<char_id>/messages", methods=["POST"])
def create_user_message(char_id):
    """Persist a user message and return its real ID before reply generation starts."""
    from app import init_char_db

    user_id = get_current_user_id()
    data = request.json or {}
    user_msg = str(data.get("message", "")).strip()
    if not user_msg:
        return jsonify({"error": "empty message"}), 400
    try:
        validate_voice_message_for_scope(
            user_id, user_msg, scope_type="chat", scope_id=char_id
        )
    except VoiceMessageError as exc:
        return jsonify({"error": "voice_message_invalid", "message": str(exc)}), 400

    db_path, _ = get_paths(char_id, user_id=user_id)
    if not os.path.exists(db_path):
        init_char_db(char_id)

    user_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            ("user", user_msg, user_ts),
        )
        user_msg_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    attach_voice_message(
        user_id,
        user_msg,
        scope_type="chat",
        scope_id=char_id,
        message_id=user_msg_id,
    )
    return jsonify({"user_id": user_msg_id, "timestamp": user_ts}), 201


@chat_bp.route("/api/<char_id>/chat", methods=["POST"])
def chat(char_id):
    from app import init_char_db, sync_memory_before_single_chat, _memory_context_changed, process_ai_media_tags, _execute_directive, _check_consecutive_tickle, _strip_consecutive_tickle, _extract_tickle_target, _sticker_content_from_ai
    user_id = get_current_user_id()
    # 1. 动态获取路?
    db_path, prompts_dir = get_paths(char_id, user_id=user_id)

    # 2. 防御性初始化
    if not os.path.exists(db_path):
        init_char_db(char_id)

    # 数据准备
    data = request.json or {}
    user_msg_raw = data.get("message", "").strip()
    if not user_msg_raw:
        return jsonify({"error": "empty message"}), 400
    try:
        validate_voice_message_for_scope(
            user_id, user_msg_raw, scope_type="chat", scope_id=char_id
        )
    except VoiceMessageError as exc:
        return jsonify({"error": "voice_message_invalid", "message": str(exc)}), 400

    # 拍一拍：检查连续拍同一人
    is_tickle, tickle_target = _extract_tickle_target(user_msg_raw)
    if is_tickle:
        tgt = tickle_target if tickle_target != "assistant" else char_id
        ok, _ = _check_consecutive_tickle(db_path, tgt, char_id)
        if not ok:
            return jsonify({"error": "consecutive_tickle", "message": "不可连续拍一拍同一人，请稍后再试"}), 400

    # --- 3. 检查深睡眠状态 ---
    is_deep_sleep = False
    cfg_file = _get_characters_config_file()
    try:
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            char_info = all_config.get(char_id, {})
            # 获取开关状态
            is_deep_sleep = char_info.get("deep_sleep", False)

            # (可选) 高级逻辑：如果想配合时间段自动判断，可以在这里加
            # 比如：虽然开关开了，但如果不在时间段内，视为醒着？
            # 或者：开关只作为总开关。这里暂时按您的要求：开关开=不回。
    except: pass

    # --- 4. 无论睡没睡，先存入用户消息 ---
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    now = beijing_now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')

    # 存用户消息
    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))
    user_msg_id = cursor.lastrowid # 获取 ID

    conn.commit()
    conn.close()
    attach_voice_message(
        user_id,
        user_msg_raw,
        scope_type="chat",
        scope_id=char_id,
        message_id=user_msg_id,
    )

    # --- 5. 如果在深睡眠，直接返回空回复，不调 AI ---
    if should_suppress_reply_for_deep_sleep(is_deep_sleep, user_msg_raw):
        print(f"--- [Deep Sleep] {char_id} 正在熟睡，不回复消息 ---")

        # 即使不回复，也把 user_id 传回去，这样用户发的气泡才有删除按钮
        return jsonify({
            "replies": [],
            "id": None,
            "user_id": user_msg_id
        })

    # ================= 醒着：正常调用 AI 逻辑 =================

    # --- 5.5 仅在切换到该单聊时同步；同一单聊连续回复不重复触发总结 ---
    memory_sync_warning = None
    if _memory_context_changed(user_id, f"single:{char_id}"):
        try:
            ok, err = sync_memory_before_single_chat(char_id, user_id=user_id)
            if not ok:
                memory_sync_warning = f"记忆同步失败：{err}，本次对话可能缺少部分群聊上下文"
                print(f"   ⚠️ {memory_sync_warning}")
        except Exception as e:
            memory_sync_warning = f"记忆同步失败：{e}，本次对话可能缺少部分群聊上下文"
            print(f"   ⚠️ {memory_sync_warning}")

    # 6. 先读取历史记录，再构建 System Prompt（便于长期记忆 RAI 使用最近对话）
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 21")
    history_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    # ===== 【全局采用 v2】使用 System Prompt v2 =====
    print(f"--- [Chat] char_id: {char_id}, using System Prompt v2 ---")

    # ===== 【v2版本】使用新的时间线聚合系统提示 =====
    messages = build_messages_for_chat_v2(char_id, user_msg_raw, recent_messages=[r["content"] for r in history_rows], user_id=user_id)

        # 添加系统提示时间信息
    character_now = _character_now(char_id)
    lang = get_ai_language(char_id, user_id=user_id)
    hour = character_now.hour

    if 5 <= hour < 11:
        if lang == "zh": period = "早上"
        elif lang == "ja": period = "朝"
        else: period = "morning"
    elif 11 <= hour < 13:
        if lang == "zh": period = "中午"
        elif lang == "ja": period = "昼"
        else: period = "noon"
    elif 13 <= hour < 18:
        if lang == "zh": period = "下午"
        elif lang == "ja": period = "午後"
        else: period = "afternoon"
    elif 18 <= hour < 23:
        if lang == "zh": period = "晚上"
        elif lang == "ja": period = "夜"
        else: period = "evening"
    else:
        if lang == "zh": period = "深夜"
        elif lang == "ja": period = "深夜"
        else: period = "late night"

    if lang == "zh":
        system_hint = (
            f"（系统提示：现在是{period} {character_now.strftime('%H:%M')}。）\n"
            f"（用户发来了一条消息。请根据时间线中的上下文，回复用户的消息。）\n"
            f"（要求：自然、简短，不要重复上一句话。）\n"
            f"（无特殊说明时用斜线表示换行和句号。）"
        )
    elif lang == "ja":
        system_hint = (
            f"（システム通知：現在は{period} {character_now.strftime('%H:%M')}です。）\n"
            f"（ユーザーからメッセージが来ました。タイムライン内容を踏まえて回信してください。）\n"
            f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）\n"
            f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
        )
    else:
        system_hint = (
            f"(System Hint: It is now {period} {character_now.strftime('%H:%M')}.)\n"
            f"(User has sent a message. Please reply based on the timeline context.)\n"
            f"(Requirements: Natural, concise, do not repeat the previous statement.)\n"
            f"(In normal cases, use slashes / for newlines and periods.)"
        )

    messages.append({"role": "system", "content": system_hint})

    # 获取当前配置
    route, current_model = get_model_config("chat", user_id=user_id) # 任务类型是 chat

    print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

    try:
        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)

        cb_resp = _circuit_breaker_json_response(user_msg_id=user_msg_id)
        if cb_resp:
            return cb_resp

        error_resp = _ai_error_json_response(
            reply_text_raw,
            user_msg_id=user_msg_id,
            model=current_model,
        )
        if error_resp:
            return error_resp

        # 清理时间戳
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()
        cleaned_reply_text, character_call_requested = consume_call_user_tag(cleaned_reply_text)

        # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
        try:
            cleaned_reply_text, affinity_delta, _dir, agent_events = process_agent_actions(char_id, cleaned_reply_text, get_current_user_id(), return_events=True)
        except Exception as e:
            print(f"  ❌ [Directive] process_agent_actions 崩溃: {e}", flush=True)
            import traceback
            traceback.print_exc()
            _dir = None
            affinity_delta = None
            agent_events = []
        print(f"  [DEBUG] _dir = {repr(_dir)}, type={type(_dir).__name__}", flush=True)

        # --- 【转向指令】处理 DIRECT_TO_GROUP / DIRECT_TO_USER ---
        if _dir:
            # 单聊中 DIRECT_TO_USER 是无效操作
            if _dir.get("type") == "user":
                print(f"  ⚠️ [Directive] 已在单聊中，忽略 DIRECT_TO_USER", flush=True)
            else:
                print(f"", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"  🔄 [Directive] {char_id} 发出转向指令: {_dir}", flush=True)
                # 后台异步执行，不阻塞当前回复
                uid = get_current_user_id()
                _ddir = dict(_dir, source_scene="single_chat")
                _cid, _ctxt = char_id, cleaned_reply_text
                def _bg_exec():
                    set_background_user(uid)
                    try:
                        _execute_directive(_ddir, _cid, _ctxt)
                    except Exception as e:
                        print(f"  ❌ [Directive BG] 指令执行失败: {e}", flush=True)
                        import traceback
                        traceback.print_exc()
                threading.Thread(target=_bg_exec, daemon=True).start()
                print(f"{'='*50}", flush=True)

        # 把 AI 回复里的 [表情]name 转成 [表情]path 再入库
        cleaned_reply_text = _strip_consecutive_tickle(cleaned_reply_text)

        # --- 【拦截器顺序调整】先处理多媒体标签，再处理表情 ---
        # 原因：表情正则 pattern = r"\[表情\](.*?)(?=\s*/\s*|$)" 可能会因为那个斜杠而误伤
        cleaned_reply_text = process_ai_media_tags(cleaned_reply_text, char_id, user_id=user_id)
        cleaned_reply_text = _sticker_content_from_ai(cleaned_reply_text)

        # 6. 存入数据库 (关键修改在这里！)
        # 消息记录始终使用北京时间；角色当地时间只用于上面的 Prompt。
        ai_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 存 AI 消息
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", cleaned_reply_text, ai_ts))

        # 【重点】获取刚插入的 AI 消息的 ID
        ai_msg_id = cursor.lastrowid

        conn.commit()
        conn.close()

        incoming_call, incoming_call_error = _create_character_call_if_requested(
            user_id, char_id, character_call_requested
        )

        reply_bubbles = split_message_bubbles(cleaned_reply_text)

        if get_ai_language(char_id, user_id=user_id) == "ja":
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        # 【重点】把 ID 返回给前端；记忆同步失败时附带提示
        resp = {
            "replies": reply_bubbles,
            "id": ai_msg_id,
            "user_id": user_msg_id
        }
        if affinity_delta:
            resp["affinity_delta"] = affinity_delta
        if memory_sync_warning:
            resp["memory_sync_warning"] = memory_sync_warning
        if agent_events:
            resp["agent_events"] = agent_events
        if incoming_call:
            resp["incoming_call"] = incoming_call
        elif incoming_call_error:
            resp["incoming_call_error"] = incoming_call_error
        cb_info = get_circuit_breaker_info()
        if cb_info:
            resp["circuit_breaker"] = cb_info
        return jsonify(resp)

    except Exception as e:
        print(f"Chat Error: {e}")
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/chat_v2", methods=["POST"])
def chat_v2(char_id):
    from app import init_char_db, sync_memory_before_single_chat, _memory_context_changed, process_ai_media_tags, _execute_directive, _strip_consecutive_tickle, _sticker_content_from_ai
    """【测试版】使用新的时间线聚合System Prompt v2版本的聊天接口。"""
    user_id = get_current_user_id()
    # 1. 路径准备
    db_path, prompts_dir = get_paths(char_id, user_id=user_id)
    if not os.path.exists(db_path):
        init_char_db(char_id)

    # 2. 获取用户输入
    data = request.json or {}
    user_msg_raw = data.get("message", "").strip()
    if not user_msg_raw:
        return jsonify({"error": "empty message"}), 400
    try:
        validate_voice_message_for_scope(
            user_id,
            user_msg_raw,
            scope_type="chat",
            scope_id=char_id,
            message_id=data.get("user_message_id"),
        )
    except (VoiceMessageError, TypeError, ValueError) as exc:
        return jsonify({"error": "voice_message_invalid", "message": str(exc)}), 400

    # 3. 检查深睡眠状态
    is_deep_sleep = False
    chat_mode = "online"
    cfg_file = _get_characters_config_file()
    try:
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            char_info = all_config.get(char_id, {})
            is_deep_sleep = char_info.get("deep_sleep", False)
            chat_mode = char_info.get("chat_mode", "online")
    except:
        pass

    if chat_mode == "offline":
        is_deep_sleep = False

    # 4. 新发送链路会先单独落库并传入 user_message_id；旧客户端仍兼容在此落库。
    pre_saved_message_id = data.get("user_message_id")
    transfer_update = None
    if pre_saved_message_id is not None:
        try:
            user_msg_id = int(pre_saved_message_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid user_message_id"}), 400
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM messages WHERE id = ?",
            (user_msg_id,),
        )
        saved_message = cursor.fetchone()
        conn.close()
        if not saved_message or saved_message[0] != "user":
            return jsonify({"error": "user message not found"}), 404
        # 数据库内容为准，可接住落库后、生成前发生的快速编辑。
        user_msg_raw = saved_message[1]
    else:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        user_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            conn.execute("BEGIN IMMEDIATE")
            user_msg_raw, transfer_update = apply_transfer_action(cursor, data, user_msg_raw)
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))
            user_msg_id = cursor.lastrowid
            conn.commit()
        except TransferActionError as exc:
            conn.rollback()
            conn.close()
            return jsonify({"error": exc.code, "message": exc.message}), exc.status_code
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        attach_voice_message(
            user_id,
            user_msg_raw,
            scope_type="chat",
            scope_id=char_id,
            message_id=user_msg_id,
        )

    # 【Agent】若该用户的浏览器 Agent 正在等待用户回复（[ASK]/[WAIT] 暂停中），
    # 则把本条消息作为 Agent 回复写入 IPC 唤醒它，由 Agent 重新截取页面快照后续跑，
    # 而不是走普通聊天。这样即便前端状态轮询有延迟/不同步也能可靠转交。
    # （排除 Agent 自己发来的 [WEB_CRUISE] 快照消息，那类调用 Agent 处于 active 状态。）
    if "[WEB_CRUISE:" not in user_msg_raw:
        try:
            state_path = _get_agent_state_path(user_id)
            if os.path.exists(state_path):
                with open(state_path, "r", encoding="utf-8") as f:
                    _agent_state = json.load(f)
                if _agent_state.get("running") and _agent_state.get("status") == "waiting_for_user":
                    _ensure_agent_user_dir(user_id)
                    with open(_get_agent_input_path(user_id), "w", encoding="utf-8") as f:
                        json.dump({"command": "reply", "message": user_msg_raw}, f, ensure_ascii=False)
                    print(f"--- [Chat v2] Agent 等待中，已将回复转交 Agent (user={user_id}) ---")
                    return jsonify({
                        "replies": [],
                        "id": None,
                        "user_id": user_msg_id,
                        "agent_forwarded": True,
                        "transfer_update": transfer_update,
                    })
        except Exception as e:
            print(f"[Chat v2] Agent 转交检查失败: {e}")

    # 5. 检查深睡眠
    if should_suppress_reply_for_deep_sleep(is_deep_sleep, user_msg_raw):
        print(f"--- [Deep Sleep v2] {char_id} 正在熟睡，不回复消息 ---")
        return jsonify({
            "replies": [],
            "id": None,
            "user_id": user_msg_id,
            "transfer_update": transfer_update,
        })

    # 6. 仅在切换到该单聊时同步；同一单聊连续回复不重复触发总结
    memory_sync_warning = None
    if _memory_context_changed(user_id, f"single:{char_id}"):
        try:
            ok, err = sync_memory_before_single_chat(char_id, user_id=user_id)
            if not ok:
                memory_sync_warning = f"记忆同步失败：{err}，本次对话可能缺少部分群聊上下文"
                print(f"   ⚠️ {memory_sync_warning}")
        except Exception as e:
            memory_sync_warning = f"记忆同步失败：{e}，本次对话可能缺少部分群聊上下文"
            print(f"   ⚠️ {memory_sync_warning}")

    # ===== 【v2核心】使用新的时间线聚合系统提示 =====
    # 读取最近消息用于RAI过滤
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content FROM messages ORDER BY timestamp DESC LIMIT 21")
    recent_messages_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()
    recent_texts = [r["content"] for r in recent_messages_rows] if recent_messages_rows else []

    # 构建v2版消息（只包含system + 最新user）
    messages = build_messages_for_chat_v2(char_id, user_msg_raw, recent_messages=recent_texts, user_id=user_id)

    # 添加时间提示
    character_now = _character_now(char_id)
    lang = get_ai_language(char_id, user_id=user_id)
    hour = character_now.hour
    time_str = character_now.strftime('%H:%M')

    if 5 <= hour < 11:
        if lang == "zh": period = "早上"
        elif lang == "ja": period = "朝"
        else: period = "morning"
    elif 11 <= hour < 13:
        if lang == "zh": period = "中午"
        elif lang == "ja": period = "昼"
        else: period = "noon"
    elif 13 <= hour < 18:
        if lang == "zh": period = "下午"
        elif lang == "ja": period = "午後"
        else: period = "afternoon"
    elif 18 <= hour < 23:
        if lang == "zh": period = "晚上"
        elif lang == "ja": period = "夜"
        else: period = "night"
    else:
        if lang == "zh": period = "深夜"
        elif lang == "ja": period = "深夜"
        else: period = "late night"

    if lang == "zh":
        system_hint = (
            f"（系统提示：现在是{period} {time_str}。）\n"
            f"（用户发来了一条消息。请根据时间线中的上下文，自然地回复用户。）\n"
            f"（要求：简短、自然，不要重复上一句话。）\n"
            f"（无特殊说明时用斜线表示换行和句号。）"
        )
    elif lang == "ja":
        system_hint = (
            f"（システム通知：現在は{period} {time_str}です。）\n"
            f"（ユーザーからメッセージが来ました。タイムラインを踏まえて回信してください。）\n"
            f"（要件：簡潔で自然。直前の発言を繰り返さないこと。）\n"
            f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
        )
    else:
        system_hint = (
            f"(System Tip: It is currently {period} {time_str}.)\n"
            f"(User sent a message. Please reply naturally based on the timeline context.)\n"
            f"(Requirements: Short, natural, do not repeat the previous sentence.)\n"
            f"(Unless specified, use slashes for newlines and periods.)"
        )

    messages.append({"role": "system", "content": system_hint})

    # 7. 调用AI
    route, current_model = get_model_config("chat", user_id=user_id)
    print(f"--- [Chat v2] char_id: {char_id}, route: {route}, model: {current_model} ---")

    try:
        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)

        cb_resp = _circuit_breaker_json_response(user_msg_id=user_msg_id, model=current_model)
        if cb_resp:
            return cb_resp

        error_resp = _ai_error_json_response(
            reply_text_raw,
            user_msg_id=user_msg_id,
            model=current_model,
        )
        if error_resp:
            return error_resp

        # 清理回复
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply = re.sub(timestamp_pattern, '', reply_text_raw).strip()
        cleaned_reply, character_call_requested = consume_call_user_tag(cleaned_reply)

        # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
        cleaned_reply, affinity_delta, directive, agent_events = process_agent_actions(char_id, cleaned_reply, get_current_user_id(), return_events=True)
        print(f"  [DEBUG] directive = {repr(directive)}, type={type(directive).__name__}", flush=True)

        # --- 【转向指令】处理 DIRECT_TO_GROUP / DIRECT_TO_USER ---
        if directive:
            if directive.get("type") == "user":
                print(f"  ⚠️ [Directive] 已在单聊中，忽略 DIRECT_TO_USER", flush=True)
            else:
                print(f"", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"  🔄 [Directive] {char_id} 发出转向指令: {directive}", flush=True)
                uid = get_current_user_id()
                _ddir = dict(directive, source_scene="single_chat")
                _cid, _ctxt = char_id, cleaned_reply
                def _bg_exec():
                    set_background_user(uid)
                    try:
                        _execute_directive(_ddir, _cid, _ctxt)
                    except Exception as e:
                        print(f"  ❌ [Directive BG] 指令执行失败: {e}", flush=True)
                        import traceback
                        traceback.print_exc()
                threading.Thread(target=_bg_exec, daemon=True).start()
                print(f"{'='*50}", flush=True)

        cleaned_reply = _strip_consecutive_tickle(cleaned_reply)
        # --- 【关键修复】多媒体标签识别失败原因：拦截顺序 ---
        # 必须在 _sticker_content_from_ai 之前处理，因为表情正则会寻找 / 作为终止符
        # 而 AI 的回复格式通常 is [GENERATE_IMAGE: ...] / 文本
        cleaned_reply = process_ai_media_tags(cleaned_reply, char_id, user_id=user_id)
        cleaned_reply = _sticker_content_from_ai(cleaned_reply)

        # 存入 AI 回复；角色领取/退回时，处理金额与币种一致的最新待处理用户转账。
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cleaned_reply, assistant_transfer_update = apply_assistant_transfer_decision(
                cursor, cleaned_reply
            )
            # 不复用 Prompt 的 character_now，避免把角色当地时间写入聊天记录。
            ai_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", cleaned_reply, ai_ts))
            ai_msg_id = cursor.lastrowid
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        if assistant_transfer_update:
            transfer_update = assistant_transfer_update

        incoming_call, incoming_call_error = _create_character_call_if_requested(
            user_id, char_id, character_call_requested
        )

        reply_bubbles = split_message_bubbles(cleaned_reply)
        if get_ai_language(char_id, user_id=user_id) == "ja" and "[WEB_CRUISE:" not in user_msg_raw:
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        resp = {
            "replies": [{"content": b, "id": ai_msg_id} for b in reply_bubbles],
            "id": ai_msg_id,
            "user_id": user_msg_id,
            "model": current_model,
            "transfer_update": transfer_update,
        }
        # 【Agent】提供未按斜线拆分的完整回复，供浏览器 Agent 可靠解析动作标签
        # （如 [GOTO:https://...] 含斜线会被 reply_bubbles 拆断）
        if "[WEB_CRUISE:" in user_msg_raw:
            resp["full_reply"] = cleaned_reply
        if affinity_delta:
            resp["affinity_delta"] = affinity_delta
        if memory_sync_warning:
            resp["memory_sync_warning"] = memory_sync_warning
        if agent_events:
            resp["agent_events"] = agent_events
        if incoming_call:
            resp["incoming_call"] = incoming_call
        elif incoming_call_error:
            resp["incoming_call_error"] = incoming_call_error
        cb_info = get_circuit_breaker_info()
        if cb_info:
            resp["circuit_breaker"] = cb_info

        return jsonify(resp)

    except Exception as e:
        print(f"Chat v2 Error: {e}")
        if transfer_update:
            return jsonify({
                "replies": [],
                "id": None,
                "user_id": user_msg_id,
                "transfer_update": transfer_update,
                "reply_error": "转账已处理，但角色回复生成失败",
            })
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/regenerate", methods=["POST"])
def regenerate_message(char_id):
    from app import process_ai_media_tags, _execute_directive, _strip_consecutive_tickle, _sticker_content_for_ai, _sticker_content_from_ai
    user_id = get_current_user_id()
    # 1. 获取路径
    db_path, prompts_dir = get_paths(char_id, user_id=user_id)
    if not os.path.exists(db_path): return jsonify({"error": "DB not found"}), 404

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 2. 检查最后一条是否为 assistant (安全检查)
        cursor.execute("SELECT id, role, content FROM messages ORDER BY id DESC LIMIT 1")
        last_row = cursor.fetchone()

        if not last_row:
            conn.close()
            return jsonify({"error": "No messages"}), 400

        if last_row['role'] != 'assistant':
            conn.close()
            return jsonify({"error": "Last message is not from assistant"}), 400

        # 3. 删除这条消息
        cursor.execute("DELETE FROM messages WHERE id = ?", (last_row['id'],))
        conn.commit()

        # 4. 先读取历史记录，再构建 System Prompt（便于长期记忆 RAI）
        cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
        history_rows = [dict(row) for row in cursor.fetchall()][::-1]
        conn.close()

        recent_texts = [r["content"] for r in history_rows] if history_rows else []
        user_latest = next((r["content"] for r in reversed(history_rows) if r["role"] == "user"), None)

        # 【全局采用 v2】
        print(f"--- [Regenerate] char_id: {char_id}, using System Prompt v2 ---")
        system_prompt = build_system_prompt_v2(char_id, recent_messages=recent_texts, user_latest_input=user_latest, user_id=user_id)
        messages = [{"role": "system", "content": system_prompt}]

        # 5. 构建上下文（history_rows 已在上方读取）
        now = _character_now(char_id)

        # 【全局采用 v2】仅添加最后一条消息（通常是用户消息）
        if history_rows and history_rows[-1]['role'] == 'user':
            last_row = history_rows[-1]
            try:
                dt_obj = datetime.strptime(last_row['timestamp'], '%Y-%m-%d %H:%M:%S')
                ts_str = dt_obj.strftime('[%m-%d %H:%M]')
                content_for_ai = _sticker_content_for_ai(last_row['content'])
                formatted_content = f"{ts_str} {content_for_ai}"
                messages.append({"role": "user", "content": formatted_content})
            except:
                messages.append({"role": "user", "content": last_row['content']})
        print(f"--- [Regenerate v2] 添加最后 1 条消息作为触发 ---")

        # ================= 【核心新增】智能补位与触发逻辑 =================
        # 根据数据库中实际的最后一条消息决定触发模式
        last_db_role = history_rows[-1]['role'] if history_rows else 'user'
        lang = get_ai_language(char_id, user_id=user_id)
        hour = now.hour
        time_str = now.strftime('%H:%M')

        # 计算时间段
        if 5 <= hour < 11:
            if lang == "zh": period = "早上"
            elif lang == "ja": period = "朝"
            else: period = "morning"
        elif 11 <= hour < 13:
            if lang == "zh": period = "中午"
            elif lang == "ja": period = "昼"
            else: period = "noon"
        elif 13 <= hour < 18:
            if lang == "zh": period = "下午"
            elif lang == "ja": period = "午後"
            else: period = "afternoon"
        elif 18 <= hour < 23:
            if lang == "zh": period = "晚上"
            elif lang == "ja": period = "夜"
            else: period = "evening"
        else:
            if lang == "zh": period = "深夜"
            elif lang == "ja": period = "深夜"
            else: period = "late night"

        if last_db_role == 'user':
            # 情况1: 最后一句话是用户说的
            # 1. 确保 messages 里面有最后一条用户消息
            if len(messages) == 1:
                messages.append({"role": "user", "content": "...continue..."})

            # 2. 准备简洁的 system 提示
            if lang == "zh":
                system_hint = (
                    f"（系统提示：现在是{period} {time_str}。）\n"
                    f"（请根据系统时间线，回复用户的消息。）\n"
                    f"（要求：自然、简短。）"
                )
            elif lang == "ja":
                system_hint = (
                    f"（システム通知：現在は{period} {time_str}です。）\n"
                    f"（タイムラインに基づいて、ユーザーに返信してください。）\n"
                    f"（条件：自然で簡潔に。）"
                )
            else:
                system_hint = (
                    f"(System Hint: It is now {period} {time_str}.)\n"
                    f"(Please reply to the user based on the timeline.)\n"
                    f"(Requirements: Natural, concise.)"
                )
            # ⚠️ 安全防范：为避免模型 API 报 400 错（如 System 消息不能在 User 消息后），
            # 直接将 system_hint 追加入 messages[0]["content"]（即最开头的 system instruction 中）
            messages[0]["content"] += "\n\n" + system_hint
            print(f"--- [Regenerate] 最后一条是用户消息，将简洁系统提示追加入首条 System 消息 ---")

        else:
            # 情况2: 最后一句话是 AI 说的（连续回复，触发主动话题）
            if lang == "zh":
                trigger_msg = (
                    f"（系统提示：现在是{period} {time_str}。）\n"
                    f"（请你根据当前时间、之前的聊天内容，**主动**向用户发起一个新的话题。）\n"
                    f"（要求：自然、简短，不要重复上一句话。）\n"
                    f"（无特殊说明时用斜线表示换行和句号。）"
                )
            else:
                trigger_msg = (
                    f"（システム通知：現在は{period} {time_str}です。）\n"
                    f"（現在の時間帯やこれまでの会話を踏まえて、**自発的に**新しい話題を振ってください。）\n"
                    f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）\n"
                    f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
                )
            print(f"--- [Regenerate] 检测到连续对话，插入主动消息触发提示 ---")
            messages.append({"role": "user", "content": trigger_msg})
        # ===========================================================
        # ===========================================================

        # 7. 调用 AI
        route, current_model = get_model_config("chat", user_id=user_id)
        print(f"--- [Regenerate] Route: {route}, Model: {current_model} ---")

        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)

        cb_resp = _circuit_breaker_json_response()
        if cb_resp:
            return cb_resp

        error_resp = _ai_error_json_response(reply_text_raw, model=current_model)
        if error_resp:
            return error_resp

        # 8. 清理 & 存入
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()
        cleaned_reply_text, character_call_requested = consume_call_user_tag(cleaned_reply_text)

        # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
        cleaned_reply_text, affinity_delta, regenerate_directive = process_agent_actions(
            char_id, cleaned_reply_text, get_current_user_id()
        )

        cleaned_reply_text = _strip_consecutive_tickle(cleaned_reply_text)

        # --- 【关键修复】重新生成时也需要拦截多媒体标签 ---
        cleaned_reply_text = process_ai_media_tags(cleaned_reply_text, char_id, user_id=user_id)
        cleaned_reply_text = _sticker_content_from_ai(cleaned_reply_text)

        ai_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("assistant", cleaned_reply_text, ai_ts))
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()

        incoming_call, incoming_call_error = _create_character_call_if_requested(
            user_id, char_id, character_call_requested
        )

        if regenerate_directive:
            uid = user_id
            directive_payload = dict(regenerate_directive, source_scene="single_chat")
            source_text = cleaned_reply_text

            def _run_regenerate_directive():
                set_background_user(uid)
                print(f"  🚀 [Regenerate Directive] 后台派发: {directive_payload}", flush=True)
                _execute_directive(directive_payload, char_id, source_text)

            threading.Thread(target=_run_regenerate_directive, daemon=True).start()

        reply_bubbles = split_message_bubbles(cleaned_reply_text)

        if get_ai_language(char_id, user_id=user_id) == "ja":
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        resp_data = {
            "status": "success",
            "replies": reply_bubbles,
            "id": new_id
        }
        if incoming_call:
            resp_data["incoming_call"] = incoming_call
        elif incoming_call_error:
            resp_data["incoming_call_error"] = incoming_call_error
        if affinity_delta:
            resp_data["affinity_delta"] = affinity_delta
        cb_info = get_circuit_breaker_info()
        if cb_info:
            resp_data["circuit_breaker"] = cb_info
        return jsonify(resp_data)

    except Exception as e:
        print(f"Regenerate Error: {e}")
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/messages/<int:msg_id>", methods=["DELETE"])
def delete_message(char_id, msg_id):
    # 1. 统一使用工具函数获取路径，防止路径写错
    db_path, _ = get_paths(char_id)

    print(f"--- [Debug] 尝试删除消息 ID: {msg_id} (DB: {db_path}) ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Database not found"}), 404

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM messages WHERE id = ?", (msg_id,))
        deleted_row = cursor.fetchone()
        # 执行删除
        cursor.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        rows_affected = cursor.rowcount # 获取受影响的行数

        conn.commit()
        conn.close()

        if rows_affected > 0:
            if deleted_row and deleted_row[0] == "user":
                delete_voice_message_for_message(
                    get_current_user_id(),
                    deleted_row[1],
                    scope_type="chat",
                    scope_id=char_id,
                    message_id=msg_id,
                )
            print(f"   ✅ 删除成功，影响行数: {rows_affected}")
            return jsonify({"status": "success"})
        else:
            print(f"   ⚠️ 删除失败: 数据库中找不到 ID={msg_id}")
            return jsonify({"error": "Message ID not found"}), 404

    except Exception as e:
        print(f"   ❌ 删除报错: {e}")
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/messages/<int:msg_id>", methods=["PUT"])
def edit_message(char_id, msg_id):  # <--- 1. 必须加上 char_id 参数
    from app import _sticker_content_from_ai
    # 2. 动态获取该角色的数据库路径
    db_path, _ = get_paths(char_id)

    print(f"--- [Debug] 编辑消息: Char={char_id}, MsgID={msg_id} ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Database not found"}), 404

    new_content = request.json.get("content", "")
    # 编辑内容中的 [表情]名称 由系统自动匹配为 [表情]path 后写入
    new_content = _sticker_content_from_ai(new_content)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM messages WHERE id = ?", (msg_id,))
        old_row = cursor.fetchone()
        if not old_row:
            conn.close()
            return jsonify({"error": "Message ID not found"}), 404
        if parse_voice_message_tag(new_content):
            try:
                validate_voice_message_for_scope(
                    get_current_user_id(),
                    new_content,
                    scope_type="chat",
                    scope_id=char_id,
                    message_id=msg_id,
                )
            except VoiceMessageError as exc:
                conn.close()
                return jsonify({"error": "voice_message_invalid", "message": str(exc)}), 400
        # 执行更新
        cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id))
        conn.commit()
        conn.close()

        old_voice = parse_voice_message_tag(old_row[1]) if old_row[0] == "user" else None
        new_voice = parse_voice_message_tag(new_content)
        if old_voice and (not new_voice or new_voice["filename"] != old_voice["filename"]):
            delete_voice_message_for_message(
                get_current_user_id(),
                old_row[1],
                scope_type="chat",
                scope_id=char_id,
                message_id=msg_id,
            )

        print(f"   ✅ 编辑保存成功")
        return jsonify({"status": "success", "content": new_content})
    except Exception as e:
        print(f"   ❌ 编辑失败: {e}")
        return jsonify({"error": str(e)}), 500


def _get_chat_bg_config_file(char_id):
    """获取聊天背景配置文件路径"""
    _, prompts_dir = get_paths(char_id)
    return os.path.join(prompts_dir, "chat_bg_config.json")


@chat_bp.route("/api/<char_id>/chat_background", methods=["GET"])
def get_chat_background(char_id):
    """获取聊天背景配置"""
    config_file = _get_chat_bg_config_file(char_id)
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            return jsonify(config)
        except:
            return jsonify({"filename": None})
    return jsonify({"filename": None})


@chat_bp.route("/api/<char_id>/upload_chat_background", methods=["POST"])
def upload_chat_background(char_id):
    """上传聊天背景图"""
    try:
        db_path, _ = get_paths(char_id)
        char_dir = os.path.dirname(db_path)
        bg_dir = char_dir  # 直接存放在角色根目录下
        os.makedirs(bg_dir, exist_ok=True)

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        # 删除旧的背景文件（所有格式）
        for old_bg in ("background.png", "background.jpg", "background.jpeg", "background.webp", "background.gif"):
            old_path = os.path.join(bg_dir, old_bg)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception as e:
                    print(f"[CharBackground] 删除旧背景失败: {e}")

        save_path = os.path.join(bg_dir, "background.png")

        # 使用PIL打开图片，统一转换为PNG
        try:
            img = Image.open(file.stream)
            if img.mode in ('RGBA', 'LA', 'P'):
                img_converted = img.convert('RGBA')
            else:
                img_converted = img.convert('RGB')
            img_converted.save(save_path, 'PNG')
        except Exception as e:
            return jsonify({"error": f"Image processing failed: {e}"}), 500

        # 上传到 COS
        user_id = get_current_user_id()
        timestamp = int(time.time())
        cos_path = f"users/{user_id}/characters/{char_id}/background.png"
        cos_url = upload_to_cos(save_path, cos_path)

        # 删除本地临时文件
        if os.path.exists(save_path):
            os.remove(save_path)

        if not cos_url:
            return jsonify({"error": "Failed to upload to COS"}), 500

        new_url = f"{cos_url}?t={timestamp}"
        return jsonify({
            "status": "success",
            "filename": "background.png",
            "url": new_url
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/save_chat_background", methods=["POST"])
def save_chat_background(char_id):
    """保存聊天背景配置"""
    try:
        config = request.json or {}
        config_file = _get_chat_bg_config_file(char_id)
        config_dir = os.path.dirname(config_file)
        os.makedirs(config_dir, exist_ok=True)

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/char_backgrounds/<char_id>/<filename>")
def serve_char_background(char_id, filename):
    """提供聊天背景图，重定向到 COS"""
    user_id = get_current_user_id()
    bucket = os.getenv('COS_BUCKET')
    region = os.getenv('COS_REGION')

    if user_id and bucket and region:
        # 单聊背景统一存放在：users/<uid>/characters/<char_id>/background.png
        # 这里的 filename 通常是 background.png
        cos_path = f"users/{user_id}/characters/{char_id}/{filename}"
        cos_url = f"https://{bucket}.cos.{region}.myqcloud.com/{cos_path}?t={int(time.time())}"
        return redirect(cos_url)

    # 降级：读取本地
    db_path, _ = get_paths(char_id)
    char_dir = os.path.dirname(db_path)
    return send_from_directory(char_dir, filename)


@chat_bp.route("/api/<char_id>/memory/snapshot", methods=["POST"])
def snapshot_memory(char_id):  # <--- 1. 加上 char_id 参数
    now = beijing_now()
    today_str = now.strftime('%Y-%m-%d')

    total_new_count = 0
    message_log = []
    completed_statuses = []

    try:
        # 凌晨检测逻辑
        if now.hour < 4:
            yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            # <--- 2. 传参给工具函数
            result_y = update_short_memory_for_date(char_id, yesterday_str)
            if not result_y.ok:
                return jsonify({
                    "status": result_y.status,
                    "message": result_y.message,
                }), 503 if result_y.status == "busy" else 502
            completed_statuses.append(result_y.status)
            if result_y.count > 0:
                total_new_count += result_y.count
                message_log.append(f"昨天新增 {result_y.count} 条")

        # 处理今天
        # <--- 3. 传参给工具函数
        result_t = update_short_memory_for_date(char_id, today_str)
        if not result_t.ok:
            return jsonify({
                "status": result_t.status,
                "message": result_t.message,
            }), 503 if result_t.status == "busy" else 502
        completed_statuses.append(result_t.status)
        if result_t.count > 0:
            total_new_count += result_t.count
            message_log.append(f"今天新增 {result_t.count} 条")

        if total_new_count > 0:
            return jsonify({
                "status": "success",
                "message": "记忆整理完成: " + "，".join(message_log),
                "count": total_new_count
            })
        else:
            status = (
                "no_messages"
                if completed_statuses and all(item == "no_messages" for item in completed_statuses)
                else "up_to_date"
            )
            message = "当天没有可整理的私聊消息" if status == "no_messages" else "短期记忆已经是最新"
            return jsonify({"status": status, "message": message, "count": 0})

    except Exception as e:
        # 打印详细错误方便调试
        print(f"Snapshot Error: {e}")
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/memory/regenerate_medium", methods=["POST"])
def regenerate_medium_memory(char_id):
    target_date = request.json.get("date")
    if not target_date: return jsonify({"error": "日期不能为空"}), 400
    result = generate_medium_memory_for_date(char_id, target_date)
    if result.status != "success":
        code = 400 if result.status == "no_messages" else (503 if result.status == "busy" else 502)
        return jsonify({
            "status": result.status,
            "error": result.message,
        }), code

    _, prompts_dir = get_paths(char_id)
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")
    with open(medium_file, "r", encoding="utf-8") as f:
        content = (json.load(f) or {}).get(target_date, "")
    return jsonify({"status": "success", "content": content, "count": result.count})



@chat_bp.route("/api/<char_id>/memory/regenerate_long", methods=["POST"])
def regenerate_long_memory(char_id):
    week_key = request.json.get("week_key") # 例如 "2025-12-Week2"
    if not week_key: return jsonify({"error": "Week Key 不能为空"}), 400
    result = generate_long_memory_for_week(char_id, week_key)
    if result.status != "success":
        if result.status in {"invalid_request", "no_messages"}:
            code = 400
        elif result.status == "busy":
            code = 503
        else:
            code = 502
        return jsonify({"status": result.status, "error": result.message}), code
    return jsonify({
        "status": "success",
        "content": result.content,
        "count": result.count,
    })


@chat_bp.route("/api/<char_id>/debug/force_maintenance")
def force_maintenance(char_id):
    from app import scheduled_maintenance
    scheduled_maintenance() # 手动调用上面那个定时函数
    return jsonify({"status": "triggered", "message": "已手动触发后台维护，请查看服务器控制台日志"})


@chat_bp.route("/api/<char_id>/prompts_data")
def get_prompts_data(char_id):
    data = {}
    files = {
        "base": "1_base_persona.json",
        "relation": "2_relationship.json",
        "long": "4_memory_long.json",
        "medium": "5_memory_medium.json",
        "short": "6_memory_short.json",
        "schedule": "7_schedule.json"
    }

    # 1. 使用 get_paths 获取 per-user 路径（支持多用户）
    _, prompts_dir = get_paths(char_id)

    print(f"\n--- [Debug] 正在读取记忆页面数据 ---")
    print(f"   -> 目标文件夹: {prompts_dir}")

    # 2. 检查文件夹是否存在
    if not os.path.exists(prompts_dir):
        print(f"   ❌ 文件夹不存在！请检查路径拼写或是否移动了文件")
        # 这种情况下返回错误信息给前端，方便您在页面上看到
        for key in files:
            data[key] = f"Error: 找不到文件夹 {prompts_dir}"
        return jsonify(data)

    # 3. 读取文件
    for key, filename in files.items():
        content = "（文件不存在或为空）"

        found_path = os.path.join(prompts_dir, filename)
        if not os.path.exists(found_path):
            found_path = None

        if found_path:
            try:
                # 【修改点】把 utf-8 改为 utf-8-sig
                with open(found_path, "r", encoding="utf-8-sig") as f:
                    if filename.endswith(".json"):
                        try:
                            json_content = json.load(f)
                            # 如果是 base 模块，需要提取里面的文本给前端编辑器
                            if key == "base" and isinstance(json_content, dict):
                                content = json_content.get("system_prompt", "")
                            elif key == "schedule":
                                content = normalize_schedule_data(json_content)
                            else:
                                content = json_content
                        except Exception as e:
                            print(f"   ⚠️ JSON 解析失败 [{filename}]: {e} -> 读取原文")
                            f.seek(0)
                            content = f.read()
                    else:
                        content = f.read()
            except Exception as e:
                content = f"读取出错: {e}"
        else:
            print(f"   ⚠️ 文件缺失: {filename}")

        data[key] = content

    return jsonify(data)



@chat_bp.route("/api/<char_id>/relationship_reverse")
def get_relationship_reverse(char_id):
    """
    反向模式：遍历所有其他角色，查看他们对 char_id 的关系定义
    """
    try:
        user_id = get_current_user_id()
        all_chars = _load_relationship_characters(user_id=user_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if char_id not in all_chars:
        return jsonify({"error": "角色不存在"}), 404

    target_info = all_chars.get(char_id, {})
    reverse_data = {}

    for cid, cinfo in all_chars.items():
        if cid == char_id:
            continue
        _, prompts_dir = get_paths(cid, user_id=user_id)
        rel_file = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(rel_file):
            try:
                rel_dict = _read_json_object(rel_file)
                found_key = _find_relationship_key(rel_dict, char_id, target_info)
                if found_key:
                    relation = _normalize_single_relationship(rel_dict[found_key], cid)
                    reverse_data[cid] = _reverse_response_value(cid, relation, all_chars)
            except Exception as e:
                print(f"Error reading relationship for {cid}: {e}")
                continue

    response = jsonify(reverse_data)
    response.headers["X-Relation-Draft-Owner"] = str(user_id or "anonymous")
    return response


@chat_bp.route("/api/<char_id>/relationship_reverse/parse", methods=["POST"])
def parse_relationship_reverse(char_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    try:
        all_chars = _load_relationship_characters(user_id=user_id)
        if char_id not in all_chars:
            return jsonify({"status": "error", "message": "角色不存在"}), 404
        resolved = _resolve_reverse_relationship_graph(
            char_id,
            (request.json or {}).get("content"),
            all_chars,
        )
        graph = {
            source_cid: _reverse_response_value(source_cid, relation, all_chars)
            for source_cid, relation in resolved.items()
        }
        return jsonify({"status": "success", "graph": graph})
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400


def _load_base_persona_excerpt(char_id, user_id, limit=2500):
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    path = os.path.join(prompts_dir, "1_base_persona.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            value = json.load(f)
        if isinstance(value, dict) and isinstance(value.get("system_prompt"), str):
            return value["system_prompt"][:limit]
    except (OSError, json.JSONDecodeError):
        pass
    return ""


@chat_bp.route("/api/<char_id>/relationship_reverse/ai_generate", methods=["POST"])
def ai_generate_relationship_reverse(char_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    payload = request.json or {}
    try:
        all_chars = _load_relationship_characters(user_id=user_id)
        if char_id not in all_chars:
            return jsonify({"error": "角色不存在"}), 404
        requested_ids = payload.get("source_character_ids") or [
            cid for cid in all_chars if cid != char_id
        ]
        if not isinstance(requested_ids, list):
            return jsonify({"error": "source_character_ids 必须是数组"}), 400
        source_ids = []
        for raw_cid in requested_ids:
            source_cid = str(raw_cid or "").strip()
            if source_cid == char_id:
                continue
            if source_cid not in all_chars:
                return jsonify({"error": f"角色不存在: {source_cid}"}), 400
            if source_cid not in source_ids:
                source_ids.append(source_cid)
        if not source_ids:
            return jsonify({"error": "请选择至少一个其他角色"}), 400
        if len(source_ids) > 20:
            return jsonify({"error": "一次最多生成 20 个角色视角"}), 400

        target_info = all_chars[char_id] or {}
        target_name = target_info.get("name") or char_id
        current_reverse = payload.get("current_relationships") or {}
        sources = []
        for source_cid in source_ids:
            info = all_chars[source_cid] or {}
            sources.append({
                "id": source_cid,
                "name": info.get("name") or source_cid,
                "remark": info.get("remark") or "",
                "persona": _load_base_persona_excerpt(source_cid, user_id),
                "current_view": current_reverse.get(source_cid, {}),
            })

        prompt = (
            "你是角色关系设定编辑器。请分别站在每个来源角色的独立第一视角，"
            f"生成他们如何看待目标角色“{target_name}”（ID: {char_id}）的关系。\n"
            "不要机械复制或反转目标角色对他们的看法；必须符合每个来源角色自己的人设。\n"
            "只允许输出下列来源角色，不要虚构新角色。JSON 顶层键必须使用来源角色 ID。\n"
            "每项格式必须为 {\"role\":字符串,\"score\":0到5数字,\"description\":字符串}。\n"
            "只输出纯 JSON，不要代码块或解释。\n\n"
            f"目标角色人设：\n{_load_base_persona_excerpt(char_id, user_id)}\n\n"
            f"来源角色资料：\n{json.dumps(sources, ensure_ascii=False)}"
        )
        messages = [{"role": "user", "content": prompt}]
        route, model = get_model_config("gen_persona", user_id=user_id)
        if route == "relay":
            response_text = call_openrouter(messages, model_name=model, user_id=user_id)
        else:
            response_text = call_gemini(messages, model_name=model, user_id=user_id)
        resolved = _resolve_reverse_relationship_graph(char_id, response_text, all_chars)
        unexpected = sorted(set(resolved) - set(source_ids))
        if unexpected:
            raise ValueError(f"AI 返回了未选择的角色: {', '.join(unexpected)}")
        graph = {
            source_cid: _reverse_response_value(source_cid, relation, all_chars)
            for source_cid, relation in resolved.items()
        }
        return jsonify({"status": "success", "graph": graph})
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/relationships/save_all", methods=["POST"])
def save_all_relationship_views(char_id):
    """Atomically save the current character graph and reverse entries in source files."""
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    payload = request.json or {}
    try:
        all_chars = _load_relationship_characters(user_id=user_id)
        if char_id not in all_chars:
            return jsonify({"error": "角色不存在"}), 404
        normal = normalize_relationship_graph(payload.get("normal") or {})
        reverse = _resolve_reverse_relationship_graph(
            char_id,
            payload.get("reverse") or {},
            all_chars,
        )
        deleted_ids = []
        for raw_cid in payload.get("deleted_reverse_ids") or []:
            source_cid = str(raw_cid or "").strip()
            if source_cid == char_id or source_cid not in all_chars:
                raise ValueError(f"无效的来源角色: {source_cid}")
            if source_cid not in deleted_ids:
                deleted_ids.append(source_cid)
        deleted_ids = [cid for cid in deleted_ids if cid not in reverse]

        updates = {}
        _, target_prompts = get_paths(char_id, user_id=user_id)
        target_file = os.path.join(target_prompts, "2_relationship.json")
        updates[target_file] = normal

        target_info = all_chars[char_id] or {}
        target_name = target_info.get("name") or char_id
        for source_cid in list(reverse) + deleted_ids:
            _, source_prompts = get_paths(source_cid, user_id=user_id)
            source_file = os.path.join(source_prompts, "2_relationship.json")
            source_graph = dict(_read_json_object(source_file))
            found_key = _find_relationship_key(source_graph, char_id, target_info)
            if source_cid in reverse:
                source_graph[found_key or target_name] = reverse[source_cid]
            elif found_key:
                del source_graph[found_key]
            updates[source_file] = source_graph

        backups = {
            path: _read_json_object(path) if os.path.exists(path) else None
            for path in updates
        }
        written = []
        try:
            for path, value in updates.items():
                _write_json_atomic(path, value)
                written.append(path)
        except Exception:
            for path in reversed(written):
                backup = backups[path]
                if backup is None:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                else:
                    _write_json_atomic(path, backup)
            raise

        reverse_response = {
            source_cid: _reverse_response_value(source_cid, relation, all_chars)
            for source_cid, relation in reverse.items()
        }
        return jsonify({
            "status": "success",
            "normal": normal,
            "reverse": reverse_response,
        })
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/save_relationship_reverse", methods=["POST"])
def save_relationship_reverse(char_id):
    """
    保存反向关系：其实就是去修改“对方”的角色关系文件
    """
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    payload = request.json or {}
    source_cid = payload.get("source_cid") # “对方”的ID
    rel_data = payload.get("data") # 新的关系内容

    if not source_cid:
        return jsonify({"error": "缺少 source_cid"}), 400

    # 获取当前角色的名字和ID
    try:
        all_chars = _load_relationship_characters(user_id=user_id)
        if char_id not in all_chars or source_cid not in all_chars or source_cid == char_id:
            return jsonify({"error": "无效的关系角色"}), 400
        target_name = all_chars.get(char_id, {}).get("name") or char_id
        target_info = all_chars.get(char_id, {}) or {}
        if rel_data is not None:
            rel_data = _normalize_single_relationship(rel_data, source_cid)
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "读取配置失败"}), 500

    # 定位“对方”的关系文件
    _, prompts_dir = get_paths(source_cid, user_id=user_id)
    rel_file = os.path.join(prompts_dir, "2_relationship.json")

    try:
        current_rel = {}
        if os.path.exists(rel_file):
            current_rel = _read_json_object(rel_file)

        # 兼容性查找：看看是用名字存的还是用 ID 存的
        found_key = _find_relationship_key(current_rel, char_id, target_info)

        if rel_data is None:
            # 删除逻辑
            if found_key:
                del current_rel[found_key]
        else:
            # 更新逻辑：如果已存在键则更新，否则新增一个键（优先用名字）
            target_key = found_key or target_name
            current_rel[target_key] = rel_data

        # 写回
        _write_json_atomic(rel_file, current_rel)

        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/save_prompt", methods=["POST"])
def save_prompt_file(char_id):
    payload = request.json or {}
    key = payload.get("key")
    new_content = payload.get("content")

    # 获取该角色的 Prompt 目录
    db_path, prompts_dir = get_paths(char_id)

    # 映射 Key 到 文件名
    files_map = {
        "base": "1_base_persona.json",
        "relation": "2_relationship.json",
        "long": "4_memory_long.json",
        "medium": "5_memory_medium.json",
        "short": "6_memory_short.json",
        "schedule": "7_schedule.json",
    }

    filename = files_map.get(key)
    if not filename:
        return jsonify({"status": "error", "message": "Invalid key"}), 400

    path = os.path.join(prompts_dir, filename)

    try:
        if key == "relation":
            new_content = normalize_relationship_graph(new_content)
        elif key == "schedule":
            new_content = normalize_schedule_data(new_content)

        if key == "base":
            json_path = os.path.join(prompts_dir, "1_base_persona.json")
            # LOCK 标签只约束模型编辑，不限制用户本人修改。
            if isinstance(new_content, dict):
                persona_text = new_content.get("system_prompt")
            else:
                persona_text = new_content
            if not isinstance(persona_text, str):
                return jsonify({"status": "error", "message": "system_prompt 必须是文本"}), 400
            validate_persona_locks(persona_text)

            with memory_file_lock(json_path):
                atomic_write_json(json_path, {"system_prompt": persona_text})
            return jsonify({"status": "success"})

        # --- 【核心新增】如果是保存短期记忆，自动校准 last_id ---
        if key == "short" and isinstance(new_content, dict):
            # 必须使用当前用户、当前角色自己的聊天数据库。旧代码连接了
            # 项目根目录的 chat_history.db，会读到错误角色甚至直接报错。
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            for date_str, day_data in new_content.items():
                # 1. 获取用户编辑后的事件列表
                events = []
                if isinstance(day_data, dict):
                    events = day_data.get("events", [])
                elif isinstance(day_data, list):
                    events = day_data # 兼容旧格式

                # 2. 如果列表被清空了，last_id 直接重置为 0 (全量重读)
                if not events:
                    if isinstance(day_data, dict): day_data['last_id'] = 0
                    else: new_content[date_str] = {"events": [], "last_id": 0}
                    print(f"[{date_str}] 事件被清空，进度重置为 0")
                    continue

                # 3. 如果还有事件，找到【最后一条事件】的时间
                last_event_time = events[-1].get('time', '00:00')

                # 4. 去数据库查这个时间点对应的最后一条消息 ID
                # 构造查询时间：精确到当天的这一分钟的最后一秒
                query_ts = f"{date_str} {last_event_time}:59"

                # 查找 <= 这个时间的最大 ID
                day_start = f"{date_str} 00:00:00"
                cursor.execute(
                    "SELECT MAX(id) FROM messages WHERE timestamp >= ? AND timestamp <= ?",
                    (day_start, query_ts),
                )
                res = cursor.fetchone()

                if res and res[0]:
                    calibrated_id = res[0]
                    # 更新 last_id
                    if isinstance(day_data, dict):
                        day_data['last_id'] = calibrated_id
                    else:
                        new_content[date_str] = {"events": events, "last_id": calibrated_id}
                    print(f"[{date_str}] 智能回滚: 锚定时间 {last_event_time} -> 重置 ID 为 {calibrated_id}")
                else:
                    # 查不到 ID (可能时间填错了)，保险起见不改，或者设为0
                    pass

            conn.close()
        # ----------------------------------------------------

        if key in {"short", "schedule"} and isinstance(new_content, dict):
            with memory_file_lock(path):
                atomic_write_json(path, new_content)
        else:
            with open(path, "w", encoding="utf-8") as f:
                if filename.endswith(".json") and isinstance(new_content, (dict, list)):
                    json.dump(new_content, f, ensure_ascii=False, indent=2)
                else:
                    f.write(str(new_content))
        return jsonify({"status": "success"})
    except ScheduleValidationError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except PersonaLockError as e:
        return jsonify({"status": "error", "code": e.code, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@chat_bp.route("/api/<char_id>/search", methods=["POST"])
def search_messages(char_id):
    keyword = request.json.get("keyword", "").strip()
    if not keyword: return jsonify([])

    # 1. 使用 get_paths 获取 per-user 数据库路径
    db_path, _ = get_paths(char_id)

    print(f"\n--- [Debug] 正在搜索: {keyword} ---")
    print(f"   -> 目标数据库: {db_path}")

    if not os.path.exists(db_path):
        print(f"   ❌ 数据库文件不存在！")
        return jsonify([])

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 2. 模糊搜索
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE content LIKE ? ORDER BY timestamp DESC", (f"%{keyword}%",))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        print(f"   ✅ 搜索完成，找到 {len(rows)} 条结果")
        return jsonify(rows)

    except Exception as e:
        print(f"   ❌ 数据库查询报错: {e}")
        return jsonify([])


@chat_bp.route("/api/<char_id>/config")
def get_char_details(char_id):
    cfg_file = _get_characters_config_file()

    if not os.path.exists(cfg_file):
        return jsonify({})

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        char_info = all_config.get(char_id)
        if char_info:
            if ensure_character_time_defaults(char_info, existing_character=True):
                safe_save_json(cfg_file, all_config)
            # 【新增】定义默认配置字典
            defaults = {
                "emotion": 1,
                "moments_index": 1,
                "emotion_locked": False,
                "moments_index_locked": False,
                "intimacy": 60,
                "light_sleep": True,
                "deep_sleep": False,
                "ds_start": "23:00",
                "ds_end": "07:00",
                "age": None,
                "tickle_suffix": "",
                "language": "",
                "chat_mode": "online",
                "bedtime_diary_enabled": True,
                "timezone": get_character_timezone(char_info),
                "timezone_source": char_info.get("timezone_source", "system_default"),
                "ds_time_basis": char_info.get("ds_time_basis", "user"),
            }
            # 将默认值合并进去 (如果 char_info 里没有该字段，就用默认的)
            # 这里的逻辑是：char_info 覆盖 defaults (已有的配置优先)
            final_info = defaults.copy()
            final_info.update(char_info)
            final_info["time_preview"] = sleep_preview(
                final_info,
                _load_user_settings(),
            )

            return jsonify(final_info)
        else:
            return jsonify({"error": "Character not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/update_meta", methods=["POST"])
def update_char_meta(char_id):
    CONFIG_FILE = _get_characters_config_file()

    if not os.path.exists(CONFIG_FILE):
        return jsonify({"error": "Config file not found"}), 404

    try:
        with memory_file_lock(CONFIG_FILE):
            return _update_char_meta_locked(char_id, CONFIG_FILE)
    except Exception as e:
        print(f"Update Meta Error: {e}")
        return jsonify({"error": str(e)}), 500


def _update_char_meta_locked(char_id, CONFIG_FILE):
    try:
        # 1. 读取现有配置
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        if char_id not in all_config:
            return jsonify({"error": "Character ID not found"}), 404

        # 2. 更新字段 (只更新前端传过来的字段)
        data = request.json or {}
        for field in ("emotion_locked", "moments_index_locked"):
            if field in data and not isinstance(data[field], bool):
                return jsonify({"error": f"{field} must be a boolean"}), 400
        info = all_config[char_id]
        ensure_character_time_defaults(info, existing_character=True)
        print(f"[update_meta] char={char_id} file={CONFIG_FILE} data={data}")
        new_remark = data.get("remark")
        new_avatar = data.get("avatar")
        new_pinned = data.get("pinned") # <--- 【新增】获取置顶状态
        new_language = data.get("language") # <--- 【新增】

        # 允许改为空字符串，所以用 is not None 判断
        if new_remark is not None:
            all_config[char_id]["remark"] = new_remark.strip()

        if new_avatar is not None:
            all_config[char_id]["avatar"] = new_avatar.strip()

        # 【新增】更新置顶状态 (必须判断是否为 None，因为 False 也是有效值)
        if new_pinned is not None:
            all_config[char_id]["pinned"] = bool(new_pinned)

        # 【新增】更新语言设置
        if new_language is not None:
            info["language"] = new_language.strip()
            if info.get("timezone_source") in {
                "system_default",
                "language_default",
            }:
                info["timezone"] = default_character_timezone(new_language)
                info["timezone_source"] = (
                    "language_default"
                    if info["timezone"] == "Asia/Tokyo"
                    else "system_default"
                )

        if data.get("timezone") is not None:
            timezone_name = str(data.get("timezone") or "").strip()
            if not is_valid_timezone(timezone_name):
                return jsonify({"error": "Invalid IANA timezone"}), 400
            info["timezone"] = timezone_name
            info["timezone_source"] = "manual"
            info.pop("timezone_location_id", None)

        # 【新增】更新语音ID
        new_voice_id = data.get("voice_id")
        if new_voice_id is not None:
            all_config[char_id]["voice_id"] = new_voice_id.strip() if new_voice_id else ""

        # 【新增】更新语音情绪
        new_voice_emotion = data.get("voice_emotion")
        if new_voice_emotion is not None:
            v = new_voice_emotion.strip() if new_voice_emotion else ""
            all_config[char_id]["voice_emotion"] = v
            print(f"[update_meta] char={char_id} voice_emotion={v!r}")

        # 聊天模式 (online/offline)
        if data.get("chat_mode") is not None:
            all_config[char_id]["chat_mode"] = data["chat_mode"]

        # --- 【新增】生理节律状态 ---
        # Locks restrict AI actions only; direct user edits remain available.
        for field in ("emotion_locked", "moments_index_locked"):
            if field in data:
                info[field] = data[field]

        # 情绪 (0-100)
        if data.get("emotion") is not None:
            all_config[char_id]["emotion"] = float(data["emotion"])

        # 性格指数 (影响主动发朋友圈概率，默认 1)
        if data.get("moments_index") is not None:
            all_config[char_id]["moments_index"] = float(data["moments_index"])

        # 亲密度 (0-100，影响用户发朋友圈后该角色的点赞/评论概率)
        if data.get("intimacy") is not None:
            v = int(data["intimacy"])
            all_config[char_id]["intimacy"] = max(0, min(100, v))

        # 浅睡眠 (Bool)
        if data.get("light_sleep") is not None:
            all_config[char_id]["light_sleep"] = bool(data["light_sleep"])

        # 深睡眠 (Bool)
        if data.get("deep_sleep") is not None:
            info["deep_sleep"] = bool(data["deep_sleep"])
            info["deep_sleep_source"] = "manual_user"
            info["sleep_manual_override"] = True

        # 深睡眠自动时间段 (Start, End)
        sleep_fields_changed = (
            data.get("ds_start") is not None or data.get("ds_end") is not None
        )
        if sleep_fields_changed:
            proposed_start = data.get("ds_start", info.get("ds_start", "23:00"))
            proposed_end = data.get("ds_end", info.get("ds_end", "07:00"))
            if parse_hhmm(proposed_start) is None or parse_hhmm(proposed_end) is None:
                return jsonify({"error": "Sleep time must use HH:MM"}), 400
            if proposed_start == proposed_end:
                return jsonify({"error": "Sleep start and end cannot be equal"}), 400
            info["ds_start"] = proposed_start
            info["ds_end"] = proposed_end
            # 手动输入的 HH:MM 同样按角色当地时间解释；设置人不决定时区。
            info["ds_time_basis"] = "character"
            info["ds_set_by"] = "user"
            info["ds_timezone_at_set"] = get_character_timezone(info)
            info["sleep_last_event_key"] = None
            info["sleep_manual_override"] = False

        # 睡前总结 (Bool)
        if data.get("bedtime_diary_enabled") is not None:
            all_config[char_id]["bedtime_diary_enabled"] = bool(data["bedtime_diary_enabled"])

        # 拍一拍后缀，默认允许为空
        if data.get("tickle_suffix") is not None:
            all_config[char_id]["tickle_suffix"] = str(data["tickle_suffix"]).strip()

        # 年龄（单独编辑，来自记忆页面）
        if data.get("age") is not None:
            try:
                age_val = data["age"]
                if age_val == "" or age_val is None:
                    all_config[char_id].pop("age", None)
                    all_config[char_id].pop("age_last_incremented", None)
                else:
                    all_config[char_id]["age"] = int(age_val)
            except (ValueError, TypeError):
                pass

        # 3. 写回文件
        # Propagate write failures so the lock switch cannot report false success.
        atomic_write_json(CONFIG_FILE, all_config)

        # 调试：立即读回确认写入
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            verify = json.load(f)
        print(f"[update_meta] 写入后确认 voice_emotion={verify.get(char_id, {}).get('voice_emotion', 'KEY MISSING')!r}")

        return jsonify({
            "status": "success",
            "timezone": get_character_timezone(verify.get(char_id, {})),
            "time_preview": sleep_preview(
                verify.get(char_id, {}),
                _load_user_settings(),
            ),
        })

    except Exception as e:
        print(f"Update Meta Error: {e}")
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/upload_avatar", methods=["POST"])
def upload_char_avatar(char_id):
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            # 1. 使用 get_paths 获取 per-user 角色目录
            db_path, _ = get_paths(char_id)
            char_dir = os.path.dirname(db_path)
            if not os.path.exists(char_dir):
                os.makedirs(char_dir, exist_ok=True)

            # 删除旧的头像文件（所有格式）
            for old_avatar in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp", "avatar.gif"):
                old_path = os.path.join(char_dir, old_avatar)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception as e:
                        print(f"[CharAvatar] 删除旧头像失败: {e}")

            # 统一保存为 avatar.png
            file_path = os.path.join(char_dir, "avatar.png")

            # 使用PIL打开图片，转换为PNG格式并保存
            try:
                img = Image.open(file.stream)
                # 如果是RGBA模式（带透明度），保留透明度；否则转换为RGB
                if img.mode in ('RGBA', 'LA', 'P'):
                    img_converted = img.convert('RGBA')
                else:
                    img_converted = img.convert('RGB')
                # 保存为PNG
                img_converted.save(file_path, 'PNG')
            except Exception as e:
                print(f"[CharAvatar] PIL转换失败，直接保存: {e}")
                # 如果PIL转换失败，直接保存原始文件
                file.seek(0)
                file.save(file_path)

            # 上传到 COS
            user_id = get_current_user_id()
            timestamp = int(time.time())
            cos_path = f"users/{user_id}/characters/{char_id}/avatar.png"
            cos_url = upload_to_cos(file_path, cos_path)

            # 删除本地临时文件
            if os.path.exists(file_path):
                os.remove(file_path)

            if not cos_url:
                return jsonify({"error": "Failed to upload to COS"}), 500

            new_url = f"{cos_url}?t={timestamp}"

            # 3. 更新 characters.json 里的路径（per-user）
            cfg_file = _get_characters_config_file()
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)

            all_config[char_id]["avatar"] = new_url

            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(all_config, f, ensure_ascii=False, indent=2)

            return jsonify({"status": "success", "url": new_url})

        except Exception as e:
            print(f"Upload Error: {e}")
            return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<target_char_id>/copy_schedule", methods=["POST"])
def copy_other_schedule(target_char_id):
    source_char_id = request.json.get("source_id")

    # 1. 获取源路径 和 目标路径
    # 修复：不再使用固定的全局 BASE_DIR，而是使用 get_paths 动态获取当前用户的角色路径
    _, source_prompts_dir = get_paths(source_char_id)
    source_path = os.path.join(source_prompts_dir, "7_schedule.json")

    _, target_prompts_dir = get_paths(target_char_id)
    target_path = os.path.join(target_prompts_dir, "7_schedule.json")

    if not os.path.exists(source_path):
        return jsonify({"error": "源角色的日程文件不存在"}), 404

    try:
        # 2. 读取源文件
        with open(source_path, "r", encoding="utf-8-sig") as f:
            source_data = normalize_schedule_data(json.load(f))

        # 3. 写入目标文件 (覆盖)
        with memory_file_lock(target_path):
            atomic_write_json(target_path, source_data)

        return jsonify({"status": "success", "data": source_data})

    except ScheduleValidationError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/character/<char_id>/delete", methods=["DELETE"])
def delete_character_api(char_id):
    user_id = get_current_user_id()
    config_file = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(config_file):
        return jsonify({"error": "Config not found"}), 404

    def load_json(path, default):
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8-sig") as f:
            value = json.load(f)
        return value if value is not None else default

    def write_json_or_raise(path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

    try:
        all_config = load_json(config_file, {})

        if char_id not in all_config:
            return jsonify({"error": "Character not found"}), 404

        deleted_info = all_config[char_id]
        deleted_names = {
            char_id,
            (deleted_info.get("name") or "").strip(),
            (deleted_info.get("remark") or "").strip(),
        }
        deleted_names.discard("")

        updates = {}
        stats = {
            "posts": 0,
            "likes": 0,
            "comments": 0,
            "positions": 0,
            "relationships": 0,
            "groups": 0,
        }

        new_config = dict(all_config)
        del new_config[char_id]
        updates[config_file] = new_config

        positions_file = _get_character_positions_file(user_id=user_id)
        positions = load_json(positions_file, {})
        if char_id in positions:
            positions = dict(positions)
            del positions[char_id]
            stats["positions"] = 1
            updates[positions_file] = positions

        config_dir = os.path.dirname(config_file)
        moments_file = os.path.join(config_dir, "moments_data.json")
        last_post_file = os.path.join(config_dir, "moments_last_post.json")
        moments = load_json(moments_file, [])
        if isinstance(moments, list):
            cleaned_moments = []
            for post in moments:
                if not isinstance(post, dict):
                    cleaned_moments.append(post)
                    continue
                if post.get("char_id") == char_id:
                    stats["posts"] += 1
                    continue

                cleaned_post = dict(post)
                likers = post.get("likers") or []
                cleaned_likers = [
                    item for item in likers
                    if not isinstance(item, dict) or item.get("liker_id") != char_id
                ]
                stats["likes"] += len(likers) - len(cleaned_likers)
                if "likers" in post:
                    cleaned_post["likers"] = cleaned_likers

                liker_ids = post.get("liker_ids") or []
                cleaned_liker_ids = [liker_id for liker_id in liker_ids if liker_id != char_id]
                stats["likes"] += len(liker_ids) - len(cleaned_liker_ids)
                if "liker_ids" in post:
                    cleaned_post["liker_ids"] = cleaned_liker_ids

                comments = post.get("comments") or []
                cleaned_comments = [
                    comment for comment in comments
                    if not isinstance(comment, dict)
                    or (
                        comment.get("commenter_id") != char_id
                        and comment.get("reply_to") != char_id
                    )
                ]
                stats["comments"] += len(comments) - len(cleaned_comments)
                if "comments" in post:
                    cleaned_post["comments"] = cleaned_comments
                cleaned_moments.append(cleaned_post)
            if cleaned_moments != moments:
                updates[moments_file] = cleaned_moments

        last_post = load_json(last_post_file, {})
        if isinstance(last_post, dict) and char_id in last_post:
            last_post = dict(last_post)
            del last_post[char_id]
            updates[last_post_file] = last_post

        groups_file = _get_groups_config_file(user_id=user_id)
        groups = load_json(groups_file, {})
        if isinstance(groups, dict):
            cleaned_groups = {}
            for group_id, group_info in groups.items():
                if not isinstance(group_info, dict):
                    cleaned_groups[group_id] = group_info
                    continue
                members = group_info.get("members") or []
                cleaned_members = [member_id for member_id in members if member_id != char_id]
                if cleaned_members != members:
                    group_info = dict(group_info)
                    group_info["members"] = cleaned_members
                    stats["groups"] += 1
                cleaned_groups[group_id] = group_info
            if cleaned_groups != groups:
                updates[groups_file] = cleaned_groups

        for other_char_id in new_config:
            _, other_prompts_dir = get_paths(other_char_id, user_id=user_id)
            relation_file = os.path.join(other_prompts_dir, "2_relationship.json")
            relation_data = load_json(relation_file, {})
            if not isinstance(relation_data, dict):
                continue
            cleaned_relation = {
                key: value
                for key, value in relation_data.items()
                if str(key).strip() not in deleted_names
            }
            if cleaned_relation != relation_data:
                stats["relationships"] += len(relation_data) - len(cleaned_relation)
                updates[relation_file] = cleaned_relation

        backups = {
            path: load_json(path, None) if os.path.exists(path) else None
            for path in updates
        }
        written_paths = []
        try:
            for path, value in updates.items():
                write_json_or_raise(path, value)
                written_paths.append(path)
        except Exception:
            for path in reversed(written_paths):
                backup = backups[path]
                if backup is None:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                else:
                    write_json_or_raise(path, backup)
            raise

        db_path, _ = get_paths(char_id, user_id=user_id)
        char_dir = os.path.dirname(db_path)
        if os.path.exists(char_dir):
            shutil.rmtree(char_dir)
        remove_read_state(_get_read_status_file(), "chat", char_id)
        if str(user_id or "").isdigit():
            from services.voice_calls import delete_calls_for_character
            delete_calls_for_character(user_id, char_id)
        return jsonify({"status": "success", "deleted": stats})

    except Exception as e:
        print(f"Delete Character Error: {e}")
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/memory/regenerate_short", methods=["POST"])
def regenerate_short_memory_api(char_id):
    data = request.json
    target_date = data.get("date")
    force = data.get("force", False) # 是否强制重读

    if not target_date:
        return jsonify({"error": "日期不能为空"}), 400

    try:
        result = update_short_memory_for_date(char_id, target_date, force_reset=force)
        if not result.ok:
            code = 503 if result.status == "busy" else 502
            return jsonify({
                "status": result.status,
                "error": result.message,
            }), code

        # 为了前端方便，返回最新的完整数据（因为update函数只返回了新增的）
        # 我们重新读一次文件返回给前端刷新
        _, prompts_dir = get_paths(char_id)
        short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")
        full_data = {}
        if os.path.exists(short_mem_path):
            with open(short_mem_path, "r", encoding="utf-8") as f:
                full_data = json.load(f)
        day_data = full_data.get(target_date, {})
        # 统一返回 dict 格式
        if isinstance(day_data, list):
            day_data = {"events": day_data, "last_id": 0}

        return jsonify({
            "status": "success",
            "result_status": result.status,
            "added_count": result.count,
            "data": day_data
        })

    except Exception as e:
        print(f"Regen Short Error: {e}")
        return jsonify({"error": str(e)}), 500


# ==================== Agent Web Cruise API ====================

def _get_agent_state_path(user_id):
    return os.path.join(USERS_ROOT, str(user_id), "agent_state.json")

def _get_agent_input_path(user_id):
    return os.path.join(USERS_ROOT, str(user_id), "agent_input.json")

def _ensure_agent_user_dir(user_id):
    user_dir = os.path.join(USERS_ROOT, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


@chat_bp.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """Agent专用AI调用 - 不存DB, 返回原始回复"""
    data = request.json
    if not data:
        return jsonify({"error": "missing body"}), 400
    messages = data.get("messages")
    char_id = data.get("char_id", "unknown")
    user_id = data.get("user_id")

    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages required"}), 400
    if not user_id:
        user_id = get_current_user_id()

    set_background_user(user_id)

    try:
        route, current_model = get_model_config("chat", user_id=user_id)
        if route == "relay":
            reply = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        else:
            reply = call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        return jsonify({"reply": reply, "model": current_model})
    except Exception as e:
        print(f"Agent Chat Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/agent/notify_user", methods=["POST"])
def agent_notify_user():
    """Agent发送可见消息给用户 - 存入chat DB"""
    data = request.json
    if not data:
        return jsonify({"error": "missing body"}), 400
    char_id = data.get("char_id")
    content = data.get("content", "")
    user_id = data.get("user_id")

    if not char_id:
        return jsonify({"error": "char_id required"}), 400
    if not user_id:
        user_id = get_current_user_id()

    set_background_user(user_id)

    try:
        db_path, _ = get_paths(char_id, user_id=user_id)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        ai_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", content, ai_ts))
        msg_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "id": msg_id})
    except Exception as e:
        print(f"Agent Notify Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/agent/reply", methods=["POST"])
def agent_reply():
    """用户回复Agent的ASK/WAIT - 写入IPC文件, 同时存用户消息到DB"""
    data = request.json
    if not data:
        return jsonify({"error": "missing body"}), 400
    message = data.get("message", "")
    user_id = data.get("user_id")
    char_id = data.get("char_id")

    if not user_id:
        user_id = get_current_user_id()

    _ensure_agent_user_dir(user_id)

    input_data = {
        "command": "reply",
        "message": message
    }

    with open(_get_agent_input_path(user_id), "w", encoding="utf-8") as f:
        json.dump(input_data, f, ensure_ascii=False)

    if char_id and message:
        try:
            set_background_user(user_id)
            db_path, _ = get_paths(char_id, user_id=user_id)
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            user_ts = beijing_now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", message, user_ts))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Agent reply save user msg error: {e}")

    return jsonify({"status": "ok"})


@chat_bp.route("/api/agent/stop", methods=["POST"])
def agent_stop():
    """用户停止Agent - 写入IPC停止信号"""
    data = request.json or {}
    user_id = data.get("user_id") or get_current_user_id()

    _ensure_agent_user_dir(user_id)

    input_data = {"command": "stop", "message": ""}
    with open(_get_agent_input_path(user_id), "w", encoding="utf-8") as f:
        json.dump(input_data, f, ensure_ascii=False)

    return jsonify({"status": "ok"})


@chat_bp.route("/api/agent/web_action", methods=["POST"])
def agent_web_action():
    """前端检测到网页指令时通知Agent - 写入IPC文件"""
    data = request.json or {}
    user_id = data.get("user_id") or get_current_user_id()
    tags = data.get("tags", [])

    _ensure_agent_user_dir(user_id)

    input_data = {"command": "web_action", "tags": tags, "source": data.get("source", "user")}
    with open(_get_agent_input_path(user_id), "w", encoding="utf-8") as f:
        json.dump(input_data, f, ensure_ascii=False)

    return jsonify({"status": "ok"})


@chat_bp.route("/api/agent/state", methods=["GET"])
def agent_state():
    """查询Agent当前状态"""
    user_id = request.args.get("user_id") or get_current_user_id()
    state_path = _get_agent_state_path(user_id)

    if not os.path.exists(state_path):
        return jsonify({"running": False, "status": "stopped"})

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        return jsonify(state)
    except Exception as e:
        return jsonify({"running": False, "status": "error", "error": str(e)})
