import re
import json
import os
import sqlite3
from datetime import datetime

def process_agent_actions(char_id, raw_text, user_id=None):
    """
    解析并执行 AI 返回文本中的动作标签 (Action Tags)。
    执行后从文本中移除这些标签，并返回纯净文本。
    支持的标签:
    [SET_EMOTION: 数字]
    [SET_PERSONALITY: 数字]
    [UPDATE_AFFINITY: +/-数字]
    [SET_SLEEP_TIME: "HH:MM-HH:MM"]
    [SET_RELATION: {"target": "ID或名称", "value": 0-5}]
    [ADD_SCHEDULE: {"date": "YYYY-MM-DD", "content": "内容"}]
    
    Returns: (cleaned_text, affinity_delta) 其中 affinity_delta 为亲密度净变动，无标签时为 None
    """
    if not raw_text:
        return raw_text, None

    cleaned_text = raw_text
    total_affinity_delta = 0.0
    has_affinity = False

    # 1. 提取情绪指数 (Emotion)
    emotion_pattern = r'\[SET_EMOTION:\s*(\d+(?:\.\d+)?)\]'
    for match in re.finditer(emotion_pattern, raw_text):
        try:
            emotion_val = float(match.group(1))
            _update_persona_param(char_id, "emotion", emotion_val)
            print(f"[Agent Action] {char_id} 情绪指数设置为 {emotion_val}")
        except Exception as e:
            print(f"[Agent Action Error] 解析 Emotion 失败: {e}")
    cleaned_text = re.sub(emotion_pattern, '', cleaned_text)

    # 2. 提取性格指数 (Personality)
    personality_pattern = r'\[SET_PERSONALITY:\s*(\d+(?:\.\d+)?)\]'
    for match in re.finditer(personality_pattern, raw_text):
        try:
            personality_val = float(match.group(1))
            _update_persona_param(char_id, "personality", personality_val)
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

    # 清理多余的空白字符，如果标签单独占一行，删除后可能会留下空行
    cleaned_text = re.sub(r'\n\s*\n', '\n', cleaned_text).strip()
    return cleaned_text, (round(total_affinity_delta, 2) if has_affinity else None)

def _update_persona_param(char_id, param_name, value):
    """更新 characters.json 中的行为参数（emotion / moments_index）"""
    try:
        from app import _get_characters_config_file, safe_save_json
        cfg_file = _get_characters_config_file()
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
    """更新睡眠时间段 — 写入 ds_start / ds_end / deep_sleep"""
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
            data[char_id]["deep_sleep"] = True
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
