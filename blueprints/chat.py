# -*- coding: utf-8 -*-
"""Single-chat blueprint: /api/<char_id>/* routes extracted from app.py."""
import os
import time
import json
import re
import sqlite3
import shutil
import threading
from datetime import datetime, timedelta

from flask import (
    Blueprint, request, jsonify, session, redirect, send_from_directory,
)
from PIL import Image

from core.config import COS_BASE_URL, CHARACTERS_DIR, USERS_ROOT, DATABASE_FILE
from core.context import get_current_user_id, set_background_user
from core.circuit_breaker import get_circuit_breaker_info
from core.utils import (
    get_paths, safe_save_json, _add_furigana_to_japanese,
    _get_characters_config_file, _get_read_status_file,
)
from services import (
    call_gemini, call_openrouter, get_model_config, build_system_prompt_v2,
    call_ai_to_summarize, update_short_memory_for_date,
)
from services.prompt_builder import build_messages_for_chat_v2, get_ai_language
from agent_utils import process_agent_actions
from cos_utils import upload_to_cos, get_cos_list

chat_bp = Blueprint('chat', __name__)


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


def get_char_db_path(char_id) -> str:
    """获取指定角色的 DB 路径（内部复用 get_paths，确保与多用户命名空间一致）。"""
    db_path, _ = get_paths(char_id)
    return db_path


def mark_char_as_read(char_id):
    """更新某个角色/群聊的最后阅读时间（写入当前用户的 read_status.json）"""
    try:
        status_file = _get_read_status_file()
        data = {}
        if os.path.exists(status_file):
            with open(status_file, "r", encoding="utf-8") as f:
                data = json.load(f)

        # 记录当前时间（char_id 或 group_id 均可用作 key）
        data[char_id] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except: pass


@chat_bp.route("/api/<char_id>/mark_read", methods=["POST"])
def mark_read_api(char_id):
    mark_char_as_read(char_id)
    return jsonify({"status": "success"})



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


@chat_bp.route("/api/<char_id>/chat", methods=["POST"])
def chat(char_id):
    from app import init_char_db, sync_memory_before_single_chat, process_ai_media_tags, _execute_directive, _check_consecutive_tickle, _strip_consecutive_tickle, _extract_tickle_target, _sticker_content_from_ai
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

    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')

    # 存用户消息
    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))
    user_msg_id = cursor.lastrowid # 获取 ID

    conn.commit()
    conn.close()

    # --- 5. 如果在深睡眠，直接返回空回复，不调 AI ---
    if is_deep_sleep:
        print(f"--- [Deep Sleep] {char_id} 正在熟睡，不回复消息 ---")

        # 即使不回复，也把 user_id 传回去，这样用户发的气泡才有删除按钮
        return jsonify({
            "replies": [],
            "id": None,
            "user_id": user_msg_id
        })

    # ================= 醒着：正常调用 AI 逻辑 =================

    # --- 5.5 单聊前自动同步：总结该角色参与的群聊短期记忆 ---
    memory_sync_warning = None
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
    now = datetime.now()
    lang = get_ai_language(char_id, user_id=user_id)
    hour = now.hour

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
            f"（系统提示：现在是{period} {now.strftime('%H:%M')}。）\n"
            f"（用户发来了一条消息。请根据时间线中的上下文，回复用户的消息。）\n"
            f"（要求：自然、简短，不要重复上一句话。）\n"
            f"（无特殊说明时用斜线表示换行和句号。）"
        )
    elif lang == "ja":
        system_hint = (
            f"（システム通知：現在は{period} {now.strftime('%H:%M')}です。）\n"
            f"（ユーザーからメッセージが来ました。タイムライン内容を踏まえて回信してください。）\n"
            f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）\n"
            f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
        )
    else:
        system_hint = (
            f"(System Hint: It is now {period} {now.strftime('%H:%M')}.)\n"
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

        # 清理时间戳
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()

        # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
        try:
            cleaned_reply_text, affinity_delta, _dir = process_agent_actions(char_id, cleaned_reply_text, get_current_user_id())
        except Exception as e:
            print(f"  ❌ [Directive] process_agent_actions 崩溃: {e}", flush=True)
            import traceback
            traceback.print_exc()
            _dir = None
            affinity_delta = None
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
                _ddir, _cid, _ctxt = _dir, char_id, cleaned_reply_text
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
        now = datetime.now()
        user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        ai_ts = (now + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 存 AI 消息
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", cleaned_reply_text, ai_ts))

        # 【重点】获取刚插入的 AI 消息的 ID
        ai_msg_id = cursor.lastrowid

        conn.commit()
        conn.close()

        reply_bubbles = list(filter(None, [part.strip() for part in cleaned_reply_text.split('/')]))

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
        cb_info = get_circuit_breaker_info()
        if cb_info:
            resp["circuit_breaker"] = cb_info
        return jsonify(resp)

    except Exception as e:
        print(f"Chat Error: {e}")
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/chat_v2", methods=["POST"])
def chat_v2(char_id):
    from app import init_char_db, sync_memory_before_single_chat, process_ai_media_tags, _execute_directive, _strip_consecutive_tickle, _sticker_content_from_ai
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

    # 4. 存入用户消息
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))
    user_msg_id = cursor.lastrowid
    conn.commit()
    conn.close()

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
                        "agent_forwarded": True
                    })
        except Exception as e:
            print(f"[Chat v2] Agent 转交检查失败: {e}")

    # 5. 检查深睡眠
    if is_deep_sleep:
        print(f"--- [Deep Sleep v2] {char_id} 正在熟睡，不回复消息 ---")
        return jsonify({
            "replies": [],
            "id": None,
            "user_id": user_msg_id
        })

    # 6. 同步记忆
    memory_sync_warning = None
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
    lang = get_ai_language(char_id, user_id=user_id)
    hour = now.hour
    time_str = now.strftime('%H:%M')

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

        # 清理回复
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply = re.sub(timestamp_pattern, '', reply_text_raw).strip()

        # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
        cleaned_reply, affinity_delta, directive = process_agent_actions(char_id, cleaned_reply, get_current_user_id())
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
                _ddir, _cid, _ctxt = directive, char_id, cleaned_reply
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

        # 存入AI回复
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        ai_ts = (now + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", cleaned_reply, ai_ts))
        ai_msg_id = cursor.lastrowid
        conn.commit()
        conn.close()

        reply_bubbles = list(filter(None, [part.strip() for part in cleaned_reply.split('/')]))
        if get_ai_language(char_id, user_id=user_id) == "ja" and "[WEB_CRUISE:" not in user_msg_raw:
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        resp = {
            "replies": [{"content": b, "id": ai_msg_id} for b in reply_bubbles],
            "id": ai_msg_id,
            "user_id": user_msg_id,
            "model": current_model
        }
        # 【Agent】提供未按斜线拆分的完整回复，供浏览器 Agent 可靠解析动作标签
        # （如 [GOTO:https://...] 含斜线会被 reply_bubbles 拆断）
        if "[WEB_CRUISE:" in user_msg_raw:
            resp["full_reply"] = cleaned_reply
        if affinity_delta:
            resp["affinity_delta"] = affinity_delta
        if memory_sync_warning:
            resp["memory_sync_warning"] = memory_sync_warning
        cb_info = get_circuit_breaker_info()
        if cb_info:
            resp["circuit_breaker"] = cb_info

        return jsonify(resp)

    except Exception as e:
        print(f"Chat v2 Error: {e}")
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/regenerate", methods=["POST"])
def regenerate_message(char_id):
    from app import process_ai_media_tags, _strip_consecutive_tickle, _sticker_content_for_ai, _sticker_content_from_ai
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
        now = datetime.now()

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

        # 8. 清理 & 存入
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()

        # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
        cleaned_reply_text, affinity_delta, _ = process_agent_actions(char_id, cleaned_reply_text, get_current_user_id())

        cleaned_reply_text = _strip_consecutive_tickle(cleaned_reply_text)

        # --- 【关键修复】重新生成时也需要拦截多媒体标签 ---
        cleaned_reply_text = process_ai_media_tags(cleaned_reply_text, char_id, user_id=user_id)
        cleaned_reply_text = _sticker_content_from_ai(cleaned_reply_text)

        ai_ts = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("assistant", cleaned_reply_text, ai_ts))
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()

        reply_bubbles = list(filter(None, [part.strip() for part in cleaned_reply_text.split('/')]))

        if get_ai_language(char_id, user_id=user_id) == "ja":
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        resp_data = {
            "status": "success",
            "replies": reply_bubbles,
            "id": new_id
        }
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

        # 执行删除
        cursor.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        rows_affected = cursor.rowcount # 获取受影响的行数

        conn.commit()
        conn.close()

        if rows_affected > 0:
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
        # 执行更新
        cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id))
        conn.commit()
        conn.close()

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
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    total_new_count = 0
    message_log = []

    try:
        # 凌晨检测逻辑
        if now.hour < 4:
            yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            # <--- 2. 传参给工具函数
            count_y, _ = update_short_memory_for_date(char_id, yesterday_str)
            if count_y > 0:
                total_new_count += count_y
                message_log.append(f"昨天新增 {count_y} 条")

        # 处理今天
        # <--- 3. 传参给工具函数
        count_t, _ = update_short_memory_for_date(char_id, today_str)
        if count_t > 0:
            total_new_count += count_t
            message_log.append(f"今天新增 {count_t} 条")

        if total_new_count > 0:
            return jsonify({
                "status": "success",
                "message": "记忆整理完成: " + "，".join(message_log),
                "count": total_new_count
            })
        else:
            return jsonify({"status": "no_data", "message": "暂时没有新对话需要整理"})

    except Exception as e:
        # 打印详细错误方便调试
        print(f"Snapshot Error: {e}")
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/memory/regenerate_medium", methods=["POST"])
def regenerate_medium_memory(char_id):
    target_date = request.json.get("date")
    if not target_date: return jsonify({"error": "日期不能为空"}), 400

    _, prompts_dir = get_paths(char_id)
    short_file = os.path.join(prompts_dir, "6_memory_short.json")
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")

    try:
        # 1. 读取短期记忆作为素材
        if not os.path.exists(short_file): return jsonify({"error": "短期记忆文件不存在"}), 404
        with open(short_file, "r", encoding="utf-8") as f:
            short_data = json.load(f)

        # 兼容格式
        day_data = short_data.get(target_date)
        events = []
        if isinstance(day_data, list): events = day_data
        elif isinstance(day_data, dict): events = day_data.get("events", [])

        if not events:
            return jsonify({"error": f"{target_date} 没有短期记忆素材，无法总结"}), 400

        # 2. 拼接素材
        text_to_summarize = "\n".join([f"[{e['time']}] {e['event']}" for e in events])

        # 3. 调用 AI (使用 medium 模式)
        summary = call_ai_to_summarize(text_to_summarize, "medium", char_id)
        if not summary: return jsonify({"error": "AI 生成失败"}), 500

        # 4. 更新 Medium 文件
        medium_data = {}
        if os.path.exists(medium_file):
            with open(medium_file, "r", encoding="utf-8") as f:
                try: medium_data = json.load(f)
                except: pass

        medium_data[target_date] = summary

        with open(medium_file, "w", encoding="utf-8") as f:
            json.dump(medium_data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "content": summary})

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/memory/regenerate_long", methods=["POST"])
def regenerate_long_memory(char_id):
    week_key = request.json.get("week_key") # 例如 "2025-12-Week2"
    if not week_key: return jsonify({"error": "Week Key 不能为空"}), 400

    _, prompts_dir = get_paths(char_id)
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")
    long_file = os.path.join(prompts_dir, "4_memory_long.json")

    try:
        # 1. 解析周 Key 对应的日期范围
        # 假设格式: YYYY-MM-WeekN
        # 逻辑：Week1 = 1-7日, Week2 = 8-14日...
        try:
            parts = week_key.split('-Week')
            ym_str = parts[0] # 2025-12
            week_num = int(parts[1])

            year, month = map(int, ym_str.split('-'))

            start_day = (week_num - 1) * 7 + 1
            end_day = min(start_day + 6, 31) # 简单防溢出，实际会有 date 校验

            # 构造这一周的所有日期字符串
            target_dates = []
            for d in range(start_day, end_day + 1):
                try:
                    # 校验日期是否合法
                    current_dt = datetime(year, month, d)
                    target_dates.append(current_dt.strftime("%Y-%m-%d"))
                except ValueError:
                    break # 超出当月天数
        except:
            return jsonify({"error": "Week Key 格式无法解析"}), 400

        # 2. 读取中期记忆作为素材
        if not os.path.exists(medium_file): return jsonify({"error": "中期记忆文件不存在"}), 404
        with open(medium_file, "r", encoding="utf-8") as f:
            medium_data = json.load(f)

        summary_buffer = []
        for d_str in target_dates:
            if d_str in medium_data:
                summary_buffer.append(f"【{d_str}】: {medium_data[d_str]}")

        if not summary_buffer:
            return jsonify({"error": f"该周 ({target_dates[0]}~{target_dates[-1]}) 没有任何中期日记素材"}), 400

        full_text = "\n".join(summary_buffer)

        # 3. 调用 AI (使用 long 模式)
        long_summary = call_ai_to_summarize(full_text, "long", char_id)
        if not long_summary: return jsonify({"error": "AI 生成失败"}), 500

        # 4. 更新 Long 文件
        long_data = {}
        if os.path.exists(long_file):
            with open(long_file, "r", encoding="utf-8") as f:
                try: long_data = json.load(f)
                except: pass

        long_data[week_key] = long_summary

        with open(long_file, "w", encoding="utf-8") as f:
            json.dump(long_data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "content": long_summary})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/<char_id>/debug/force_maintenance")
def force_maintenance(char_id):
    from app import scheduled_maintenance
    scheduled_maintenance() # 手动调用上面那个定时函数
    return jsonify({"status": "triggered", "message": "已手动触发后台维护，请查看服务器控制台日志"})


@chat_bp.route("/api/<char_id>/prompts_data")
def get_prompts_data(char_id):
    from app import migrate_persona_extract_age
    # 每次加载时尝试迁移（若尚未迁移）
    migrate_persona_extract_age(char_id)

    data = {}
    # 修改 base 的映射，使其支持 JSON 或 MD
    files = {
        "base": ["1_base_persona.json", "1_base_persona.md"],
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

        # 处理可能的多个文件名（针对 base 迁移）
        candidate_files = filename if isinstance(filename, list) else [filename]
        found_path = None
        for f_name in candidate_files:
            p = os.path.join(prompts_dir, f_name)
            if os.path.exists(p):
                found_path = p
                filename = f_name # 锁定实际找到的文件名
                break

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
                                # 顺便存入视觉设定
                                data["visual_descriptions"] = json_content.get("visual_descriptions", {})
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
    user_id = get_current_user_id()
    cfg_file = _get_characters_config_file()

    if not os.path.exists(cfg_file):
        return jsonify({})

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_chars = json.load(f)
    except:
        return jsonify({})

    # 获取当前角色的名字（用于在别人的关系表中查找）
    target_info = all_chars.get(char_id, {})
    target_name = target_info.get("name") or char_id

    reverse_data = {}

    # 遍历所有角色
    for cid, cinfo in all_chars.items():
        if cid == char_id:
            continue

        # 获取该角色的 prompts 目录
        _, prompts_dir = get_paths(cid)
        rel_file = os.path.join(prompts_dir, "2_relationship.json")

        if os.path.exists(rel_file):
            try:
                with open(rel_file, "r", encoding="utf-8-sig") as f:
                    rel_dict = json.load(f)

                # 在该角色的关系表中查找目标角色
                # 兼容性查找：优先匹配 ID (cid)，其次匹配角色名 (target_name)
                found_key = None
                if char_id in rel_dict:
                    found_key = char_id
                elif target_name in rel_dict:
                    found_key = target_name

                if found_key:
                    reverse_data[cid] = rel_dict[found_key]
                    # 补充一个字段方便前端显示
                    reverse_data[cid]["char_name"] = cinfo.get("name") or cid
            except Exception as e:
                print(f"Error reading relationship for {cid}: {e}")
                continue

    return jsonify(reverse_data)



@chat_bp.route("/api/<char_id>/save_relationship_reverse", methods=["POST"])
def save_relationship_reverse(char_id):
    """
    保存反向关系：其实就是去修改“对方”的角色关系文件
    """
    payload = request.json or {}
    source_cid = payload.get("source_cid") # “对方”的ID
    rel_data = payload.get("data") # 新的关系内容

    if not source_cid:
        return jsonify({"error": "缺少 source_cid"}), 400

    # 获取当前角色的名字和ID
    cfg_file = _get_characters_config_file()
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_chars = json.load(f)
        target_name = all_chars.get(char_id, {}).get("name") or char_id
    except:
        return jsonify({"error": "读取配置失败"}), 500

    # 定位“对方”的关系文件
    _, prompts_dir = get_paths(source_cid)
    rel_file = os.path.join(prompts_dir, "2_relationship.json")

    try:
        current_rel = {}
        if os.path.exists(rel_file):
            with open(rel_file, "r", encoding="utf-8-sig") as f:
                current_rel = json.load(f)

        # 兼容性查找：看看是用名字存的还是用 ID 存的
        found_key = None
        if target_name in current_rel:
            found_key = target_name
        elif char_id in current_rel:
            found_key = char_id

        if rel_data is None:
            # 删除逻辑
            if found_key:
                del current_rel[found_key]
        else:
            # 更新逻辑：如果已存在键则更新，否则新增一个键（优先用名字）
            target_key = found_key or target_name
            current_rel[target_key] = rel_data

        # 写回
        os.makedirs(os.path.dirname(rel_file), exist_ok=True)
        with open(rel_file, "w", encoding="utf-8") as f:
            json.dump(current_rel, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@chat_bp.route("/api/<char_id>/save_prompt", methods=["POST"])
def save_prompt_file(char_id):
    key = request.json.get("key")
    new_content = request.json.get("content") # 可以是字符串(md)或对象(json)

    # 获取该角色的 Prompt 目录
    _, prompts_dir = get_paths(char_id)

    # 映射 Key 到 文件名
    files_map = {
        "base": "1_base_persona.md",
        "relation": "2_relationship.json",
        "user": "3_user_persona.md",
        "long": "4_memory_long.json",
        "medium": "5_memory_medium.json",
        "short": "6_memory_short.json",
        "schedule": "7_schedule.json",
        "format": "8_format.md"
    }

    filename = files_map.get(key)
    if not filename:
        return jsonify({"status": "error", "message": "Invalid key"}), 400

    path = os.path.join(prompts_dir, filename)

    try:
        if key == "base":
            # 如果是 base，我们要存为 JSON
            json_path = os.path.join(prompts_dir, "1_base_persona.json")
            old_data = {
                "system_prompt": "",
                "visual_descriptions": {"tags": "", "description": ""},
                "custom_settings": {"reply_style": "默认", "interaction_rules": ""}
            }
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                except Exception: pass

            # 更新字段 (由前端传来的可能是纯文本或带 visual 的对象)
            if isinstance(new_content, dict):
                old_data.update(new_content)
            else:
                old_data["system_prompt"] = str(new_content)

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(old_data, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})

        # --- 【核心新增】如果是保存短期记忆，自动校准 last_id ---
        if key == "short" and isinstance(new_content, dict):
            conn = sqlite3.connect(DATABASE_FILE)
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
                cursor.execute("SELECT MAX(id) FROM messages WHERE timestamp <= ?", (query_ts,))
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

        with open(path, "w", encoding="utf-8") as f:
            if filename.endswith(".json") and isinstance(new_content, (dict, list)):
                json.dump(new_content, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(new_content))
        return jsonify({"status": "success"})
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
            # 【新增】定义默认配置字典
            defaults = {
                "emotion": 1,
                "moments_index": 1,
                "intimacy": 60,
                "light_sleep": True,
                "deep_sleep": False,
                "ds_start": "23:00",
                "ds_end": "07:00",
                "age": None,
                "tickle_suffix": "",
                "language": "",
                "chat_mode": "online",
                "bedtime_diary_enabled": True
            }
            # 将默认值合并进去 (如果 char_info 里没有该字段，就用默认的)
            # 这里的逻辑是：char_info 覆盖 defaults (已有的配置优先)
            final_info = defaults.copy()
            final_info.update(char_info)

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
        # 1. 读取现有配置
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        if char_id not in all_config:
            return jsonify({"error": "Character ID not found"}), 404

        # 2. 更新字段 (只更新前端传过来的字段)
        data = request.json
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
            all_config[char_id]["language"] = new_language.strip()

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
            all_config[char_id]["deep_sleep"] = bool(data["deep_sleep"])

        # 深睡眠自动时间段 (Start, End)
        if data.get("ds_start") is not None:
            all_config[char_id]["ds_start"] = data["ds_start"]
        if data.get("ds_end") is not None:
            all_config[char_id]["ds_end"] = data["ds_end"]

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
        # 【修改】使用安全保存
        safe_save_json(CONFIG_FILE, all_config)

        # 调试：立即读回确认写入
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            verify = json.load(f)
        print(f"[update_meta] 写入后确认 voice_emotion={verify.get(char_id, {}).get('voice_emotion', 'KEY MISSING')!r}")

        return jsonify({"status": "success"})

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
            source_data = json.load(f)

        # 3. 写入目标文件 (覆盖)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(source_data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "data": source_data})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/api/character/<char_id>/delete", methods=["DELETE"])
def delete_character_api(char_id):
    config_file = _get_characters_config_file()
    if not os.path.exists(config_file):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        if char_id not in all_config:
            return jsonify({"error": "Character not found"}), 404

        del all_config[char_id]
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(all_config, f, ensure_ascii=False, indent=2)

        db_path, _ = get_paths(char_id)
        char_dir = os.path.dirname(db_path)
        if os.path.exists(char_dir):
            shutil.rmtree(char_dir)
        return jsonify({"status": "success"})

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
        count, events = update_short_memory_for_date(char_id, target_date, force_reset=force)

        # 为了前端方便，返回最新的完整数据（因为update函数只返回了新增的）
        # 我们重新读一次文件返回给前端刷新
        _, prompts_dir = get_paths(char_id)
        short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")
        with open(short_mem_path, "r", encoding="utf-8") as f:
            full_data = json.load(f)
            day_data = full_data.get(target_date, {})
            # 统一返回 dict 格式
            if isinstance(day_data, list): day_data = {"events": day_data, "last_id": 0}

        return jsonify({
            "status": "success",
            "added_count": count,
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
        now = datetime.now()
        ai_ts = now.strftime('%Y-%m-%d %H:%M:%S')
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
            now = datetime.now()
            user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
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
