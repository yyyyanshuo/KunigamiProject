import os
import re
import json
import shutil
import tempfile
import pykakasi

from core.config import (
    BASE_DIR, USERS_ROOT, CHARACTERS_DIR, CONFIG_FILE, GROUPS_CONFIG_FILE,
    GROUPS_DIR, USER_SETTINGS_FILE, DEVICE_ACCOUNTS_FILE, QUICK_PHRASES_FILE,
    READ_STATUS_FILE, GEMINI_KEY, OPENROUTER_KEY,
)
from core.context import get_current_user_id


# --- kakasi 初始化 (日语注音用) ---
kks = pykakasi.kakasi()

EMOJI_SPLIT_RE = re.compile(
    r'('
    r'[\U0001F1E6-\U0001F1FF]'
    r'|[\U0001F300-\U0001FAFF]'
    r'|[\u2600-\u26FF]'
    r'|[\u2700-\u27BF]'
    r'|[\uFE0F]'
    r')+'
)


def _add_furigana_to_japanese(text: str) -> str:
    if not text:
        return text
    pattern = r'(\[表情\][^\s/]+|\[图片\]\([^)]+\)\([\s\S]*?\)|\[recall\])'
    parts = re.split(pattern, text)

    out = ""
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out += part
        else:
            emoji_map = {}

            def replace_emoji(match):
                emoji_key = f"__EMOJI_{len(emoji_map)}__"
                emoji_map[emoji_key] = match.group(0)
                return emoji_key

            part_with_placeholders = re.sub(EMOJI_SPLIT_RE, replace_emoji, part)

            line_parts = re.split(r'(\r\n|\n|\r)', part_with_placeholders)
            for line_part in line_parts:
                if not line_part or line_part in ("\r\n", "\n", "\r"):
                    out += line_part
                    continue

                result = kks.convert(line_part)

                joined_orig = "".join((it.get("orig") or "") for it in result)
                if joined_orig != line_part:
                    out += line_part
                    continue

                for item in result:
                    orig = item["orig"]
                    hira = item["hira"]

                    if not re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', orig):
                        out += orig
                        continue

                    suf = ""
                    while orig and hira and orig[-1] == hira[-1]:
                        suf = orig[-1] + suf
                        orig = orig[:-1]
                        hira = hira[:-1]

                    pre = ""
                    while orig and hira and orig[0] == hira[0]:
                        pre += orig[0]
                        orig = orig[1:]
                        hira = hira[1:]

                    has_kanji_reading = re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', hira)
                    if orig and hira and orig != hira and not has_kanji_reading:
                        out += f"{pre}<ruby>{orig}<rt>{hira}</rt></ruby>{suf}"
                    else:
                        out += pre + orig + suf

            for emoji_key, emoji_char in emoji_map.items():
                out = out.replace(emoji_key, emoji_char)

    return out


# ==================== 路径解析函数 ====================

def _get_characters_config_file(user_id=None) -> str:
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    if user_id:
        cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "characters.json")
    return CONFIG_FILE


def _get_groups_config_file(user_id=None) -> str:
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    if user_id:
        cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "groups.json")
    return GROUPS_CONFIG_FILE


def get_all_char_ids_for_current_user() -> list:
    d = get_characters_config_for_current_user()
    return list(d.keys())


def get_characters_config_for_current_user() -> dict:
    cfg = _get_characters_config_file()
    if not os.path.exists(cfg):
        return {}
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_groups_config_for_current_user() -> dict:
    cfg = _get_groups_config_file()
    if not os.path.exists(cfg):
        return {}
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_all_group_ids_for_current_user() -> list:
    cfg = _get_groups_config_file()
    if not os.path.exists(cfg):
        return []
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return list(json.load(f).keys())
    except Exception:
        return []


def _get_locations_file() -> str:
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "locations.json")
    return os.path.join(BASE_DIR, "configs", "locations.json")


def _get_character_positions_file() -> str:
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "character_positions.json")
    return os.path.join(BASE_DIR, "configs", "character_positions.json")


def _get_user_position_file() -> str:
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "user_position.json")
    return os.path.join(BASE_DIR, "configs", "user_position.json")


def _get_read_status_file() -> str:
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "read_status.json")
    return READ_STATUS_FILE


def _get_quick_phrases_file() -> str:
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "quick_phrases.json")
    return QUICK_PHRASES_FILE


def _get_user_settings_file() -> str:
    uid = get_current_user_id()
    if uid:
        base = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "user_settings.json")
    return USER_SETTINGS_FILE


def _load_user_settings() -> dict:
    path = _get_user_settings_file()
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    return data


def _save_user_settings(data: dict):
    path = _get_user_settings_file()
    safe_save_json(path, data)


def safe_save_json(filepath, data):
    dir_name = os.path.dirname(filepath)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, filepath)
    except Exception as e:
        print(f"Save JSON Error: {e}")
        os.remove(temp_path)


def get_current_username():
    default_name = "User"
    data = _load_user_settings()
    return data.get("current_user_name", default_name)


def get_effective_gemini_key(user_id=None):
    data = _load_user_settings()
    if user_id:
        path = os.path.join(USERS_ROOT, str(user_id), "configs", "user_settings.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
    return data.get("gemini_api_key") or GEMINI_KEY


def get_effective_openrouter_key(user_id=None):
    data = _load_user_settings()
    if user_id:
        path = os.path.join(USERS_ROOT, str(user_id), "configs", "user_settings.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
    return data.get("openrouter_api_key") or OPENROUTER_KEY


def get_paths(char_id, user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()

    if user_id:
        user_char_root = os.path.join(USERS_ROOT, str(user_id), "characters")
        template_dir = os.path.join(CHARACTERS_DIR, char_id)
        char_dir = os.path.join(user_char_root, char_id)

        if not os.path.exists(char_dir) and os.path.exists(template_dir):
            os.makedirs(char_dir, exist_ok=True)
            try:
                for name in os.listdir(template_dir):
                    if name == "chat.db":
                        continue
                    src = os.path.join(template_dir, name)
                    dst = os.path.join(char_dir, name)
                    if os.path.isdir(src):
                        if not os.path.exists(dst):
                            shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
            except Exception as e:
                print(f"[Users] 拷贝角色模板失败 {char_id}: {e}")
    else:
        char_dir = os.path.join(CHARACTERS_DIR, char_id)

    db_path = os.path.join(char_dir, "chat.db")
    prompts_dir = os.path.join(char_dir, "prompts")
    return db_path, prompts_dir


# ==================== 地图数据辅助函数 ====================
import math


def init_map_data():
    locations_file = _get_locations_file()
    if not os.path.exists(locations_file):
        default = {
            "locations": [
                {
                    "id": "home",
                    "name": "家",
                    "description": "温馨的小窝，一切开始的地方",
                    "x": 0.0,
                    "y": 0.0,
                    "r": 0.0,
                    "theta": 0.0,
                    "is_default": True,
                    "real_world": None
                }
            ]
        }
        safe_save_json(locations_file, default)

    pos_file = _get_character_positions_file()
    if not os.path.exists(pos_file):
        chars_cfg = _get_characters_config_file()
        char_positions = {}
        if os.path.exists(chars_cfg):
            try:
                with open(chars_cfg, "r", encoding="utf-8") as f:
                    chars_data = json.load(f)
                for cid in chars_data:
                    char_positions[cid] = {
                        "location_id": "home",
                        "x": 0.0,
                        "y": 0.0,
                        "known_location_ids": ["home"]
                    }
            except:
                pass
        safe_save_json(pos_file, char_positions)

    user_pos_file = _get_user_position_file()
    if not os.path.exists(user_pos_file):
        default_user_pos = {
            "x": 0.0,
            "y": 0.0,
            "location_id": "home"
        }
        safe_save_json(user_pos_file, default_user_pos)


def load_locations():
    f = _get_locations_file()
    if not os.path.exists(f):
        init_map_data()
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return {"locations": []}


def save_locations(data):
    safe_save_json(_get_locations_file(), data)


def load_character_positions():
    f = _get_character_positions_file()
    if not os.path.exists(f):
        init_map_data()
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return {}


def save_character_positions(data):
    safe_save_json(_get_character_positions_file(), data)


def load_user_position():
    f = _get_user_position_file()
    if not os.path.exists(f):
        init_map_data()
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return {"x": 0.0, "y": 0.0, "location_id": "home"}


def save_user_position(data):
    safe_save_json(_get_user_position_file(), data)


def calc_distance(x1, y1, x2, y2):
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def get_location_by_id(location_id):
    locs = load_locations()
    for loc in locs.get("locations", []):
        if loc["id"] == location_id:
            return loc
    return None


def get_location_at_coord(x, y):
    locs = load_locations()
    best = None
    best_dist = float("inf")
    for loc in locs.get("locations", []):
        d = calc_distance(x, y, loc["x"], loc["y"])
        if d < best_dist:
            best_dist = d
            best = loc
    if best and best_dist < 0.1:
        return best
    return None


def auto_toggle_chat_mode_on_move(char_id=None, old_location_id=None, new_location_id=None, user_id=None):
    """
    角色/用户移动时，根据与用户是否同处一个地点，自动切换角色的 chat_mode。

    角色移动时 (char_id 不为 None):
      - old_location_id == 用户所在位置 → 角色离开用户 → online
      - new_location_id == 用户所在位置 → 角色到达用户 → offline

    用户移动时 (char_id 为 None):
      - 遍历所有角色, location_id == old_location_id → online
      - 遍历所有角色, location_id == new_location_id → offline
    """
    if not old_location_id and not new_location_id:
        return
    if old_location_id == new_location_id:
        return

    user_pos = load_user_position()
    user_loc = user_pos.get("location_id")

    cfg_file = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(cfg_file):
        return

    with open(cfg_file, "r", encoding="utf-8") as f:
        chars_data = json.load(f)

    modified = False

    if char_id:
        if char_id not in chars_data:
            return
        current_mode = chars_data[char_id].get("chat_mode")
        if old_location_id and old_location_id == user_loc and current_mode != "online":
            chars_data[char_id]["chat_mode"] = "online"
            modified = True
            print(f"[Auto ChatMode] {char_id} left user's location -> online")
        if new_location_id and new_location_id == user_loc and current_mode != "offline":
            chars_data[char_id]["chat_mode"] = "offline"
            modified = True
            print(f"[Auto ChatMode] {char_id} arrived at user's location -> offline")
    else:
        positions = load_character_positions()
        for cid, pos in positions.items():
            if cid not in chars_data:
                continue
            loc = pos.get("location_id")
            if old_location_id and loc == old_location_id:
                if chars_data[cid].get("chat_mode") != "online":
                    chars_data[cid]["chat_mode"] = "online"
                    modified = True
                    print(f"[Auto ChatMode] User left, {cid} -> online")
            if new_location_id and loc == new_location_id:
                if chars_data[cid].get("chat_mode") != "offline":
                    chars_data[cid]["chat_mode"] = "offline"
                    modified = True
                    print(f"[Auto ChatMode] User arrived, {cid} -> offline")

    if modified:
        safe_save_json(cfg_file, chars_data)


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
