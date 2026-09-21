import re
import json
import os
import sqlite3
import unicodedata
from datetime import datetime
from core.utils import auto_toggle_chat_mode_on_move
from services.content_actions import (
    PERSONA_ACTIONS,
    PLAN_ACTIONS,
    RELATION_ACTIONS,
    apply_persona_action,
    apply_plan_action,
    apply_relation_action,
    extract_content_actions,
)
from services.memory_store import atomic_write_json, load_json_object, memory_file_lock

def process_agent_actions(char_id, raw_text, user_id=None, return_events=False):
    """
    解析并执行 AI 返回文本中的动作标签 (Action Tags)。
    执行后从文本中移除这些标签，并返回纯净文本。
    支持的标签:
    [SET_EMOTION: 数字]
    [SET_PERSONALITY: 数字]
    [UPDATE_AFFINITY: +/-数字]
    [SET_SLEEP_TIME: "HH:MM-HH:MM"]
    [SET_RELATION: {"target": "角色名称(非ID)", "value": 0-5}]
    [ADD_SCHEDULE: {"date": "YYYY-MM-DD", "content": "内容"}]
    [ADD_SCHEDULE: {"content": "无明确日期的计划"}]
    [ADD/DELETE/EDIT/REWRITE_PERSONA: JSON]
    [ADD/DELETE/EDIT/REWRITE_RELATION: JSON]
    [ADD/DELETE/EDIT/REWRITE_PLAN: JSON]
    [MOOD: 预设情绪值]  (仅限: 平静 开心 悲伤 愤怒 兴奋 害羞 温柔 冷淡)
    [DIRECT_TO_GROUP: 角色1, 角色2, +user]  /  [DIRECT_TO_GROUP: 群名 | 角色1, 角色2]
    [DIRECT_TO_USER]
    [DIRECT_END]
    [NONE]

    Returns: (cleaned_text, affinity_delta, directive)
      return_events=True 时返回 (cleaned_text, affinity_delta, directive, agent_events)
      affinity_delta 为亲密度净变动，无标签时为 None
      directive 为转向指令，无标签时为 None
      directive = {"type": "user"} 或 {"type": "group", "member_ids": [...], ...}
    """
    if not raw_text:
        if return_events:
            return raw_text, None, None, []
        return raw_text, None, None

    cleaned_text = raw_text
    total_affinity_delta = 0.0
    has_affinity = False
    directive = None
    agent_events = []

    # 人设 / 关系 / 计划的结构化增删改重写。这里先用完整 JSON
    # 解析器提取，避免传统 {.*?} 正则在嵌套对象处提前截断。
    content_actions, cleaned_text = extract_content_actions(cleaned_text)
    if content_actions:
        agent_events.extend(
            _execute_content_actions(char_id, content_actions, user_id=user_id)
        )

    # 1. 提取情绪指数 (Emotion)
    emotion_pattern = r'\[SET_EMOTION:\s*(\d+(?:\.\d+)?)\]'
    for match in re.finditer(emotion_pattern, raw_text):
        try:
            emotion_val = float(match.group(1))
            if _update_persona_param(char_id, "emotion", emotion_val, user_id=user_id):
                print(f"[Agent Action] {char_id} 情绪指数设置为 {emotion_val}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Emotion 失败: {e}")
    cleaned_text = re.sub(emotion_pattern, '', cleaned_text)

    # 2. 提取性格指数 (Personality)
    personality_pattern = r'\[SET_PERSONALITY:\s*(\d+(?:\.\d+)?)\]'
    for match in re.finditer(personality_pattern, raw_text):
        try:
            personality_val = float(match.group(1))
            if _update_persona_param(char_id, "personality", personality_val, user_id=user_id):
                print(f"[Agent Action] {char_id} 性格指数设置为 {personality_val}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Personality 失败: {e}")
    cleaned_text = re.sub(personality_pattern, '', cleaned_text)

    # 3. 提取亲密度 (Affinity) -> 累加逻辑
    affinity_pattern = r'\[UPDATE_AFFINITY:\s*([+-]?\d+(?:\.\d+)?)\]'
    for match in re.finditer(affinity_pattern, raw_text):
        try:
            delta = float(match.group(1))
            _update_user_affinity(char_id, delta, user_id)
            total_affinity_delta += delta
            has_affinity = True
            print(f"[Agent Action] {char_id} 亲密度变动 {delta}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Affinity 失败: {e}")
    cleaned_text = re.sub(affinity_pattern, '', cleaned_text)

    # 4. 提取深睡眠时间 (SleepWindow)
    sleep_time_pattern = r'\[SET_SLEEP_TIME:\s*"([^"]+)"\]'
    for match in re.finditer(sleep_time_pattern, raw_text):
        try:
            sleep_range = match.group(1)
            _update_sleep_time(char_id, sleep_range, user_id)
            print(f"[Agent Action] {char_id} 深睡眠时间设置为 {sleep_range}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 SleepTime 失败: {e}")
    cleaned_text = re.sub(sleep_time_pattern, '', cleaned_text)

    # 5. 提取关系图谱 (RelMap)
    relation_pattern = r'\[SET_RELATION:\s*(\{.*?\})\]'
    for match in re.finditer(relation_pattern, raw_text):
        try:
            rel_data = json.loads(match.group(1))
            target = rel_data.get("target")
            value = rel_data.get("value")
            if target and value is not None:
                _update_relationship(char_id, target, value, user_id=user_id)
                print(f"[Agent Action] {char_id} 与 {target} 关系指数设置为 {value}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Relation 失败: {e}")
    cleaned_text = re.sub(relation_pattern, '', cleaned_text)

    # 6. 提取近期日程 (Schedule) -> 追加逻辑
    schedule_pattern = r'\[ADD_SCHEDULE:\s*(\{.*?\})\]'
    for match in re.finditer(schedule_pattern, raw_text):
        try:
            sched_data = json.loads(match.group(1))
            date_str = sched_data.get("date")
            content = sched_data.get("content")
            if content:
                changed, category = _add_schedule(
                    char_id, date_str, content, user_id=user_id
                )
                label = date_str if category == "dated" else "无时间计划"
                action = "增加" if changed else "跳过重复"
                print(f"[Agent Action] {char_id} {action}日程 {label}: {content}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Schedule 失败: {e}")
    cleaned_text = re.sub(schedule_pattern, '', cleaned_text)

    # 7. 提取聊天模式切换 (Chat Mode)
    chat_mode_pattern = r'\[SET_CHAT_MODE:\s*(online|offline)\]'
    for match in re.finditer(chat_mode_pattern, raw_text, re.IGNORECASE):
        try:
            mode = match.group(1).lower()
            _update_chat_mode(char_id, mode, user_id=user_id)
            print(f"[Agent Action] {char_id} 聊天模式切换为 {mode}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Chat Mode 失败: {e}")
    cleaned_text = re.sub(chat_mode_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 8. 提取情绪标签 (Mood) — 仅限预设值（中文）
    VALID_MOODS = {"平静", "开心", "悲伤", "愤怒", "兴奋", "害羞", "温柔", "冷淡"}
    mood_pattern = r'\[MOOD:\s*(.+?)\]'
    for match in re.finditer(mood_pattern, raw_text):
        try:
            mood_val = match.group(1).strip()
            if mood_val in VALID_MOODS:
                _update_mood(char_id, mood_val, user_id=user_id)
                print(f"[Agent Action] {char_id} 情绪标签更新为 {mood_val}")
            else:
                print(f"[Agent Action] {char_id} MOOD 值 '{mood_val}' 不在预设列表中，已忽略（允许值: {', '.join(sorted(VALID_MOODS))}）")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Mood 失败: {e}")
    cleaned_text = re.sub(mood_pattern, '', cleaned_text)

    # 安全提示是持久化展示标识，不作为一次性动作事件消费。
    # 兼容旧版带提示语的标签，统一保存固定标识，文案交由前端管理。
    safety_pattern = r'[\[【]\s*SAFETY_ALERT\s*(?:[:：]\s*(.*?))?[\]】]'
    cleaned_text = re.sub(safety_pattern, '[SAFETY_ALERT]', cleaned_text, flags=re.IGNORECASE | re.DOTALL)

    # 9. 提取对话转向指令 (Direct To Group)
    direct_pattern = r'[\[【]\s*DIRECT_TO_GROUP\s*[:：]\s*(.+?)[\]】]'
    for match in re.finditer(direct_pattern, raw_text, re.IGNORECASE):
        try:
            d_content = match.group(1).strip()
            include_user = False
            custom_name = None

            # 检查尾部 +user 标记
            if d_content.lower().endswith('+user'):
                include_user = True
                d_content = d_content[:d_content.lower().rfind('+user')].strip().rstrip(',').strip()

            # 检查自定义群名: 群名 | member1, member2
            if '|' in d_content or '｜' in d_content:
                parts = re.split(r'[|｜]', d_content, maxsplit=1)
                custom_name = parts[0].strip()
                member_part = parts[1].strip()
            else:
                member_part = d_content

            # 解析成员列表
            member_names = [m.strip() for m in re.split(r'[,，、]', member_part) if m.strip()]
            member_ids = [_resolve_char_id(m, user_id=user_id) for m in member_names]
            member_ids = [m for m in member_ids if m]  # 过滤掉解析失败的

            if member_ids:
                directive = {
                    "type": "group",
                    "member_ids": member_ids,
                    "include_user": include_user,
                    "custom_name": custom_name
                }
                print(f"[Agent Action] {char_id} 发起转向指令 -> members={member_ids}, include_user={include_user}, name={custom_name}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 DIRECT_TO_GROUP 失败: {e}")
    cleaned_text = re.sub(direct_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 10. 提取切换到单聊指令 (Direct To User)
    user_pattern = r'[\[【]\s*DIRECT_TO_USER\s*[\]】]'
    if re.search(user_pattern, raw_text, re.IGNORECASE):
        directive = {"type": "user"}
        print(f"[Agent Action] {char_id} 发起切换到单聊指令")
    cleaned_text = re.sub(user_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 11. 清理结束对话标签 (Direct End)
    end_pattern = r'[\[【]\s*DIRECT_END\s*[\]】]'
    cleaned_text = re.sub(end_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 12. 提取无操作标签 (None / 无需改动的占位标签)
    none_pattern = r'\[NONE\]'
    if re.search(none_pattern, cleaned_text, re.IGNORECASE):
        print(f"[Agent Action] {char_id} 无操作标签 (NONE)，跳过所有动作")
    cleaned_text = re.sub(none_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 13. 提取位置移动标签 (MOVE_TO / MOVE_TO_COORD / EXPLORE)
    location_result = _process_location_tags(char_id, raw_text, user_id)
    if location_result:
        agent_events.append({
            "type": "location_change",
            **location_result,
        })
    for tag_pattern in [
        r'\[MOVE_TO:\s*[^\]]+\]',
        r'\[MOVE_TO_COORD:\s*[\d.\-]+,\s*[\d.\-]+\s*\]',
        r'\[EXPLORE:\s*[\d.\-]+,\s*[\d.\-]+,\s*"[^"]*",\s*"[^"]*"\s*\]',
    ]:
        cleaned_text = re.sub(tag_pattern, '', cleaned_text)

    # 清理多余的空白字符，如果标签单独占一行，删除后可能会留下空行
    # 13.5 兜底清理任何残留、畸形或大写下划线的 Agent 动作指令标签，例如 [UPDATE_AFFINITY:user,-1] 或 [SET_EMOTION:arrogant] 等
    # 只要包含了预定义的命令关键字或大写加下划线标签，均彻底清除。
    robust_agent_pattern = r'[\[【]\s*(?:UPDATE_AFFINITY|SET_EMOTION|SET_PERSONALITY|SET_SLEEP_TIME|SET_RELATION|ADD_SCHEDULE|ADD_PERSONA|DELETE_PERSONA|EDIT_PERSONA|REWRITE_PERSONA|ADD_RELATION|DELETE_RELATION|EDIT_RELATION|REWRITE_RELATION|ADD_PLAN|DELETE_PLAN|EDIT_PLAN|REWRITE_PLAN|MOOD|DIRECT_TO_GROUP|DIRECT_TO_USER|DIRECT_END|NONE|MOVE_TO|MOVE_TO_COORD|EXPLORE|SET_CHAT_MODE|MUSIC_[A-Z0-9_]+)(?::|：)?\s*.*?[\]】]'
    cleaned_text = re.sub(robust_agent_pattern, '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)

    # 额外兜底清理所有全大写加下划线的指令标签，避免未定义或畸形的指令流出（不清理 SEARCH_IMG 和 GENERATE_IMAGE 标签，交由媒体解析器专门处理；THOUGHTS 和 SAFETY_ALERT 为持久化展示标签，需保留）
    general_pattern = r'[\[【]\s*(?!(?:SAFETY_ALERT|VOICE|SEARCH_IMG|GENERATE_IMAGE|CLICK_REF|CLICK|TYPE|GOTO|BACK|FINISH|ASK|WAIT|WEB_CRUISE|STOP|REPLY|THOUGHTS)\b)(?:[A-Z_][A-Z0-9_]*)(?::|：)?\s*.*?[\]】]'
    cleaned_text = re.sub(general_pattern, '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)

    cleaned_text = re.sub(r'\n\s*\n', '\n', cleaned_text).strip()
    result = (cleaned_text, (round(total_affinity_delta, 2) if has_affinity else None), directive)
    if return_events:
        return (*result, agent_events)
    return result


def _execute_content_actions(char_id, actions, user_id=None):
    """Apply each document's actions as one atomic transaction."""

    from app import get_paths

    _, prompts_dir = get_paths(char_id, user_id=user_id)
    os.makedirs(prompts_dir, exist_ok=True)
    events = []

    def record(action, status, code=None, message=None):
        event = {
            "type": "content_action",
            "action": action.name,
            "status": status,
        }
        if code:
            event["code"] = code
        if message:
            event["message"] = message
        events.append(event)

    persona_actions = [action for action in actions if action.name in PERSONA_ACTIONS]
    if persona_actions:
        json_path = os.path.join(prompts_dir, "1_base_persona.json")
        try:
            with memory_file_lock(json_path):
                data = load_json_object(json_path) if os.path.exists(json_path) else {}
                current = data.get("system_prompt", "")
                if not isinstance(current, str):
                    raise ValueError("system_prompt 必须是文本")
                updated = current
                for action in persona_actions:
                    updated = apply_persona_action(updated, action.name, action.payload)
                # 核心人设只保留 system_prompt，同时清除旧外貌和无效设置字段。
                atomic_write_json(json_path, {"system_prompt": updated})
            for action in persona_actions:
                record(action, "success")
        except Exception as exc:
            code = getattr(exc, "code", "storage_error")
            for action in persona_actions:
                record(action, "failed", code, str(exc))
            print(f"[Agent Action Error] Persona transaction rolled back: {exc}")

    relation_actions = [action for action in actions if action.name in RELATION_ACTIONS]
    if relation_actions:
        path = os.path.join(prompts_dir, "2_relationship.json")
        try:
            with memory_file_lock(path):
                updated = load_json_object(path)
                for action in relation_actions:
                    updated = apply_relation_action(updated, action.name, action.payload)
                atomic_write_json(path, updated)
            for action in relation_actions:
                record(action, "success")
        except Exception as exc:
            code = getattr(exc, "code", "storage_error")
            for action in relation_actions:
                record(action, "failed", code, str(exc))
            print(f"[Agent Action Error] Relation transaction rolled back: {exc}")

    plan_actions = [action for action in actions if action.name in PLAN_ACTIONS]
    if plan_actions:
        path = os.path.join(prompts_dir, "7_schedule.json")
        try:
            with memory_file_lock(path):
                updated = load_json_object(path)
                for action in plan_actions:
                    updated = apply_plan_action(updated, action.name, action.payload)
                atomic_write_json(path, updated)
            for action in plan_actions:
                record(action, "success")
        except Exception as exc:
            code = getattr(exc, "code", "invalid_schedule")
            for action in plan_actions:
                record(action, "failed", code, str(exc))
            print(f"[Agent Action Error] Plan transaction rolled back: {exc}")

    for event in events:
        print(
            f"[Agent Action] {char_id} {event['action']} -> {event['status']}"
            + (f" ({event.get('code')}: {event.get('message')})" if event.get("code") else "")
        )
    return events

def _update_persona_param(char_id, param_name, value, user_id=None):
    """Apply an AI index update only if the user has not locked that index."""
    field = {"emotion": "emotion", "personality": "moments_index"}.get(param_name)
    if field is None:
        return False
    try:
        from core.utils import _get_characters_config_file
        cfg_file = _get_characters_config_file(user_id=user_id)
        if not os.path.exists(cfg_file):
            print(f"[Agent Action] WARNING: config file not found: {cfg_file}")
            return False
        # Read the latest lock inside the same transaction as the value write.
        with memory_file_lock(cfg_file):
            data = load_json_object(cfg_file, missing_ok=False)
            info = data.get(char_id)
            if not isinstance(info, dict):
                print(f"[Agent Action] WARNING: char_id '{char_id}' not found in {cfg_file}")
                return False
            if info.get(f"{field}_locked", False):
                print(f"[Agent Action] {char_id} {field} 已锁定，跳过自动修改")
                return False
            info[field] = float(value)
            atomic_write_json(cfg_file, data)
        return True
    except Exception as e:
        print(f"[Agent Action Error] Update Persona Param: {e}")
        return False

def _update_user_affinity(char_id, delta, current_user_id=None):
    """累加亲密度到 characters.json 中"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file(user_id=current_user_id)
        if not os.path.exists(cfg_file):
            print(f"[Agent Action] WARNING: config file not found: {cfg_file}")
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if char_id not in data:
            print(f"[Agent Action] WARNING: char_id '{char_id}' not found in {cfg_file}")
            return

        current_intimacy = float(data[char_id].get("intimacy", 60))
        new_intimacy = max(0, min(100, current_intimacy + delta))
        data[char_id]["intimacy"] = new_intimacy
        safe_save_json(cfg_file, data)
        print(f"[Agent Action] {char_id} intimacy {current_intimacy} -> {new_intimacy} written to {cfg_file}")
    except Exception as e:
        print(f"[Agent Action Error] Update Affinity: {e}")

def _update_sleep_time(char_id, sleep_range, current_user_id=None):
    """更新角色当地睡眠时间段，不直接改变 deep_sleep 开关。"""
    try:
        from app import _get_characters_config_file, safe_save_json
        from core.time_utils import (
            ensure_character_time_defaults,
            get_character_timezone,
            parse_hhmm,
        )
        cfg_file = _get_characters_config_file(user_id=current_user_id)
        if not os.path.exists(cfg_file):
            print(f"[Agent Action] WARNING: config file not found: {cfg_file}")
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if char_id not in data:
            print(f"[Agent Action] WARNING: char_id '{char_id}' not found in {cfg_file}")
            return

        parts = sleep_range.split("-")
        if len(parts) == 2:
            start, end = parts[0].strip(), parts[1].strip()
            if parse_hhmm(start) is None or parse_hhmm(end) is None or start == end:
                print(f"[Agent Action] WARNING: invalid sleep_range: {sleep_range}")
                return
            info = data[char_id]
            ensure_character_time_defaults(info, existing_character=True)
            info["ds_start"] = start
            info["ds_end"] = end
            info["ds_time_basis"] = "character"
            info["ds_set_by"] = "character"
            info["ds_timezone_at_set"] = get_character_timezone(info)
            info["sleep_last_event_key"] = None
            info["sleep_manual_override"] = False
            safe_save_json(cfg_file, data)
            print(
                f"[Agent Action] {char_id} local sleep {start}-{end} "
                f"({info['ds_timezone_at_set']}) written to {cfg_file}"
            )
        else:
            print(f"[Agent Action] WARNING: invalid sleep_range format: {sleep_range}")
    except Exception as e:
        print(f"[Agent Action Error] Update Sleep Time: {e}")

def _update_relationship(char_id, target, value, user_id=None):
    """更新 2_relationship.json"""
    from app import get_paths, safe_save_json
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    rel_path = os.path.join(prompts_dir, "2_relationship.json")
    if os.path.exists(rel_path):
        try:
            with open(rel_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if target in data:
                data[target]["score"] = value
            else:
                # 如果没有这个 target，就新建一个基本的记录
                data[target] = {"role": "未知", "score": value, "description": ""}

            safe_save_json(rel_path, data)
        except Exception as e:
            print(f"Update Relationship Error: {e}")

def _add_schedule(char_id, date_str, content, user_id=None):
    """追加有日期或无时间计划到 7_schedule.json。"""
    from app import get_paths
    from services.schedule import append_schedule_item

    _, prompts_dir = get_paths(char_id, user_id=user_id)
    sched_path = os.path.join(prompts_dir, "7_schedule.json")
    return append_schedule_item(sched_path, date_str, content)

def _update_chat_mode(char_id, mode, user_id=None):
    """更新角色的聊天模式 (online/offline) 到 characters.json"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file(user_id=user_id)
        if not os.path.exists(cfg_file):
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if char_id not in data:
            return
        data[char_id]["chat_mode"] = mode
        safe_save_json(cfg_file, data)
        print(f"[Agent Action] {char_id} chat_mode -> {mode}")
    except Exception as e:
        print(f"[Agent Action Error] Update Chat Mode: {e}")

def _normalize_character_lookup_key(value):
    """Normalize harmless display-name variants before character lookup."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = normalized.strip("\"'“”‘’「」『』")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.translate(str.maketrans({
        "・": "·",
        "•": "·",
        "･": "·",
        "‧": "·",
    }))
    return normalized.casefold()


def _resolve_char_id(target, user_id=None):
    """
    将名字/ID/备注 解析为 char_id。
    优先精确匹配ID，然后匹配name，最后匹配remark。
    """
    if not target:
        return None
    target_key = _normalize_character_lookup_key(target)
    try:
        from app import _get_characters_config_file
        cfg_file = _get_characters_config_file(user_id=user_id)
        if not os.path.exists(cfg_file):
            return None
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1. 精确匹配 ID (不区分大小写)
        for cid in data:
            if _normalize_character_lookup_key(cid) == target_key:
                return cid

        # 2. 匹配 name
        for cid, cinfo in data.items():
            if _normalize_character_lookup_key(cinfo.get("name")) == target_key:
                return cid

        # 3. 匹配 remark
        for cid, cinfo in data.items():
            if _normalize_character_lookup_key(cinfo.get("remark")) == target_key:
                return cid

        print(f"[Agent Action] WARNING: 无法将 '{target}' 解析为角色ID")
        return None
    except Exception as e:
        print(f"[Agent Action Error] _resolve_char_id: {e}")
        return None

def _update_mood(char_id, mood, user_id=None):
    """更新 preset mood（voice_emotion）到 characters.json"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file(user_id=user_id)
        if not os.path.exists(cfg_file):
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if char_id not in data:
            return
        data[char_id]["voice_emotion"] = mood
        safe_save_json(cfg_file, data)
        print(f"[Agent Action] {char_id} voice_emotion -> {mood}")
    except Exception as e:
        print(f"[Agent Action Error] Update Mood: {e}")

def _process_location_tags(char_id, raw_text, user_id=None):
    """处理位置移动标签: MOVE_TO, MOVE_TO_COORD, EXPLORE"""
    if not re.search(r"\[(?:MOVE_TO|MOVE_TO_COORD|EXPLORE)\s*:", raw_text or "", re.IGNORECASE):
        return None
    import math
    from app import get_char_name, _get_characters_config_file
    from core.utils import (
        load_character_positions,
        load_user_position,
        load_locations,
        save_locations,
        calc_distance,
        get_location_by_id,
        get_location_at_exact_coord,
        move_character_position,
        normalize_map_state,
    )

    char_cfg = _get_characters_config_file(user_id=user_id)
    try:
        with open(char_cfg, "r", encoding="utf-8") as f:
            chars = json.load(f) or {}
    except Exception:
        chars = {}
    if char_id not in chars:
        return {"status": "failed", "errors": ["character not found"]}

    positions, _, _ = normalize_map_state(user_id=user_id)
    if char_id not in positions:
        try:
            move_character_position(
                char_id,
                0.0,
                0.0,
                location_id="home",
                force=True,
                user_id=user_id,
            )
            positions = load_character_positions(user_id=user_id)
        except ValueError as e:
            return {"status": "failed", "errors": [str(e)]}

    moved = False
    action_desc = None
    errors = []

    # 1. [MOVE_TO: location_id]
    move_to_pattern = r'\[MOVE_TO:\s*([^\]]+?)\]'
    for match in re.finditer(move_to_pattern, raw_text):
        try:
            loc_id = match.group(1).strip()
            loc = get_location_by_id(loc_id, user_id=user_id)
            if not loc:
                error = f"MOVE_TO '{loc_id}': location not found"
                errors.append(error)
                print(f"[Agent Action] {char_id} {error}")
                continue
            _, pos = move_character_position(
                char_id,
                loc["x"],
                loc["y"],
                location_id=loc_id,
                user_id=user_id,
            )
            action_desc = f"{get_char_name(char_id)}移动到了{loc.get('name', loc_id)}"
            moved = True
            print(f"[Agent Action] {char_id} MOVE_TO -> {loc_id} ({pos['x']}, {pos['y']})")
        except ValueError as e:
            errors.append(f"MOVE_TO '{match.group(1).strip()}': {e}")
            print(f"[Agent Action Error] MOVE_TO: {e}")
        except Exception as e:
            errors.append(f"MOVE_TO: {e}")
            print(f"[Agent Action Error] MOVE_TO: {e}")

    # 2. [MOVE_TO_COORD: x, y]
    move_coord_pattern = r'\[MOVE_TO_COORD:\s*([\d.\-]+),\s*([\d.\-]+)\s*\]'
    for match in re.finditer(move_coord_pattern, raw_text):
        try:
            target_x = float(match.group(1))
            target_y = float(match.group(2))
            _, pos = move_character_position(
                char_id,
                target_x,
                target_y,
                user_id=user_id,
            )
            loc_at = get_location_by_id(pos.get("location_id"), user_id=user_id) if pos.get("location_id") else None
            loc_name = loc_at.get("name") if loc_at else f"({round(pos['x'],2)}, {round(pos['y'],2)})"
            action_desc = f"{get_char_name(char_id)}移动到了{loc_name}"
            moved = True
            print(f"[Agent Action] {char_id} MOVE_TO_COORD -> ({pos['x']}, {pos['y']})")
        except ValueError as e:
            errors.append(f"MOVE_TO_COORD ({match.group(1)},{match.group(2)}): {e}")
            print(f"[Agent Action Error] MOVE_TO_COORD: {e}")
        except Exception as e:
            errors.append(f"MOVE_TO_COORD: {e}")
            print(f"[Agent Action Error] MOVE_TO_COORD: {e}")

    # 3. [EXPLORE: x, y, "name", "description"]
    explore_pattern = r'\[EXPLORE:\s*([\d.\-]+),\s*([\d.\-]+),\s*"([^"]*)",\s*"([^"]*)"\s*\]'
    for match in re.finditer(explore_pattern, raw_text):
        try:
            target_x = float(match.group(1))
            target_y = float(match.group(2))
            loc_name = match.group(3).strip()
            loc_desc = match.group(4).strip()
            current_positions = load_character_positions(user_id=user_id)
            current_pos = current_positions[char_id]
            d = calc_distance(current_pos["x"], current_pos["y"], target_x, target_y)
            if d >= 1.0:
                error = f"EXPLORE ({target_x},{target_y}): distance {round(d,2)} is not below 1.0"
                errors.append(error)
                print(f"[Agent Action] {char_id} {error}")
                continue
            locs = load_locations(user_id=user_id)
            existing_location = get_location_at_exact_coord(
                target_x,
                target_y,
                user_id=user_id,
            )
            if existing_location:
                _, pos = move_character_position(
                    char_id,
                    target_x,
                    target_y,
                    location_id=existing_location["id"],
                    user_id=user_id,
                )
                existing_name = existing_location.get("name", existing_location["id"])
                action_desc = f"{get_char_name(char_id)}移动到了已有地点{existing_name}"
                moved = True
                print(
                    f"[Agent Action] {char_id} EXPLORE matched existing "
                    f"location -> {existing_location['id']}"
                )
                continue
            loc_id = "loc_" + str(len(locs.get("locations", [])))
            existing_ids = {l["id"] for l in locs.get("locations", [])}
            counter = 1
            while loc_id in existing_ids:
                loc_id = f"loc_{len(locs.get('locations', []))}_{counter}"
                counter += 1
            r_val = math.sqrt(target_x*target_x + target_y*target_y)
            theta_val = math.atan2(target_y, target_x)
            new_loc = {
                "id": loc_id,
                "name": loc_name,
                "description": loc_desc,
                "x": target_x,
                "y": target_y,
                "r": round(r_val, 4),
                "theta": round(theta_val, 4),
                "is_default": False,
                "created_by": char_id,
            }
            locs["locations"].append(new_loc)
            save_locations(locs, user_id=user_id)
            _, pos = move_character_position(
                char_id,
                target_x,
                target_y,
                location_id=loc_id,
                user_id=user_id,
            )
            action_desc = f"{get_char_name(char_id)}探索到了{loc_name}并移动到此处"
            moved = True
            print(f"[Agent Action] {char_id} EXPLORE -> {loc_name} ({target_x}, {target_y})")
        except ValueError as e:
            errors.append(f"EXPLORE: {e}")
            print(f"[Agent Action Error] EXPLORE: {e}")
        except Exception as e:
            errors.append(f"EXPLORE: {e}")
            print(f"[Agent Action Error] EXPLORE: {e}")

    if moved:
        positions = load_character_positions(user_id=user_id)
        pos = positions[char_id]
        user_pos = load_user_position(user_id=user_id)
        encounter_msgs = []
        for eid, epos in positions.items():
            if eid == char_id:
                continue
            if calc_distance(pos["x"], pos["y"], epos["x"], epos["y"]) < 0.1:
                encounter_msgs.append(f"与{get_char_name(eid)}相遇")
        if calc_distance(pos["x"], pos["y"], user_pos["x"], user_pos["y"]) < 0.1:
            encounter_msgs.append("与用户相遇")
        if encounter_msgs:
            action_desc = (action_desc or "") + "，" + "；".join(encounter_msgs)

        return {
            "status": "success",
            "char_id": char_id,
            "description": action_desc,
            "location_id": pos.get("location_id"),
            "x": pos["x"],
            "y": pos["y"],
            "errors": errors,
        }
    if errors:
        return {"status": "failed", "char_id": char_id, "errors": errors}
    return None


# ==================== 音乐操作标签解析 ====================

def parse_music_tags(text):
    """
    从 AI 回复文本中提取音乐操作标签。
    返回: (cleaned_text, tags_list)

    支持的标签:
    [MUSIC_MODE_ENTER] / [MUSIC_MODE_EXIT]
    [MUSIC_SEARCH:关键词:数量]
    [MUSIC_PLAY:歌曲ID]
    [MUSIC_PAUSE] / [MUSIC_RESUME] / [MUSIC_STOP]
    [MUSIC_NEXT] / [MUSIC_PREV]
    [MUSIC_PLAYLIST_LIST]
    [MUSIC_PLAYLIST_VIEW:歌单ID]
    [MUSIC_PLAYLIST_CREATE:名称]
    [MUSIC_PLAYLIST_ADD:歌单ID:歌曲ID]
    [MUSIC_PLAYLIST_DELETE:歌单ID]
    """
    if not text:
        return text, []

    tags = []
    cleaned = text

    # [MUSIC_MODE_ENTER]
    if re.search(r'\[MUSIC_MODE_ENTER\]', cleaned):
        tags.append({"type": "enter"})
        cleaned = re.sub(r'\[MUSIC_MODE_ENTER\]', '', cleaned)

    # [MUSIC_MODE_EXIT]
    if re.search(r'\[MUSIC_MODE_EXIT\]', cleaned):
        tags.append({"type": "exit"})
        cleaned = re.sub(r'\[MUSIC_MODE_EXIT\]', '', cleaned)

    # [MUSIC_SEARCH:关键词:数量]
    search_pattern = r'\[MUSIC_SEARCH:\s*([^:\]]+):\s*(\d+)\]'
    for m in re.finditer(search_pattern, text):
        tags.append({
            "type": "search",
            "keyword": m.group(1).strip(),
            "limit": int(m.group(2))
        })
    cleaned = re.sub(search_pattern, '', cleaned)

    # [MUSIC_SEARCH:关键词] (无数量限制)
    search_pattern2 = r'\[MUSIC_SEARCH:\s*([^\]]+)\]'
    for m in re.finditer(search_pattern2, text):
        # 避免重复匹配上面的带数量的模式
        already = [t for t in tags if t["type"] == "search" and t["keyword"] == m.group(1).strip()]
        if not already:
            tags.append({
                "type": "search",
                "keyword": m.group(1).strip(),
                "limit": 10
            })
    cleaned = re.sub(search_pattern2, '', cleaned)

    # [MUSIC_PLAY:歌曲ID]
    play_pattern = r'\[MUSIC_PLAY:\s*(\d+)\]'
    for m in re.finditer(play_pattern, text):
        tags.append({"type": "play", "song_id": int(m.group(1))})
    cleaned = re.sub(play_pattern, '', cleaned)

    # [MUSIC_PAUSE]
    if re.search(r'\[MUSIC_PAUSE\]', cleaned):
        tags.append({"type": "pause"})
        cleaned = re.sub(r'\[MUSIC_PAUSE\]', '', cleaned)

    # [MUSIC_RESUME]
    if re.search(r'\[MUSIC_RESUME\]', cleaned):
        tags.append({"type": "resume"})
        cleaned = re.sub(r'\[MUSIC_RESUME\]', '', cleaned)

    # [MUSIC_STOP]
    if re.search(r'\[MUSIC_STOP\]', cleaned):
        tags.append({"type": "stop"})
        cleaned = re.sub(r'\[MUSIC_STOP\]', '', cleaned)

    # [MUSIC_NEXT]
    if re.search(r'\[MUSIC_NEXT\]', cleaned):
        tags.append({"type": "next"})
        cleaned = re.sub(r'\[MUSIC_NEXT\]', '', cleaned)

    # [MUSIC_PREV]
    if re.search(r'\[MUSIC_PREV\]', cleaned):
        tags.append({"type": "prev"})
        cleaned = re.sub(r'\[MUSIC_PREV\]', '', cleaned)

    # [MUSIC_PLAYLIST_LIST]
    if re.search(r'\[MUSIC_PLAYLIST_LIST\]', cleaned):
        tags.append({"type": "playlist_list"})
        cleaned = re.sub(r'\[MUSIC_PLAYLIST_LIST\]', '', cleaned)

    # [MUSIC_PLAYLIST_VIEW:歌单ID]
    view_pattern = r'\[MUSIC_PLAYLIST_VIEW:\s*(\S+)\]'
    for m in re.finditer(view_pattern, text):
        tags.append({"type": "playlist_view", "playlist_id": m.group(1).strip()})
    cleaned = re.sub(view_pattern, '', cleaned)

    # [MUSIC_PLAYLIST_CREATE:名称]
    create_pattern = r'\[MUSIC_PLAYLIST_CREATE:\s*([^\]]+)\]'
    for m in re.finditer(create_pattern, text):
        tags.append({"type": "playlist_create", "name": m.group(1).strip()})
    cleaned = re.sub(create_pattern, '', cleaned)

    # [MUSIC_PLAYLIST_ADD:歌单ID:歌曲ID]
    add_pattern = r'\[MUSIC_PLAYLIST_ADD:\s*(\S+):\s*(\d+)\]'
    for m in re.finditer(add_pattern, text):
        tags.append({"type": "playlist_add", "playlist_id": m.group(1).strip(), "song_id": int(m.group(2))})
    cleaned = re.sub(add_pattern, '', cleaned)

    # [MUSIC_PLAYLIST_DELETE:歌单ID]
    del_pattern = r'\[MUSIC_PLAYLIST_DELETE:\s*(\S+)\]'
    for m in re.finditer(del_pattern, text):
        tags.append({"type": "playlist_delete", "playlist_id": m.group(1).strip()})
    cleaned = re.sub(del_pattern, '', cleaned)

    # 清理多余空行
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    return cleaned, tags
