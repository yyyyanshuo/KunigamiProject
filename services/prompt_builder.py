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
from core.memory_periods import parse_week_key_to_dates
from core.time_utils import (
    BEIJING_TZ,
    beijing_now,
    get_character_timezone,
    get_user_timezone,
    get_zone,
    parse_beijing_timestamp,
    utc_now,
)
from core.utils import (
    _add_furigana_to_japanese, get_paths, get_current_username,
    _get_characters_config_file, _get_groups_config_file, _load_user_settings,
    load_character_positions, load_user_position, load_locations,
    calc_distance, get_location_by_id, get_group_dir,
    normalize_map_state,
)
from services.voice_messages import voice_message_for_ai
from services.voice_calls import voice_call_for_ai


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


def get_char_name(char_id, user_id=None):
    config_file = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(config_file):
        return char_id
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get(char_id, {}).get("name", char_id)
    except:
        return char_id


def get_char_age(char_id, user_id=None):
    config_file = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(config_file):
        return None
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            age = data.get(char_id, {}).get("age")
            return int(age) if age is not None else None
    except Exception:
        return None


def _get_character_time_info(char_id, user_id=None):
    info = {}
    try:
        config_file = _get_characters_config_file(user_id=user_id)
        with open(config_file, "r", encoding="utf-8") as f:
            info = (json.load(f) or {}).get(char_id, {}) or {}
    except Exception:
        info = {}
    timezone_name = get_character_timezone(info)
    now = utc_now().astimezone(get_zone(timezone_name))
    return info, timezone_name, now


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


def get_user_age(user_id=None):
    data = _load_user_settings()
    if user_id:
        settings_path = os.path.join(USERS_ROOT, str(user_id), "configs", "user_settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
    age = data.get("user_age")
    try:
        return int(age) if age is not None else None
    except Exception:
        return None


def _get_username_for_user(user_id=None):
    if user_id:
        settings_path = os.path.join(USERS_ROOT, str(user_id), "configs", "user_settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    return (json.load(f).get("current_user_name") or "User").strip()
            except Exception:
                pass
    return get_current_username()


def _get_settings_for_user(user_id=None):
    if user_id:
        settings_path = os.path.join(
            USERS_ROOT, str(user_id), "configs", "user_settings.json"
        )
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                pass
    return _load_user_settings()


def build_agent_current_state_section(char_id, prompts_dir, target_char_id=None, group_id=None, user_id=None):
    """Build only the mutable runtime state; other prompt sections own context data."""
    try:
        cfg_file = _get_characters_config_file(user_id=user_id)
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)
        info = all_config.get(char_id)
        if not isinstance(info, dict):
            return ""
    except Exception as e:
        print(f"[Current State] failed to read character metadata for {char_id}: {e}")
        return ""

    lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
    bool_text = {
        "zh": lambda value: "开启" if value else "关闭",
        "ja": lambda value: "オン" if value else "オフ",
        "en": lambda value: "on" if value else "off",
    }.get(lang, lambda value: "开启" if value else "关闭")

    state = {
        "emotion": info.get("emotion", 1),
        "moments_index": info.get("moments_index", 1),
        "intimacy": info.get("intimacy", 60),
        "voice_emotion": info.get("voice_emotion") or "平静",
        "chat_mode": info.get("chat_mode") or "online",
        "light_sleep": bool(info.get("light_sleep", True)),
        "deep_sleep": bool(info.get("deep_sleep", False)),
        "sleep_window": f"{info.get('ds_start', '23:00')}-{info.get('ds_end', '07:00')}",
    }

    if lang == "ja":
        lines = [
            f"- 社交欲求度 emotion: {state['emotion']}（0〜20）",
            f"- 投稿・表現欲求 moments_index: {state['moments_index']}（0.1〜10）",
            f"- ユーザー親密度 intimacy: {state['intimacy']}（0〜100）",
            f"- 音声感情 voice_emotion: {state['voice_emotion']}",
            f"- チャットモード chat_mode: {state['chat_mode']}",
            f"- 浅い睡眠: {bool_text(state['light_sleep'])}",
            f"- 深い睡眠: {bool_text(state['deep_sleep'])}（時間帯 {state['sleep_window']}）",
        ]
        header = "【現在の状態 / Current State】"
    elif lang == "en":
        lines = [
            f"- Social desire emotion: {state['emotion']} (0-20)",
            f"- Posting/expression desire moments_index: {state['moments_index']} (0.1-10)",
            f"- User intimacy: {state['intimacy']} (0-100)",
            f"- Voice mood voice_emotion: {state['voice_emotion']}",
            f"- Chat mode: {state['chat_mode']}",
            f"- Light sleep: {bool_text(state['light_sleep'])}",
            f"- Deep sleep: {bool_text(state['deep_sleep'])} (window {state['sleep_window']})",
        ]
        header = "【Current State / 当前状态】"
    else:
        lines = [
            f"- 社交渴望度 emotion：{state['emotion']}（0~20）",
            f"- 朋友圈表达欲 moments_index：{state['moments_index']}（0.1~10）",
            f"- 对用户亲密度 intimacy：{state['intimacy']}（0~100）",
            f"- 语音情绪 voice_emotion：{state['voice_emotion']}",
            f"- 聊天模式 chat_mode：{state['chat_mode']}",
            f"- 浅睡眠：{bool_text(state['light_sleep'])}",
            f"- 深睡眠：{bool_text(state['deep_sleep'])}（时间段 {state['sleep_window']}）",
        ]
        header = "【当前状态 / Current State】"

    return header + "\n" + "\n".join(lines)


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

    selected = select_relevant_long_memory(
        long_mem,
        recent_messages,
        user_latest_input=user_latest_input,
        char_id=char_id,
    )
    print(f"[DEBUG] extract_long_memory: 筛选后得到 {len(selected)} 条有效记忆")
    if not selected:
        print(f"[DEBUG] extract_long_memory: 筛选结果为空")
        return result

    _, timezone_name, _ = _get_character_time_info(char_id, user_id=user_id)
    character_zone = get_zone(timezone_name)
    for week_key, content in selected:
        date_range = parse_week_key_to_dates(week_key)
        print(f"[DEBUG] extract_long_memory: week_key={week_key}, date_range={date_range}")
        if date_range:
            _, last_date = date_range
            ts_23_59 = datetime.combine(
                last_date, dt_time(23, 59), tzinfo=BEIJING_TZ
            ).astimezone(character_zone)
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

    _, timezone_name, _ = _get_character_time_info(char_id, user_id=user_id)
    character_zone = get_zone(timezone_name)
    now = beijing_now()
    for i in range(7, 0, -1):
        day_date = (now - timedelta(days=i)).date()
        day_key = day_date.strftime("%Y-%m-%d")

        if day_key in med_mem:
            content = str(med_mem[day_key]).strip()
            print(f"[DEBUG] extract_medium_memory: 找到 {day_key} 的记忆 - {content[:50]}")
            if content:
                ts_23_59 = datetime.combine(
                    day_date, dt_time(23, 59), tzinfo=BEIJING_TZ
                ).astimezone(character_zone)
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

    _, timezone_name, _ = _get_character_time_info(char_id, user_id=user_id)
    character_zone = get_zone(timezone_name)
    current_utc = utc_now()
    cutoff_utc = current_utc - timedelta(hours=24)
    now_beijing = current_utc.astimezone(BEIJING_TZ)
    dates_to_load = [
        (now_beijing - timedelta(days=1)).strftime("%Y-%m-%d"),
        now_beijing.strftime("%Y-%m-%d"),
    ]

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
                        beijing_ts = datetime.combine(
                            date_obj,
                            dt_time(h, m),
                            tzinfo=BEIJING_TZ,
                        )
                    except Exception:
                        continue
                else:
                    continue

                event_utc = beijing_ts.astimezone(get_zone("UTC"))
                if not (cutoff_utc < event_utc <= current_utc + timedelta(minutes=2)):
                    continue

                local_ts = event_utc.astimezone(character_zone)
                result.append((event_text, local_ts.date(), local_ts))

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

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()

        print(f"[DEBUG] extract_recent_messages: 查询到 {len(rows)} 条消息")

        _, timezone_name, _ = _get_character_time_info(char_id, user_id=user_id)
        character_zone = get_zone(timezone_name)
        for i, row in enumerate(reversed(rows)):
            try:
                role = row["role"]
                content = voice_message_for_ai(str(row["content"] or ""))
                if not group_id:
                    content = voice_call_for_ai(user_id, content)
                ts_str = row["timestamp"]

                try:
                    beijing_dt = parse_beijing_timestamp(ts_str)
                    if beijing_dt is None:
                        raise ValueError("invalid timestamp")
                    msg_dt = beijing_dt.astimezone(character_zone)
                except Exception as e:
                    print(f"[DEBUG] extract_recent_messages: [{i}] 时间戳解析失败 - {e}")
                    msg_dt = utc_now().astimezone(character_zone)

                time_label = msg_dt.strftime("%H:%M")

                if group_id:
                    if role == "user":
                        role_label = "用户"
                    else:
                        role_label = get_char_name(role, user_id=user_id)
                else:
                    role_label = "user" if role == "user" else "你"

                content_display = f"[{time_label}] 【{role_label}】{content}"
                result.append((role, content_display, msg_dt))
            except Exception as e:
                # 单条脏数据或终端编码问题不能截断整段最近上下文。
                print(f"[DEBUG] extract_recent_messages: [{i}] 消息处理失败 - {e}")
    except Exception as e:
        print(f"[DEBUG] extract_recent_messages: 数据库操作失败 - {e}")
        import traceback
        print(traceback.format_exc())
    finally:
        if conn is not None:
            conn.close()

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


def build_system_prompt_v2(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, target_char_id=None, group_id=None, include_long_memory=True, include_recent_messages=True, user_id=None, call_mode=False):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    prompt_parts = []

    _, prompts_dir = get_paths(char_id, user_id=user_id)
    try:
        normalize_map_state(user_id=user_id)
    except Exception:
        pass
    _, character_timezone, now = _get_character_time_info(
        char_id, user_id=user_id
    )
    today_str = now.strftime("%Y-%m-%d")

    char_name = get_char_name(char_id, user_id=user_id)
    char_age = get_char_age(char_id, user_id=user_id)
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
        user_name = _get_username_for_user(user_id)
        user_age = get_user_age(user_id=user_id)
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
        current_user_name = _get_username_for_user(user_id)
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                rel_data = json.load(f) or {}

            target_rel = None
            display_name = current_user_name

            if target_char_id and target_char_id != "user":
                target_name = get_char_name(target_char_id, user_id=user_id)
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
                    with open(_get_characters_config_file(user_id=user_id), "r", encoding="utf-8") as cf:
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

    current_state_section = build_agent_current_state_section(
        char_id,
        prompts_dir,
        target_char_id=target_char_id,
        group_id=group_id,
        user_id=user_id,
    )
    if current_state_section:
        prompt_parts.append(current_state_section)

    if include_global_format and not call_mode:
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
        prompt_parts.append(
            "【Memory Time Rule / 记忆时间规则】\n"
            "- 时间线中的日期和时间均已换算为角色当前所在地的当地时间。\n"
            "- 中期记忆是历史概括，短期记忆是最近24小时的详细事件；"
            "两者可能描述同一经历，不代表事件发生了两次。\n"
            "- 若记忆层之间存在细节差异，以最近原始消息和短期记忆为准。"
        )
    else:
        print(f"[DEBUG v2] WARNING: timeline_events 为空！")

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

    user_settings = _get_settings_for_user(user_id)
    user_now = utc_now().astimezone(get_zone(get_user_timezone(user_settings)))
    beijing_time = beijing_now()
    time_info = (
        f"現在は {now.strftime('%Y-%m-%d %H:%M')} （{period}）です。\n"
        f"- 角色当前时区：{character_timezone}\n"
        f"- 用户当地时间：{user_now.strftime('%Y-%m-%d %H:%M')}\n"
        f"- 系统北京时间：{beijing_time.strftime('%Y-%m-%d %H:%M')}\n"
        "- “今天、昨天、明天”和日程日期均以角色当地日期理解。"
    )
    prompt_parts.append(f"【現在時刻】\n{time_info}")

    # ===== location context / 地点感知 =====
    try:
        char_positions, user_pos, locs = normalize_map_state(user_id=user_id)
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
                    cname = get_char_name(cid, user_id=user_id)
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
                    known_list.append(f"  {l['name']} [id={l['id']}]（坐标 {l['x']},{l['y']}，距离 {round(dist,2)}）")
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
                    nearby_list.append(f"  {loc['name']} [id={loc['id']}]（坐标 {loc['x']},{loc['y']}，距离 {round(d,2)}）")
            if nearby_list:
                location_lines.append(f"- 附近可感知的地点（距离<1格，但尚未去过）：\n" + "\n".join(nearby_list))
            else:
                location_lines.append(f"- 附近可感知的地点：无")

            prompt_parts.append(f"【現在の場所 / 当前环境与位置】\n" + "\n".join(location_lines))
    except Exception as e:
        print(f"[DEBUG] Location prompt injection error: {e}")

    # ===== weather / 天气感知 =====
    try:
        char_positions = load_character_positions(user_id=user_id)
        if char_id in char_positions:
            cp = char_positions[char_id]
            loc_id = cp.get("location_id")
            if loc_id:
                loc = get_location_by_id(loc_id, user_id=user_id)
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
    if call_mode:
        pass
    elif lang == "ja":
        prompt_parts.append(
            "【位置移動コマンド / Location Movement Commands】\n"
            "距離<1の任意の地点/座標に移動できます。到着後その地点は「認知地点」に追加されます：\n"
            "- [MOVE_TO:地点ID] ※認知/知覚リストに**既に存在する地点**への移動にのみ使用可能\n"
            "- [MOVE_TO_COORD:x,y] 指定座標に単純移動（新地点は作らない）\n"
            "- [EXPLORE:x,y,\"名称\",\"説明\"] 未探索の地点に移動して新地点を確立\n"
            "⚠️ 「出発する・離れる・到着する・別の場所へ行く」と本文で述べる場合、同じ返答に必ず対応する移動タグを付けてください。"
            "移動タグなしで現在地と矛盾する場所にいると主張してはいけません。現在地と座標は上の状態を唯一の基準にしてください。"
        )
    elif lang == "en":
        prompt_parts.append(
            "【Location Movement Commands / 位置移动指令】\n"
            "Move to any location/coordinate within distance<1. On arrival the location is added to your known list:\n"
            "- [MOVE_TO:location_id] ※ Only usable for locations that **already exist** in your known/perceived list\n"
            "- [MOVE_TO_COORD:x,y] Simple move to coordinates (does not create a new location)\n"
            "- [EXPLORE:x,y,\"name\",\"desc\"] Move to an unknown coordinate and establish a new location\n"
            "⚠️ If your message says you leave, depart, arrive, or go somewhere else, include the matching movement tag in the same reply. "
            "Never claim to be somewhere inconsistent with Current State without moving; Current State coordinates are authoritative."
        )
    else:
        prompt_parts.append(
            "【位置移动指令 / Location Movement Commands】\n"
            "距离<1格内的任意地点或坐标都可以移动过去，到达后该地点会自动加入你的认知列表：\n"
            "- [MOVE_TO:地点ID] ※只能在目标地点**已经存在**于你的认知/感知列表中时使用\n"
            "- [MOVE_TO_COORD:x,y] 移动到指定坐标，单纯移动，不建立新地点\n"
            "- [EXPLORE:x,y,\"名称\",\"描述\"] 前往一个不在认知/感知中存在的地点并建立新地点\n"
            "⚠️ 如果正文说自己出发、离开、到达或去了别处，必须在同一轮附上对应的位置移动标签。"
            "不得在没有移动标签时声称自己位于与“当前状态”不一致的地点；当前状态中的地点和坐标是唯一准确信息。"
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

    if not call_mode:
        chat_mode = _get_char_chat_mode(char_id, user_id=user_id)
        mode_context = get_mode_context(lang, chat_mode=chat_mode)
        if mode_context:
            prompt_parts.append(f"【Mode Context / 模式上下文】\n{mode_context}")

    lang = get_ai_language(char_id, group_id=group_id, user_id=user_id)
    if not call_mode and lang == "ja":
        agent_enforce = (
            "\n\n【Agent Output Requirement / エージェント出力要件】\n"
            "【最重要】毎回の返信末尾（改行して）に、**必ず 1〜3 個**の Agent Action Tag を出力してください。\n"
            "上記プロトコルに記載されているタグ形式を厳守してください。\n"
            "⚠️ 複数タグの場合、それぞれの `[]` を改行で並べてください（例：`[SET_EMOTION:5]\\n[UPDATE_AFFINITY:+2]`）。\n"
            "単一の `[]` 内にカンマ区切りで複数指令を詰め込まないでください。\n"
            "今回のターンでパラメータ変更やアクションが本当に何もない場合は、代わりに `[NONE]` と出力してください。"
        )
    elif not call_mode and lang == "en":
        agent_enforce = (
            "\n\n【Agent Output Requirement / 智能体输出要件】\n"
            "【CRITICAL】At the end of every reply (on a new line), you MUST output **1~3** Agent Action Tags.\n"
            "Strictly follow the tag formats described in the protocol above.\n"
            "⚠️ Multiple tags must each appear on their own line (e.g. `[SET_EMOTION:5]\\n[UPDATE_AFFINITY:+2]`).\n"
            "Do NOT cram multiple commands into a single `[]` separated by commas.\n"
            "If there is genuinely nothing to change or execute this turn, output `[NONE]` instead."
        )
    elif not call_mode:
        agent_enforce = (
            "\n\n【Agent Output Requirement / 智能体输出要件】\n"
            "【最重要】每轮回复末尾（另起一行），必须输出 **1~3 条** Agent Action Tag。\n"
            "严格按照上述协议中描述的标签格式输出。\n"
            "⚠️ 多条标签时，每条 `[]` 独占一行换行并列（例：`[SET_EMOTION:5]\\n[UPDATE_AFFINITY:+2]`）。\n"
            "不要在单个 `[]` 内用逗号分隔多条指令。\n"
            "如果当前轮次确实没有任何参数需要调整、没有任何动作需要执行，则输出 `[NONE]` 作为占位。"
        )
    if not call_mode:
        prompt_parts.append(agent_enforce)

    return "\n\n".join(prompt_parts)


def build_messages_for_chat_v2(char_id, user_input, recent_messages=None, user_id=None) -> list:
    normalized_input = voice_call_for_ai(user_id, voice_message_for_ai(user_input))
    normalized_recent = [
        voice_call_for_ai(user_id, voice_message_for_ai(item))
        for item in (recent_messages or [])
    ]
    system_prompt = build_system_prompt_v2(
        char_id,
        include_global_format=True,
        recent_messages=normalized_recent,
        user_latest_input=normalized_input,
        user_id=user_id,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": normalized_input}
    ]

    return messages


def build_system_prompt(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, include_long_memory=True, target_char_id=None, user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    prompt_parts = []

    _, _, now = _get_character_time_info(char_id, user_id=user_id)
    today_str = now.strftime("%Y-%m-%d")

    BASE_DIR_ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CONFIG_DIR = os.path.join(BASE_DIR_, "configs")
    _, prompts_dir = get_paths(char_id, user_id=user_id)

    current_user_name = _get_username_for_user(user_id)

    print(f"--- [Debug] 正在为 [{char_id}] 构建 Prompt，路径: {prompts_dir} ---")

    char_name = get_char_name(char_id, user_id=user_id)
    char_age = get_char_age(char_id, user_id=user_id)
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
                target_name = get_char_name(target_char_id, user_id=user_id)
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

    current_state_section = build_agent_current_state_section(
        char_id,
        prompts_dir,
        target_char_id=target_char_id,
        user_id=user_id,
    )
    if current_state_section:
        prompt_parts.append(current_state_section)

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
