import os
import time
import json
import re
import random
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import (
    Blueprint, request, jsonify, session, redirect,
    render_template, send_from_directory,
)
from PIL import Image

from agent_utils import process_agent_actions
from cos_utils import upload_to_cos
from core.config import GROUPS_DIR, USERS_ROOT, BASE_DIR
from core.context import get_current_user_id, set_background_user
from core.circuit_breaker import get_circuit_breaker_info
from core.utils import (
    get_paths,
    safe_save_json,
    _get_characters_config_file,
    _get_groups_config_file,
    _add_furigana_to_japanese,
)

group_bp = Blueprint('group', __name__)


def _group_circuit_breaker_response(user_msg_id=None, memory_sync_warning=None, affinity_delta=None):
    cb_info = get_circuit_breaker_info()
    if not cb_info:
        return None
    resp = {
        "replies": [],
        "circuit_breaker": cb_info,
    }
    if user_msg_id is not None:
        resp["user_id"] = user_msg_id
    if memory_sync_warning:
        resp["memory_sync_warning"] = memory_sync_warning
    if affinity_delta:
        resp["affinity_delta"] = round(affinity_delta, 2)
    return jsonify(resp)


# ==================== 群聊路径与辅助函数 ====================

def get_group_dir(group_id: str) -> str:
    """
    获取群聊目录路径。
    如有登录用户，则使用 users/<user_id>/groups/<group_id>/ 作为工作目录；
    否则使用全局 GROUPS_DIR/<group_id>。
    """
    user_id = get_current_user_id()
    if user_id:
        return os.path.join(USERS_ROOT, str(user_id), "groups", group_id)
    return os.path.join(GROUPS_DIR, group_id)


def extract_group_recent_messages_with_labels(group_id, limit=20) -> list:
    """提取群聊最近消息，为每条标注角色和时间戳。

    返回: [(role, content_with_label, datetime_ts)]
    """
    from app import get_char_name

    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")
    result = []

    if not os.path.exists(db_path):
        return result

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()

        for row in reversed(rows):
            role = row["role"]
            content = row["content"]
            ts_str = row["timestamp"]

            try:
                msg_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                msg_dt = datetime.now()

            role_label = "user" if role == "user" else get_char_name(role)
            time_label = msg_dt.strftime("%H:%M")
            content_display = f"[{time_label}] [{role_label}]: {content}"

            result.append((role, content_display, msg_dt))
    except Exception as e:
        print(f"extract_group_recent_messages: 数据库操作失败 - {e}")

    return result


def build_group_relationship_prompt(current_char_id, other_member_ids):
    """
    当 current_char_id 说话时，注入他对群里其他人的看法。
    关键：需要把 other_member_ids (如 isagi) 转换为 关系JSON里的 Key (如 洁世一)
    """
    # 1. 读取角色配置（多用户感知），建立 ID -> Name 的映射表
    id_to_name_map = {}
    try:
        cfg_path = _get_characters_config_file()
        with open(cfg_path, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            for cid, cinfo in chars_config.items():
                id_to_name_map[cid] = cinfo.get("name", cid)
    except: pass

    # 2. 读取当前角色的关系文件
    _, prompts_dir = get_paths(current_char_id)
    rel_file = os.path.join(prompts_dir, "2_relationship.json")

    prompt_text = "【Group Relationships / 群聊关系认知】\n(你是群聊的一员，请参考以下你与其他成员的关系)\n"

    if not os.path.exists(rel_file):
        return ""

    try:
        with open(rel_file, "r", encoding="utf-8") as f:
            # 这里的 Key 是名字 (如 "洁世一")
            rels_data = json.load(f)

        found_any = False

        # 3. 遍历在场的其他人，查找关系
        for other_id in other_member_ids:
            if other_id == "user": continue

            # 获取对方的名字
            target_name = id_to_name_map.get(other_id, other_id)

            # 在关系表里查找
            # 尝试匹配对方名字或者对方 ID (兼容历史数据存放了 ID 的情况)
            rel_info = rels_data.get(target_name) or rels_data.get(other_id)

            if rel_info:
                role = rel_info.get('role', '未知')
                desc = rel_info.get('description', '特になし')
                score = rel_info.get('score', 1)
                prompt_text += f"- 対 {target_name}: {role} (関係度:{score}) {desc}\n"
                found_any = True
            else:
                # 如果没找到特定关系，也可以不写，或者写个默认
                pass

        if not found_any:
            return "" # 如果跟群里的人都没关系，就不加这段 prompt

        return prompt_text

    except Exception as e:
        print(f"Build Group Rel Error: {e}")
        return ""


def distribute_group_memory(group_id, group_name, members, new_events, date_str):
    """
    将群聊新生成的事件，追加到每个成员的 6_memory_group_log.json 中
    """
    if not new_events:
        print("   [Distribute] 没有新事件需要分发")
        return

    print(f"   [Distribute] 正在分发 {len(new_events)} 条事件给成员: {members}")

    for char_id in members:
        if char_id == "user": continue # 跳过用户

        try:
            # 1. 找到该角色的文件路径
            _, prompts_dir = get_paths(char_id)
            # 【修改】目标文件改为 6_memory_short.json
            short_file = os.path.join(prompts_dir, "6_memory_short.json")

            # 2. 读取现有数据
            current_data = {}
            if os.path.exists(short_file):
                with open(short_file, "r", encoding="utf-8") as f:
                    try: current_data = json.load(f)
                    except: pass

            # 兼容新旧格式 (获取当天的 dict)
            day_data = current_data.get(date_str, {})
            # 如果是旧格式列表，转为字典结构
            if isinstance(day_data, list):
                existing_events = day_data
                last_id = 0
            else:
                existing_events = day_data.get("events", [])
                last_id = day_data.get("last_id", 0)

            # 3. 追加新事件 (格式化一下，标明来源)
            count_added = 0
            for event in new_events:
                # 格式化内容：[群聊:群名] 事件
                # 【修改】这里确保 event['event'] 是纯文本，不包含奇怪的 AI 生成头信息
                clean_event_text = event['event'].replace('AI生成信息发送的内容', '').strip()
                event_content = f"[群聊:{group_name}] {clean_event_text}"

                # 简单去重
                is_duplicate = False
                for old in existing_events:
                    if old['time'] == event['time'] and event_content in old['event']:
                        is_duplicate = True
                        break

                if not is_duplicate:
                    existing_events.append({
                        "time": event['time'],
                        "event": event_content
                    })
                    count_added += 1

            if count_added > 0:
                # 按时间重新排序 (保证群聊和私聊按时间穿插)
                existing_events.sort(key=lambda x: x['time'])

                # 保存回文件 (保持 last_id 不变，因为这些群聊消息不属于私聊数据库)
                current_data[date_str] = {
                    "events": existing_events,
                    "last_id": last_id
                }

                with open(short_file, "w", encoding="utf-8") as f:
                    json.dump(current_data, f, ensure_ascii=False, indent=2)

                print(f"     -> [{char_id}] 合并成功 (+{count_added}条)")

        except Exception as e:
            print(f"     ❌ 同步给 [{char_id}] 失败: {e}")


def _get_groups_for_char(char_id):
    """获取该角色参与的所有群聊 ID 列表 (使用 per-user 配置)"""
    groups_cfg = _get_groups_config_file()
    if not os.path.exists(groups_cfg):
        return []
    try:
        with open(groups_cfg, "r", encoding="utf-8") as f:
            groups = json.load(f)
        return [gid for gid, info in groups.items() if char_id in info.get("members", [])]
    except Exception:
        return []


def sync_memory_before_group_chat(group_id):
    """
    群聊前：总结群成员的单聊 + 群成员参与的其他群聊（跳过当前群）的短期记忆。
    不包含本群群聊记忆。
    返回 (success: bool, error_msg: str|None)
    """
    from app import update_short_memory_for_date

    groups_cfg = _get_groups_config_file()
    if not os.path.exists(groups_cfg):
        return True, None
    try:
        with open(groups_cfg, "r", encoding="utf-8") as f:
            group_info = json.load(f).get(group_id, {})
        members = [m for m in group_info.get("members", []) if m != "user"]
    except Exception:
        members = []

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    dates = [today_str]
    if now.hour < 4:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates.insert(0, yesterday_str)

    try:
        # 1. 各成员单聊记忆
        for char_id in members:
            for d in dates:
                try:
                    update_short_memory_for_date(char_id, d)
                except Exception as e:
                    print(f"   [Sync] 成员 {char_id} 单聊日期 {d} 同步失败: {e}")
                    return False, f"成员单聊记忆同步失败: {e}"

        # 2. 群成员参与的其他群聊记忆（跳过当前群），汇总后通过 distribute 写入各成员短期记忆
        other_group_ids_seen = set()
        for char_id in members:
            for gid in _get_groups_for_char(char_id):
                if gid == group_id:
                    continue
                if gid in other_group_ids_seen:
                    continue
                other_group_ids_seen.add(gid)
                for d in dates:
                    try:
                        update_group_short_memory(gid, d)
                    except Exception as e:
                        print(f"   [Sync] 成员参与的其他群 {gid} 日期 {d} 同步失败: {e}")
                        return False, f"其他群聊记忆同步失败: {e}"

        return True, None
    except Exception as e:
        print(f"   [Sync] 群聊前记忆同步失败: {e}")
        return False, str(e)


def update_group_log(char_id, event_content, timestamp_str):
    _, prompts_dir = get_paths(char_id)
    log_file = os.path.join(prompts_dir, "6_memory_group_log.json")

    date_str = timestamp_str.split(' ')[0]
    time_str = timestamp_str.split(' ')[1][:5]

    current_data = {}
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    if date_str not in current_data:
        current_data[date_str] = []

    current_data[date_str].append({
        "time": time_str,
        "event": event_content
    })

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(current_data, f, ensure_ascii=False, indent=2)


def _get_group_chat_bg_config_file(group_id):
    """获取群聊背景配置文件路径"""
    group_dir = get_group_dir(group_id)
    return os.path.join(group_dir, "chat_bg_config.json")


def update_group_short_memory(group_id, target_date_str):
    from app import call_ai_to_summarize

    # 1. 路径准备
    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")
    memory_file = os.path.join(group_dir, "memory_short.json") # 群聊自己的记忆文件

    # 2. 读取群配置 (使用 per-user 配置)
    groups_cfg = _get_groups_config_file()
    if not os.path.exists(groups_cfg):
        return 0, []

    with open(groups_cfg, "r", encoding="utf-8") as f:
        groups_config = json.load(f)
        group_info = groups_config.get(group_id, {})

    group_name = group_info.get("name", "Group")
    members = group_info.get("members", [])

    # 3. 读取现有群记忆 (获取 last_id)
    current_data = {}
    if os.path.exists(memory_file):
        with open(memory_file, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    day_data = current_data.get(target_date_str, {})
    # 兼容处理：如果是列表转字典
    if isinstance(day_data, list):
        existing_events = day_data
        last_id = 0
    else:
        existing_events = day_data.get("events", [])
        last_id = day_data.get("last_id", 0)

    # 4. 查询群数据库
    if not os.path.exists(db_path): return 0, []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    # 只读取 ID > last_id 的新消息
    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows: return 0, []

    new_max_id = rows[-1][0]

    # 5. 拼接文本 (需要转换 role ID 为名字)
    # 加载名字映射 (使用 per-user 配置)
    id_to_name = {}
    try:
        chars_cfg = _get_characters_config_file()
        with open(chars_cfg, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for k, v in c_conf.items(): id_to_name[k] = v.get("name", k)
    except: pass

    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        # 如果是 user 显示用户，如果是 char_id 显示名字
        name = "ユーザー" if role == "user" else id_to_name.get(role, role)
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 6. 调用 AI 总结
    # 这里我们复用 call_ai_to_summarize，用 "short" 模式提取事件
    # 这里的 char_id 可以随便传一个群成员的，或者传 None，因为 short 模式主要是提取事实
    summary_text = call_ai_to_summarize(chat_log, "group_log", "system")

    if not summary_text: return 0, []

    # 7. 解析 AI 返回结果
    new_events = []
    for line in summary_text.split('\n'):
        line = line.strip()
        if line:
            match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
            event_time = match_time.group(1) if match_time else datetime.now().strftime("%H:%M")
            event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
            new_events.append({"time": event_time, "event": event_text})

    if not new_events: return 0, []

    # 8. 保存到群聊记忆 (追加模式)
    final_events = existing_events + new_events

    # 如果是重置模式(last_id=0)，且原本有数据，这里可以加去重逻辑(类似单人)，这里暂略，直接追加

    current_data[target_date_str] = {
        "events": final_events,
        "last_id": new_max_id
    }

    with open(memory_file, "w", encoding="utf-8") as f:
        json.dump(current_data, f, ensure_ascii=False, indent=2)

    # ================= 关键修复点 =================
    # 9. 【必须】调用分发函数，传给个人
    if new_events:
        print(f"--- [Sync] 开始同步群聊记忆到个人文件 ---")
        distribute_group_memory(group_id, group_name, members, new_events, target_date_str)
    # ============================================

    return len(new_events), new_events


# ==================== 群聊页面路由 ====================

@group_bp.route("/chat/group/<group_id>")
def group_chat_view(group_id):
    return render_template("chat.html", group_id=group_id)


@group_bp.route("/memory/group/<group_id>")
def group_memory_view(group_id):
    return render_template("group_memory.html")


# ==================== 群聊历史 / 聊天接口 ====================

@group_bp.route("/api/group/<group_id>/history", methods=["GET"])
def get_group_history(group_id):
    from app import _sticker_content_from_ai, get_ai_language

    limit = request.args.get('limit', 20, type=int)
    target_id = request.args.get('target_id', type=int)
    before_id = request.args.get('before_id', type=int)
    after_id = request.args.get('after_id', type=int)

    # 使用多用户命名空间下的群聊目录
    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")
    if not os.path.exists(db_path):
        return jsonify({"messages": [], "total": 0})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    messages = []

    # A. 向上滚动
    if before_id:
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    # B. 向下轮询
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
    total = cursor.fetchone()[0]
    conn.close()

    # 日语注音处理（不写回DB）
    for m in messages:
        sender_role = m.get("role")
        if sender_role and sender_role != "user":
            if get_ai_language(sender_role, group_id=group_id) == "ja":
                m["content"] = _add_furigana_to_japanese(m["content"])

    return jsonify({"messages": messages, "total": total})


# --- 【修正版】群聊核心接口 (完整逻辑：@解析 + 串行 + 变量修复) ---
@group_bp.route("/api/group/<group_id>/chat", methods=["POST"])
def group_chat(group_id):
    from app import (
        _memory_context_changed,
        build_system_prompt_v2,
        get_ai_language,
        get_model_config,
        call_openrouter,
        call_gemini,
        _extract_tickle_target,
        _check_consecutive_tickle,
        _strip_consecutive_tickle,
        process_ai_media_tags,
        _sticker_content_from_ai,
        _sticker_content_for_ai,
        _execute_directive,
    )

    # 1. 基础准备
    data = request.json
    user_msg = data.get("message", "").strip()
    if not user_msg: return jsonify({"error": "empty"}), 400
    # 获取 user_id 用于后续 relay
    user_id = get_current_user_id()

    # --- 群聊前自动同步：仅在切换上下文时总结群内各角色单聊 + 本群群聊短期记忆 ---
    memory_sync_warning = None
    if _memory_context_changed(user_id, f"group:{group_id}"):
        try:
            ok, err = sync_memory_before_group_chat(group_id)
            if not ok:
                memory_sync_warning = f"记忆同步失败：{err}，本次对话可能缺少部分单聊上下文"
                print(f"   ⚠️ {memory_sync_warning}")
        except Exception as e:
            memory_sync_warning = f"记忆同步失败：{e}，本次对话可能缺少部分单聊上下文"
            print(f"   ⚠️ {memory_sync_warning}")

    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")

    # 2. 读取群成员 (使用 per-user 配置)
    groups_cfg = _get_groups_config_file()
    chars_cfg = _get_characters_config_file()
    all_members = []
    if os.path.exists(groups_cfg):
        with open(groups_cfg, "r", encoding="utf-8") as f:
            group_conf = json.load(f)
            if group_id in group_conf:
                all_members = group_conf[group_id].get("members", [])

    # 排除用户
    ai_members_all = [m for m in all_members if m != "user"]
    if not ai_members_all: return jsonify({"error": "No AI members"}), 404

    # --- 【关键修正 1】提前初始化变量 ---
    replies_for_frontend = []
    group_affinity_delta = 0.0

    # --- 【关键步骤】获取在线成员 (过滤掉深睡眠的) ---
    # 需要读取 characters.json 查看 deep_sleep 状态 (使用 per-user 配置)
    id_to_name = {}
    name_to_id = {}
    online_ai_members = [] # 最终的在线名单

    if os.path.exists(chars_cfg):
        with open(chars_cfg, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for cid, cinfo in c_conf.items():
                # ID -> Name (用于显示)
                name = cinfo.get("name", cid)
                id_to_name[cid] = name

                # Name -> ID (用于解析 @)
                # 映射 "国神" -> "kunigami"
                name_to_id[name] = cid
                # 映射 "英雄" (备注) -> "kunigami"
                if cinfo.get("remark"):
                    name_to_id[cinfo.get("remark")] = cid

                # 2. 检查是否在线 (Deep Sleep False, 但线下模式无视深睡眠)
                # 只有在群成员列表里 且 没有深睡眠 的才算在线
                if cid in ai_members_all:
                    is_sleeping = cinfo.get("deep_sleep", False)
                    member_chat_mode = cinfo.get("chat_mode", "online")
                    if member_chat_mode == "offline":
                        is_sleeping = False
                    if not is_sleeping:
                        online_ai_members.append(cid)
                    else:
                        print(f"   [GroupChat] 成员 {name}({cid}) 正在熟睡，跳过。")

    # 拍一拍：群聊中检查连续拍同一人
    is_tickle, tickle_target = _extract_tickle_target(user_msg)
    if is_tickle:
        ok, _ = _check_consecutive_tickle(db_path, tickle_target, None)
        if not ok:
            return jsonify({"error": "consecutive_tickle", "message": "不可连续拍一拍同一人，请稍后再试"}), 400

    # 如果全员都在睡觉，直接返回空
    if not online_ai_members:
        print("--- [GroupChat] 全员睡眠中，无人回复 ---")
        # 依然要存用户消息
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        now = datetime.now()
        user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg, user_ts))
        conn.commit()
        conn.close()
        resp = {"replies": []}
        if memory_sync_warning:
            resp["memory_sync_warning"] = memory_sync_warning
        return jsonify(resp)

    # 3. 存入用户消息
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                   ("user", user_msg, user_ts))
    user_msg_id = cursor.lastrowid # 【新增】获取刚存入的用户消息 ID
    conn.commit()
    conn.close()

    # 4. 决定回复顺序 (智能 @ 逻辑)
    responder_ids = []

    # A. 【最高优先级】检测 @所有人
    # 这里的判断很简单：只要字符串里包含这个词就行
    if "@所有人" in user_msg:
        print("--- [GroupChat] 模式: @所有人 (在线全员) ---")
        responder_ids = list(online_ai_members)
        # 打乱顺序，让每次“开会”的发言顺序都不一样，更真实
        random.shuffle(responder_ids)

    # B. 如果没有 @所有人，再检测具体名字
    else:
        # 解析用户消息里的 @
        mentioned_names = re.findall(r'@(.*?)(?:\s|$)', user_msg)

        if mentioned_names:
            print(f"--- [GroupChat] 检测到 @: {mentioned_names} ---")
            for name in mentioned_names:
                # 尝试匹配 ID
                if name in name_to_id:
                    target_id = name_to_id[name]
                    # 只有在线的才回
                    if target_id in online_ai_members:
                        responder_ids.append(target_id)
                    else:
                        print(f"   -> @{name} 在线状态不满足，不回复")
                else:
                    print(f"   -> 未找到名为 '{name}' 的群成员")

    # C. 如果没有有效 @，回退到随机逻辑
    if not responder_ids:
        # 获取当前群里 AI 的实际数量
        count_online = len(online_ai_members)

        # 逻辑：想要随机回复 1~2 人，但不能超过实际人数
        # 比如：如果只有 1 个 AI，那就只能回 1 次
        # 如果有 3 个 AI，可以随机回 1 或 2 次
        target_k = random.randint(1, count_online)

        responder_ids = random.sample(online_ai_members, k=target_k)
        print(f"--- [GroupChat] 模式: 随机抽取 {len(responder_ids)} 人 ---")
    else:
        print(f"--- [GroupChat] 指定模式: 顺序 {responder_ids} ---")

    # 5. 预加载历史记录 (Context Buffer)
    context_buffer = []

    # 6. 串行循环生成
    # 【注意】这里遍历的是确定的 responder_ids 列表
    for i, speaker_id in enumerate(responder_ids):

        speaker_name = id_to_name.get(speaker_id, speaker_id)
        print(f"   -> 第 {i+1} 轮: 由 [{speaker_name}] 发言")

        # --- B. 先读取群聊历史，再构建 Prompt（便于长期记忆 RAI）---
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
        history_rows = [dict(row) for row in cursor.fetchall()][::-1]
        conn.close()

        recent_texts = [r["content"] for r in history_rows] if history_rows else []
        user_latest = history_rows[-1]["content"] if history_rows and history_rows[-1]["role"] == "user" else None

        # 【全局采用 v2】直接使用v2系统提示
        sys_prompt = build_system_prompt_v2(speaker_id, include_global_format=True, recent_messages=recent_texts, user_latest_input=user_latest, group_id=group_id)

        other_members = [m for m in all_members if m != speaker_id]
        rel_prompt = build_group_relationship_prompt(speaker_id, other_members)

        full_sys_prompt = sys_prompt + "\n\n" + rel_prompt + "\n【Current Situation】\n当前是在群聊中。"

        # 注入群聊线上线下模式上下文
        current_group_cfg = (group_conf or {}).get(group_id, {})
        group_mode = current_group_cfg.get("group_chat_mode", "online")
        include_user = current_group_cfg.get("include_user", True)
        lang = get_ai_language(speaker_id, group_id=group_id)
        if lang == "ja":
            mode_str = "今はオンラインチャットです" if group_mode == "online" else "今はオフラインで一緒に過ごしています"
            if include_user:
                mode_str += "。ユーザーも同席しています"
            else:
                mode_str += "。ユーザーは不在です"
            if group_mode == "online":
                mode_str += "。括弧（）で動作を描写することを禁止する。特殊メッセージ形式（音声・ファイル・絵文字等）は使用可能"
            else:
                mode_str += "。特殊メッセージ形式（音声・ファイル・絵文字等）の使用を禁止する。括弧（）で動作を描写できる"
        elif lang == "en":
            mode_str = "You are chatting online" if group_mode == "online" else "You are spending time together offline"
            if include_user:
                mode_str += ". User is also present"
            else:
                mode_str += ". User is not present"
            if group_mode == "online":
                mode_str += ". Do NOT use parentheses to describe actions. Special message formats (voice, files, emojis, etc.) are allowed"
            else:
                mode_str += ". Do NOT use any special message formats. You CAN use parentheses to describe actions"
        else:
            mode_str = "现在你们是线上聊天" if group_mode == "online" else "现在你们在线下相处"
            if include_user:
                mode_str += "。用户也在"
            else:
                mode_str += "。用户不在"
            if group_mode == "online":
                mode_str += "。禁止用括号描述动作；可以使用特殊消息格式（语音、文件、表情等）"
            else:
                mode_str += "。禁止使用任何特殊消息格式；可用括号描述动作"
        full_sys_prompt += mode_str + "。请注意上下文，与其他成员自然互动。"

        messages = [{"role": "system", "content": full_sys_prompt}]

        # --- C. 处理历史记录 (智能时间戳 + 名字标签) ---

        # 1. 判断时间跨度 (是否跨天)
        show_full_date = False
        now_dt = datetime.now() # 获取当前时间用于比较
        if history_rows:
            try:
                first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if first_ts.date() != now_dt.date():
                    show_full_date = True
            except: pass

        # 2. 时间线已包含最近群聊消息，此处仅追加最后1条作为触发
        if history_rows:
            row = history_rows[-1]
            # a. 处理时间戳格式
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                if show_full_date:
                    ts_str = dt_obj.strftime('[%m-%d %H:%M]')
                else:
                    ts_str = dt_obj.strftime('[%H:%M]')
            except:
                ts_str = ""

            # b. 处理名字
            r_id = row['role']
            d_name = "User" if r_id == "user" else id_to_name.get(r_id, r_id)

            # c. 组合 Content
            msg_role = "user"
            content_for_ai = _sticker_content_for_ai(row['content'])
            content_with_tag = f"{ts_str} [{d_name}]: {content_for_ai}"

            messages.append({"role": msg_role, "content": content_with_tag})

        # 1. 获取当前配置
        route, current_model = get_model_config("chat", user_id=user_id) # 任务类型是 chat

        print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

        try:
            if route == "relay":
                reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model, user_id=user_id)
            else:
                reply_text = call_gemini(messages, char_id=speaker_id, model_name=current_model, user_id=user_id)

            cb_resp = _group_circuit_breaker_response(
                user_msg_id=user_msg_id,
                memory_sync_warning=memory_sync_warning,
                affinity_delta=group_affinity_delta,
            )
            if cb_resp:
                return cb_resp

            timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
            cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()

            # --- 【新增】拦截动作标签 (Emotion/Affinity等) ---
            cleaned_reply, delta, dir_d = process_agent_actions(speaker_id, cleaned_reply, get_current_user_id())
            if delta:
                group_affinity_delta += delta
            print(f"  [DEBUG] dir_d = {repr(dir_d)}, type={type(dir_d).__name__}", flush=True)

            # --- 【转向指令】处理 DIRECT_TO_GROUP / DIRECT_TO_USER ---
            if dir_d:
                skip = False
                if dir_d.get("type") == "group":
                    # 检查是否会转向同一个群：计算目标群成员，若与当前群成员一致则跳过
                    target_members = set([speaker_id] + dir_d.get("member_ids", []))
                    current_members = set(m for m in all_members if m != "user")
                    if target_members == current_members:
                        print(f"  ⚠️ [Directive] 目标群与当前群成员相同，忽略 DIRECT_TO_GROUP", flush=True)
                        skip = True
                if not skip:
                    print(f"", flush=True)
                    print(f"{'='*50}", flush=True)
                    print(f"  🔄 [Directive] 群聊中 {speaker_name} 发出转向指令: {dir_d}", flush=True)
                    uid = get_current_user_id()
                    _ddir, _sid, _ctxt = dir_d, speaker_id, cleaned_reply
                    def _bg_exec():
                        set_background_user(uid)
                        try:
                            _execute_directive(_ddir, _sid, _ctxt)
                        except Exception as e:
                            print(f"  ❌ [Directive BG] 指令执行失败: {e}", flush=True)
                            import traceback
                            traceback.print_exc()
                    threading.Thread(target=_bg_exec, daemon=True).start()
                    print(f"{'='*50}", flush=True)

            cleaned_reply = _strip_consecutive_tickle(cleaned_reply)

            # 去除 AI 自带的名字前缀
            name_pattern = f"^\\[{speaker_name}\\][:：]\\s*"
            cleaned_reply = re.sub(name_pattern, '', cleaned_reply).strip()

            # --- 【关键修复】拦截器顺序调整 ---
            cleaned_reply = process_ai_media_tags(cleaned_reply, speaker_id)
            # 把 [表情]name 转成 [表情]path 再入库
            cleaned_reply = _sticker_content_from_ai(cleaned_reply)

            if not cleaned_reply: continue

            # --- D. 存档 ---
            ai_ts = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           (speaker_id, cleaned_reply, ai_ts))
            # 【关键修复】获取刚刚插入的这条消息的 ID
            new_msg_id = cursor.lastrowid
            conn.commit()
            conn.close()

            # 更新 Buffer (供下一个人看)
            context_buffer.append({
                "role_id": speaker_id,
                "display_name": speaker_name,
                "content": cleaned_reply
            })

            # 【关键修正 3】添加到返回列表
            replies_for_frontend.append({
                "id": new_msg_id,
                "char_id": speaker_id,
                "name": speaker_name,
                "content": cleaned_reply,
                "timestamp": ai_ts
            })

        except Exception as e:
            print(f"Group Chat Error ({speaker_id}): {e}")

    # 注音处理
    for rep in replies_for_frontend:
        # group chat 返回的是单个 string 还是分段？其实分段在前端分，这里 content 是 string
        s_id = rep.get("char_id")
        if s_id and get_ai_language(s_id, group_id=group_id) == "ja":
                rep["content"] = _add_furigana_to_japanese(rep["content"])

    # 7. 最终返回；记忆同步失败时附带提示
    resp = {"replies": replies_for_frontend, "user_id": user_msg_id}
    if group_affinity_delta:
        resp["affinity_delta"] = round(group_affinity_delta, 2)
    if memory_sync_warning:
        resp["memory_sync_warning"] = memory_sync_warning
    cb_info = get_circuit_breaker_info()
    if cb_info:
        resp["circuit_breaker"] = cb_info
    return jsonify(resp)


# --- 【新增】群聊消息删除接口 ---
@group_bp.route("/api/group/<group_id>/messages/<int:msg_id>", methods=["DELETE"])
def delete_group_message(group_id, msg_id):
    # 1. 获取群聊数据库路径
    # 确保 GROUPS_DIR 已定义 (在文件头部)
    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")

    print(f"--- [Debug] 删除群消息: Group={group_id}, MsgID={msg_id} ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Group DB not found"}), 404

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        rows_affected = cursor.rowcount

        conn.commit()
        conn.close()

        if rows_affected > 0:
            print(f"   ✅ 群消息删除成功")
            return jsonify({"status": "success"})
        else:
            return jsonify({"error": "Message ID not found"}), 404

    except Exception as e:
        print(f"   ❌ 群消息删除失败: {e}")
        return jsonify({"error": str(e)}), 500


# --- 【新增】群聊消息编辑接口 ---
@group_bp.route("/api/group/<group_id>/messages/<int:msg_id>", methods=["PUT"])
def edit_group_message(group_id, msg_id):
    from app import _sticker_content_from_ai

    # 1. 获取群聊数据库路径
    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")

    print(f"--- [Debug] 编辑群消息: Group={group_id}, MsgID={msg_id} ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Group DB not found"}), 404

    new_content = request.json.get("content", "")
    # 编辑内容中的 [表情]名称 由系统自动匹配为 [表情]path 后写入
    new_content = _sticker_content_from_ai(new_content)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id))
        conn.commit()
        conn.close()

        print(f"   ✅ 群消息编辑成功")
        return jsonify({"status": "success", "content": new_content})

    except Exception as e:
        print(f"   ❌ 群消息编辑失败: {e}")
        return jsonify({"error": str(e)}), 500


# ==================== 群聊背景设置 API ====================

@group_bp.route("/api/group/<group_id>/chat_background", methods=["GET"])
def get_group_chat_background(group_id):
    """获取群聊背景配置"""
    config_file = _get_group_chat_bg_config_file(group_id)
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            return jsonify(config)
        except:
            return jsonify({"filename": None})
    return jsonify({"filename": None})


@group_bp.route("/api/group/<group_id>/upload_chat_background", methods=["POST"])
def upload_group_chat_background(group_id):
    """上传群聊背景图"""
    try:
        group_dir = get_group_dir(group_id)
        bg_dir = group_dir  # 直接存放在群组目录下
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
                    print(f"[GroupBackground] 删除旧背景失败: {e}")

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
        cos_path = f"users/{user_id}/groups/{group_id}/background.png"
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


@group_bp.route("/api/group/<group_id>/save_chat_background", methods=["POST"])
def save_group_chat_background(group_id):
    """保存群聊背景配置"""
    try:
        config = request.json or {}
        config_file = _get_group_chat_bg_config_file(group_id)
        config_dir = os.path.dirname(config_file)
        os.makedirs(config_dir, exist_ok=True)

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@group_bp.route("/group_backgrounds/<group_id>/<filename>")
def serve_group_background(group_id, filename):
    """提供群聊背景图，重定向到 COS"""
    user_id = get_current_user_id()
    bucket = os.getenv('COS_BUCKET')
    region = os.getenv('COS_REGION')

    if user_id and bucket and region:
        # 群聊背景存放在 users/<uid>/groups/<group_id>/...
        cos_path = f"users/{user_id}/groups/{group_id}/{filename}"
        cos_url = f"https://{bucket}.cos.{region}.myqcloud.com/{cos_path}?t={int(time.time())}"
        return redirect(cos_url)

    # 降级：本地读取
    group_dir = get_group_dir(group_id)
    return send_from_directory(group_dir, filename)


# ==================== 群聊记忆快照 / 数据 API ====================

# --- 【修正】群聊快照接口 (真实实现) ---
@group_bp.route("/api/group/<group_id>/memory/snapshot", methods=["POST"])
def snapshot_group_memory(group_id):
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    total_new = 0
    msg_log = []

    try:
        # 1. 凌晨检测 (补录昨天)
        if now.hour < 4:
            yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            print(f"--- [Group Snapshot] 检查昨天 {yesterday} ---")
            c_y, _ = update_group_short_memory(group_id, yesterday)
            if c_y > 0:
                total_new += c_y
                msg_log.append(f"昨天 {c_y} 条")

        # 2. 处理今天
        print(f"--- [Group Snapshot] 检查今天 {today_str} ---")
        c_t, _ = update_group_short_memory(group_id, today_str)
        if c_t > 0:
            total_new += c_t
            msg_log.append(f"今天 {c_t} 条")

        if total_new > 0:
            return jsonify({
                "status": "success",
                "message": "群聊记忆整理完成 (并已同步给成员): " + "，".join(msg_log),
                "count": total_new
            })
        else:
            return jsonify({"status": "no_data", "message": "暂无新群聊消息"})

    except Exception as e:
        print(f"Group Snapshot Error: {e}")
        return jsonify({"error": str(e)}), 500


# 2. 获取群聊数据 (配置 + 记忆)
@group_bp.route("/api/group/<group_id>/prompts_data")
def get_group_prompts_data(group_id):
    group_dir = get_group_dir(group_id)
    memory_file = os.path.join(group_dir, "memory_short.json")

    data = {
        "meta": {},   # 群名、头像、成员
        "short": {}   # 群聊记录
    }

    # 读取配置（per-user）
    groups_cfg = _get_groups_config_file()
    if os.path.exists(groups_cfg):
        with open(groups_cfg, "r", encoding="utf-8") as f:
            all_groups = json.load(f)
            data["meta"] = all_groups.get(group_id, {})

    # 读取群聊记忆
    if os.path.exists(memory_file):
        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                data["short"] = json.load(f)
        except:
            data["short"] = {}

    return jsonify(data)


# --- 【确认/修正】保存群聊记忆接口 ---
@group_bp.route("/api/group/<group_id>/save_memory", methods=["POST"])
def save_group_memory(group_id):
    # 1. 获取路径
    group_dir = get_group_dir(group_id)
    memory_file = os.path.join(group_dir, "memory_short.json")

    # 2. 获取内容
    new_content = request.json.get("content")

    if not os.path.exists(group_dir):
        return jsonify({"error": "Group dir not found"}), 404

    try:
        # 3. 写入文件
        with open(memory_file, "w", encoding="utf-8") as f:
            json.dump(new_content, f, ensure_ascii=False, indent=2)
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Save Group Memory Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# 4. 更新群聊元数据 (头像/名称)
@group_bp.route("/api/group/<group_id>/update_meta", methods=["POST"])
def update_group_meta(group_id):
    groups_cfg = _get_groups_config_file()
    if not os.path.exists(groups_cfg):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(groups_cfg, "r", encoding="utf-8") as f:
            all_groups = json.load(f)

        if group_id not in all_groups:
            return jsonify({"error": "Group not found"}), 404

        data = request.json
        # 更新字段
        if "name" in data: all_groups[group_id]["name"] = data["name"].strip()
        if "avatar" in data: all_groups[group_id]["avatar"] = data["avatar"].strip()
        if "language" in data: all_groups[group_id]["language"] = data["language"].strip()

        # 【新增】主动消息开关
        if "active_mode" in data:
            all_groups[group_id]["active_mode"] = bool(data["active_mode"])

        # 置顶开关（与单聊一致，通讯录中置顶显示）
        if "pinned" in data:
            all_groups[group_id]["pinned"] = bool(data["pinned"])

        # 群聊线上线下模式 (online/offline)
        if "group_chat_mode" in data:
            all_groups[group_id]["group_chat_mode"] = data["group_chat_mode"]

        # 是否包含用户
        if "include_user" in data:
            all_groups[group_id]["include_user"] = bool(data["include_user"])

        with open(groups_cfg, "w", encoding="utf-8") as f:
            json.dump(all_groups, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- 【新增】群聊搜索接口 ---
@group_bp.route("/api/group/<group_id>/search", methods=["POST"])
def search_group_messages(group_id):
    keyword = request.json.get("keyword", "").strip()
    if not keyword: return jsonify([])

    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")
    if not os.path.exists(db_path): return jsonify([])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE content LIKE ? ORDER BY timestamp DESC", (f"%{keyword}%",))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return jsonify(rows)


# ==================== 群聊配置 / 头像 / 增删 ====================

@group_bp.route("/api/group/<group_id>/config")
def get_group_details(group_id):
    # 使用 per-user groups.json / characters.json
    groups_cfg = _get_groups_config_file()
    chars_cfg = _get_characters_config_file()

    if not os.path.exists(groups_cfg):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(groups_cfg, "r", encoding="utf-8") as f:
            groups_config = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to load groups config: {e}"}), 500

    group_info = groups_config.get(group_id)
    if not group_info:
        return jsonify({"error": "Group not found"}), 404

    # 读取当前用户的角色配置，填充成员详细信息
    members_details = {}

    # 1. 先尝试读取私有配置
    private_chars = {}
    if os.path.exists(chars_cfg):
        try:
            with open(chars_cfg, "r", encoding="utf-8") as f:
                private_chars = json.load(f)
        except Exception as e:
            print(f"[get_group_details] 加载私有成员信息失败: {e}")

    # 2. 尝试读取全局配置
    global_chars = {}
    global_cfg_file = os.path.join(BASE_DIR, "configs", "characters.json")
    if os.path.exists(global_cfg_file):
        try:
            with open(global_cfg_file, "r", encoding="utf-8") as f:
                global_chars = json.load(f)
        except Exception as e:
            print(f"[get_group_details] 加载全局成员信息失败: {e}")

    for member_id in group_info.get("members", []):
        if member_id in private_chars:
            members_details[member_id] = private_chars[member_id]
        elif member_id in global_chars:
            members_details[member_id] = global_chars[member_id]

    return jsonify({
        "group_info": group_info,
        "members": members_details
    })


@group_bp.route("/api/group/<group_id>/upload_avatar", methods=["POST"])
def upload_group_avatar(group_id):
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            # 1. 确定保存路径: groups/<group_id>/
            target_group_dir = get_group_dir(group_id)
            if not os.path.exists(target_group_dir):
                os.makedirs(target_group_dir)

            # 删除旧的头像文件（所有格式）
            for old_avatar in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp", "avatar.gif"):
                old_path = os.path.join(target_group_dir, old_avatar)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception as e:
                        print(f"[GroupAvatar] 删除旧头像失败: {e}")

            # 2. 统一保存为 avatar.png
            filename = "avatar.png"
            file_path = os.path.join(target_group_dir, filename)

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
                print(f"[GroupAvatar] PIL转换失败，直接保存: {e}")
                # 如果PIL转换失败，直接保存原始文件
                file.seek(0)
                file.save(file_path)

            # 上传到 COS
            user_id = get_current_user_id()
            timestamp = int(time.time())
            cos_path = f"users/{user_id}/groups/{group_id}/avatar.png"
            cos_url = upload_to_cos(file_path, cos_path)

            # 删除本地临时文件
            if os.path.exists(file_path):
                os.remove(file_path)

            if not cos_url:
                return jsonify({"error": "Failed to upload to COS"}), 500

            new_url = f"{cos_url}?t={timestamp}"

            # 3. 更新 groups.json 配置
            groups_cfg = _get_groups_config_file()
            if os.path.exists(groups_cfg):
                with open(groups_cfg, "r", encoding="utf-8") as f:
                    groups_config = json.load(f)

                if group_id in groups_config:
                    groups_config[group_id]["avatar"] = new_url

                    # 使用安全保存
                    safe_save_json(groups_cfg, groups_config)

                    return jsonify({"status": "success", "url": new_url})
                else:
                    return jsonify({"error": "Group config not found"}), 404
            else:
                return jsonify({"error": "Groups config file missing"}), 500

        except Exception as e:
            print(f"Group Upload Error: {e}")
            return jsonify({"error": str(e)}), 500


@group_bp.route("/api/groups/add", methods=["POST"])
def add_group():
    try:
        data = request.json
        new_id = data.get("id", "").strip()
        new_name = data.get("name", "").strip()
        members = data.get("members", []) # list of char_ids

        # 1. 校验
        if not new_id or not new_name:
            return jsonify({"error": "ID和名称不能为空"}), 400
        if len(members) < 2:
            return jsonify({"error": "群聊至少需要2名成员"}), 400

        if not re.match(r'^[a-zA-Z0-9_]+$', new_id):
            return jsonify({"error": "ID 只能包含字母、数字或下划线"}), 400

        # 2. 读取/初始化配置（per-user）
        groups_cfg = _get_groups_config_file()
        groups_config = {}
        if os.path.exists(groups_cfg):
            with open(groups_cfg, "r", encoding="utf-8") as f:
                groups_config = json.load(f)

        if new_id in groups_config:
            return jsonify({"error": "该群聊ID已存在"}), 400

        # 3. 创建文件夹
        target_group_dir = get_group_dir(new_id)
        if not os.path.exists(target_group_dir):
            os.makedirs(target_group_dir)

        # 4. 初始化群聊数据库
        db_path = os.path.join(target_group_dir, "chat.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # 群聊表结构与单人一致，但 role 字段可能会存具体的 char_id
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()
        conn.close()

        # 5. 更新配置（写入当前用户 groups.json）
        groups_config[new_id] = {
            "name": new_name,
            "avatar": "/static/default_group.png", # 记得在static放个图
            "pinned": False,
            "members": members,
            "active_mode": False  # 【修改】新建群默认开启主动消息
        }

        with open(groups_cfg, "w", encoding="utf-8") as f:
            json.dump(groups_config, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Add Group Error: {e}")
        return jsonify({"error": str(e)}), 500


@group_bp.route("/api/group/<group_id>/delete", methods=["DELETE"])
def delete_group_api(group_id):
    groups_cfg = _get_groups_config_file()
    if not os.path.exists(groups_cfg):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(groups_cfg, "r", encoding="utf-8") as f:
            groups_config = json.load(f)

        if group_id not in groups_config:
            return jsonify({"error": "Group not found"}), 404

        del groups_config[group_id]
        with open(groups_cfg, "w", encoding="utf-8") as f:
            json.dump(groups_config, f, ensure_ascii=False, indent=2)

        group_dir = get_group_dir(group_id)
        if os.path.exists(group_dir):
            shutil.rmtree(group_dir)
        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Delete Group Error: {e}")
        return jsonify({"error": str(e)}), 500
