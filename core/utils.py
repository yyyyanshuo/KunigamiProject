import os
import re
import json
import shutil
import sqlite3
import tempfile
import pykakasi

from core.config import (
    BASE_DIR, USERS_ROOT, CHARACTERS_DIR, CONFIG_FILE, GROUPS_CONFIG_FILE,
    GROUPS_DIR, USER_SETTINGS_FILE, DEVICE_ACCOUNTS_FILE, QUICK_PHRASES_FILE,
    READ_STATUS_FILE, GEMINI_KEY, OPENROUTER_KEY, USERS_DB,
)
from core.context import get_current_user_id
from core.credentials import get_user_credential
from core.time_utils import (
    ensure_character_time_defaults,
    get_character_timezone,
    is_valid_timezone,
    resolve_timezone_from_coordinates,
)


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


def is_character_available_for_chat(info) -> bool:
    """Return whether a character may participate in a one-to-one chat.

    Character chat_mode belongs to the character/user conversation, so it is
    intentionally not used for group-chat availability.
    """
    value = info if isinstance(info, dict) else {}
    if value.get("chat_mode", "online") == "offline":
        return True
    return not bool(value.get("deep_sleep", False))


def is_character_available_for_group_chat(info, group_chat_mode="online") -> bool:
    """Return availability using the group's own online/offline scene mode.

    Online groups respect each character's deep-sleep flag. In an offline
    (in-person) group scene, deep sleep does not prevent group participation.
    The character's individual chat_mode is deliberately ignored.
    """
    if str(group_chat_mode or "online").strip().lower() == "offline":
        return True
    value = info if isinstance(info, dict) else {}
    return not bool(value.get("deep_sleep", False))


def _add_furigana_to_japanese(text: str) -> str:
    if not text:
        return text
    # 系统提示是系统事件原文，不参与日语注音。
    from core.system_messages import is_system_prompt_message
    if is_system_prompt_message(text):
        return text
    # Lazy import avoids a core.utils -> services package initialization cycle.
    from services.image_tags import protect_image_tags
    text, image_tags = protect_image_tags(text, "__KUNIGAMI_IMAGE_TAG_")
    pattern = r'(\[表情\][^\s/]+|\[recall\])'
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
                    # 特殊符号可能使 kakasi 重复/丢失原文；仅重新转换日文片段，
                    # 其余字符原样保留，避免整行汉字都失去注音。
                    result = []
                    for chunk in re.split(r'([\u3400-\u4dbf\u4e00-\u9fff\u3041-\u3096\u30a1-\u30faー々]+)', line_part):
                        if not chunk:
                            continue
                        converted = kks.convert(chunk) if re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', chunk) else []
                        if converted and "".join(item.get("orig", "") for item in converted) == chunk:
                            result.extend(converted)
                        else:
                            result.append({"orig": chunk, "hira": chunk})

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

    for index, raw_tag in enumerate(image_tags):
        out = out.replace(f"__KUNIGAMI_IMAGE_TAG_{index}__", raw_tag)
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


def _get_locations_file(user_id=None) -> str:
    uid = user_id if user_id is not None else get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "locations.json")
    return os.path.join(BASE_DIR, "configs", "locations.json")


def _get_character_positions_file(user_id=None) -> str:
    uid = user_id if user_id is not None else get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "character_positions.json")
    return os.path.join(BASE_DIR, "configs", "character_positions.json")


def _get_user_position_file(user_id=None) -> str:
    uid = user_id if user_id is not None else get_current_user_id()
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
    data = _load_user_settings()
    configured = str(data.get("current_user_name") or "").strip()
    if configured:
        return configured
    uid = get_current_user_id()
    if uid:
        try:
            conn = sqlite3.connect(USERS_DB)
            try:
                row = conn.execute("SELECT display_name, email FROM users WHERE id = ?", (int(uid),)).fetchone()
            finally:
                conn.close()
            if row:
                return row[0] or row[1] or "User"
        except (sqlite3.Error, TypeError, ValueError):
            pass
    return "User"


def get_effective_gemini_key(user_id=None):
    uid = user_id if user_id is not None else get_current_user_id()
    if uid:
        user_key = get_user_credential(uid, "gemini", allow_legacy=True)
        if user_key:
            return user_key
    return GEMINI_KEY


def get_effective_openrouter_key(user_id=None):
    uid = user_id if user_id is not None else get_current_user_id()
    if uid:
        user_key = get_user_credential(uid, "openrouter", allow_legacy=True)
        if user_key:
            return user_key
    return OPENROUTER_KEY


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


def init_map_data(user_id=None):
    locations_file = _get_locations_file(user_id=user_id)
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

    pos_file = _get_character_positions_file(user_id=user_id)
    if not os.path.exists(pos_file):
        chars_cfg = _get_characters_config_file(user_id=user_id)
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

    user_pos_file = _get_user_position_file(user_id=user_id)
    if not os.path.exists(user_pos_file):
        default_user_pos = {
            "x": 0.0,
            "y": 0.0,
            "location_id": "home"
        }
        safe_save_json(user_pos_file, default_user_pos)


def load_locations(user_id=None):
    f = _get_locations_file(user_id=user_id)
    if not os.path.exists(f):
        init_map_data(user_id=user_id)
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return {"locations": []}


def save_locations(data, user_id=None):
    safe_save_json(_get_locations_file(user_id=user_id), data)


def normalize_real_world_timezone(real_world):
    """Validate or infer the IANA timezone for a real-world map location."""
    if not isinstance(real_world, dict):
        return real_world
    normalized = dict(real_world)
    explicit = normalized.get("timezone")
    if explicit:
        if not is_valid_timezone(explicit):
            raise ValueError("invalid timezone")
        normalized["timezone"] = explicit.strip()
        normalized.setdefault("timezone_source", "manual")
        return normalized

    lat, lon = normalized.get("lat"), normalized.get("lon")
    if lat is None or lon is None:
        return normalized
    try:
        inferred = resolve_timezone_from_coordinates(float(lat), float(lon))
    except (TypeError, ValueError):
        inferred = None
    if inferred:
        normalized["timezone"] = inferred
        normalized["timezone_source"] = "coordinates"
    return normalized


def sync_character_timezone_to_location(char_id, location, user_id=None):
    """Apply a mapped location timezone and return an audit description."""
    change = {"changed": False}
    real_world = location.get("real_world") if isinstance(location, dict) else None
    location_timezone = (
        real_world.get("timezone") if isinstance(real_world, dict) else None
    )
    if not is_valid_timezone(location_timezone):
        return change

    cfg_file = _get_characters_config_file(user_id=user_id)
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            characters = json.load(f) or {}
    except Exception:
        return change
    info = characters.get(char_id)
    if not isinstance(info, dict):
        return change

    ensure_character_time_defaults(info, existing_character=True)
    old_timezone = get_character_timezone(info)
    new_timezone = location_timezone.strip()
    metadata_changed = (
        info.get("timezone_source") != "location"
        or info.get("timezone_location_id") != location.get("id")
    )
    if old_timezone != new_timezone or metadata_changed:
        info["timezone"] = new_timezone
        info["timezone_source"] = "location"
        info["timezone_location_id"] = location.get("id")
        safe_save_json(cfg_file, characters)
    return {
        "changed": old_timezone != new_timezone,
        "from": old_timezone,
        "to": new_timezone,
        "old_timezone": old_timezone,
        "new_timezone": new_timezone,
        "reason": "location",
        "location_id": location.get("id"),
    }


def load_character_positions(user_id=None):
    f = _get_character_positions_file(user_id=user_id)
    if not os.path.exists(f):
        init_map_data(user_id=user_id)
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return {}


def save_character_positions(data, user_id=None):
    safe_save_json(_get_character_positions_file(user_id=user_id), data)


def load_user_position(user_id=None):
    f = _get_user_position_file(user_id=user_id)
    if not os.path.exists(f):
        init_map_data(user_id=user_id)
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return {"x": 0.0, "y": 0.0, "location_id": "home"}


def save_user_position(data, user_id=None):
    safe_save_json(_get_user_position_file(user_id=user_id), data)


def calc_distance(x1, y1, x2, y2):
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def get_location_by_id(location_id, user_id=None):
    locs = load_locations(user_id=user_id)
    for loc in locs.get("locations", []):
        if loc["id"] == location_id:
            return loc
    return None


def get_location_at_coord(x, y, user_id=None):
    locs = load_locations(user_id=user_id)
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


def coordinates_equal(x1, y1, x2, y2, tolerance=1e-9):
    """Return whether two stored map coordinates represent the same point."""
    try:
        return (
            abs(float(x1) - float(x2)) <= tolerance
            and abs(float(y1) - float(y2)) <= tolerance
        )
    except (TypeError, ValueError):
        return False


def get_location_at_exact_coord(x, y, user_id=None, exclude_id=None):
    """Find a location at the same coordinate, without nearby-place matching."""
    locs = load_locations(user_id=user_id)
    for loc in locs.get("locations", []):
        if exclude_id is not None and loc.get("id") == exclude_id:
            continue
        if coordinates_equal(x, y, loc.get("x"), loc.get("y")):
            return loc
    return None


def normalize_map_state(user_id=None, drop_orphan_positions=True):
    """Repair map references and ensure every configured character is present."""
    locations = load_locations(user_id=user_id)
    locs_by_id = {
        str(loc.get("id")): loc
        for loc in locations.get("locations", [])
        if loc.get("id") is not None
    }
    positions = load_character_positions(user_id=user_id)
    cfg_file = _get_characters_config_file(user_id=user_id)
    valid_char_ids = None
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                valid_char_ids = set((json.load(f) or {}).keys())
        except Exception:
            valid_char_ids = None

    changed = False
    # The positions file outlives the initial map setup. Characters created or
    # imported afterwards therefore need to be added here so map consumers can
    # render and select them immediately.
    if valid_char_ids is not None:
        home = locs_by_id.get("home")
        initial_x = float(home.get("x", 0.0)) if home else 0.0
        initial_y = float(home.get("y", 0.0)) if home else 0.0
        for cid in valid_char_ids:
            if cid not in positions:
                positions[cid] = {
                    "location_id": "home" if home else None,
                    "x": initial_x,
                    "y": initial_y,
                    "known_location_ids": ["home"] if home else [],
                }
                changed = True

    for cid in list(positions.keys()):
        if drop_orphan_positions and valid_char_ids is not None and cid not in valid_char_ids:
            del positions[cid]
            changed = True
            continue

        pos = positions.get(cid)
        if not isinstance(pos, dict):
            positions[cid] = {
                "location_id": "home" if "home" in locs_by_id else None,
                "x": float(locs_by_id.get("home", {}).get("x", 0.0)),
                "y": float(locs_by_id.get("home", {}).get("y", 0.0)),
                "known_location_ids": ["home"] if "home" in locs_by_id else [],
            }
            changed = True
            continue

        try:
            x = float(pos.get("x", 0.0))
            y = float(pos.get("y", 0.0))
        except (TypeError, ValueError):
            x, y = 0.0, 0.0
            changed = True

        loc_id = pos.get("location_id")
        if loc_id in locs_by_id:
            loc = locs_by_id[loc_id]
            canonical_x, canonical_y = float(loc["x"]), float(loc["y"])
            if x != canonical_x or y != canonical_y:
                x, y = canonical_x, canonical_y
                changed = True
        else:
            if loc_id is not None:
                loc_id = None
                changed = True
            matched = None
            for candidate in locs_by_id.values():
                if calc_distance(x, y, float(candidate["x"]), float(candidate["y"])) < 0.1:
                    matched = candidate
                    break
            if matched is not None:
                loc_id = matched["id"]
                x, y = float(matched["x"]), float(matched["y"])
                changed = True

        known = [
            lid for lid in (pos.get("known_location_ids") or [])
            if lid in locs_by_id
        ]
        if loc_id and loc_id not in known:
            known.append(loc_id)
        normalized = {
            **pos,
            "location_id": loc_id,
            "x": x,
            "y": y,
            "known_location_ids": list(dict.fromkeys(known)),
        }
        if normalized != pos:
            positions[cid] = normalized
            changed = True

    if changed:
        save_character_positions(positions, user_id=user_id)

    user_position = load_user_position(user_id=user_id)
    user_changed = False
    try:
        ux = float(user_position.get("x", 0.0))
        uy = float(user_position.get("y", 0.0))
    except (TypeError, ValueError):
        ux, uy = 0.0, 0.0
        user_changed = True
    user_loc_id = user_position.get("location_id")
    if user_loc_id in locs_by_id:
        loc = locs_by_id[user_loc_id]
        canonical = (float(loc["x"]), float(loc["y"]))
        if (ux, uy) != canonical:
            ux, uy = canonical
            user_changed = True
    else:
        if user_loc_id is not None:
            user_loc_id = None
            user_changed = True
        for candidate in locs_by_id.values():
            if calc_distance(ux, uy, float(candidate["x"]), float(candidate["y"])) < 0.1:
                user_loc_id = candidate["id"]
                ux, uy = float(candidate["x"]), float(candidate["y"])
                user_changed = True
                break
    normalized_user = {**user_position, "location_id": user_loc_id, "x": ux, "y": uy}
    if user_changed or normalized_user != user_position:
        save_user_position(normalized_user, user_id=user_id)

    # chat_mode is a derived location state: offline means physically co-located.
    if valid_char_ids is not None:
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                chars_data = json.load(f) or {}
            chat_mode_changed = False
            for cid, pos in positions.items():
                if cid not in chars_data:
                    continue
                if ensure_character_time_defaults(
                    chars_data[cid], existing_character=True
                ):
                    chat_mode_changed = True
                location = locs_by_id.get(pos.get("location_id"))
                real_world = (
                    location.get("real_world")
                    if isinstance(location, dict)
                    else None
                )
                location_timezone = (
                    real_world.get("timezone")
                    if isinstance(real_world, dict)
                    else None
                )
                if (
                    is_valid_timezone(location_timezone)
                    and chars_data[cid].get("timezone_source") != "manual"
                ):
                    if (
                        chars_data[cid].get("timezone") != location_timezone
                        or chars_data[cid].get("timezone_source") != "location"
                        or chars_data[cid].get("timezone_location_id")
                        != location.get("id")
                    ):
                        chars_data[cid]["timezone"] = location_timezone
                        chars_data[cid]["timezone_source"] = "location"
                        chars_data[cid]["timezone_location_id"] = location.get("id")
                        chat_mode_changed = True
                same_named_location = (
                    pos.get("location_id") is not None
                    and pos.get("location_id") == normalized_user.get("location_id")
                )
                same_coordinates = calc_distance(
                    float(pos.get("x", 0.0)),
                    float(pos.get("y", 0.0)),
                    float(normalized_user.get("x", 0.0)),
                    float(normalized_user.get("y", 0.0)),
                ) < 0.1
                expected_mode = "offline" if same_named_location or same_coordinates else "online"
                if chars_data[cid].get("chat_mode") != expected_mode:
                    chars_data[cid]["chat_mode"] = expected_mode
                    chat_mode_changed = True
            if chat_mode_changed:
                safe_save_json(cfg_file, chars_data)
        except Exception as e:
            print(f"[Map Normalize] failed to reconcile chat modes: {e}")

    return positions, normalized_user, locations


def move_character_position(char_id, x, y, location_id=None, force=False, user_id=None):
    """Move an existing character while keeping coordinates and location_id canonical."""
    cfg_file = _get_characters_config_file(user_id=user_id)
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            chars = json.load(f) or {}
    except Exception:
        chars = {}
    if char_id not in chars:
        raise ValueError("character not found")

    positions, _, locations = normalize_map_state(user_id=user_id)
    if char_id not in positions:
        positions[char_id] = {
            "location_id": "home",
            "x": 0.0,
            "y": 0.0,
            "known_location_ids": ["home"],
        }

    locs_by_id = {loc["id"]: loc for loc in locations.get("locations", [])}
    if location_id:
        if location_id not in locs_by_id:
            raise ValueError("location not found")
        target = locs_by_id[location_id]
        target_x, target_y = float(target["x"]), float(target["y"])
    else:
        target_x, target_y = float(x), float(y)
        matched = None
        for loc in locs_by_id.values():
            if calc_distance(target_x, target_y, float(loc["x"]), float(loc["y"])) < 0.1:
                matched = loc
                break
        location_id = matched["id"] if matched else None
        if matched:
            target_x, target_y = float(matched["x"]), float(matched["y"])

    old = dict(positions[char_id])
    known = [
        lid for lid in (old.get("known_location_ids") or [])
        if lid in locs_by_id
    ]
    distance = calc_distance(
        float(old.get("x", 0.0)),
        float(old.get("y", 0.0)),
        target_x,
        target_y,
    )
    is_known_destination = bool(location_id and location_id in known)
    if distance >= 1.0 and not is_known_destination and not force:
        raise ValueError(
            f"distance {round(distance, 2)} is not below 1.0 and destination is unknown"
        )

    if location_id and location_id not in known:
        known.append(location_id)
    positions[char_id] = {
        **old,
        "location_id": location_id,
        "x": target_x,
        "y": target_y,
        "known_location_ids": known,
    }
    save_character_positions(positions, user_id=user_id)
    auto_toggle_chat_mode_on_move(
        char_id=char_id,
        old_location_id=old.get("location_id"),
        new_location_id=location_id,
        user_id=user_id,
    )
    result_position = dict(positions[char_id])
    if location_id and location_id in locs_by_id:
        result_position["timezone_change"] = sync_character_timezone_to_location(
            char_id,
            locs_by_id[location_id],
            user_id=user_id,
        )
    else:
        result_position["timezone_change"] = {"changed": False}
    return old, result_position


def move_user_position(x, y, location_id=None, user_id=None):
    """Move the user while keeping coordinates and location_id canonical."""
    _, current, locations = normalize_map_state(user_id=user_id)
    locs_by_id = {loc["id"]: loc for loc in locations.get("locations", [])}
    if location_id:
        if location_id not in locs_by_id:
            raise ValueError("location not found")
        target = locs_by_id[location_id]
        target_x, target_y = float(target["x"]), float(target["y"])
    else:
        target_x, target_y = float(x), float(y)
        matched = None
        for loc in locs_by_id.values():
            if calc_distance(target_x, target_y, float(loc["x"]), float(loc["y"])) < 0.1:
                matched = loc
                break
        location_id = matched["id"] if matched else None
        if matched:
            target_x, target_y = float(matched["x"]), float(matched["y"])

    old_location_id = current.get("location_id")
    updated = {
        **current,
        "location_id": location_id,
        "x": target_x,
        "y": target_y,
    }
    save_user_position(updated, user_id=user_id)
    auto_toggle_chat_mode_on_move(
        old_location_id=old_location_id,
        new_location_id=location_id,
        user_id=user_id,
    )
    return current, updated


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

    user_pos = load_user_position(user_id=user_id)
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
        positions = load_character_positions(user_id=user_id)
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


def ensure_group_chat_storage(group_id: str, user_id=None):
    """Ensure a configured group has a writable directory and messages table."""
    effective_user_id = user_id if user_id is not None else get_current_user_id()
    if effective_user_id:
        group_dir = os.path.join(USERS_ROOT, str(effective_user_id), "groups", group_id)
    else:
        group_dir = os.path.join(GROUPS_DIR, group_id)

    os.makedirs(group_dir, exist_ok=True)
    db_path = os.path.join(group_dir, "chat.db")
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
    return group_dir, db_path
