import re
import json
import os
import sqlite3
from datetime import datetime
from core.utils import auto_toggle_chat_mode_on_move

def process_agent_actions(char_id, raw_text, user_id=None):
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
    [MOOD: 预设情绪值]  (仅限: 平静 开心 悲伤 愤怒 兴奋 害羞 温柔 冷淡)
    [DIRECT_TO_GROUP: 角色1, 角色2, +user]  /  [DIRECT_TO_GROUP: 群名 | 角色1, 角色2]
    [DIRECT_TO_USER]
    [DIRECT_END]
    [NONE]

    Returns: (cleaned_text, affinity_delta, directive)
      affinity_delta 为亲密度净变动，无标签时为 None
      directive 为转向指令，无标签时为 None
      directive = {"type": "user"} 或 {"type": "group", "member_ids": [...], ...}
    """
    if not raw_text:
        return raw_text, None, None

    cleaned_text = raw_text
    total_affinity_delta = 0.0
    has_affinity = False
    directive = None

    # 1. 提取情绪指数 (Emotion)
    emotion_pattern = r'\[SET_EMOTION:\s*(\d+(?:\.\d+)?)\]'
    for match in re.finditer(emotion_pattern, raw_text):
        try:
            emotion_val = float(match.group(1))
            _update_persona_param(char_id, "emotion", emotion_val, user_id=user_id)
            print(f"[Agent Action] {char_id} 情绪指数设置为 {emotion_val}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Emotion 失败: {e}")
    cleaned_text = re.sub(emotion_pattern, '', cleaned_text)

    # 2. 提取性格指数 (Personality)
    personality_pattern = r'\[SET_PERSONALITY:\s*(\d+(?:\.\d+)?)\]'
    for match in re.finditer(personality_pattern, raw_text):
        try:
            personality_val = float(match.group(1))
            _update_persona_param(char_id, "personality", personality_val, user_id=user_id)
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
                _update_relationship(char_id, target, value)
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
            if date_str and content:
                _add_schedule(char_id, date_str, content)
                print(f"[Agent Action] {char_id} 增加日程 {date_str}: {content}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Schedule 失败: {e}")
    cleaned_text = re.sub(schedule_pattern, '', cleaned_text)

    # 7. 提取聊天模式切换 (Chat Mode)
    chat_mode_pattern = r'\[SET_CHAT_MODE:\s*(online|offline)\]'
    for match in re.finditer(chat_mode_pattern, raw_text, re.IGNORECASE):
        try:
            mode = match.group(1).lower()
            _update_chat_mode(char_id, mode)
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
                _update_mood(char_id, mood_val)
                print(f"[Agent Action] {char_id} 情绪标签更新为 {mood_val}")
            else:
                print(f"[Agent Action] {char_id} MOOD 值 '{mood_val}' 不在预设列表中，已忽略（允许值: {', '.join(sorted(VALID_MOODS))}）")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Mood 失败: {e}")
    cleaned_text = re.sub(mood_pattern, '', cleaned_text)

    # 9. 提取对话转向指令 (Direct To Group)
    direct_pattern = r'\[DIRECT_TO_GROUP:\s*(.+?)\]'
    for match in re.finditer(direct_pattern, raw_text):
        try:
            d_content = match.group(1).strip()
            include_user = False
            custom_name = None

            # 检查尾部 +user 标记
            if d_content.lower().endswith('+user'):
                include_user = True
                d_content = d_content[:d_content.lower().rfind('+user')].strip().rstrip(',').strip()

            # 检查自定义群名: 群名 | member1, member2
            if '|' in d_content:
                parts = d_content.split('|', 1)
                custom_name = parts[0].strip()
                member_part = parts[1].strip()
            else:
                member_part = d_content

            # 解析成员列表
            member_names = [m.strip() for m in member_part.split(',') if m.strip()]
            member_ids = [_resolve_char_id(m) for m in member_names]
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
    cleaned_text = re.sub(direct_pattern, '', cleaned_text)

    # 10. 提取切换到单聊指令 (Direct To User)
    user_pattern = r'\[DIRECT_TO_USER\]'
    if re.search(user_pattern, raw_text, re.IGNORECASE):
        directive = {"type": "user"}
        print(f"[Agent Action] {char_id} 发起切换到单聊指令")
    cleaned_text = re.sub(user_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 11. 清理结束对话标签 (Direct End)
    end_pattern = r'\[DIRECT_END\]'
    cleaned_text = re.sub(end_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 12. 提取无操作标签 (None / 无需改动的占位标签)
    none_pattern = r'\[NONE\]'
    if re.search(none_pattern, cleaned_text, re.IGNORECASE):
        print(f"[Agent Action] {char_id} 无操作标签 (NONE)，跳过所有动作")
    cleaned_text = re.sub(none_pattern, '', cleaned_text, flags=re.IGNORECASE)

    # 13. 提取位置移动标签 (MOVE_TO / MOVE_TO_COORD / EXPLORE)
    location_result = _process_location_tags(char_id, raw_text, user_id)
    for tag_pattern in [
        r'\[MOVE_TO:\s*[^\]]+\]',
        r'\[MOVE_TO_COORD:\s*[\d.\-]+,\s*[\d.\-]+\s*\]',
        r'\[EXPLORE:\s*[\d.\-]+,\s*[\d.\-]+,\s*"[^"]*",\s*"[^"]*"\s*\]',
    ]:
        cleaned_text = re.sub(tag_pattern, '', cleaned_text)

    # 清理多余的空白字符，如果标签单独占一行，删除后可能会留下空行
    # 13.5 兜底清理任何残留、畸形或大写下划线的 Agent 动作指令标签，例如 [UPDATE_AFFINITY:user,-1] 或 [SET_EMOTION:arrogant] 等
    # 只要包含了预定义的命令关键字或大写加下划线标签，均彻底清除。
    robust_agent_pattern = r'[\[【]\s*(?:UPDATE_AFFINITY|SET_EMOTION|SET_PERSONALITY|SET_SLEEP_TIME|SET_RELATION|ADD_SCHEDULE|MOOD|DIRECT_TO_GROUP|DIRECT_TO_USER|DIRECT_END|NONE|MOVE_TO|MOVE_TO_COORD|EXPLORE|SET_CHAT_MODE|MUSIC_[A-Z0-9_]+)(?::|：)?\s*.*?[\]】]'
    cleaned_text = re.sub(robust_agent_pattern, '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)

    # 额外兜底清理所有全大写加下划线的指令标签，避免未定义或畸形的指令流出（不清理 SEARCH_IMG 和 GENERATE_IMAGE 标签，交由媒体解析器专门处理；THOUGHTS 为内心独白展示标签，需保留）
    general_pattern = r'[\[【]\s*(?!(?:SEARCH_IMG|GENERATE_IMAGE|CLICK|TYPE|GOTO|BACK|FINISH|ASK|WAIT|WEB_CRUISE|STOP|REPLY|THOUGHTS)\b)(?:[A-Z_][A-Z0-9_]*)(?::|：)?\s*.*?[\]】]'
    cleaned_text = re.sub(general_pattern, '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)

    cleaned_text = re.sub(r'\n\s*\n', '\n', cleaned_text).strip()
    return cleaned_text, (round(total_affinity_delta, 2) if has_affinity else None), directive

def _update_persona_param(char_id, param_name, value, user_id=None):
    """更新 characters.json 中的行为参数（emotion / moments_index）"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file(user_id=user_id)
        if not os.path.exists(cfg_file):
            print(f"[Agent Action] WARNING: config file not found: {cfg_file}")
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if char_id not in data:
            print(f"[Agent Action] WARNING: char_id '{char_id}' not found in {cfg_file}, keys: {list(data.keys())[:5]}")
            return

        if param_name == "emotion":
            data[char_id]["emotion"] = float(value)
            print(f"[Agent Action] {char_id} emotion -> {value} written to {cfg_file}")
        elif param_name == "personality":
            data[char_id]["moments_index"] = float(value)
            print(f"[Agent Action] {char_id} moments_index -> {value} written to {cfg_file}")
        safe_save_json(cfg_file, data)
    except Exception as e:
        print(f"[Agent Action Error] Update Persona Param: {e}")

def _update_user_affinity(char_id, delta, current_user_id=None):
    """累加亲密度到 characters.json 中"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file()
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
    """更新睡眠时间段 — 仅写入 ds_start / ds_end，不动 deep_sleep 开关"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file()
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
            data[char_id]["ds_start"] = parts[0].strip()
            data[char_id]["ds_end"] = parts[1].strip()
            safe_save_json(cfg_file, data)
            print(f"[Agent Action] {char_id} deep_sleep {parts[0].strip()}-{parts[1].strip()} written to {cfg_file}")
        else:
            print(f"[Agent Action] WARNING: invalid sleep_range format: {sleep_range}")
    except Exception as e:
        print(f"[Agent Action Error] Update Sleep Time: {e}")

def _update_relationship(char_id, target, value):
    """更新 2_relationship.json"""
    from app import get_paths, safe_save_json
    _, prompts_dir = get_paths(char_id)
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

def _add_schedule(char_id, date_str, content):
    """追加日程到 7_schedule.json"""
    from app import get_paths, safe_save_json
    _, prompts_dir = get_paths(char_id)
    sched_path = os.path.join(prompts_dir, "7_schedule.json")
    if os.path.exists(sched_path):
        try:
            with open(sched_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 追加逻辑：如果不存该日期，新建；如果存在，用分号隔开拼接
            if date_str in data:
                data[date_str] = f"{data[date_str]}; {content}"
            else:
                data[date_str] = content

            safe_save_json(sched_path, data)
        except Exception as e:
            print(f"Add Schedule Error: {e}")

def _update_chat_mode(char_id, mode):
    """更新角色的聊天模式 (online/offline) 到 characters.json"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file()
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

def _resolve_char_id(target):
    """
    将名字/ID/备注 解析为 char_id。
    优先精确匹配ID，然后匹配name，最后匹配remark。
    """
    if not target:
        return None
    target_lower = target.strip().lower()
    try:
        from app import _get_characters_config_file
        cfg_file = _get_characters_config_file()
        if not os.path.exists(cfg_file):
            return None
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1. 精确匹配 ID (不区分大小写)
        for cid in data:
            if cid.lower() == target_lower:
                return cid

        # 2. 匹配 name
        for cid, cinfo in data.items():
            if cinfo.get("name", "").strip() == target.strip():
                return cid

        # 3. 匹配 remark
        for cid, cinfo in data.items():
            if cinfo.get("remark", "").strip() == target.strip():
                return cid

        print(f"[Agent Action] WARNING: 无法将 '{target}' 解析为角色ID")
        return None
    except Exception as e:
        print(f"[Agent Action Error] _resolve_char_id: {e}")
        return None

def _update_mood(char_id, mood):
    """更新 preset mood（voice_emotion）到 characters.json"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file()
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
    import math
    from app import (load_character_positions, save_character_positions,
                     load_locations, save_locations, safe_save_json,
                     calc_distance, get_location_by_id, get_location_at_coord,
                     check_co_encounters,
                     get_char_name, _get_characters_config_file)

    positions = load_character_positions()
    if char_id not in positions:
        char_cfg = _get_characters_config_file(user_id=user_id)
        if os.path.exists(char_cfg):
            try:
                with open(char_cfg, "r", encoding="utf-8") as f:
                    chars = json.load(f)
                if char_id in chars:
                    positions[char_id] = {"location_id": "home", "x": 0.0, "y": 0.0, "known_location_ids": ["home"]}
                    save_character_positions(positions)
            except:
                return
        else:
            return

    pos = positions[char_id]
    old_x, old_y = pos["x"], pos["y"]
    old_location_id = pos.get("location_id")
    moved = False
    target_x, target_y = old_x, old_y
    action_desc = None

    # 1. [MOVE_TO: location_id]
    move_to_pattern = r'\[MOVE_TO:\s*([^\]]+?)\]'
    for match in re.finditer(move_to_pattern, raw_text):
        try:
            loc_id = match.group(1).strip()
            loc = get_location_by_id(loc_id)
            if not loc:
                print(f"[Agent Action] {char_id} MOVE_TO '{loc_id}' location not found")
                continue
            target_x, target_y = loc["x"], loc["y"]
            d = calc_distance(old_x, old_y, target_x, target_y)
            if d > 1.0:
                print(f"[Agent Action] {char_id} MOVE_TO '{loc_id}' distance {round(d,2)} > 1, too far")
                continue
            pos["x"] = target_x
            pos["y"] = target_y
            pos["location_id"] = loc_id
            action_desc = f"{get_char_name(char_id)}移动到了{loc.get('name', loc_id)}"
            moved = True
            print(f"[Agent Action] {char_id} MOVE_TO -> {loc_id} ({target_x}, {target_y})")
        except Exception as e:
            print(f"[Agent Action Error] MOVE_TO: {e}")

    # 2. [MOVE_TO_COORD: x, y]
    move_coord_pattern = r'\[MOVE_TO_COORD:\s*([\d.\-]+),\s*([\d.\-]+)\s*\]'
    for match in re.finditer(move_coord_pattern, raw_text):
        try:
            target_x = float(match.group(1))
            target_y = float(match.group(2))
            d = calc_distance(old_x, old_y, target_x, target_y)
            if d > 1.0:
                print(f"[Agent Action] {char_id} MOVE_TO_COORD ({target_x},{target_y}) distance {round(d,2)} > 1, too far")
                continue
            pos["x"] = target_x
            pos["y"] = target_y
            loc_at = get_location_at_coord(target_x, target_y)
            pos["location_id"] = loc_at["id"] if loc_at else None
            loc_name = loc_at.get("name", f"({round(target_x,2)}, {round(target_y,2)})") if loc_at else f"({round(target_x,2)}, {round(target_y,2)})"
            action_desc = f"{get_char_name(char_id)}移动到了{loc_name}"
            moved = True
            print(f"[Agent Action] {char_id} MOVE_TO_COORD -> ({target_x}, {target_y})")
        except Exception as e:
            print(f"[Agent Action Error] MOVE_TO_COORD: {e}")

    # 3. [EXPLORE: x, y, "name", "description"]
    explore_pattern = r'\[EXPLORE:\s*([\d.\-]+),\s*([\d.\-]+),\s*"([^"]*)",\s*"([^"]*)"\s*\]'
    for match in re.finditer(explore_pattern, raw_text):
        try:
            target_x = float(match.group(1))
            target_y = float(match.group(2))
            loc_name = match.group(3).strip()
            loc_desc = match.group(4).strip()
            d = calc_distance(old_x, old_y, target_x, target_y)
            if d > 1.0:
                print(f"[Agent Action] {char_id} EXPLORE ({target_x},{target_y}) distance {round(d,2)} > 1, too far")
                continue
            locs = load_locations()
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
                "is_default": False
            }
            locs["locations"].append(new_loc)
            save_locations(locs)
            pos["x"] = target_x
            pos["y"] = target_y
            pos["location_id"] = loc_id
            known = pos.get("known_location_ids", [])
            if loc_id not in known:
                known.append(loc_id)
                pos["known_location_ids"] = known
            action_desc = f"{get_char_name(char_id)}探索到了{loc_name}并移动到此处"
            moved = True
            print(f"[Agent Action] {char_id} EXPLORE -> {loc_name} ({target_x}, {target_y})")
        except Exception as e:
            print(f"[Agent Action Error] EXPLORE: {e}")

    if moved:
        # 到达的地点加入认知
        cur_loc_id = pos.get("location_id")
        if cur_loc_id:
            known = pos.get("known_location_ids", [])
            if cur_loc_id not in known:
                known.append(cur_loc_id)
                pos["known_location_ids"] = known
        save_character_positions(positions)
        auto_toggle_chat_mode_on_move(char_id=char_id, old_location_id=old_location_id, new_location_id=cur_loc_id, user_id=user_id)

        loc_id = pos.get("location_id")
        encounters = check_co_encounters(char_id, pos["x"], pos["y"], loc_id)
        encounter_msgs = []
        if encounters:
            for eid in encounters:
                if eid == "user":
                    encounter_msgs.append("与用户相遇")
                else:
                    ename = get_char_name(eid)
                    encounter_msgs.append(f"与{ename}相遇")
        if encounter_msgs:
            action_desc = (action_desc or "") + "，" + "；".join(encounter_msgs)

        return {"char_id": char_id, "description": action_desc, "x": target_x, "y": target_y}
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
