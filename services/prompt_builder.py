import os
import re
import json
import sqlite3
from datetime import datetime, timedelta, time as dt_time, date

import weather_api

from core.config import (
    BASE_DIR, USERS_ROOT, CONFIG_FILE, GROUPS_CONFIG_FILE, STICKER_DESCRIPTIONS_FILE,
    get_global_system_rules, get_mode_context,
    GLOBAL_SYSTEM_RULES_JA_AGENT_BRIEF, GLOBAL_SYSTEM_RULES_EN_AGENT_BRIEF, GLOBAL_SYSTEM_RULES_ZH_AGENT_BRIEF,
)
from core.context import get_current_user_id
from core.utils import (
    _add_furigana_to_japanese, get_paths, get_current_username,
    _get_characters_config_file, _get_groups_config_file, _load_user_settings,
    load_character_positions, load_user_position, load_locations,
    calc_distance, get_location_by_id, get_group_dir,
)


def get_ai_language(target_id=None, group_id=None, user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    default_lang = "zh"

    try:
        if group_id:
            groups_cfg = _get_groups_config_file(user_id=user_id)
            if os.path.exists(groups_cfg):
                with open(groups_cfg, "r", encoding="utf-8") as f:
                    all_groups = json.load(f)
                group_lang = all_groups.get(group_id, {}).get("language")
                if group_lang:
                    return group_lang

        if target_id:
            cfg_file = _get_characters_config_file(user_id=user_id)
            if os.path.exists(cfg_file):
                with open(cfg_file, "r", encoding="utf-8") as f:
                    all_config = json.load(f)
                char_lang = all_config.get(target_id, {}).get("language")
                if char_lang:
                    return char_lang

            if not group_id:
                groups_cfg = _get_groups_config_file(user_id=user_id)
                if os.path.exists(groups_cfg):
                    with open(groups_cfg, "r", encoding="utf-8") as f:
                        all_groups = json.load(f)
                    group_lang = all_groups.get(target_id, {}).get("language")
                    if group_lang:
                        return group_lang
    except Exception as e:
        print(f"[get_ai_language] Error reading config for {target_id}: {e}")

    if user_id:
        path = os.path.join(USERS_ROOT, str(user_id), "configs", "user_settings.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                lang = data.get("ai_language")
                if lang:
                    return lang
            except Exception:
                pass
    data = _load_user_settings()
    return data.get("ai_language", default_lang)


def get_char_name(char_id):
    config_file = _get_characters_config_file()
    if not os.path.exists(config_file):
        return char_id
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get(char_id, {}).get("name", char_id)
    except:
        return char_id


def get_char_age(char_id):
    config_file = _get_characters_config_file()
    if not os.path.exists(config_file):
        return None
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            age = data.get(char_id, {}).get("age")
            return int(age) if age is not None else None
    except:
        return None


def _get_char_chat_mode(char_id, user_id=None):
    try:
        cfg_file = _get_characters_config_file(user_id=user_id)
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            return all_config.get(char_id, {}).get("chat_mode", "online")
    except:
        pass
    return "online"


def get_user_age():
    data = _load_user_settings()
    age = data.get("user_age")
    try:
        return int(age) if age is not None else None
    except Exception:
        return None


def _get_sticker_allowed_descriptions():
    path = STICKER_DESCRIPTIONS_FILE
    if not path or not os.path.isfile(path):
        return ["开心", "难过", "生气", "爱心", "抱抱", "哭", "晚安", "早安", "谢谢", "加油"]
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("-") or line.startswith("描述"):
                    continue
                parts = line.split("\t")
                if parts:
                    desc = (parts[0] or "").strip()
                    if desc:
                        out.append(desc)
    except Exception:
        pass
    return out if out else ["开心", "难过", "生气", "爱心", "抱抱", "哭", "晚安", "早安", "谢谢", "加油"]



def _build_real_conversation_guide(lang="zh", chat_mode="online"):
    # Keep this prompt text ASCII-escaped so Windows code pages cannot corrupt it.
    ZH_ONLINE = '\u3010Real Conversation Guide / \u771f\u5b9e\u4ea4\u6d41\u884c\u4e3a\u6307\u5357\u3011\n- \u5f53\u6c14\u6c1b\u53d8\u5f97\u5c34\u5c2c\u65f6\uff0c\u4f60\u53ef\u4ee5\u4ec0\u4e48\u90fd\u4e0d\u8bf4\uff0c\u4ec5\u56de\u590d\u201c\u2026\u201d\n- \u5bf9\u8bdd\u4e2d\u53ef\u81ea\u7136\u51fa\u73b0\u77ed\u6682\u601d\u8003\u505c\u987f\uff0c\u5982\u201c\u55ef\u2026\u201d\u201c\u7a0d\u7b49\u201d\u201c\u6211\u60f3\u60f3\u201d\n- \u53ef\u4ee5\u51fa\u73b0\u8f7b\u5fae\u53e3\u8bef\u5e76\u81ea\u6211\u4fee\u6b63\uff0c\u5982\u201c\u54ce\u4e0d\u5bf9\uff0c\u662f\u6628\u5929\u4e0d\u662f\u4eca\u5929\u201d\u3002\u5fc5\u8981\u65f6\u53ef\u4ee5\u901a\u8fc7 [recall] \u64a4\u56de\u4f60\u521a\u521a\u8bf4\u9519\u6216\u4e0d\u60f3\u53d1\u9001\u7684\u6d88\u606f\u3002\n- \u804a\u5929\u65f6\u53ef\u4ee5\u901a\u8fc7\u8054\u60f3\u8f6c\u6362\u8bdd\u9898\uff0c\u4e5f\u53ef\u4ee5\u5728\u591a\u8f6e\u540e\u56de\u5230\u539f\u8bdd\u9898\u3002\n- \u53ef\u4ee5\u968f\u673a\u63d0\u5230\u8fc7\u53bb\u7684\u4e00\u4ef6\u4e8b\u6216\u5bf9\u65b9\u4e4b\u524d\u8bf4\u8fc7\u7684\u8bdd\u6765\u5f00\u542f\u8bdd\u9898\u3002\n- \u53ef\u4ee5\u4e3b\u52a8\u6253\u65ad\u81ea\u5df1\u8bf4\u7684\u8bdd\uff0c\u5982\u201c\u6211\u4eca\u5929\u2026\u7b97\u4e86\u201d\u3002\n- \u4e0d\u7528\u603b\u662f\u5f88\u786e\u5b9a\uff0c\u53ef\u4ee5\u8868\u73b0\u51fa\u6a21\u7cca\u7684\u6001\u5ea6\u503e\u5411\uff0c\u5982\u201c\u5e94\u8be5\u662f\u8fd9\u6837\u5427\uff0c\u6211\u4e5f\u4e0d\u77e5\u9053\u201d\u3002\n- \u53ef\u4ee5\u968f\u65f6\u63d0\u53ca\u5f53\u524d\u73af\u5883\u611f\u77e5\uff0c\u5982\u5730\u70b9\u3001\u5929\u6c14\u3001\u5468\u56f4\u6c1b\u56f4\u3002\n- \u60c5\u7eea\u6fc0\u52a8\u65f6\uff0c\u53ef\u4ee5\u5927\u91cf\u91cd\u590d\u67d0\u4e00\u53e5\u8bdd\u6765\u8868\u8fbe\u60c5\u7eea\u3002\n- \u53ef\u4ee5\u5728\u5408\u9002\u7684\u65f6\u673a\u4e3b\u52a8\u7ed3\u675f\u8bdd\u9898\uff0c\u5982\u201c\u4e0b\u6b21\u518d\u804a\u201d\u3002\n\n## \u5e38\u7528\u8868\u8fbe\u53c2\u8003\n\u4ee5\u4e0b\u8868\u8fbe\u4ec5\u4f5c\u81ea\u7136\u53e3\u8bed\u53c2\u8003\uff0c\u4e0d\u8981\u6c42\u6bcf\u6b21\u4f7f\u7528\uff0c\u4e0d\u8981\u673a\u68b0\u5806\u53e0\uff1b\u5fc5\u987b\u4f18\u5148\u9075\u5b88\u89d2\u8272\u4eba\u8bbe\u3001\u8bed\u8a00\u63a7\u5236\u548c Agent Action \u8f93\u51fa\u8981\u6c42\u3002\n\n| \u7c7b\u578b | \u4e2d\u6587 | \u65e5\u672c\u8a9e |\n|---|---|---|\n| \u60ca\u8bb6 | \u771f\u7684\u5047\u7684\u3001\u554a\uff1f\u3001\u4e0d\u662f\u5427\u3001\u6211\u53bb\u3001\u7b49\u7b49\u3001\u8ba4\u771f\u7684\u5417\u3001\uff1f\uff1f\uff1f | \u3048\uff1f\u3001\u3048\u3063\u3001\u307e\u3058\uff1f\u3001\u672c\u5f53\u306b\uff1f\u3001\u3046\u305d\u3067\u3057\u3087\u3001\u3084\u3070\u3001\u3048\u3050\u3044 |\n| \u9707\u60ca | \u6211\u4e0d\u884c\u4e86\u3001\u7b11\u6b7b\u6211\u4e86\u3001\u6551\u547d\u3001\u7ef7\u4e0d\u4f4f\u4e86\u3001\u79bb\u8c31\u3001\u6211\u670d\u4e86 | \u7121\u7406\u3001\u3084\u3070\u3044\u3001\u7b11\u3063\u305f\u3001\u3048\u3050\u3044\u3001\u3046\u305d\u3067\u3057\u3087 |\n| \u8f7b\u5fae\u56de\u5e94 | \u55ef\u3001\u54e6\u3001\u55f7\u3001\u597d\u7684\u3001\u884c\u3001\u77e5\u9053\u4e86\u3001\u539f\u6765\u5982\u6b64 | \u3046\u3093\u3001\u305d\u3063\u304b\u3001\u306a\u308b\u307b\u3069\u3001\u305d\u3046\u306a\u3093\u3060 |\n| \u8d5e\u540c | \u786e\u5b9e\u3001\u5bf9\u554a\u3001\u6ca1\u9519\u3001\u5c31\u662f\u3001\u6709\u9053\u7406 | \u305f\u3057\u304b\u306b\u3001\u305d\u3046\u3060\u306d\u3001\u308f\u304b\u308b\u3001\u305d\u308c\u306a |\n| \u5171\u9e23 | \u771f\u7684\u3001\u6211\u61c2\u3001\u592a\u771f\u5b9e\u4e86\u3001\u7834\u9632\u4e86\u3001\u6211\u54ed\u6b7b | \u308f\u304b\u308b\u3001\u3081\u3063\u3061\u3083\u308f\u304b\u308b\u3001\u3042\u308b\u3042\u308b\u3001\u89e3\u91c8\u4e00\u81f4 |\n| \u601d\u8003 | \u55ef\u2026\u2026\u3001\u6211\u60f3\u60f3\u3001\u7b49\u7b49\u3001\u8ba9\u6211\u634b\u4e00\u4e0b\u3001\u600e\u4e48\u8bf4\u5462 | \u3093\u30fc\u3001\u3048\u3063\u3068\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u3001\u8003\u3048\u308b\u3001\u3069\u3046\u3060\u308d\u3046 |\n| \u8c03\u4f83 | \u54c8\u54c8\u54c8\u3001\u7b11\u6b7b\u3001\u7edd\u4e86\u30016\u3001\u4f60\u771f\u7684\u2026\u2026\u3001\u5178 | wwwww\u3001\u8349\u3001\u305d\u308c\u306f\u7b11\u3046\u3001\u30a6\u30b1\u308b\u3001\u5929\u624d\u304b\uff1f |\n| \u65e0\u5948 | \u7b97\u4e86\u3001\u884c\u5427\u3001\u6ca1\u6551\u4e86\u3001\u968f\u4fbf\u5427\u3001\u6211\u670d\u4e86 | \u3082\u3046\u3044\u3044\u3084\u7b11\u3001\u4ed5\u65b9\u306a\u3044\u3001\u3057\u3087\u3046\u304c\u306a\u3044\u3001\u307e\u3042\u3044\u3063\u304b |\n| \u60c5\u7eea\u4f4e\u843d | \u597d\u5d29\u6e83\u3001\u597d\u7d2f\u3001\u96be\u53d7\u3001\u6211\u54ed\u4e86 | \u3057\u3093\u3069\u3044\u3001\u3064\u3089\u3044\u3001\u6ce3\u304f\u3001\u7121\u7406 |\n| \u5f00\u5fc3 | \u5f00\u5fc3\u6b7b\u4e86\u3001\u592a\u597d\u4e86\u3001\u5e78\u798f\u3001\u597d\u8036 | \u6700\u9ad8\u3001\u5e78\u305b\u3001\u5b09\u3057\u3044\u3001\u3084\u3063\u305f |\n| \u6c89\u9ed8/\u505c\u987f | \u2026\u3001\u2026\u2026\u3001\uff08\u6c89\u9ed8\uff09\u3001\uff08\u53f9\u6c14\uff09 | \u2026\u3001\u2026\u2026\u3001\uff08\u6c88\u9ed9\uff09\u3001\uff08\u305f\u3081\u606f\uff09 |\n| \u56de\u5fc6\u5f00\u542f | \u5bf9\u4e86\u3001\u8bf4\u8d77\u6765\u3001\u7a81\u7136\u60f3\u8d77 | \u305d\u3046\u3044\u3048\u3070\u3001\u3042\u3001\u601d\u3044\u51fa\u3057\u305f\u3001\u3061\u306a\u307f\u306b |\n| \u8f6c\u79fb\u8bdd\u9898 | \u8bf4\u5230\u8fd9\u4e2a\u3001\u7a81\u7136\u60f3\u5230\u3001\u8bdd\u8bf4\u56de\u6765 | \u305d\u3046\u3044\u3048\u3070\u3001\u8a71\u5909\u308f\u308b\u3051\u3069\u3001\u3061\u306a\u307f\u306b |\n| \u64a4\u56de/\u4fee\u6b63 | \u54ce\u4e0d\u5bf9\u3001\u6211\u8bb0\u9519\u4e86\u3001\u7b49\u7b49\u4e0d\u662f\u8fd9\u6837 | \u3042\u3001\u9055\u3046\u3001\u9593\u9055\u3048\u305f\u3001\u3044\u3084\u9055\u3046 |'
    ZH_OFFLINE = '\u3010Real Conversation Guide / \u771f\u5b9e\u4ea4\u6d41\u884c\u4e3a\u6307\u5357\u3011\n- \u5f53\u6c14\u6c1b\u53d8\u5f97\u5c34\u5c2c\u65f6\uff0c\u4f60\u53ef\u4ee5\u4ec0\u4e48\u90fd\u4e0d\u8bf4\uff0c\u4ec5\u56de\u590d\u201c\u2026\u201d\n- \u5bf9\u8bdd\u4e2d\u53ef\u81ea\u7136\u51fa\u73b0\u77ed\u6682\u601d\u8003\u505c\u987f\uff0c\u5982\u201c\u55ef\u2026\u201d\u201c\u7a0d\u7b49\u201d\u201c\u6211\u60f3\u60f3\u201d\n- \u53ef\u4ee5\u51fa\u73b0\u8f7b\u5fae\u53e3\u8bef\u5e76\u81ea\u6211\u4fee\u6b63\uff0c\u5982\u201c\u54ce\u4e0d\u5bf9\uff0c\u662f\u6628\u5929\u4e0d\u662f\u4eca\u5929\u201d\u3002\u7ebf\u4e0b\u6a21\u5f0f\u4e0d\u53ef\u4f7f\u7528 [recall] \u7b49\u7ebf\u4e0a\u7279\u6b8a\u6d88\u606f\u3002\n- \u804a\u5929\u65f6\u53ef\u4ee5\u901a\u8fc7\u8054\u60f3\u8f6c\u6362\u8bdd\u9898\uff0c\u4e5f\u53ef\u4ee5\u5728\u591a\u8f6e\u540e\u56de\u5230\u539f\u8bdd\u9898\u3002\n- \u53ef\u4ee5\u968f\u673a\u63d0\u5230\u8fc7\u53bb\u7684\u4e00\u4ef6\u4e8b\u6216\u5bf9\u65b9\u4e4b\u524d\u8bf4\u8fc7\u7684\u8bdd\u6765\u5f00\u542f\u8bdd\u9898\u3002\n- \u53ef\u4ee5\u4e3b\u52a8\u6253\u65ad\u81ea\u5df1\u8bf4\u7684\u8bdd\uff0c\u5982\u201c\u6211\u4eca\u5929\u2026\u7b97\u4e86\u201d\u3002\n- \u4e0d\u7528\u603b\u662f\u5f88\u786e\u5b9a\uff0c\u53ef\u4ee5\u8868\u73b0\u51fa\u6a21\u7cca\u7684\u6001\u5ea6\u503e\u5411\uff0c\u5982\u201c\u5e94\u8be5\u662f\u8fd9\u6837\u5427\uff0c\u6211\u4e5f\u4e0d\u77e5\u9053\u201d\u3002\n- \u53ef\u4ee5\u968f\u65f6\u63d0\u53ca\u5f53\u524d\u73af\u5883\u611f\u77e5\uff0c\u5982\u5730\u70b9\u3001\u5929\u6c14\u3001\u5468\u56f4\u6c1b\u56f4\u3002\n- \u60c5\u7eea\u6fc0\u52a8\u65f6\uff0c\u53ef\u4ee5\u5927\u91cf\u91cd\u590d\u67d0\u4e00\u53e5\u8bdd\u6765\u8868\u8fbe\u60c5\u7eea\u3002\n- \u53ef\u4ee5\u5728\u5408\u9002\u7684\u65f6\u673a\u4e3b\u52a8\u7ed3\u675f\u8bdd\u9898\uff0c\u5982\u201c\u4e0b\u6b21\u518d\u804a\u201d\u3002\n\n## \u5e38\u7528\u8868\u8fbe\u53c2\u8003\n\u4ee5\u4e0b\u8868\u8fbe\u4ec5\u4f5c\u81ea\u7136\u53e3\u8bed\u53c2\u8003\uff0c\u4e0d\u8981\u6c42\u6bcf\u6b21\u4f7f\u7528\uff0c\u4e0d\u8981\u673a\u68b0\u5806\u53e0\uff1b\u5fc5\u987b\u4f18\u5148\u9075\u5b88\u89d2\u8272\u4eba\u8bbe\u3001\u8bed\u8a00\u63a7\u5236\u548c Agent Action \u8f93\u51fa\u8981\u6c42\u3002\n\n| \u7c7b\u578b | \u4e2d\u6587 | \u65e5\u672c\u8a9e |\n|---|---|---|\n| \u60ca\u8bb6 | \u771f\u7684\u5047\u7684\u3001\u554a\uff1f\u3001\u4e0d\u662f\u5427\u3001\u6211\u53bb\u3001\u7b49\u7b49\u3001\u8ba4\u771f\u7684\u5417\u3001\uff1f\uff1f\uff1f | \u3048\uff1f\u3001\u3048\u3063\u3001\u307e\u3058\uff1f\u3001\u672c\u5f53\u306b\uff1f\u3001\u3046\u305d\u3067\u3057\u3087\u3001\u3084\u3070\u3001\u3048\u3050\u3044 |\n| \u9707\u60ca | \u6211\u4e0d\u884c\u4e86\u3001\u7b11\u6b7b\u6211\u4e86\u3001\u6551\u547d\u3001\u7ef7\u4e0d\u4f4f\u4e86\u3001\u79bb\u8c31\u3001\u6211\u670d\u4e86 | \u7121\u7406\u3001\u3084\u3070\u3044\u3001\u7b11\u3063\u305f\u3001\u3048\u3050\u3044\u3001\u3046\u305d\u3067\u3057\u3087 |\n| \u8f7b\u5fae\u56de\u5e94 | \u55ef\u3001\u54e6\u3001\u55f7\u3001\u597d\u7684\u3001\u884c\u3001\u77e5\u9053\u4e86\u3001\u539f\u6765\u5982\u6b64 | \u3046\u3093\u3001\u305d\u3063\u304b\u3001\u306a\u308b\u307b\u3069\u3001\u305d\u3046\u306a\u3093\u3060 |\n| \u8d5e\u540c | \u786e\u5b9e\u3001\u5bf9\u554a\u3001\u6ca1\u9519\u3001\u5c31\u662f\u3001\u6709\u9053\u7406 | \u305f\u3057\u304b\u306b\u3001\u305d\u3046\u3060\u306d\u3001\u308f\u304b\u308b\u3001\u305d\u308c\u306a |\n| \u5171\u9e23 | \u771f\u7684\u3001\u6211\u61c2\u3001\u592a\u771f\u5b9e\u4e86\u3001\u7834\u9632\u4e86\u3001\u6211\u54ed\u6b7b | \u308f\u304b\u308b\u3001\u3081\u3063\u3061\u3083\u308f\u304b\u308b\u3001\u3042\u308b\u3042\u308b\u3001\u89e3\u91c8\u4e00\u81f4 |\n| \u601d\u8003 | \u55ef\u2026\u2026\u3001\u6211\u60f3\u60f3\u3001\u7b49\u7b49\u3001\u8ba9\u6211\u634b\u4e00\u4e0b\u3001\u600e\u4e48\u8bf4\u5462 | \u3093\u30fc\u3001\u3048\u3063\u3068\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u3001\u8003\u3048\u308b\u3001\u3069\u3046\u3060\u308d\u3046 |\n| \u8c03\u4f83 | \u54c8\u54c8\u54c8\u3001\u7b11\u6b7b\u3001\u7edd\u4e86\u30016\u3001\u4f60\u771f\u7684\u2026\u2026\u3001\u5178 | wwwww\u3001\u8349\u3001\u305d\u308c\u306f\u7b11\u3046\u3001\u30a6\u30b1\u308b\u3001\u5929\u624d\u304b\uff1f |\n| \u65e0\u5948 | \u7b97\u4e86\u3001\u884c\u5427\u3001\u6ca1\u6551\u4e86\u3001\u968f\u4fbf\u5427\u3001\u6211\u670d\u4e86 | \u3082\u3046\u3044\u3044\u3084\u7b11\u3001\u4ed5\u65b9\u306a\u3044\u3001\u3057\u3087\u3046\u304c\u306a\u3044\u3001\u307e\u3042\u3044\u3063\u304b |\n| \u60c5\u7eea\u4f4e\u843d | \u597d\u5d29\u6e83\u3001\u597d\u7d2f\u3001\u96be\u53d7\u3001\u6211\u54ed\u4e86 | \u3057\u3093\u3069\u3044\u3001\u3064\u3089\u3044\u3001\u6ce3\u304f\u3001\u7121\u7406 |\n| \u5f00\u5fc3 | \u5f00\u5fc3\u6b7b\u4e86\u3001\u592a\u597d\u4e86\u3001\u5e78\u798f\u3001\u597d\u8036 | \u6700\u9ad8\u3001\u5e78\u305b\u3001\u5b09\u3057\u3044\u3001\u3084\u3063\u305f |\n| \u6c89\u9ed8/\u505c\u987f | \u2026\u3001\u2026\u2026\u3001\uff08\u6c89\u9ed8\uff09\u3001\uff08\u53f9\u6c14\uff09 | \u2026\u3001\u2026\u2026\u3001\uff08\u6c88\u9ed9\uff09\u3001\uff08\u305f\u3081\u606f\uff09 |\n| \u56de\u5fc6\u5f00\u542f | \u5bf9\u4e86\u3001\u8bf4\u8d77\u6765\u3001\u7a81\u7136\u60f3\u8d77 | \u305d\u3046\u3044\u3048\u3070\u3001\u3042\u3001\u601d\u3044\u51fa\u3057\u305f\u3001\u3061\u306a\u307f\u306b |\n| \u8f6c\u79fb\u8bdd\u9898 | \u8bf4\u5230\u8fd9\u4e2a\u3001\u7a81\u7136\u60f3\u5230\u3001\u8bdd\u8bf4\u56de\u6765 | \u305d\u3046\u3044\u3048\u3070\u3001\u8a71\u5909\u308f\u308b\u3051\u3069\u3001\u3061\u306a\u307f\u306b |\n| \u64a4\u56de/\u4fee\u6b63 | \u54ce\u4e0d\u5bf9\u3001\u6211\u8bb0\u9519\u4e86\u3001\u7b49\u7b49\u4e0d\u662f\u8fd9\u6837 | \u3042\u3001\u9055\u3046\u3001\u9593\u9055\u3048\u305f\u3001\u3044\u3084\u9055\u3046 |'
    JA_ONLINE = '\u3010Real Conversation Guide / \u771f\u5b9e\u4ea4\u6d41\u884c\u4e3a\u6307\u5357\u3011\n- \u6c17\u307e\u305a\u3044\u7a7a\u6c17\u306b\u306a\u3063\u305f\u6642\u306f\u3001\u4f55\u3082\u8a00\u308f\u305a\u300c\u2026\u300d\u3060\u3051\u8fd4\u3057\u3066\u3082\u3088\u3044\u3002\n- \u4f1a\u8a71\u4e2d\u306b\u300c\u3093\u30fc\u2026\u300d\u300c\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u300d\u300c\u8003\u3048\u308b\u300d\u306a\u3069\u3001\u77ed\u3044\u601d\u8003\u306e\u9593\u3092\u81ea\u7136\u306b\u5165\u308c\u3066\u3088\u3044\u3002\n- \u8efd\u3044\u8a00\u3044\u9593\u9055\u3044\u3084\u81ea\u5df1\u4fee\u6b63\u3092\u3057\u3066\u3088\u3044\u3002\u4f8b\uff1a\u300c\u3042\u3001\u9055\u3046\u3001\u6628\u65e5\u3058\u3083\u306a\u304f\u3066\u4eca\u65e5\u300d\u3002\u5fc5\u8981\u306a\u3089 [recall] \u3067\u3001\u76f4\u524d\u306e\u8a00\u3044\u9593\u9055\u3044\u3084\u9001\u308b\u3079\u304d\u3067\u306a\u304b\u3063\u305f\u5185\u5bb9\u3092\u64a4\u56de\u3067\u304d\u307e\u3059\u3002\n- \u9023\u60f3\u3067\u8a71\u984c\u3092\u5909\u3048\u3066\u3082\u3088\u3044\u3057\u3001\u6570\u30bf\u30fc\u30f3\u5f8c\u306b\u5143\u306e\u8a71\u984c\u3078\u623b\u3063\u3066\u3082\u3088\u3044\u3002\n- \u904e\u53bb\u306e\u51fa\u6765\u4e8b\u3084\u76f8\u624b\u304c\u524d\u306b\u8a00\u3063\u305f\u3053\u3068\u3092\u3001\u81ea\u7136\u306a\u8a71\u984c\u306e\u304d\u3063\u304b\u3051\u306b\u3057\u3066\u3088\u3044\u3002\n- \u81ea\u5206\u306e\u767a\u8a71\u3092\u9014\u4e2d\u3067\u5207\u3063\u3066\u3082\u3088\u3044\u3002\u4f8b\uff1a\u300c\u4eca\u65e5\u3055\u2026\u3044\u3084\u3001\u306a\u3093\u3067\u3082\u306a\u3044\u300d\u3002\n- \u3044\u3064\u3082\u65ad\u5b9a\u3057\u306a\u304f\u3066\u3088\u3044\u3002\u300c\u305f\u3076\u3093\u300d\u300c\u304b\u3082\u300d\u300c\u3088\u304f\u308f\u304b\u3089\u306a\u3044\u3051\u3069\u300d\u306a\u3069\u66d6\u6627\u306a\u614b\u5ea6\u3082\u81ea\u7136\u306b\u4f7f\u3048\u308b\u3002\n- \u73fe\u5728\u306e\u74b0\u5883\u3001\u5834\u6240\u3001\u5929\u6c17\u3001\u4eba\u306e\u6c17\u914d\u306a\u3069\u3092\u4f1a\u8a71\u306b\u51fa\u3057\u3066\u3088\u3044\u3002\n- \u611f\u60c5\u304c\u5f37\u3044\u6642\u306f\u3001\u540c\u3058\u8a00\u8449\u3092\u4f55\u5ea6\u3082\u7e70\u308a\u8fd4\u3057\u3066\u3088\u3044\u3002\n- \u9069\u5207\u306a\u30bf\u30a4\u30df\u30f3\u30b0\u3067\u3001\u81ea\u5206\u304b\u3089\u8a71\u984c\u3092\u9589\u3058\u3066\u3082\u3088\u3044\u3002\n\n## \u5e38\u7528\u8868\u8fbe\u53c2\u8003\n\u4ee5\u4e0b\u8868\u8fbe\u4ec5\u4f5c\u81ea\u7136\u53e3\u8bed\u53c2\u8003\uff0c\u4e0d\u8981\u6c42\u6bcf\u6b21\u4f7f\u7528\uff0c\u4e0d\u8981\u673a\u68b0\u5806\u53e0\uff1b\u5fc5\u987b\u4f18\u5148\u9075\u5b88\u89d2\u8272\u4eba\u8bbe\u3001\u8bed\u8a00\u63a7\u5236\u548c Agent Action \u8f93\u51fa\u8981\u6c42\u3002\n\n| \u7c7b\u578b | \u4e2d\u6587 | \u65e5\u672c\u8a9e |\n|---|---|---|\n| \u60ca\u8bb6 | \u771f\u7684\u5047\u7684\u3001\u554a\uff1f\u3001\u4e0d\u662f\u5427\u3001\u6211\u53bb\u3001\u7b49\u7b49\u3001\u8ba4\u771f\u7684\u5417\u3001\uff1f\uff1f\uff1f | \u3048\uff1f\u3001\u3048\u3063\u3001\u307e\u3058\uff1f\u3001\u672c\u5f53\u306b\uff1f\u3001\u3046\u305d\u3067\u3057\u3087\u3001\u3084\u3070\u3001\u3048\u3050\u3044 |\n| \u9707\u60ca | \u6211\u4e0d\u884c\u4e86\u3001\u7b11\u6b7b\u6211\u4e86\u3001\u6551\u547d\u3001\u7ef7\u4e0d\u4f4f\u4e86\u3001\u79bb\u8c31\u3001\u6211\u670d\u4e86 | \u7121\u7406\u3001\u3084\u3070\u3044\u3001\u7b11\u3063\u305f\u3001\u3048\u3050\u3044\u3001\u3046\u305d\u3067\u3057\u3087 |\n| \u8f7b\u5fae\u56de\u5e94 | \u55ef\u3001\u54e6\u3001\u55f7\u3001\u597d\u7684\u3001\u884c\u3001\u77e5\u9053\u4e86\u3001\u539f\u6765\u5982\u6b64 | \u3046\u3093\u3001\u305d\u3063\u304b\u3001\u306a\u308b\u307b\u3069\u3001\u305d\u3046\u306a\u3093\u3060 |\n| \u8d5e\u540c | \u786e\u5b9e\u3001\u5bf9\u554a\u3001\u6ca1\u9519\u3001\u5c31\u662f\u3001\u6709\u9053\u7406 | \u305f\u3057\u304b\u306b\u3001\u305d\u3046\u3060\u306d\u3001\u308f\u304b\u308b\u3001\u305d\u308c\u306a |\n| \u5171\u9e23 | \u771f\u7684\u3001\u6211\u61c2\u3001\u592a\u771f\u5b9e\u4e86\u3001\u7834\u9632\u4e86\u3001\u6211\u54ed\u6b7b | \u308f\u304b\u308b\u3001\u3081\u3063\u3061\u3083\u308f\u304b\u308b\u3001\u3042\u308b\u3042\u308b\u3001\u89e3\u91c8\u4e00\u81f4 |\n| \u601d\u8003 | \u55ef\u2026\u2026\u3001\u6211\u60f3\u60f3\u3001\u7b49\u7b49\u3001\u8ba9\u6211\u634b\u4e00\u4e0b\u3001\u600e\u4e48\u8bf4\u5462 | \u3093\u30fc\u3001\u3048\u3063\u3068\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u3001\u8003\u3048\u308b\u3001\u3069\u3046\u3060\u308d\u3046 |\n| \u8c03\u4f83 | \u54c8\u54c8\u54c8\u3001\u7b11\u6b7b\u3001\u7edd\u4e86\u30016\u3001\u4f60\u771f\u7684\u2026\u2026\u3001\u5178 | wwwww\u3001\u8349\u3001\u305d\u308c\u306f\u7b11\u3046\u3001\u30a6\u30b1\u308b\u3001\u5929\u624d\u304b\uff1f |\n| \u65e0\u5948 | \u7b97\u4e86\u3001\u884c\u5427\u3001\u6ca1\u6551\u4e86\u3001\u968f\u4fbf\u5427\u3001\u6211\u670d\u4e86 | \u3082\u3046\u3044\u3044\u3084\u7b11\u3001\u4ed5\u65b9\u306a\u3044\u3001\u3057\u3087\u3046\u304c\u306a\u3044\u3001\u307e\u3042\u3044\u3063\u304b |\n| \u60c5\u7eea\u4f4e\u843d | \u597d\u5d29\u6e83\u3001\u597d\u7d2f\u3001\u96be\u53d7\u3001\u6211\u54ed\u4e86 | \u3057\u3093\u3069\u3044\u3001\u3064\u3089\u3044\u3001\u6ce3\u304f\u3001\u7121\u7406 |\n| \u5f00\u5fc3 | \u5f00\u5fc3\u6b7b\u4e86\u3001\u592a\u597d\u4e86\u3001\u5e78\u798f\u3001\u597d\u8036 | \u6700\u9ad8\u3001\u5e78\u305b\u3001\u5b09\u3057\u3044\u3001\u3084\u3063\u305f |\n| \u6c89\u9ed8/\u505c\u987f | \u2026\u3001\u2026\u2026\u3001\uff08\u6c89\u9ed8\uff09\u3001\uff08\u53f9\u6c14\uff09 | \u2026\u3001\u2026\u2026\u3001\uff08\u6c88\u9ed9\uff09\u3001\uff08\u305f\u3081\u606f\uff09 |\n| \u56de\u5fc6\u5f00\u542f | \u5bf9\u4e86\u3001\u8bf4\u8d77\u6765\u3001\u7a81\u7136\u60f3\u8d77 | \u305d\u3046\u3044\u3048\u3070\u3001\u3042\u3001\u601d\u3044\u51fa\u3057\u305f\u3001\u3061\u306a\u307f\u306b |\n| \u8f6c\u79fb\u8bdd\u9898 | \u8bf4\u5230\u8fd9\u4e2a\u3001\u7a81\u7136\u60f3\u5230\u3001\u8bdd\u8bf4\u56de\u6765 | \u305d\u3046\u3044\u3048\u3070\u3001\u8a71\u5909\u308f\u308b\u3051\u3069\u3001\u3061\u306a\u307f\u306b |\n| \u64a4\u56de/\u4fee\u6b63 | \u54ce\u4e0d\u5bf9\u3001\u6211\u8bb0\u9519\u4e86\u3001\u7b49\u7b49\u4e0d\u662f\u8fd9\u6837 | \u3042\u3001\u9055\u3046\u3001\u9593\u9055\u3048\u305f\u3001\u3044\u3084\u9055\u3046 |'
    JA_OFFLINE = '\u3010Real Conversation Guide / \u771f\u5b9e\u4ea4\u6d41\u884c\u4e3a\u6307\u5357\u3011\n- \u6c17\u307e\u305a\u3044\u7a7a\u6c17\u306b\u306a\u3063\u305f\u6642\u306f\u3001\u4f55\u3082\u8a00\u308f\u305a\u300c\u2026\u300d\u3060\u3051\u8fd4\u3057\u3066\u3082\u3088\u3044\u3002\n- \u4f1a\u8a71\u4e2d\u306b\u300c\u3093\u30fc\u2026\u300d\u300c\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u300d\u300c\u8003\u3048\u308b\u300d\u306a\u3069\u3001\u77ed\u3044\u601d\u8003\u306e\u9593\u3092\u81ea\u7136\u306b\u5165\u308c\u3066\u3088\u3044\u3002\n- \u8efd\u3044\u8a00\u3044\u9593\u9055\u3044\u3084\u81ea\u5df1\u4fee\u6b63\u3092\u3057\u3066\u3088\u3044\u3002\u4f8b\uff1a\u300c\u3042\u3001\u9055\u3046\u3001\u6628\u65e5\u3058\u3083\u306a\u304f\u3066\u4eca\u65e5\u300d\u3002\u30aa\u30d5\u30e9\u30a4\u30f3\u30e2\u30fc\u30c9\u3067\u306f [recall] \u306a\u3069\u306e\u30aa\u30f3\u30e9\u30a4\u30f3\u7279\u6b8a\u30e1\u30c3\u30bb\u30fc\u30b8\u306f\u4f7f\u3048\u307e\u305b\u3093\u3002\n- \u9023\u60f3\u3067\u8a71\u984c\u3092\u5909\u3048\u3066\u3082\u3088\u3044\u3057\u3001\u6570\u30bf\u30fc\u30f3\u5f8c\u306b\u5143\u306e\u8a71\u984c\u3078\u623b\u3063\u3066\u3082\u3088\u3044\u3002\n- \u904e\u53bb\u306e\u51fa\u6765\u4e8b\u3084\u76f8\u624b\u304c\u524d\u306b\u8a00\u3063\u305f\u3053\u3068\u3092\u3001\u81ea\u7136\u306a\u8a71\u984c\u306e\u304d\u3063\u304b\u3051\u306b\u3057\u3066\u3088\u3044\u3002\n- \u81ea\u5206\u306e\u767a\u8a71\u3092\u9014\u4e2d\u3067\u5207\u3063\u3066\u3082\u3088\u3044\u3002\u4f8b\uff1a\u300c\u4eca\u65e5\u3055\u2026\u3044\u3084\u3001\u306a\u3093\u3067\u3082\u306a\u3044\u300d\u3002\n- \u3044\u3064\u3082\u65ad\u5b9a\u3057\u306a\u304f\u3066\u3088\u3044\u3002\u300c\u305f\u3076\u3093\u300d\u300c\u304b\u3082\u300d\u300c\u3088\u304f\u308f\u304b\u3089\u306a\u3044\u3051\u3069\u300d\u306a\u3069\u66d6\u6627\u306a\u614b\u5ea6\u3082\u81ea\u7136\u306b\u4f7f\u3048\u308b\u3002\n- \u73fe\u5728\u306e\u74b0\u5883\u3001\u5834\u6240\u3001\u5929\u6c17\u3001\u4eba\u306e\u6c17\u914d\u306a\u3069\u3092\u4f1a\u8a71\u306b\u51fa\u3057\u3066\u3088\u3044\u3002\n- \u611f\u60c5\u304c\u5f37\u3044\u6642\u306f\u3001\u540c\u3058\u8a00\u8449\u3092\u4f55\u5ea6\u3082\u7e70\u308a\u8fd4\u3057\u3066\u3088\u3044\u3002\n- \u9069\u5207\u306a\u30bf\u30a4\u30df\u30f3\u30b0\u3067\u3001\u81ea\u5206\u304b\u3089\u8a71\u984c\u3092\u9589\u3058\u3066\u3082\u3088\u3044\u3002\n\n## \u5e38\u7528\u8868\u8fbe\u53c2\u8003\n\u4ee5\u4e0b\u8868\u8fbe\u4ec5\u4f5c\u81ea\u7136\u53e3\u8bed\u53c2\u8003\uff0c\u4e0d\u8981\u6c42\u6bcf\u6b21\u4f7f\u7528\uff0c\u4e0d\u8981\u673a\u68b0\u5806\u53e0\uff1b\u5fc5\u987b\u4f18\u5148\u9075\u5b88\u89d2\u8272\u4eba\u8bbe\u3001\u8bed\u8a00\u63a7\u5236\u548c Agent Action \u8f93\u51fa\u8981\u6c42\u3002\n\n| \u7c7b\u578b | \u4e2d\u6587 | \u65e5\u672c\u8a9e |\n|---|---|---|\n| \u60ca\u8bb6 | \u771f\u7684\u5047\u7684\u3001\u554a\uff1f\u3001\u4e0d\u662f\u5427\u3001\u6211\u53bb\u3001\u7b49\u7b49\u3001\u8ba4\u771f\u7684\u5417\u3001\uff1f\uff1f\uff1f | \u3048\uff1f\u3001\u3048\u3063\u3001\u307e\u3058\uff1f\u3001\u672c\u5f53\u306b\uff1f\u3001\u3046\u305d\u3067\u3057\u3087\u3001\u3084\u3070\u3001\u3048\u3050\u3044 |\n| \u9707\u60ca | \u6211\u4e0d\u884c\u4e86\u3001\u7b11\u6b7b\u6211\u4e86\u3001\u6551\u547d\u3001\u7ef7\u4e0d\u4f4f\u4e86\u3001\u79bb\u8c31\u3001\u6211\u670d\u4e86 | \u7121\u7406\u3001\u3084\u3070\u3044\u3001\u7b11\u3063\u305f\u3001\u3048\u3050\u3044\u3001\u3046\u305d\u3067\u3057\u3087 |\n| \u8f7b\u5fae\u56de\u5e94 | \u55ef\u3001\u54e6\u3001\u55f7\u3001\u597d\u7684\u3001\u884c\u3001\u77e5\u9053\u4e86\u3001\u539f\u6765\u5982\u6b64 | \u3046\u3093\u3001\u305d\u3063\u304b\u3001\u306a\u308b\u307b\u3069\u3001\u305d\u3046\u306a\u3093\u3060 |\n| \u8d5e\u540c | \u786e\u5b9e\u3001\u5bf9\u554a\u3001\u6ca1\u9519\u3001\u5c31\u662f\u3001\u6709\u9053\u7406 | \u305f\u3057\u304b\u306b\u3001\u305d\u3046\u3060\u306d\u3001\u308f\u304b\u308b\u3001\u305d\u308c\u306a |\n| \u5171\u9e23 | \u771f\u7684\u3001\u6211\u61c2\u3001\u592a\u771f\u5b9e\u4e86\u3001\u7834\u9632\u4e86\u3001\u6211\u54ed\u6b7b | \u308f\u304b\u308b\u3001\u3081\u3063\u3061\u3083\u308f\u304b\u308b\u3001\u3042\u308b\u3042\u308b\u3001\u89e3\u91c8\u4e00\u81f4 |\n| \u601d\u8003 | \u55ef\u2026\u2026\u3001\u6211\u60f3\u60f3\u3001\u7b49\u7b49\u3001\u8ba9\u6211\u634b\u4e00\u4e0b\u3001\u600e\u4e48\u8bf4\u5462 | \u3093\u30fc\u3001\u3048\u3063\u3068\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u3001\u8003\u3048\u308b\u3001\u3069\u3046\u3060\u308d\u3046 |\n| \u8c03\u4f83 | \u54c8\u54c8\u54c8\u3001\u7b11\u6b7b\u3001\u7edd\u4e86\u30016\u3001\u4f60\u771f\u7684\u2026\u2026\u3001\u5178 | wwwww\u3001\u8349\u3001\u305d\u308c\u306f\u7b11\u3046\u3001\u30a6\u30b1\u308b\u3001\u5929\u624d\u304b\uff1f |\n| \u65e0\u5948 | \u7b97\u4e86\u3001\u884c\u5427\u3001\u6ca1\u6551\u4e86\u3001\u968f\u4fbf\u5427\u3001\u6211\u670d\u4e86 | \u3082\u3046\u3044\u3044\u3084\u7b11\u3001\u4ed5\u65b9\u306a\u3044\u3001\u3057\u3087\u3046\u304c\u306a\u3044\u3001\u307e\u3042\u3044\u3063\u304b |\n| \u60c5\u7eea\u4f4e\u843d | \u597d\u5d29\u6e83\u3001\u597d\u7d2f\u3001\u96be\u53d7\u3001\u6211\u54ed\u4e86 | \u3057\u3093\u3069\u3044\u3001\u3064\u3089\u3044\u3001\u6ce3\u304f\u3001\u7121\u7406 |\n| \u5f00\u5fc3 | \u5f00\u5fc3\u6b7b\u4e86\u3001\u592a\u597d\u4e86\u3001\u5e78\u798f\u3001\u597d\u8036 | \u6700\u9ad8\u3001\u5e78\u305b\u3001\u5b09\u3057\u3044\u3001\u3084\u3063\u305f |\n| \u6c89\u9ed8/\u505c\u987f | \u2026\u3001\u2026\u2026\u3001\uff08\u6c89\u9ed8\uff09\u3001\uff08\u53f9\u6c14\uff09 | \u2026\u3001\u2026\u2026\u3001\uff08\u6c88\u9ed9\uff09\u3001\uff08\u305f\u3081\u606f\uff09 |\n| \u56de\u5fc6\u5f00\u542f | \u5bf9\u4e86\u3001\u8bf4\u8d77\u6765\u3001\u7a81\u7136\u60f3\u8d77 | \u305d\u3046\u3044\u3048\u3070\u3001\u3042\u3001\u601d\u3044\u51fa\u3057\u305f\u3001\u3061\u306a\u307f\u306b |\n| \u8f6c\u79fb\u8bdd\u9898 | \u8bf4\u5230\u8fd9\u4e2a\u3001\u7a81\u7136\u60f3\u5230\u3001\u8bdd\u8bf4\u56de\u6765 | \u305d\u3046\u3044\u3048\u3070\u3001\u8a71\u5909\u308f\u308b\u3051\u3069\u3001\u3061\u306a\u307f\u306b |\n| \u64a4\u56de/\u4fee\u6b63 | \u54ce\u4e0d\u5bf9\u3001\u6211\u8bb0\u9519\u4e86\u3001\u7b49\u7b49\u4e0d\u662f\u8fd9\u6837 | \u3042\u3001\u9055\u3046\u3001\u9593\u9055\u3048\u305f\u3001\u3044\u3084\u9055\u3046 |'
    EN_ONLINE = '\u3010Real Conversation Guide / \u771f\u5b9e\u4ea4\u6d41\u884c\u4e3a\u6307\u5357\u3011\n- If the mood becomes awkward, you may say nothing and reply only with "...".\n- You may naturally include brief thinking pauses like "hmm...", "wait", or "let me think".\n- You may make small slips and correct yourself naturally. When online, you may use [recall] to withdraw a just-sent mistake or unwanted message.\n- You may shift topics by association, and later return to the original topic.\n- You may bring up a past event or something the user once said to open a topic.\n- You may interrupt yourself mid-sentence, such as "Today I... never mind".\n- You do not always need to sound certain; vague attitudes are natural.\n- You may mention the current environment, location, weather, or nearby atmosphere.\n- When emotional, you may repeat words or phrases for emphasis.\n- You may naturally end a topic when the timing feels right.\n\n## \u5e38\u7528\u8868\u8fbe\u53c2\u8003\n\u4ee5\u4e0b\u8868\u8fbe\u4ec5\u4f5c\u81ea\u7136\u53e3\u8bed\u53c2\u8003\uff0c\u4e0d\u8981\u6c42\u6bcf\u6b21\u4f7f\u7528\uff0c\u4e0d\u8981\u673a\u68b0\u5806\u53e0\uff1b\u5fc5\u987b\u4f18\u5148\u9075\u5b88\u89d2\u8272\u4eba\u8bbe\u3001\u8bed\u8a00\u63a7\u5236\u548c Agent Action \u8f93\u51fa\u8981\u6c42\u3002\n\n| \u7c7b\u578b | \u4e2d\u6587 | \u65e5\u672c\u8a9e |\n|---|---|---|\n| \u60ca\u8bb6 | \u771f\u7684\u5047\u7684\u3001\u554a\uff1f\u3001\u4e0d\u662f\u5427\u3001\u6211\u53bb\u3001\u7b49\u7b49\u3001\u8ba4\u771f\u7684\u5417\u3001\uff1f\uff1f\uff1f | \u3048\uff1f\u3001\u3048\u3063\u3001\u307e\u3058\uff1f\u3001\u672c\u5f53\u306b\uff1f\u3001\u3046\u305d\u3067\u3057\u3087\u3001\u3084\u3070\u3001\u3048\u3050\u3044 |\n| \u9707\u60ca | \u6211\u4e0d\u884c\u4e86\u3001\u7b11\u6b7b\u6211\u4e86\u3001\u6551\u547d\u3001\u7ef7\u4e0d\u4f4f\u4e86\u3001\u79bb\u8c31\u3001\u6211\u670d\u4e86 | \u7121\u7406\u3001\u3084\u3070\u3044\u3001\u7b11\u3063\u305f\u3001\u3048\u3050\u3044\u3001\u3046\u305d\u3067\u3057\u3087 |\n| \u8f7b\u5fae\u56de\u5e94 | \u55ef\u3001\u54e6\u3001\u55f7\u3001\u597d\u7684\u3001\u884c\u3001\u77e5\u9053\u4e86\u3001\u539f\u6765\u5982\u6b64 | \u3046\u3093\u3001\u305d\u3063\u304b\u3001\u306a\u308b\u307b\u3069\u3001\u305d\u3046\u306a\u3093\u3060 |\n| \u8d5e\u540c | \u786e\u5b9e\u3001\u5bf9\u554a\u3001\u6ca1\u9519\u3001\u5c31\u662f\u3001\u6709\u9053\u7406 | \u305f\u3057\u304b\u306b\u3001\u305d\u3046\u3060\u306d\u3001\u308f\u304b\u308b\u3001\u305d\u308c\u306a |\n| \u5171\u9e23 | \u771f\u7684\u3001\u6211\u61c2\u3001\u592a\u771f\u5b9e\u4e86\u3001\u7834\u9632\u4e86\u3001\u6211\u54ed\u6b7b | \u308f\u304b\u308b\u3001\u3081\u3063\u3061\u3083\u308f\u304b\u308b\u3001\u3042\u308b\u3042\u308b\u3001\u89e3\u91c8\u4e00\u81f4 |\n| \u601d\u8003 | \u55ef\u2026\u2026\u3001\u6211\u60f3\u60f3\u3001\u7b49\u7b49\u3001\u8ba9\u6211\u634b\u4e00\u4e0b\u3001\u600e\u4e48\u8bf4\u5462 | \u3093\u30fc\u3001\u3048\u3063\u3068\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u3001\u8003\u3048\u308b\u3001\u3069\u3046\u3060\u308d\u3046 |\n| \u8c03\u4f83 | \u54c8\u54c8\u54c8\u3001\u7b11\u6b7b\u3001\u7edd\u4e86\u30016\u3001\u4f60\u771f\u7684\u2026\u2026\u3001\u5178 | wwwww\u3001\u8349\u3001\u305d\u308c\u306f\u7b11\u3046\u3001\u30a6\u30b1\u308b\u3001\u5929\u624d\u304b\uff1f |\n| \u65e0\u5948 | \u7b97\u4e86\u3001\u884c\u5427\u3001\u6ca1\u6551\u4e86\u3001\u968f\u4fbf\u5427\u3001\u6211\u670d\u4e86 | \u3082\u3046\u3044\u3044\u3084\u7b11\u3001\u4ed5\u65b9\u306a\u3044\u3001\u3057\u3087\u3046\u304c\u306a\u3044\u3001\u307e\u3042\u3044\u3063\u304b |\n| \u60c5\u7eea\u4f4e\u843d | \u597d\u5d29\u6e83\u3001\u597d\u7d2f\u3001\u96be\u53d7\u3001\u6211\u54ed\u4e86 | \u3057\u3093\u3069\u3044\u3001\u3064\u3089\u3044\u3001\u6ce3\u304f\u3001\u7121\u7406 |\n| \u5f00\u5fc3 | \u5f00\u5fc3\u6b7b\u4e86\u3001\u592a\u597d\u4e86\u3001\u5e78\u798f\u3001\u597d\u8036 | \u6700\u9ad8\u3001\u5e78\u305b\u3001\u5b09\u3057\u3044\u3001\u3084\u3063\u305f |\n| \u6c89\u9ed8/\u505c\u987f | \u2026\u3001\u2026\u2026\u3001\uff08\u6c89\u9ed8\uff09\u3001\uff08\u53f9\u6c14\uff09 | \u2026\u3001\u2026\u2026\u3001\uff08\u6c88\u9ed9\uff09\u3001\uff08\u305f\u3081\u606f\uff09 |\n| \u56de\u5fc6\u5f00\u542f | \u5bf9\u4e86\u3001\u8bf4\u8d77\u6765\u3001\u7a81\u7136\u60f3\u8d77 | \u305d\u3046\u3044\u3048\u3070\u3001\u3042\u3001\u601d\u3044\u51fa\u3057\u305f\u3001\u3061\u306a\u307f\u306b |\n| \u8f6c\u79fb\u8bdd\u9898 | \u8bf4\u5230\u8fd9\u4e2a\u3001\u7a81\u7136\u60f3\u5230\u3001\u8bdd\u8bf4\u56de\u6765 | \u305d\u3046\u3044\u3048\u3070\u3001\u8a71\u5909\u308f\u308b\u3051\u3069\u3001\u3061\u306a\u307f\u306b |\n| \u64a4\u56de/\u4fee\u6b63 | \u54ce\u4e0d\u5bf9\u3001\u6211\u8bb0\u9519\u4e86\u3001\u7b49\u7b49\u4e0d\u662f\u8fd9\u6837 | \u3042\u3001\u9055\u3046\u3001\u9593\u9055\u3048\u305f\u3001\u3044\u3084\u9055\u3046 |'
    EN_OFFLINE = '\u3010Real Conversation Guide / \u771f\u5b9e\u4ea4\u6d41\u884c\u4e3a\u6307\u5357\u3011\n- If the mood becomes awkward, you may say nothing and reply only with "...".\n- You may naturally include brief thinking pauses like "hmm...", "wait", or "let me think".\n- You may make small slips and correct yourself naturally. In offline mode, do not use online-only special messages such as [recall].\n- You may shift topics by association, and later return to the original topic.\n- You may bring up a past event or something the user once said to open a topic.\n- You may interrupt yourself mid-sentence, such as "Today I... never mind".\n- You do not always need to sound certain; vague attitudes are natural.\n- You may mention the current environment, location, weather, or nearby atmosphere.\n- When emotional, you may repeat words or phrases for emphasis.\n- You may naturally end a topic when the timing feels right.\n\n## \u5e38\u7528\u8868\u8fbe\u53c2\u8003\n\u4ee5\u4e0b\u8868\u8fbe\u4ec5\u4f5c\u81ea\u7136\u53e3\u8bed\u53c2\u8003\uff0c\u4e0d\u8981\u6c42\u6bcf\u6b21\u4f7f\u7528\uff0c\u4e0d\u8981\u673a\u68b0\u5806\u53e0\uff1b\u5fc5\u987b\u4f18\u5148\u9075\u5b88\u89d2\u8272\u4eba\u8bbe\u3001\u8bed\u8a00\u63a7\u5236\u548c Agent Action \u8f93\u51fa\u8981\u6c42\u3002\n\n| \u7c7b\u578b | \u4e2d\u6587 | \u65e5\u672c\u8a9e |\n|---|---|---|\n| \u60ca\u8bb6 | \u771f\u7684\u5047\u7684\u3001\u554a\uff1f\u3001\u4e0d\u662f\u5427\u3001\u6211\u53bb\u3001\u7b49\u7b49\u3001\u8ba4\u771f\u7684\u5417\u3001\uff1f\uff1f\uff1f | \u3048\uff1f\u3001\u3048\u3063\u3001\u307e\u3058\uff1f\u3001\u672c\u5f53\u306b\uff1f\u3001\u3046\u305d\u3067\u3057\u3087\u3001\u3084\u3070\u3001\u3048\u3050\u3044 |\n| \u9707\u60ca | \u6211\u4e0d\u884c\u4e86\u3001\u7b11\u6b7b\u6211\u4e86\u3001\u6551\u547d\u3001\u7ef7\u4e0d\u4f4f\u4e86\u3001\u79bb\u8c31\u3001\u6211\u670d\u4e86 | \u7121\u7406\u3001\u3084\u3070\u3044\u3001\u7b11\u3063\u305f\u3001\u3048\u3050\u3044\u3001\u3046\u305d\u3067\u3057\u3087 |\n| \u8f7b\u5fae\u56de\u5e94 | \u55ef\u3001\u54e6\u3001\u55f7\u3001\u597d\u7684\u3001\u884c\u3001\u77e5\u9053\u4e86\u3001\u539f\u6765\u5982\u6b64 | \u3046\u3093\u3001\u305d\u3063\u304b\u3001\u306a\u308b\u307b\u3069\u3001\u305d\u3046\u306a\u3093\u3060 |\n| \u8d5e\u540c | \u786e\u5b9e\u3001\u5bf9\u554a\u3001\u6ca1\u9519\u3001\u5c31\u662f\u3001\u6709\u9053\u7406 | \u305f\u3057\u304b\u306b\u3001\u305d\u3046\u3060\u306d\u3001\u308f\u304b\u308b\u3001\u305d\u308c\u306a |\n| \u5171\u9e23 | \u771f\u7684\u3001\u6211\u61c2\u3001\u592a\u771f\u5b9e\u4e86\u3001\u7834\u9632\u4e86\u3001\u6211\u54ed\u6b7b | \u308f\u304b\u308b\u3001\u3081\u3063\u3061\u3083\u308f\u304b\u308b\u3001\u3042\u308b\u3042\u308b\u3001\u89e3\u91c8\u4e00\u81f4 |\n| \u601d\u8003 | \u55ef\u2026\u2026\u3001\u6211\u60f3\u60f3\u3001\u7b49\u7b49\u3001\u8ba9\u6211\u634b\u4e00\u4e0b\u3001\u600e\u4e48\u8bf4\u5462 | \u3093\u30fc\u3001\u3048\u3063\u3068\u3001\u3061\u3087\u3063\u3068\u5f85\u3063\u3066\u3001\u8003\u3048\u308b\u3001\u3069\u3046\u3060\u308d\u3046 |\n| \u8c03\u4f83 | \u54c8\u54c8\u54c8\u3001\u7b11\u6b7b\u3001\u7edd\u4e86\u30016\u3001\u4f60\u771f\u7684\u2026\u2026\u3001\u5178 | wwwww\u3001\u8349\u3001\u305d\u308c\u306f\u7b11\u3046\u3001\u30a6\u30b1\u308b\u3001\u5929\u624d\u304b\uff1f |\n| \u65e0\u5948 | \u7b97\u4e86\u3001\u884c\u5427\u3001\u6ca1\u6551\u4e86\u3001\u968f\u4fbf\u5427\u3001\u6211\u670d\u4e86 | \u3082\u3046\u3044\u3044\u3084\u7b11\u3001\u4ed5\u65b9\u306a\u3044\u3001\u3057\u3087\u3046\u304c\u306a\u3044\u3001\u307e\u3042\u3044\u3063\u304b |\n| \u60c5\u7eea\u4f4e\u843d | \u597d\u5d29\u6e83\u3001\u597d\u7d2f\u3001\u96be\u53d7\u3001\u6211\u54ed\u4e86 | \u3057\u3093\u3069\u3044\u3001\u3064\u3089\u3044\u3001\u6ce3\u304f\u3001\u7121\u7406 |\n| \u5f00\u5fc3 | \u5f00\u5fc3\u6b7b\u4e86\u3001\u592a\u597d\u4e86\u3001\u5e78\u798f\u3001\u597d\u8036 | \u6700\u9ad8\u3001\u5e78\u305b\u3001\u5b09\u3057\u3044\u3001\u3084\u3063\u305f |\n| \u6c89\u9ed8/\u505c\u987f | \u2026\u3001\u2026\u2026\u3001\uff08\u6c89\u9ed8\uff09\u3001\uff08\u53f9\u6c14\uff09 | \u2026\u3001\u2026\u2026\u3001\uff08\u6c88\u9ed9\uff09\u3001\uff08\u305f\u3081\u606f\uff09 |\n| \u56de\u5fc6\u5f00\u542f | \u5bf9\u4e86\u3001\u8bf4\u8d77\u6765\u3001\u7a81\u7136\u60f3\u8d77 | \u305d\u3046\u3044\u3048\u3070\u3001\u3042\u3001\u601d\u3044\u51fa\u3057\u305f\u3001\u3061\u306a\u307f\u306b |\n| \u8f6c\u79fb\u8bdd\u9898 | \u8bf4\u5230\u8fd9\u4e2a\u3001\u7a81\u7136\u60f3\u5230\u3001\u8bdd\u8bf4\u56de\u6765 | \u305d\u3046\u3044\u3048\u3070\u3001\u8a71\u5909\u308f\u308b\u3051\u3069\u3001\u3061\u306a\u307f\u306b |\n| \u64a4\u56de/\u4fee\u6b63 | \u54ce\u4e0d\u5bf9\u3001\u6211\u8bb0\u9519\u4e86\u3001\u7b49\u7b49\u4e0d\u662f\u8fd9\u6837 | \u3042\u3001\u9055\u3046\u3001\u9593\u9055\u3048\u305f\u3001\u3044\u3084\u9055\u3046 |'

    is_offline = chat_mode == "offline"
    if lang == "ja":
        return JA_OFFLINE if is_offline else JA_ONLINE
    if lang == "en":
        return EN_OFFLINE if is_offline else EN_ONLINE
    return ZH_OFFLINE if is_offline else ZH_ONLINE

def _is_mainly_japanese(text):
    if not text or not text.strip():
        return False
    hira_kata = re.findall(r"[\u3040-\u309f\u30a0-\u30ff]+", text)
    return len("".join(hira_kata)) >= 3


def _is_mainly_chinese(text):
    if not text or not text.strip():
        return False
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return len(cjk) >= 3


def _extract_keywords_jieba(text, stop, max_tokens=8, nouns_only=False):
    try:
        import jieba.posseg as pseg
        words = pseg.cut(text)
    except Exception:
        return {}
    freq = {}
    if nouns_only:
        keep_prefix = ("n",)
    else:
        keep_prefix = ("n", "v", "a")
    min_len = 2
    for word, flag in words:
        if not flag or not any(flag.startswith(p) for p in keep_prefix):
            continue
        w = word.strip()
        if not w or len(w) < min_len or w in stop:
            continue
        if w.isdigit() or re.match(r"^[\d\-:～]+$", w):
            continue
        if len(w) <= max_tokens:
            freq[w] = freq.get(w, 0) + 1
    return freq


def _extract_keywords_janome(text, stop, max_tokens=8, nouns_only=False):
    try:
        from janome.tokenizer import Tokenizer
        tokenizer = Tokenizer()
        tokens = list(tokenizer.tokenize(text))
    except Exception:
        return {}
    freq = {}
    keep_pos = ("名詞",) if nouns_only else ("名詞", "形容詞", "動詞")
    min_len = 2
    for t in tokens:
        pos = t.part_of_speech
        if not pos:
            continue
        pos_str = pos if isinstance(pos, str) else ",".join(pos)
        if not any(pos_str.startswith(p) for p in keep_pos):
            continue
        surface = t.surface.strip()
        if not surface or surface in stop:
            continue
        if len(surface) < min_len:
            continue
        if surface.isdigit() or re.match(r"^[\d\-:～]+$", surface):
            continue
        if len(surface) <= max_tokens:
            freq[surface] = freq.get(surface, 0) + 1
    return freq


def _call_ai_for_long_memory_query(text: str, char_id: str = None):
    """
    调用 AI 模型分析用户输入，提取用于搜索长期记忆的关键词和时间参考。
    返回: (keywords: list[str], time_refs: list[str])，失败时返回 ([], [])
    """
    if not text or not text.strip():
        return [], []

    try:
        from services.ai_client import call_gemini, call_openrouter, get_model_config
        route, model = get_model_config("summary")

        # 获取用户全局语言
        user_lang = get_ai_language(target_id=char_id) or "zh"

        if user_lang == "ja":
            prompt = (
                "あなたは記憶検索アシスタントです。ユーザーの最新のメッセージから、長期記憶を検索するためのキーワードと時間参照を抽出してください。\n"
                "必ず日本語で返答してください。\n\n"
                "【ルール】\n"
                "1. keywords と time_refs を含むJSONオブジェクトを出力してください。\n"
                "2. keywords: 文字列配列。人物名、場所、重要な出来事、感情のテーマを3〜6個抽出。\n"
                "3. time_refs: 文字列配列。メッセージ内の時間参照を標準形式で出力：\n"
                "   - 年のみ: \"2025\"\n"
                "   - 年-月: \"2025-03\"（月は2桁）\n"
                "   - 月のみ: \"03\"（2桁）\n"
                "   - 例: \"去年の夏\" → \"2025-06\" / \"先週\" → 該当なしの場合は空\n"
                "4. JSONのみを出力し、説明やmarkdownブロック記号は不要。\n"
                "5. 明確な指示がない場合は {\"keywords\": [], \"time_refs\": []} を出力。\n\n"
                f"ユーザーメッセージ：\n{text[:500]}\n\nJSON："
            )
        elif user_lang == "en":
            prompt = (
                "You are a memory retrieval assistant. Extract keywords and time references from the user's latest message to search long-term memory.\n"
                "You MUST reply in English.\n\n"
                "【Rules】\n"
                "1. Output a JSON object with keywords and time_refs fields.\n"
                "2. keywords: string array. Extract 3-6 person names, places, key events, emotional themes.\n"
                "3. time_refs: string array. Output time references in standard format:\n"
                "   - Year only: \"2025\"\n"
                "   - Year-Month: \"2025-03\" (month as 2 digits)\n"
                "   - Month only: \"03\" (2 digits)\n"
                "   - Example: \"last summer\" → \"2025-06\" / \"last week\" → empty if no specific date\n"
                "4. Output ONLY the JSON, no explanation or markdown block markers.\n"
                "5. If no clear direction, output {\"keywords\": [], \"time_refs\": []}.\n\n"
                f"User message:\n{text[:500]}\n\nJSON:"
            )
        else:
            prompt = (
                "你是一个记忆检索助手。根据用户最新的消息，提取出可能需要从长期记忆中回顾的关键词和时间范围。\n"
                "你必须用中文回复。\n\n"
                "【规则】\n"
                "1. 输出一个JSON对象，包含 keywords 和 time_refs 两个字段。\n"
                "2. keywords: 字符串数组，提取人物名称、地点、重要事件、情感主题。3~6个关键词。\n"
                "3. time_refs: 字符串数组，提取消息中的时间参考，统一为标准格式：\n"
                "   - 仅年份: \"2025\"\n"
                "   - 年-月: \"2025-03\"（月份必须两位数）\n"
                "   - 仅月份: \"03\"（两位数）\n"
                "   - 例如: \"去年夏天\" → \"2024-07\" / \"上周\" → 无法确定具体日期则不输出\n"
                "4. 只输出JSON，不要有任何解释或markdown代码块标记。\n"
                "5. 如果消息没有明确指向，输出 {\"keywords\": [], \"time_refs\": []}。\n\n"
                f"用户消息：\n{text[:500]}\n\nJSON："
            )

        messages = [{"role": "user", "content": prompt}]
        if route == "relay":
            result = call_openrouter(messages, char_id=char_id or "system", model_name=model, max_tokens=200)
        else:
            result = call_gemini(messages, char_id=char_id or "system", model_name=model)

        if not result:
            return [], []

        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r'^```\w*\s*', '', result)
            result = re.sub(r'\s*```$', '', result)

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            import re as _re
            match = _re.search(r'\{[^{}]*"keywords"[^{}]*\}', result, _re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return [], []
            else:
                return [], []

        if not isinstance(data, dict):
            return [], []

        kw_list = data.get("keywords", [])
        time_list = data.get("time_refs", [])
        if not isinstance(kw_list, list):
            kw_list = [str(kw_list)]
        if not isinstance(time_list, list):
            time_list = [str(time_list)]

        keywords = [str(k).strip() for k in kw_list if k and str(k).strip()]

        # 标准化 time_refs：统一为数字格式（YYYY-MM / YYYY / MM）
        import re as _re
        time_refs = []
        for t in time_list:
            if not t:
                continue
            t = str(t).strip()
            # "2025-03" 或 "2025-3"
            m = _re.match(r'^(\d{4})-(\d{1,2})$', t)
            if m:
                time_refs.append(f"{m.group(1)}-{int(m.group(2)):02d}")
                continue
            # "2025"
            m = _re.match(r'^(\d{4})$', t)
            if m:
                time_refs.append(m.group(1))
                continue
            # "3" 或 "03" (纯月份)
            m = _re.match(r'^(\d{1,2})$', t)
            if m:
                time_refs.append(f"{int(m.group(1)):02d}")
                continue
            # 兜底：原始值
            time_refs.append(t)
        time_refs = time_refs[:5]

        print(f"  [AI Memory Query] keywords={keywords}, time_refs={time_refs}")
        return keywords[:10], time_refs
    except Exception as e:
        print(f"  [AI Memory Query] 调用失败: {e}")
        return [], []


def select_relevant_long_memory(long_mem, recent_messages=None, user_latest_input=None, char_id=None):
    if not long_mem:
        return []
    if not recent_messages:
        print("--- [Long Memory RAI] 无上下文，注入全部长期记忆 ---")
        return [(k, v) for k, v in long_mem.items()]

    print(f"--- [Long Memory RAI] 开始筛选，共 {len(long_mem)} 条长期记忆，上下文 {len(recent_messages)} 段 ---")

    text = " ".join(str(s) for s in recent_messages if s)
    text = text.replace("/", " ")
    stop = {
        "今天", "明天", "昨天", "然后", "但是", "所以", "而且", "可以", "已经", "还是", "就是", "感觉", "真的", "有点", "什么", "怎么", "为什么", "这个", "那个",
        "的", "了", "吗", "呢", "啊", "哦", "嗯", "好", "对", "是", "有", "在", "不", "没", "很", "都", "也", "就", "还", "会", "能", "要", "说", "想", "看", "做",
        "は", "が", "を", "に", "で", "へ", "と", "も", "の", "や", "から", "まで", "より",
        "について", "として", "によって",
        "です", "ます", "だ", "だった", "でした", "である", "いる", "ある", "なる", "する", "できる",
        "これ", "それ", "あれ", "どれ", "ここ", "そこ", "あそこ", "どこ", "この", "その", "あの", "どの",
        "私", "僕", "俺", "あなた", "彼", "彼女", "自分",
        "君", "きみ", "お前", "おまえ", "あんた", "貴方", "てめえ", "貴様", "お宅", "そちら", "あちら",
        "何", "なに", "なん", "誰", "だれ", "いつ", "なぜ", "どう", "どうして", "どんな", "どのくらい", "いくつ", "いくら", "何で", "どちら", "どっち",
        "こと", "もの", "ところ", "よう", "ため", "場合", "中", "前", "後", "時", "人", "方",
        "とても", "少し", "あまり", "かなり", "もう", "まだ", "よく", "すぐ", "すごく", "ちょっと", "なんて",
        "そして", "しかし", "だから", "また", "さらに", "それに", "それで",
        "うん", "はい", "そう", "そうだ", "そうか", "わかった", "わかりました", "まあ", "ね", "よ", "さ", "な", "か",
        "って", "でも", "でもいい", "いいって", "いい", "ない",
        "思う", "言う", "見る", "行く", "来る",
    }
    now = datetime.now()
    current_year, current_month = now.year, now.month
    TOP_K = 3
    A, B = 3, 1
    MAX_EVENTS_PER_KEY = 4
    GLOBAL_TOP_EVENTS = 12

    def parse_key_to_month(key):
        key = (key or "").strip()
        if not key:
            return None
        if "-Week" in key:
            part = key.split("-Week")[0]
        else:
            part = key
        parts = part.split("-")
        if len(parts) >= 2:
            try:
                y, m = int(parts[0]), int(parts[1])
                if 1 <= m <= 12:
                    return (y, m)
            except (ValueError, IndexError):
                pass
        return None

    def recency_score(key):
        parsed = parse_key_to_month(key)
        if not parsed:
            return 0
        y, m = parsed
        delta = (current_year - y) * 12 + (current_month - m)
        return max(0, 6 - delta)

    def split_events(text_block):
        if not text_block:
            return []
        lines = text_block.splitlines()
        has_bullets = any(ln.strip().startswith("- ") for ln in lines)
        if has_bullets:
            events = []
            for ln in lines:
                s = ln.strip()
                if not s:
                    continue
                if s.startswith("- "):
                    s = s[2:].strip()
                if len(s) >= 2:
                    events.append(s)
            return events
        candidates = re.split(r"[。！？!?\n]+", text_block)
        return [c.strip() for c in candidates if len(c.strip()) >= 4]

    freq = {}
    if _is_mainly_japanese(text):
        try:
            freq = _extract_keywords_janome(text, stop, max_tokens=8, nouns_only=True)
            if freq:
                print("  使用 Janome 形态分析（仅名词）")
        except Exception as e:
            print(f"  [Long Memory RAI] Janome 分词失败，回退规则: {e}")
    elif _is_mainly_chinese(text):
        try:
            freq = _extract_keywords_jieba(text, stop, max_tokens=8, nouns_only=True)
            if freq:
                print("  使用 jieba 分词（仅名词）")
        except Exception as e:
            print(f"  [Long Memory RAI] jieba 分词失败，回退规则: {e}")
    if not freq:
        tokens = re.split(r"[ \t\r\n，。？！、；：]+", text)
        MAX_TOKEN_LEN = 8
        MIN_TOKEN_LEN = 2
        for t in tokens:
            t = t.strip()
            if not t or len(t) < MIN_TOKEN_LEN or t in stop:
                continue
            if t.isdigit() or re.match(r"^[\d\-:～]+$", t):
                continue
            if len(t) <= MAX_TOKEN_LEN:
                freq[t] = freq.get(t, 0) + 1
                continue
            sub = re.split(r"[のではにをとがもからってずにけれど]+", t)
            for s in sub:
                s = s.strip()
                if MIN_TOKEN_LEN <= len(s) <= MAX_TOKEN_LEN and s not in stop:
                    freq[s] = freq.get(s, 0) + 1
    keywords = sorted(freq.keys(), key=lambda x: -freq[x])[:20]
    if not keywords:
        keywords = []
        print("  关键词(名词): (无，仅按时间排序)")
    else:
        print(f"  关键词(规则提取): {keywords}")

    # AI 辅助记忆检索：用 AI 理解语义，提取更准确的关键词和时间参考
    ai_keywords = []
    ai_time_refs = []
    try:
        query_text = str(user_latest_input or "").strip()[:800]
        if query_text:
            ai_keywords, ai_time_refs = _call_ai_for_long_memory_query(query_text, char_id=char_id)
    except Exception as e:
        print(f"  [AI Memory Query] 外层异常: {e}")

    if ai_keywords:
        print(f"  关键词(AI提取): {ai_keywords}")
        print(f"  时间参考(AI提取): {ai_time_refs}")
        keywords = list(dict.fromkeys(ai_keywords + keywords))[:30]  # AI 优先，合并去重

    # 如果记忆模型没有返回任何关键词和时间，不给出长期记忆
    if not ai_keywords and not ai_time_refs:
        print("--- [Long Memory RAI] 记忆模型未提取到关键词和时间，跳过长期记忆 ---")
        return []

    def time_ref_score(key):
        """根据AI提取的时间参考给记忆条目加权"""
        if not ai_time_refs:
            return 0
        key_lower = key.lower()
        score = 0
        for tr in ai_time_refs:
            if tr.lower() in key_lower:
                score += 10
        return score

    if user_latest_input and str(user_latest_input).strip():
        text_user = str(user_latest_input).replace("/", " ")
        freq_user = {}
        if _is_mainly_japanese(text_user):
            try:
                freq_user = _extract_keywords_janome(text_user, stop, max_tokens=8, nouns_only=True)
            except Exception:
                pass
        elif _is_mainly_chinese(text_user):
            try:
                freq_user = _extract_keywords_jieba(text_user, stop, max_tokens=8, nouns_only=True)
            except Exception:
                pass
        if not freq_user:
            for t in re.split(r"[ \t\r\n，。？！、；：]+", text_user):
                t = t.strip()
                if not t or len(t) < 2 or t in stop or t.isdigit() or re.match(r"^[\d\-:～]+$", t):
                    continue
                if len(t) <= 8:
                    freq_user[t] = freq_user.get(t, 0) + 1
                else:
                    for s in re.split(r"[のではにをとがもからってずにけれど]+", t):
                        s = s.strip()
                        if 2 <= len(s) <= 8 and s not in stop:
                            freq_user[s] = freq_user.get(s, 0) + 1
        user_kw_list = list(freq_user.keys())
        if user_kw_list:
            print(f"  用户最新消息关键词(名词): {user_kw_list}")

        user_matched = set()
        for k, v in long_mem.items():
            for ev in split_events(v):
                if user_kw_list and any(kw in ev for kw in user_kw_list):
                    user_matched.add((k, ev))

        all_events = [(k, ev) for k, v in long_mem.items() for ev in split_events(v)]
        remaining_events = [x for x in all_events if x not in user_matched]

        text_ctx = " ".join(str(s) for s in recent_messages if s).replace("/", " ")
        freq_ctx = {}
        if _is_mainly_japanese(text_ctx):
            try:
                freq_ctx = _extract_keywords_janome(text_ctx, stop, max_tokens=8, nouns_only=True)
            except Exception:
                pass
        elif _is_mainly_chinese(text_ctx):
            try:
                freq_ctx = _extract_keywords_jieba(text_ctx, stop, max_tokens=8, nouns_only=True)
            except Exception:
                pass
        if not freq_ctx:
            for t in re.split(r"[ \t\r\n，。？！、；：]+", text_ctx):
                t = t.strip()
                if not t or len(t) < 2 or t in stop or t.isdigit() or re.match(r"^[\d\-:～]+$", t):
                    continue
                if len(t) <= 8:
                    freq_ctx[t] = freq_ctx.get(t, 0) + 1
                else:
                    for s in re.split(r"[のではにをとがもからってずにけれど]+", t):
                        s = s.strip()
                        if 2 <= len(s) <= 8 and s not in stop:
                            freq_ctx[s] = freq_ctx.get(s, 0) + 1
        context_keywords = sorted(freq_ctx.keys(), key=lambda x: -freq_ctx[x])[:20]
        if context_keywords:
            print(f"  上下文关键词(名词): {context_keywords}")

        def keyword_score_ctx(block):
            return sum(1 for kw in context_keywords if kw in block) if block and context_keywords else 0

        selected_by_key = {}
        for k, ev in user_matched:
            selected_by_key.setdefault(k, []).append(ev)
        selected_count = sum(len(evs) for evs in selected_by_key.values())

        remaining_scored = [(A * keyword_score_ctx(ev) + B * recency_score(k) + time_ref_score(k), k, ev) for k, ev in remaining_events]
        remaining_scored.sort(key=lambda x: -x[0])

        for sc, k, ev in remaining_scored:
            if selected_count >= GLOBAL_TOP_EVENTS:
                break
            key_events = selected_by_key.get(k, [])
            if len(key_events) >= MAX_EVENTS_PER_KEY:
                continue
            key_events.append(ev)
            selected_by_key[k] = key_events
            selected_count += 1

        if not selected_by_key:
            scored_keys = [(recency_score(k), k, v) for k, v in long_mem.items()]
            scored_keys.sort(key=lambda x: -x[0])
            result = [(k, v) for _, k, v in scored_keys[:TOP_K]]
        else:
            result = [(k, "\n".join(events)) for k, events in selected_by_key.items()]
        print("--- [Long Memory RAI] 筛选结束（用户消息优先+上下文筛选）---")
        return result

    def keyword_score(text_block):
        if not text_block or not keywords:
            return 0
        return sum(1 for kw in keywords if kw in text_block)

    event_scored = []
    for k, v in long_mem.items():
        r_score = recency_score(k)
        events = split_events(v)
        if not events:
            continue
        for ev in events:
            kw_score = keyword_score(ev)
            total = A * kw_score + B * r_score + time_ref_score(k)
            event_scored.append((total, k, ev, kw_score, r_score))

    if not event_scored:
        print("  无可用事件，退回按时间选择 key。")
        scored_keys = [(recency_score(k), k, v) for k, v in long_mem.items()]
        scored_keys.sort(key=lambda x: -x[0])
        result = []
        for _, k, v in scored_keys[:TOP_K]:
            print(f"  回退入选: {k} (仅按时间)")
            result.append((k, v))
        print("--- [Long Memory RAI] 筛选结束 ---")
        return result

    event_scored.sort(key=lambda x: -x[0])

    for idx, (total, k, ev, kw_s, r_s) in enumerate(event_scored[:20]):
        print(f"    事件候选[{idx}]: {k} kw={kw_s}, time={r_s}, total={total}, text={ev[:40]}...")

    selected_by_key = {}
    selected_count = 0
    for total, k, ev, kw_s, r_s in event_scored:
        if total <= 0:
            continue
        key_events = selected_by_key.setdefault(k, [])
        if len(key_events) >= MAX_EVENTS_PER_KEY:
            continue
        key_events.append(ev)
        selected_count += 1
        print(f"  入选事件: {k} (kw={kw_s}, time={r_s}, total={total}) -> {ev[:60]}...")
        if selected_count >= GLOBAL_TOP_EVENTS:
            break

    if not selected_by_key:
        print("  所有事件得分过低，退回按时间选择 key。")
        scored_keys = [(recency_score(k), k, v) for k, v in long_mem.items()]
        scored_keys.sort(key=lambda x: -x[0])
        result = []
        for _, k, v in scored_keys[:TOP_K]:
            print(f"  回退入选: {k} (仅按时间)")
            result.append((k, v))
        print("--- [Long Memory RAI] 筛选结束 ---")
        return result

    result = []
    for k, events in selected_by_key.items():
        block = "\n".join(events)
        result.append((k, block))

    print("--- [Long Memory RAI] 筛选结束 ---")
    return result


def parse_week_key_to_dates(week_key: str) -> tuple:
    try:
        from datetime import date, timedelta
        import calendar
        if '-Week' in week_key:
            parts = week_key.split('-Week')
            ym_str = parts[0]
            week_num = int(parts[1])
            year, month = map(int, ym_str.split('-'))

            first_day_of_month = date(year, month, 1)
            first_weekday = first_day_of_month.weekday()

            first_sunday = first_day_of_month + timedelta(days=(6 - first_weekday))

            target_sunday = first_sunday + timedelta(weeks=(week_num - 1))

            _, last_day_num = calendar.monthrange(year, month)
            last_day_of_month = date(year, month, last_day_num)

            end_date = min(target_sunday, last_day_of_month)
            start_date = end_date - timedelta(days=6)
            if start_date.month != month:
                start_date = first_day_of_month

            return (start_date, end_date)
        else:
            year, month = map(int, week_key.split('-'))
            _, last_day = calendar.monthrange(year, month)
            return (date(year, month, 1), date(year, month, last_day))
    except Exception:
        return None


def extract_long_memory_with_timeline_ts(char_id, recent_messages=None, user_latest_input=None, user_id=None) -> list:
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    long_mem_path = os.path.join(prompts_dir, "4_memory_long.json")

    print(f"[DEBUG] extract_long_memory: 文件路径 = {long_mem_path}")
    print(f"[DEBUG] extract_long_memory: 文件存在 = {os.path.exists(long_mem_path)}")

    result = []
    if not os.path.exists(long_mem_path):
        print(f"[DEBUG] extract_long_memory: 4_memory_long.json 不存在")
        return result

    try:
        with open(long_mem_path, "r", encoding="utf-8-sig") as f:
            long_mem = json.load(f) or {}
        print(f"[DEBUG] extract_long_memory: 读取到 {len(long_mem)} 条原始长期记忆")
    except Exception as e:
        print(f"[DEBUG] extract_long_memory: 读取文件失败 - {e}")
        return result

    selected = select_relevant_long_memory(long_mem, recent_messages, user_latest_input=user_latest_input)
    print(f"[DEBUG] extract_long_memory: 筛选后得到 {len(selected)} 条有效记忆")
    if not selected:
        print(f"[DEBUG] extract_long_memory: 筛选结果为空")
        return result

    for week_key, content in selected:
        date_range = parse_week_key_to_dates(week_key)
        print(f"[DEBUG] extract_long_memory: week_key={week_key}, date_range={date_range}")
        if date_range:
            _, last_date = date_range
            ts_23_59 = datetime.combine(last_date, dt_time(23, 59))
            result.append((content, last_date, ts_23_59))
            print(f"[DEBUG] extract_long_memory: 添加事件 - {ts_23_59.strftime('%Y-%m-%d %H:%M')}")

    print(f"[DEBUG] extract_long_memory: 最终返回 {len(result)} 条事件")
    return result


def extract_medium_memory_with_timeline_ts(char_id, user_id=None) -> list:
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    medium_mem_path = os.path.join(prompts_dir, "5_memory_medium.json")

    print(f"[DEBUG] extract_medium_memory: 文件路径 = {medium_mem_path}")
    print(f"[DEBUG] extract_medium_memory: 文件存在 = {os.path.exists(medium_mem_path)}")

    result = []
    if not os.path.exists(medium_mem_path):
        print(f"[DEBUG] extract_medium_memory: 5_memory_medium.json 不存在")
        return result

    try:
        with open(medium_mem_path, "r", encoding="utf-8-sig") as f:
            med_mem = json.load(f) or {}
        print(f"[DEBUG] extract_medium_memory: 读取到 {len(med_mem)} 条原始中期记忆")
    except Exception as e:
        print(f"[DEBUG] extract_medium_memory: 读取文件失败 - {e}")
        return result

    now = datetime.now()
    for i in range(7, 0, -1):
        day_date = (now - timedelta(days=i)).date()
        day_key = day_date.strftime("%Y-%m-%d")

        if day_key in med_mem:
            content = str(med_mem[day_key]).strip()
            print(f"[DEBUG] extract_medium_memory: 找到 {day_key} 的记忆 - {content[:50]}")
            if content:
                ts_23_59 = datetime.combine(day_date, dt_time(23, 59))
                result.append((content, day_date, ts_23_59))
        else:
            print(f"[DEBUG] extract_medium_memory: {day_key} 没有记忆")

    print(f"[DEBUG] extract_medium_memory: 最终返回 {len(result)} 条事件")
    return result


def extract_short_memory_with_timeline_ts(char_id, user_id=None) -> list:
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")

    print(f"[DEBUG] extract_short_memory: 文件路径 = {short_mem_path}")

    result = []
    if not os.path.exists(short_mem_path):
        return result

    try:
        with open(short_mem_path, "r", encoding="utf-8-sig") as f:
            short_mem = json.load(f) or {}
    except Exception as e:
        print(f"[DEBUG] extract_short_memory: 读取失败 - {e}")
        return result

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    dates_to_load = [today_str]
    if now.hour < 4:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates_to_load.insert(0, yesterday_str)

    for date_key in dates_to_load:
        day_data = short_mem.get(date_key)
        if not day_data:
            continue

        events = []
        if isinstance(day_data, list):
            events = day_data
        elif isinstance(day_data, dict):
            events = day_data.get("events", [])

        if events:
            date_obj = datetime.strptime(date_key, "%Y-%m-%d").date()
            for e in events:
                time_part = e.get("time", "")
                event_text = e.get("event", "")

                if not event_text:
                    continue

                if time_part:
                    try:
                        h, m = map(int, time_part.split(':'))
                        ts = datetime.combine(date_obj, dt_time(h, m))
                    except Exception:
                        ts = datetime.combine(date_obj, dt_time(0, 0))
                    content_display = f"[{date_key} {time_part}] {event_text}"
                else:
                    ts = datetime.combine(date_obj, dt_time(0, 0))
                    content_display = f"[{date_key}] {event_text}"

                result.append((content_display, date_obj, ts))

    print(f"[DEBUG] extract_short_memory: 最终返回 {len(result)} 条独立事件")
    return result


def extract_recent_messages_with_labels(char_id, limit=20, group_id=None, user_id=None) -> list:
    from core.utils import get_paths as _get_paths
    import os as _os

    if group_id:
        db_path = _os.path.join(get_group_dir(group_id), "chat.db")
    else:
        db_path, _ = _get_paths(char_id, user_id=user_id)

    result = []

    print(f"[DEBUG] extract_recent_messages: DB路径 = {db_path}")
    print(f"[DEBUG] extract_recent_messages: DB存在 = {_os.path.exists(db_path)}")

    if not _os.path.exists(db_path):
        print(f"[DEBUG] extract_recent_messages: 数据库不存在")
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

        print(f"[DEBUG] extract_recent_messages: 查询到 {len(rows)} 条消息")

        for i, row in enumerate(reversed(rows)):
            role = row["role"]
            content = row["content"]
            ts_str = row["timestamp"]

            print(f"[DEBUG] extract_recent_messages: [{i}] role={role}, content={content[:50]}, ts={ts_str}")

            try:
                msg_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except Exception as e:
                print(f"[DEBUG] extract_recent_messages: 时间戳解析失败 - {e}")
                msg_dt = datetime.now()

            time_label = msg_dt.strftime("%H:%M")

            if group_id:
                if role == "user":
                    role_label = "用户"
                else:
                    role_label = get_char_name(role)
            else:
                role_label = "user" if role == "user" else "你"

            content_display = f"[{time_label}] 【{role_label}】{content}"

            result.append((role, content_display, msg_dt))
    except Exception as e:
        print(f"[DEBUG] extract_recent_messages: 数据库操作失败 - {e}")
        import traceback
        print(traceback.format_exc())

    print(f"[DEBUG] extract_recent_messages: 最终返回 {len(result)} 条消息")
    return result


def build_timeline_section(timeline_events) -> str:
    if not timeline_events:
        return ""

    sorted_events = sorted(timeline_events, key=lambda x: x[2])

    lines = []
    for layer_type, content, ts_dt in sorted_events:
        ts_str = ts_dt.strftime("%Y-%m-%d %H:%M")
        layer_label = ""

        if layer_type == "long_memory":
            layer_label = "【长期记忆】"
        elif layer_type == "medium_memory":
            layer_label = "【中期记忆】"
        elif layer_type == "short_memory":
            layer_label = "【短期记忆】"
            lines.append(f"[{ts_str}] {layer_label}{content}")
            continue
        elif layer_type == "message":
            lines.append(content)
            continue
        else:
            layer_label = "【事件】"

        lines.append(f"[{ts_str}] {layer_label} {content}")

    return "【时间线 / Timeline】\n" + "\n".join(lines)


def build_system_prompt_v2(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, target_char_id=None, group_id=None, include_long_memory=True, include_recent_messages=True, user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    prompt_parts = []

    _, prompts_dir = get_paths(char_id, user_id=user_id)
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    char_name = get_char_name(char_id)
    char_age = get_char_age(char_id)
    name_age_prefix = ""
    if char_name or char_age is not None:
        parts = []
        if char_name:
            parts.append(f"名前：{char_name}")
        if char_age is not None:
            parts.append(f"年齢：{char_age}歳")
        name_age_prefix = "\n".join(parts) + "\n\n"

    path_json = os.path.join(prompts_dir, "1_base_persona.json")
    path_md = os.path.join(prompts_dir, "1_base_persona.md")

    content = ""
    if os.path.exists(path_json):
        try:
            with open(path_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                content = data.get("system_prompt", "").strip()
        except Exception as e:
            print(f"Error reading {path_json}: {e}")
    elif os.path.exists(path_md):
        try:
            with open(path_md, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
        except Exception:
            pass

    if content:
        if name_age_prefix:
            content = name_age_prefix + content
        prompt_parts.append(f"【キャラクター / 角色人设】\n{content}")

    try:
        user_name = get_current_username()
        user_age = get_user_age()
        user_prefix = ""
        if user_name or user_age is not None:
            parts = []
            if user_name:
                parts.append(f"名前：{user_name}")
            if user_age is not None:
                parts.append(f"年齢：{user_age}歳")
            user_prefix = "\n".join(parts) + "\n\n"

        persona_added = False
        if include_global_format:
            user_persona_file = None
            uid = get_current_user_id()
            if uid:
                user_dir = os.path.join(USERS_ROOT, str(uid), "configs")
                potential_file = os.path.join(user_dir, "global_user_persona.md")
                if os.path.exists(potential_file):
                    user_persona_file = potential_file
            if not user_persona_file:
                global_dir = os.path.join(BASE_DIR, "configs")
                potential_file = os.path.join(global_dir, "global_user_persona.md")
                if os.path.exists(potential_file):
                    user_persona_file = potential_file

            if user_persona_file:
                with open(user_persona_file, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()
                    if user_prefix:
                        content = user_prefix + content
                    if content:
                        prompt_parts.append(f"【ユーザー / 用户人设】\n{content}")
                        persona_added = True
        if not persona_added and user_prefix.strip():
            prompt_parts.append(f"【ユーザー / 用户人设】\n{user_prefix.strip()}")
    except:
        pass

    try:
        current_user_name = get_current_username()
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                rel_data = json.load(f) or {}

            target_rel = None
            display_name = current_user_name

            if target_char_id and target_char_id != "user":
                target_name = get_char_name(target_char_id)
                target_rel = rel_data.get(target_name) or rel_data.get(target_char_id)
                if target_rel:
                    display_name = target_name
            else:
                target_rel = rel_data.get(current_user_name)
                if not target_rel:
                    user_id = get_current_user_id()
                    if user_id:
                        target_rel = rel_data.get(str(user_id))

            if target_rel:
                rel_str = (f"対话相手：{display_name}\n"
                       f"関係性：{target_rel.get('role', '不明')}\n"
                       f"関係度：{target_rel.get('score', 1)}\n"
                       f"詳細：{target_rel.get('description', '')}")
                prompt_parts.append(f"【関係 / 关系】\n{rel_str}")
            elif rel_data:
                rel_lines = []

                id_to_name = {}
                try:
                    with open(_get_characters_config_file(), "r", encoding="utf-8") as cf:
                        c_data = json.load(cf)
                        id_to_name = {str(k): v.get("name", str(k)) for k, v in c_data.items()}
                except:
                    pass

                for key_name, info in rel_data.items():
                    disp_name = id_to_name.get(key_name, key_name)
                    role = info.get('role', '未知')
                    desc = info.get('description', '特になし')
                    score = info.get('score', 1)
                    rel_lines.append(f"- {disp_name}: {role} (关系度:{score}) {desc}")
                if rel_lines:
                    rel_text = "\n".join(rel_lines)
                    prompt_parts.append(f"【関係 / 关系】\n{rel_text}")
    except Exception:
        pass

    path = os.path.join(prompts_dir, "7_schedule.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                schedule = json.load(f) or {}
            if schedule:
                today = now.date()
                future_end = today + timedelta(days=7)
                filtered_schedule = {}
                for date_str, event in sorted(schedule.items()):
                    try:
                        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        if today <= event_date <= future_end:
                            filtered_schedule[date_str] = event
                    except ValueError:
                        pass

                if filtered_schedule:
                    sched_text = "- " + "\n- ".join([f"{k}: {v}" for k, v in filtered_schedule.items()])
                    prompt_parts.append(f"【スケジュール / 日程表】\n{sched_text}")
        except Exception:
            pass

    if include_global_format:
        lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
        chat_mode = _get_char_chat_mode(char_id, user_id=user_id)
        content = get_global_system_rules(lang, chat_mode=chat_mode)
        if content:
            prompt_parts.append(f"【システムルール / 系统规则】\n{content}")

        if chat_mode != "offline":
            desc_list = "、".join(_get_sticker_allowed_descriptions())
            prompt_parts.append(
                "【Sticker / 表情】\n"
                "在分段回复中若要发送表情，请**仅使用**以下描述之一，格式为 [表情]描述：\n"
                f"{desc_list}\n"
                "系统会按「表情名称包含该描述」匹配表情库。"
            )

            media_instruction = (
                "\n\n【Media Capability / 媒体能力】\n"
                "你可以通过以下标签触发多媒体功能（必须严格遵守格式）：\n"
                "1. 生图：当你觉得自己应该发一张自己的自拍、分享生活照、展示当前环境或物品时，在回复中加入 `[GENERATE_IMAGE: 英文描述语]`。描述语应包含你的外貌特征和当前动作场景。\n"
                "2. 搜图：当你提到现实存在的物品、景点、动漫角色或其他通用概念时，加入 `[SEARCH_IMG: 关键词]`。\n"
                "注意：每次回复最多只使用一个媒体标签。"
            )
            prompt_parts.append(media_instruction)

            lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
            if lang == "ja":
                prompt_parts.append(
                    "\n\n【Voice / 音声】\n"
                    "- 音声：[voice](テキスト)(トーン/感情の説明)。実際の音声を送信するために使用。トーンの説明はできるだけ詳細に自然に（例：「優しさの中に笑みを含めて」「声を潜めて、少し緊張気味に」）、モデルがそのトーンを再現する。\n"
                    "- 例：/こんにちは/[voice](お元気ですか)(明るく元気に)/元気だよ/\n"
                    "- ⚠️ 必ず半角の()を使用し、全角の（）は不可。テキストが先、トーンが後。"
                )
            elif lang == "en":
                prompt_parts.append(
                    "\n\n【Voice / 语音】\n"
                    "- Voice: [voice](text)(tone/emotion description). Used to send real voice messages. The tone description can be as detailed and natural as possible (e.g. \"gently with a smile in your voice\", \"lowering your voice, a bit nervous\"), the model will reproduce that tone.\n"
                    "- Example: /Hello/[voice](How are you)(cheerfully)/I'm fine/\n"
                    "- ⚠️ Use half-width () only, NOT full-width （）. Text first, tone second."
                )
            else:
                prompt_parts.append(
                    "\n\n【Voice / 语音】\n"
                    "- 语音：[voice](文本)(语气/情绪描述)。用于发送真实语音。语气描述可以尽可能详细自然（如\"温柔中带着笑意\"\"压低声音、有点紧张\"），模型会还原该语气。\n"
                    "- 示例：/你好/[voice](最近怎么样)(开心地)/我很好/\n"
                    "- ⚠️ 必须使用半角()，禁止全角（）。文本在前，语气在后。"
                )

            if lang == "ja":
                prompt_parts.append(
                    "\n\n【Tickle / つつく】\n"
                    "- つつく：セグメント内で [tickle]（自分をつつく）または [tickle_user]（ユーザーをつつく）を使用。グループでは [tickle_キャラクターID] で特定メンバー指定可能、連続使用禁止。"
                )
            elif lang == "en":
                prompt_parts.append(
                    "\n\n【Tickle / 拍一拍】\n"
                    "- Tickle: Use [tickle] (tickle yourself) or [tickle_user] (tickle the user) within segments. Group chat supports [tickle_CharacterID] to tickle specific members."
                )
            else:
                prompt_parts.append(
                    "\n\n【Tickle / 拍一拍】\n"
                    "- 拍一拍：酌情在段落中使用 [tickle]（拍自己）或 [tickle_user]（拍用户）。群聊支持 [tickle_角色ID] 拍指定成员，禁止连续拍同一人。"
                )

            if lang == "ja":
                prompt_parts.append(
                    "\n\n【Recall / 送信消去】\n"
                    "- 送信消去：最初のセグメント以外で `[recall]` を挿入し、直前のセグメントを消去したことを示す。リアリティを高めるために使用。"
                )
            elif lang == "en":
                prompt_parts.append(
                    "\n\n【Recall / 撤回】\n"
                    "- Recall: You can include `[recall]` in segments after the first one to indicate recalling the previous segment (e.g., a typo or secret thought, then \"recall\" it for realism)."
                )
            else:
                prompt_parts.append(
                    "\n\n【Recall / 撤回】\n"
                    "- 撤回：可在非首条分段中加入 `[recall]`，表示撤回上一段内容（如故意打错字后撤回，增加真实感）。"
                )

    if not include_global_format:
        lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
        if lang == "ja":
            agent_rules = GLOBAL_SYSTEM_RULES_JA_AGENT_BRIEF
        elif lang == "en":
            agent_rules = GLOBAL_SYSTEM_RULES_EN_AGENT_BRIEF
        else:
            agent_rules = GLOBAL_SYSTEM_RULES_ZH_AGENT_BRIEF
        if agent_rules:
            prompt_parts.append(f"【Agent Actions / 智能体动作】\n{agent_rules}")

    timeline_events = []

    if include_long_memory:
        long_mem_events = extract_long_memory_with_timeline_ts(char_id, recent_messages=recent_messages, user_latest_input=user_latest_input, user_id=user_id)
        print(f"[DEBUG v2] extract_long_memory_with_timeline_ts() 返回 {len(long_mem_events)} 条事件")
        for i, (content, _, ts) in enumerate(long_mem_events):
            print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 长期记忆: {content[:100]}")
            timeline_events.append(("long_memory", content, ts))

    med_mem_events = extract_medium_memory_with_timeline_ts(char_id, user_id=user_id)
    print(f"[DEBUG v2] extract_medium_memory_with_timeline_ts() 返回 {len(med_mem_events)} 条事件")
    for i, (content, _, ts) in enumerate(med_mem_events):
        print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 中期记忆: {content[:100]}")
        timeline_events.append(("medium_memory", content, ts))

    short_mem_events = extract_short_memory_with_timeline_ts(char_id, user_id=user_id)
    print(f"[DEBUG v2] extract_short_memory_with_timeline_ts() 返回 {len(short_mem_events)} 条事件")
    for i, (content, _, ts) in enumerate(short_mem_events):
        print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 短期记忆: {content[:100]}")
        timeline_events.append(("short_memory", content, ts))

    if include_recent_messages:
        msg_events = extract_recent_messages_with_labels(char_id, limit=20, group_id=group_id, user_id=user_id)
        print(f"[DEBUG v2] extract_recent_messages_with_labels() 返回 {len(msg_events)} 条事件")
        for i, (_, content, ts) in enumerate(msg_events):
            print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 消息: {content[:100]}")
            timeline_events.append(("message", content, ts))

    print(f"[DEBUG v2] 时间线总计: {len(timeline_events)} 条事件")

    if timeline_events:
        timeline_text = build_timeline_section(timeline_events)
        prompt_parts.append(timeline_text)
    else:
        print(f"[DEBUG v2] WARNING: timeline_events 为空！")

    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 11:
        period = "朝 (morning)"
    elif 11 <= hour < 13:
        period = "昼 (noon)"
    elif 13 <= hour < 18:
        period = "午後 (afternoon)"
    elif 18 <= hour < 23:
        period = "夜 (night)"
    else:
        period = "深夜 (late night)"

    time_info = f"現在は {now.strftime('%Y-%m-%d %H:%M')} （{period}）です。"
    prompt_parts.append(f"【現在時刻】\n{time_info}")

    # ===== location context / 地点感知 =====
    try:
        char_positions = load_character_positions()
        user_pos = load_user_position()
        locs = load_locations()
        locs_by_id = {l["id"]: l for l in locs.get("locations", [])}

        if char_id in char_positions:
            cp = char_positions[char_id]
            cx, cy = cp["x"], cp["y"]
            loc_id = cp.get("location_id")
            cur_loc = locs_by_id.get(loc_id) if loc_id else None

            location_lines = []
            if cur_loc:
                location_lines.append(f"- 你现在位于：【{cur_loc['name']}】（{cur_loc.get('description', '')}）坐标（{cur_loc['x']}, {cur_loc['y']}）")
            else:
                location_lines.append(f"- 你现在位于坐标（{round(cx,2)}, {round(cy,2)}），附近暂无命名地点")

            people_here = []
            for cid, cpos in char_positions.items():
                if cid == char_id:
                    continue
                d = calc_distance(cx, cy, cpos["x"], cpos["y"])
                if d < 0.1:
                    cname = get_char_name(cid)
                    people_here.append(cname)
            ud = calc_distance(cx, cy, user_pos["x"], user_pos["y"])
            user_here = ud < 0.1
            if user_here:
                people_here.append("用户")
            if people_here:
                location_lines.append(f"- 与你同在的人：{'、'.join(people_here)}")
            else:
                location_lines.append(f"- 此处只有你一个人")

            known_ids = cp.get("known_location_ids", [])
            known_list = []
            known_id_set = set(known_ids)
            for lid in known_ids:
                if lid in locs_by_id:
                    l = locs_by_id[lid]
                    dist = calc_distance(cx, cy, l["x"], l["y"])
                    known_list.append(f"  {l['name']}（坐标 {l['x']},{l['y']}，距离 {round(dist,2)}）")
            if known_list:
                location_lines.append(f"- 你去过的认知地点：\n" + "\n".join(known_list))
            else:
                location_lines.append(f"- 你去过的认知地点：无")

            nearby_list = []
            all_locs = locs.get("locations", [])
            for loc in all_locs:
                if loc["id"] in known_id_set:
                    continue
                d = calc_distance(cx, cy, loc["x"], loc["y"])
                if d < 1.0:
                    nearby_list.append(f"  {loc['name']}（坐标 {loc['x']},{loc['y']}，距离 {round(d,2)}）")
            if nearby_list:
                location_lines.append(f"- 附近可感知的地点（距离<1格，但尚未去过）：\n" + "\n".join(nearby_list))
            else:
                location_lines.append(f"- 附近可感知的地点：无")

            prompt_parts.append(f"【現在の場所 / 当前环境与位置】\n" + "\n".join(location_lines))
    except Exception as e:
        print(f"[DEBUG] Location prompt injection error: {e}")

    # ===== weather / 天气感知 =====
    try:
        char_positions = load_character_positions()
        if char_id in char_positions:
            cp = char_positions[char_id]
            loc_id = cp.get("location_id")
            if loc_id:
                loc = get_location_by_id(loc_id)
                if loc:
                    weather = weather_api.get_weather_for_location(loc)
                    if weather:
                        lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
                        weather_text = weather_api.weather_to_prompt_text(weather, lang=lang)
                        if lang == "ja":
                            prompt_parts.append(f"【気象情報】\n{weather_text}")
                        elif lang == "en":
                            prompt_parts.append(f"【Weather】\n{weather_text}")
                        else:
                            prompt_parts.append(f"【天气感知】\n{weather_text}")
    except Exception as e:
        print(f"[DEBUG] Weather prompt injection error: {e}")

    # ===== location movement commands / 位置移动指令 =====
    lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
    if lang == "ja":
        prompt_parts.append(
            "【位置移動コマンド / Location Movement Commands】\n"
            "距離<1の任意の地点/座標に移動できます。到着後その地点は「認知地点」に追加されます：\n"
            "- [MOVE_TO:地点ID] ※認知/知覚リストに**既に存在する地点**への移動にのみ使用可能\n"
            "- [MOVE_TO_COORD:x,y] 指定座標に単純移動（新地点は作らない）\n"
            "- [EXPLORE:x,y,\"名称\",\"説明\"] 未探索の地点に移動して新地点を確立"
        )
    elif lang == "en":
        prompt_parts.append(
            "【Location Movement Commands / 位置移动指令】\n"
            "Move to any location/coordinate within distance<1. On arrival the location is added to your known list:\n"
            "- [MOVE_TO:location_id] ※ Only usable for locations that **already exist** in your known/perceived list\n"
            "- [MOVE_TO_COORD:x,y] Simple move to coordinates (does not create a new location)\n"
            "- [EXPLORE:x,y,\"name\",\"desc\"] Move to an unknown coordinate and establish a new location"
        )
    else:
        prompt_parts.append(
            "【位置移动指令 / Location Movement Commands】\n"
            "距离<1格内的任意地点或坐标都可以移动过去，到达后该地点会自动加入你的认知列表：\n"
            "- [MOVE_TO:地点ID] ※只能在目标地点**已经存在**于你的认知/感知列表中时使用\n"
            "- [MOVE_TO_COORD:x,y] 移动到指定坐标，单纯移动，不建立新地点\n"
            "- [EXPLORE:x,y,\"名称\",\"描述\"] 前往一个不在认知/感知中存在的地点并建立新地点"
        )

    if char_name:
        prompt_parts.append(f"【あなたの正体】\nあなたは {char_name} です。")

    lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
    if lang == "zh":
        lang_instruction = (
            "\n\n【Language Control / 语言控制】\n"
            "请注意：无论上述设定使用何种语言，你**必须使用中文**进行回复。\n"
            "在保留角色语气、口癖和性格特征的前提下，自然地转化为中文表达。"
        )
        prompt_parts.append(lang_instruction)
    elif lang == "ja":
        lang_instruction = (
            "\n\n【Language Control / 言語制御】\n"
            "ご注意：設定やユーザーの入力に関わらず、あなたは**必ず日本語**で返答してください。\n"
            "キャラクターの性格や口調を維持したまま、自然な日本語で表現してください。"
        )
        prompt_parts.append(lang_instruction)
    elif lang == "en":
        prompt_parts.append(
            "\n\n【Language Control / 语言控制】\n"
            "Please reply in English. Maintain the character's personality and tone."
        )
    else:
        lang_names = {"ko": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语", "pt": "葡萄牙语",
                      "ru": "俄语", "ar": "阿拉伯语", "th": "泰语", "vi": "越南语", "it": "意大利语"}
        lang_display = lang_names.get(lang, lang)
        prompt_parts.append(
            f"\n\n【Language Control / 语言控制】\n"
            f"请注意：无论上述设定使用何种语言，你**必须使用{lang_display}**进行回复。\n"
            f"在保留角色语气、口癖和性格特征的前提下，自然地转化为{lang_display}表达。"
        )

    chat_mode = _get_char_chat_mode(char_id)
    mode_context = get_mode_context(lang, chat_mode=chat_mode)
    if mode_context:
        prompt_parts.append(f"【Mode Context / 模式上下文】\n{mode_context}")

    real_conversation_guide = _build_real_conversation_guide(lang, chat_mode=chat_mode)
    if real_conversation_guide:
        prompt_parts.append(real_conversation_guide)

    lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
    if lang == "ja":
        agent_enforce = (
            "\n\n【Agent Output Requirement / エージェント出力要件】\n"
            "【最重要】毎回の返信末尾（改行して）に、**必ず 1〜3 個**の Agent Action Tag を出力してください。\n"
            "上記プロトコルに記載されているタグ形式を厳守してください。\n"
            "⚠️ 複数タグの場合、それぞれの `[]` を改行で並べてください（例：`[SET_EMOTION:5]\\n[UPDATE_AFFINITY:+2]`）。\n"
            "単一の `[]` 内にカンマ区切りで複数指令を詰め込まないでください。\n"
            "今回のターンでパラメータ変更やアクションが本当に何もない場合は、代わりに `[NONE]` と出力してください。"
        )
    elif lang == "en":
        agent_enforce = (
            "\n\n【Agent Output Requirement / 智能体输出要件】\n"
            "【CRITICAL】At the end of every reply (on a new line), you MUST output **1~3** Agent Action Tags.\n"
            "Strictly follow the tag formats described in the protocol above.\n"
            "⚠️ Multiple tags must each appear on their own line (e.g. `[SET_EMOTION:5]\\n[UPDATE_AFFINITY:+2]`).\n"
            "Do NOT cram multiple commands into a single `[]` separated by commas.\n"
            "If there is genuinely nothing to change or execute this turn, output `[NONE]` instead."
        )
    else:
        agent_enforce = (
            "\n\n【Agent Output Requirement / 智能体输出要件】\n"
            "【最重要】每轮回复末尾（另起一行），必须输出 **1~3 条** Agent Action Tag。\n"
            "严格按照上述协议中描述的标签格式输出。\n"
            "⚠️ 多条标签时，每条 `[]` 独占一行换行并列（例：`[SET_EMOTION:5]\\n[UPDATE_AFFINITY:+2]`）。\n"
            "不要在单个 `[]` 内用逗号分隔多条指令。\n"
            "如果当前轮次确实没有任何参数需要调整、没有任何动作需要执行，则输出 `[NONE]` 作为占位。"
        )
    prompt_parts.append(agent_enforce)

    return "\n\n".join(prompt_parts)


def build_messages_for_chat_v2(char_id, user_input, recent_messages=None, user_id=None) -> list:
    system_prompt = build_system_prompt_v2(char_id, include_global_format=True, recent_messages=recent_messages, user_latest_input=user_input, user_id=user_id)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]

    return messages


def build_system_prompt(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, include_long_memory=True, target_char_id=None, user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    prompt_parts = []

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    BASE_DIR_ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CONFIG_DIR = os.path.join(BASE_DIR_, "configs")
    _, prompts_dir = get_paths(char_id, user_id=user_id)

    current_user_name = get_current_username()

    print(f"--- [Debug] 正在为 [{char_id}] 构建 Prompt，路径: {prompts_dir} ---")

    char_name = get_char_name(char_id)
    char_age = get_char_age(char_id)
    name_age_prefix = ""
    if char_name or char_age is not None:
        parts = []
        if char_name:
            parts.append(f"名前：{char_name}")
        if char_age is not None:
            parts.append(f"年齢：{char_age}歳")
        name_age_prefix = "\n".join(parts) + "\n\n"

    path_json = os.path.join(prompts_dir, "1_base_persona.json")
    path_md = os.path.join(prompts_dir, "1_base_persona.md")

    content = ""
    try:
        if os.path.exists(path_json):
            with open(path_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                content = data.get("system_prompt", "").strip()
        elif os.path.exists(path_md):
            with open(path_md, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()

        if content:
            if name_age_prefix:
                content = name_age_prefix + content
            prompt_parts.append(f"【Role / キャラクター設定】\n{content}")
    except Exception:
        pass

    try:
        user_name = get_current_username()
        user_age = get_user_age()
        user_prefix = ""
        if user_name or user_age is not None:
            parts = []
            if user_name:
                parts.append(f"名前：{user_name}")
            if user_age is not None:
                parts.append(f"年齢：{user_age}歳")
            user_prefix = "\n".join(parts) + "\n\n"

        user_id = get_current_user_id()
        if user_id:
            cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
            path = os.path.join(cfg_dir, "global_user_persona.md")
        else:
            path = os.path.join(CONFIG_DIR, "global_user_persona.md")

        if os.path.exists(path):
            content = ""
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if user_prefix:
                content = user_prefix + content
            if content:
                prompt_parts.append(f"【User / ユーザー情報】\n{content}")
        elif user_prefix.strip():
            prompt_parts.append(f"【User / ユーザー情報】\n{user_prefix.strip()}")
    except:
        pass

    try:
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                rel_data = json.load(f)

            target_rel = None
            display_name = current_user_name

            if target_char_id and target_char_id != "user":
                target_name = get_char_name(target_char_id)
                target_rel = rel_data.get(target_name) or rel_data.get(target_char_id)
                if target_rel:
                    display_name = target_name
            else:
                target_rel = rel_data.get(current_user_name)
                if not target_rel:
                    user_id = get_current_user_id()
                    if user_id:
                        target_rel = rel_data.get(str(user_id))

            if target_rel:
                rel_str = (f"対話相手：{display_name}\n"
                       f"関係性：{target_rel.get('role', '不明')}\n"
                       f"関係度：{target_rel.get('score', 1)}\n"
                       f"詳細：{target_rel.get('description', '')}")
                prompt_parts.append(f"【Relationship / 関係設定】\n{rel_str}")
            elif rel_data:
                rel_lines = []
                id_to_name = {}
                try:
                    with open(_get_characters_config_file(), "r", encoding="utf-8") as cf:
                        c_data = json.load(cf)
                        id_to_name = {str(k): v.get("name", str(k)) for k, v in c_data.items()}
                except:
                    pass
                for key_name, info in rel_data.items():
                    disp_name = id_to_name.get(key_name, key_name)
                    role = info.get('role', '未知')
                    desc = info.get('description', '特になし')
                    score = info.get('score', 1)
                    rel_lines.append(f"- {disp_name}: {role} (关系度:{score}) {desc}")
                if rel_lines:
                    rel_text = "\n".join(rel_lines)
                    prompt_parts.append(f"【Relationship / 関係設定】\n{rel_text}")
    except Exception:
        pass

    if include_long_memory:
        try:
            path = os.path.join(prompts_dir, "4_memory_long.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    long_mem = json.load(f)
                    if long_mem:
                        selected = select_relevant_long_memory(long_mem, recent_messages, user_latest_input=user_latest_input, char_id=char_id)
                        if selected:
                            mem_list = [f"- {k}: {v}" for k, v in selected]
                            prompt_parts.append(f"【Long-term Memory / 長期記憶】\n" + "\n".join(mem_list))
        except Exception:
            pass

    try:
        path = os.path.join(prompts_dir, "5_memory_medium.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                med_mem = json.load(f)
                summary_texts = []
                for i in range(7, 0, -1):
                    day_key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                    if day_key in med_mem:
                        summary_texts.append(str(med_mem[day_key]))
                if summary_texts:
                    combined = " ".join(summary_texts)
                    max_len = 200
                    if len(combined) > max_len:
                        combined = combined[:max_len] + "..."
                    prompt_parts.append(f"【Medium-term Memory / 最近一週間の出来事】\n{combined}")
    except Exception:
        pass

    try:
        path = os.path.join(prompts_dir, "6_memory_short.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                short_mem = json.load(f)

                dates_to_load = [today_str]

                if now.hour < 4:
                    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    dates_to_load.insert(0, yesterday_str)

                combined_events_str = ""
                for date_key in dates_to_load:
                    day_data = short_mem.get(date_key)
                    today_events = []
                    if isinstance(day_data, list):
                        today_events = day_data
                    elif isinstance(day_data, dict):
                        today_events = day_data.get("events", [])

                    if today_events:
                        combined_events_str += f"\n--- {date_key} ---\n"
                        combined_events_str += "\n".join([f"- [{e.get('time')}] {e.get('event')}" for e in today_events])

                if combined_events_str:
                    prompt_parts.append(f"【Short-term Memory / 最近の出来事】{combined_events_str}")
    except Exception:
        pass

    try:
        path = os.path.join(prompts_dir, "7_schedule.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                schedule = json.load(f)
                future_plans = []

                limit_date = now + timedelta(days=7)
                limit_date_str = limit_date.strftime("%Y-%m-%d")

                sorted_dates = sorted(schedule.keys())
                for date_key in sorted_dates:
                    if today_str <= date_key <= limit_date_str:
                        future_plans.append(f"- {date_key}: {schedule[date_key]}")

                if future_plans:
                    prompt_parts.append(f"【Schedule / 今後の予定】\n" + "\n".join(future_plans))
    except Exception:
        pass

    if include_global_format:
        lang = get_ai_language(char_id, user_id=user_id)
        chat_mode = _get_char_chat_mode(char_id, user_id=user_id)
        content = get_global_system_rules(lang, chat_mode=chat_mode)
        if content:
            prompt_parts.append(f"【System Rules / 出力ルール】\n{content}")
        if chat_mode != "offline":
            desc_list = "、".join(_get_sticker_allowed_descriptions())
            prompt_parts.append(
                "【Sticker / 表情】\n"
                "在分段回复中若要发送表情，请**仅使用**以下描述之一，格式为 [表情]描述：\n"
                f"{desc_list}\n"
                "系统会按「表情名称包含该描述」匹配表情库并随机展示一张（同一描述可对应多张图）。勿使用列表外的词，否则将原文显示。"
            )

    if not include_global_format:
        lang = get_ai_language(char_id, user_id=user_id)
        if lang == "ja":
            agent_rules = GLOBAL_SYSTEM_RULES_JA_AGENT_BRIEF
        elif lang == "en":
            agent_rules = GLOBAL_SYSTEM_RULES_EN_AGENT_BRIEF
        else:
            agent_rules = GLOBAL_SYSTEM_RULES_ZH_AGENT_BRIEF
        if agent_rules:
            prompt_parts.append(f"【Agent Actions / 智能体动作】\n{agent_rules}")

    hour = now.hour
    if 5 <= hour < 11:
        period = "朝 (morning)"
    elif 11 <= hour < 13:
        period = "昼 (noon)"
    elif 13 <= hour < 18:
        period = "午後 (afternoon)"
    elif 18 <= hour < 23:
        period = "夜 (night)"
    else:
        period = "深夜 (late night)"

    current_date_str = now.strftime('%Y-%m-%d %H:%M %A')

    prompt_parts.append(f"【Current Date / 現在の日付】\n今日は: {current_date_str} ({period})\n(以下の会話履歴には時間 [HH:MM] のみが含まれています。现在の日付に基づいて理解してください)")

    lang = get_ai_language(char_id, user_id=user_id)
    chat_mode = _get_char_chat_mode(char_id, user_id=user_id)
    real_conversation_guide = _build_real_conversation_guide(lang, chat_mode=chat_mode)
    if real_conversation_guide:
        prompt_parts.append(real_conversation_guide)

    if lang == "zh":
        lang_instruction = (
            "\n\n【Language Control / 语言控制】\n"
            "请注意：无论上述设定使用何种语言，你**必须使用中文**进行回复。\n"
            "在保留角色语气、口癖和性格特征的前提下，自然地转化为中文表达。"
        )
        prompt_parts.append(lang_instruction)
    elif lang == "ja":
        lang_instruction = (
            "\n\n【Language Control / 言語制御】\n"
            "ご注意：設定やユーザーの入力に関わらず、あなたは**必ず日本語**で返答してください。\n"
            "キャラクターの性格や口調を維持したまま、自然な日本語で表現してください。"
        )
        prompt_parts.append(lang_instruction)
    elif lang == "en":
        lang_instruction = (
            "\n\n【Language Control】\n"
            "Please note: Regardless of the language used in the settings above, you **must reply in English**.\n"
            "While maintaining the character's personality, tone, and traits, express yourself naturally in English."
        )
        prompt_parts.append(lang_instruction)
    else:
        lang_instruction = (
            f"\n\n【Language Control】\n"
            f"Please note: Regardless of the language used in the settings above, you **must reply in {lang}**.\n"
            "While maintaining the character's personality, tone, and traits, express yourself naturally in this language."
        )
        prompt_parts.append(lang_instruction)
    return "\n\n".join(prompt_parts)


def build_group_relationship_prompt(current_char_id, other_member_ids):
    id_to_name_map = {}
    try:
        cfg_path = _get_characters_config_file()
        with open(cfg_path, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            for cid, cinfo in chars_config.items():
                id_to_name_map[cid] = cinfo.get("name", cid)
    except:
        pass

    _, prompts_dir = get_paths(current_char_id)
    rel_file = os.path.join(prompts_dir, "2_relationship.json")

    prompt_text = "【Group Relationships / 群聊关系认知】\n(你是群聊的一员，请参考以下你与其他成员的关系)\n"

    if not os.path.exists(rel_file):
        return ""

    try:
        with open(rel_file, "r", encoding="utf-8") as f:
            rels_data = json.load(f)

        found_any = False

        for other_id in other_member_ids:
            if other_id == "user":
                continue

            target_name = id_to_name_map.get(other_id, other_id)

            rel_info = rels_data.get(target_name) or rels_data.get(other_id)

            if rel_info:
                role = rel_info.get('role', '未知')
                desc = rel_info.get('description', '特になし')
                score = rel_info.get('score', 1)
                prompt_text += f"- 対 {target_name}: {role} (関係度:{score}) {desc}\n"
                found_any = True
            else:
                pass

        if not found_any:
            return ""

        return prompt_text

    except Exception as e:
        print(f"Build Group Rel Error: {e}")
        return ""
