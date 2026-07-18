import os
import re
import json
import threading
import random
import uuid
import shutil
from datetime import datetime, timedelta
from typing import Tuple
from flask import Blueprint, request, jsonify, session, render_template
from urllib.parse import urlparse
from PIL import Image, ImageOps

from core.config import (
    COS_BASE_URL, USERS_ROOT, BASE_DIR,
)
from core.context import get_current_user_id, set_background_user
from core.utils import (
    get_paths, safe_save_json, _get_characters_config_file,
    get_characters_config_for_current_user, get_current_username,
    _load_user_settings, _get_groups_config_file,
)

# --- Fallback constants (redefined in blueprint scope) ---
MOMENTS_DATA_FILE = os.path.join(BASE_DIR, "configs", "moments_data.json")
MOMENTS_LAST_POST_FILE = os.path.join(BASE_DIR, "configs", "moments_last_post.json")
ACTIVE_MOMENTS_ENABLED_FILE = os.path.join(BASE_DIR, "configs", "active_moments_enabled.json")

moments_bp = Blueprint('moments', __name__, template_folder='templates')


# =============================================================================
# Helper Functions
# =============================================================================

def clean_moments_agent_instructions(text):
    """
    清除朋友圈文本中可能出现的所有 Agent 动作标签（包括各种畸形、非标准和残留的标签，如 [UPDATE_AFFINITY:user,-1] 等）。
    不影响搜图转化后的 markdown 标签 [图片](filename)(keyword)。
    """
    if not text:
        return text

    # 匹配已知的 Agent 指令标签关键字 (不区分大小写)，支持中英文括号
    pattern = r'[\[【]\s*(?:UPDATE_AFFINITY|SET_EMOTION|SET_PERSONALITY|SET_SLEEP_TIME|SET_RELATION|ADD_SCHEDULE|MOOD|DIRECT_TO_GROUP|DIRECT_TO_USER|DIRECT_END|NONE|MOVE_TO|MOVE_TO_COORD|EXPLORE|SET_CHAT_MODE|MUSIC_[A-Z0-9_]+)(?::|：)?\s*.*?[\]】]'
    text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)

    # 额外兜底清理所有大写加下划线的指令标签，避免未定义或畸形的指令流出（不清理 SEARCH_IMG 和 GENERATE_IMAGE）
    general_pattern = r'[\[【]\s*(?!(?:SEARCH_IMG|GENERATE_IMAGE)\b)(?:[A-Z_][A-Z0-9_]*)(?::|：)?\s*.*?[\]】]'
    text = re.sub(general_pattern, '', text, flags=re.IGNORECASE | re.DOTALL)

    # 移除可能产生的多余空行
    text = re.sub(r'\n\s*\n', '\n', text).strip()
    return text

def get_moments_paths(user_id=None) -> Tuple[str, str]:
    """
    获取当前用户的朋友圈数据文件路径。
    如有登录用户，则使用 users/<user_id>/configs/moments_*.json；
    否则退回全局 MOMENTS_DATA_FILE / MOMENTS_LAST_POST_FILE。
    """
    if user_id is None:
        user_id = get_current_user_id()
    if user_id:
        base = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(base, exist_ok=True)
        return (
            os.path.join(base, "moments_data.json"),
            os.path.join(base, "moments_last_post.json"),
        )
    return MOMENTS_DATA_FILE, MOMENTS_LAST_POST_FILE


def _get_active_moments_enabled_file():
    """返回当前用户的 active_moments_enabled.json 路径（含后台用户上下文）"""
    uid = get_current_user_id()
    if uid:
        return os.path.join(USERS_ROOT, str(uid), "configs", "active_moments_enabled.json")
    return ACTIVE_MOMENTS_ENABLED_FILE


def _get_active_moments_enabled():
    """读取是否开启主动朋友圈，默认 True。"""
    path = _get_active_moments_enabled_file()
    if not os.path.exists(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("enabled", True)
    except Exception:
        return True


def _set_active_moments_enabled(enabled):
    """写入是否开启主动朋友圈。"""
    path = _get_active_moments_enabled_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe_save_json(path, {"enabled": bool(enabled)})


def _get_moments_id_display(user_id=None):
    """返回 (id -> avatar, id -> remark) 用于朋友圈展示。含 user 与所有角色。"""
    avatars, remarks = {}, {}
    # 当前登录用户的头像与昵称
    try:
        user_cfg = _load_user_settings(user_id=user_id)
        avatars["user"] = user_cfg.get("avatar") or "/user_avatar"
        remarks["user"] = user_cfg.get("current_user_name") or "我"
    except Exception:
        pass
    if not avatars.get("user"): avatars["user"] = "/user_avatar"
    if not remarks.get("user"): remarks["user"] = "我"
    cfg_file = _get_characters_config_file(user_id=user_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                for cid, info in json.load(f).items():
                    avatars[cid] = info.get("avatar") or "/static/default_avatar.png"
                    remarks[cid] = info.get("remark") or info.get("name") or cid
        except: pass
    return avatars, remarks


def _get_moments_name_to_id(user_id=None):
    """返回 {显示名: cid} 的映射，包含 name、remark、cid 三种标识。用于解析 @ 提及。"""
    mapping = {}
    cfg_file = _get_characters_config_file(user_id=user_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                for cid, info in json.load(f).items():
                    mapping[cid] = cid
                    if info.get("name"):
                        mapping[info["name"]] = cid
                    if info.get("remark"):
                        mapping[info["remark"]] = cid
        except: pass
    return mapping


def _find_moment_post(raw, char_id, timestamp_str):
    """在 raw 列表中找到 char_id + timestamp 匹配的一条，返回 (index, post) 或 (None, None)。"""
    for i, post in enumerate(raw):
        if post.get("char_id") == char_id and post.get("timestamp") == timestamp_str:
            return i, post
    return None, None


def process_moments_media_tags(text, char_id, user_id=None):
    """
    专门为朋友圈处理媒体标签：
    1. 仅支持 [SEARCH_IMG: 关键词]
    2. 如果 AI 误用了 [GENERATE_IMAGE: ...]，强制转换为搜索以节省额度
    3. 结果保存到 moments/YYYYMM 目录下以匹配前端展示逻辑
    """
    if not text:
        return text

    from app import _call_serper_search

    # 正则规则
    gen_pattern = r'[\[【]GENERATE_IMAGE[:：\s]*(.*?)[\]】]'
    search_pattern = r'[\[【]SEARCH_IMG[:：\s]*(.*?)[\]】]'

    # 计算朋友圈存储子目录 (YYYYMM)
    yyyymm = datetime.now().strftime("%Y%m")
    moments_prefix = f"moments/{yyyymm}"

    # 处理搜图
    def replace_search(match):
        keyword = match.group(1).strip()
        if not keyword: return ""
        print(f"--- [Moments Search] 命中搜图标签: {keyword} ---")
        url = _call_serper_search(keyword, cos_prefix=moments_prefix, user_id=user_id)
        if url:
            # 朋友圈渲染逻辑：对于本地或 COS 存储路径/URL，仅提取文件名
            if "search_" in url or "moments/" in url:
                clean_url = url.split('?')[0]
                filename = clean_url.split('/')[-1]
                return f"[图片]({filename})({keyword})"
            else:
                # 否则说明是未成功上传至 COS 的外部原始搜索外链，保留完整外链供前端加载展示
                return f"[图片]({url})({keyword})"
        return f" (没找到相关图片: {keyword}) "

    # 如果 AI 用了生图标签，在朋友圈场景下强制转为搜图
    text = re.sub(gen_pattern, replace_search, text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(search_pattern, replace_search, text, flags=re.IGNORECASE | re.DOTALL)

    return text


def append_moment_event_to_short_memory(char_id, context_text, user_id=None):
    """
    将朋友圈互动用 AI 总结为一句话，追加到角色的当日短期记忆中。
    使用与记忆总结相同的模型（summary），context_text 为互动描述。
    """
    if not char_id or char_id == "user" or not (context_text or "").strip():
        return
    from app import call_ai_to_summarize
    try:
        summary = call_ai_to_summarize((context_text or "").strip(), "moment", char_id, user_id=user_id)
        if not summary:
            return
        line = summary.strip().split("\n")[0].strip()
        line = re.sub(r"^-\s*\[\d{2}:\d{2}\]\s*", "", line).strip()
        if not line:
            return
        _, prompts_dir = get_paths(char_id)
        short_file = os.path.join(prompts_dir, "6_memory_short.json")
        date_str = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H:%M")

        current_data = {}
        if os.path.exists(short_file):
            with open(short_file, "r", encoding="utf-8") as f:
                try:
                    current_data = json.load(f)
                except Exception:
                    pass

        day_data = current_data.get(date_str, {})
        if isinstance(day_data, list):
            existing_events = list(day_data)
            last_id = 0
        else:
            existing_events = list(day_data.get("events", []))
            last_id = day_data.get("last_id", 0)

        existing_events.append({"time": time_str, "event": line})
        existing_events.sort(key=lambda x: x["time"])

        current_data[date_str] = {"events": existing_events, "last_id": last_id}
        os.makedirs(os.path.dirname(short_file), exist_ok=True)
        with open(short_file, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   [Moments] 写入短期记忆失败 [{char_id}]: {e}")


def sync_memory_before_moments(char_id, user_id=None):
    """
    发朋友圈前，同步该角色的单聊及群聊记忆。
    """
    from app import sync_memory_before_single_chat, update_short_memory_for_date
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    dates = [today_str]
    if now.hour < 4:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates.insert(0, yesterday_str)

    try:
        # 1. 先同步群聊记忆
        ok, err = sync_memory_before_single_chat(char_id, user_id=user_id)
        if not ok:
            return ok, err
        # 2. 再汇总单聊记忆 (复用现有的单聊日结函数)
        for d in dates:
            try:
                update_short_memory_for_date(char_id, d, user_id=user_id)
            except Exception as e:
                print(f"   [Sync] 发朋友圈前单聊记忆 {char_id} 日期 {d} 同步失败: {e}")
        return True, None
    except Exception as e:
        print(f"   [Sync] 发朋友圈前记忆同步失败: {e}")
        return False, str(e)


def _get_moments_relationship_candidates(char_id, user_id=None):
    """从角色的 2_relationship.json 中取出除用户外的 (char_id, score) 列表。关系 key 为名字，需映射到 char_id。"""
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    rel_path = os.path.join(prompts_dir, "2_relationship.json")
    if not os.path.exists(rel_path):
        return []
    try:
        with open(rel_path, "r", encoding="utf-8") as f:
            rel_data = json.load(f)
    except Exception:
        return []
    current_user_name = get_current_username()
    # 名字 -> char_id 映射（per-user characters.json）
    name_to_cid = {}
    cfg_file = _get_characters_config_file(user_id=user_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                for cid, info in json.load(f).items():
                    name = (info.get("name") or "").strip()
                    remark = (info.get("remark") or "").strip()
                    if name:
                        name_to_cid[name] = cid
                    if remark and remark != name:
                        name_to_cid[remark] = cid
        except Exception:
            pass
    candidates = []
    for name, obj in rel_data.items():
        if not isinstance(obj, dict):
            continue
        if name.strip() == current_user_name:
            continue
        score = float(obj.get("score", 0))
        if score <= 0:
            continue
        cid = name_to_cid.get(name.strip())
        if cid:
            candidates.append((cid, score))
    # 同一角色可能因 name/remark 出现多次，按 char_id 合并分数
    merged = {}
    for cid, score in candidates:
        merged[cid] = merged.get(cid, 0) + score
    return [(cid, s) for cid, s in merged.items()]


def _weighted_sample_no_replacement(candidates, k):
    """从 [(char_id, score), ...] 中按权重无放回抽取最多 k 个 char_id。"""
    if not candidates or k <= 0:
        return []
    k = min(k, len(candidates))
    result = []
    remaining = list(candidates)
    total = sum(s for _, s in remaining)
    if total <= 0:
        return []
    for _ in range(k):
        r = random.uniform(0, total)
        for i, (cid, s) in enumerate(remaining):
            r -= s
            if r <= 0:
                result.append(cid)
                total -= s
                remaining.pop(i)
                break
        else:
            if remaining:
                cid, s = remaining.pop()
                result.append(cid)
                total -= s
    return result


def _generate_moment_comment(commenter_id, post_author_id, post_content, is_mentioned=False, user_id=None):
    """
    为朋友圈生成一条简短评论。
    is_mentioned: 是否是被 @ 提及的角色
    """
    from app import (
        call_gemini, call_openrouter, get_model_config, get_ai_language,
        should_use_prompt_v2, build_system_prompt_v2, build_system_prompt,
        process_agent_actions, _execute_directive,
    )
    # 生成前：同步该角色的短期记忆
    try:
        sync_memory_before_moments(commenter_id)
    except Exception as e:
        print(f"   [Moment Comment] 记忆同步失败 {commenter_id}: {e}，继续生成")

    recent_messages = [post_content]

    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()

    # 朋友圈评论不需要全局格式规则
    if should_use_prompt_v2(commenter_id):
        sys_prompt = build_system_prompt_v2(commenter_id, include_global_format=False, recent_messages=recent_messages, target_char_id=post_author_id, include_recent_messages=False, user_id=user_id)
    else:
        sys_prompt = build_system_prompt(commenter_id, include_global_format=False, recent_messages=recent_messages, target_char_id=post_author_id, user_id=user_id)

    # 从当前用户的 characters.json 中读取双方名字，便于在 Prompt 中明确说明评论对象与关系
    commenter_name = commenter_id
    author_name = post_author_id
    try:
        cfg_file = _get_characters_config_file(user_id=user_id)
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_chars = json.load(f)
            if isinstance(all_chars, dict):
                c_info = all_chars.get(commenter_id, {})
                a_info = all_chars.get(post_author_id, {})
                commenter_name = (c_info.get("remark") or c_info.get("name") or commenter_id) or commenter_id
                author_name = (a_info.get("remark") or a_info.get("name") or post_author_id) or post_author_id
    except Exception:
        pass

    lang = get_ai_language(commenter_id, user_id=user_id)
    now = datetime.now()

    mention_instruction = ""
    if is_mentioned:
        if lang == "zh":
            mention_instruction = f"注意：你在该动态中被 @（提及）了，可能对方在征求你的意见或希望你看到。请在评论中自然地体现出这一点。\n\n"
        elif lang == "ja":
            mention_instruction = f"注意：あなたはこの投稿で @（メンション）されました。相手はあなたに気づいてほしいか、意見を求めているようです。コメントで自然に反応してください。\n\n"
        else:
            mention_instruction = f"Note: You were @(mentioned) in this post. The author likely wants your attention or opinion. Please react naturally in your comment.\n\n"

    if lang == "zh":
        user_msg = (
            "【评论任务说明】\n"
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
            "你现在要为一条朋友圈写一条简短评论（仅一句话，100字以内）。只输出评论内容，不要加引号，也不要加「评论：」之类的前缀。\n\n"
            f"{mention_instruction}"
            "【朋友圈原文】\n"
            f"发布者：{author_name}\n"
            f"内容：{post_content}\n\n"
            "【评论对象与关系（请重点理解）】\n"
            f"- 被评论者：{author_name}（ID: {post_author_id}）\n"
            "你（当前说话的角色）与 TA 之间的具体关系（例如：队友、学长学弟、朋友、恋人、家人等）已经在系统角色设定与关系图谱中给出。\n"
            "\n【对话转向（推荐使用）】\n"
            "在评论区也可以把互动延续到私聊或群聊——\n"
            "- 拉人建群：想和评论区的其他角色一起聊？评论末尾另起一行输出 `[DIRECT_TO_GROUP: 角色名, +user]`。\n"
            "- 切回单聊：想和用户私下聊？输出 `[DIRECT_TO_USER]`。\n"
            "- 这些标签用户看不见，直接输出就行。\n"
            f"重要：当前语言设定为 {lang}，请务必使用该语言回复。"
        )
    elif lang == "ja":
        user_msg = (
            "【コメントタスク説明】\n"
            f"現在時刻：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
            "あなたは今、ある朋友圈の投稿に対して短いコメント（1文のみ、100文字以内）を書きます。コメント内容のみを出力し、引用符や「コメント：」などの接頭辞は不要です。\n\n"
            f"{mention_instruction}"
            "【投稿原文】\n"
            f"投稿者：{author_name}\n"
            f"内容：{post_content}\n\n"
            "【コメント対象と関係性（重要）】\n"
            f"- 投稿者：{author_name}（ID: {post_author_id}）\n"
            "あなた（現在のキャラクター）と投稿者との具体的な関係性（例：チームメイト、先輩後輩、友人、恋人、家族など）は、システム設定と関係性マップで定義されています。\n"
            "\n【会話切り替え（推奨）】\n"
            "コメント欄からでも会話をグループチャットや個別チャットに切り替えられます——\n"
            "- グループ作成：他のキャラクターと一緒に話したい？コメント末尾に改行して `[DIRECT_TO_GROUP: キャラ名, +user]` を出力。\n"
            "- 個別チャットに戻る：ユーザーと二人で話したい？`[DIRECT_TO_USER]` を出力。\n"
            "- これらのタグはユーザーに見えないので、気軽に出力しよう。\n"
            f"重要：指定言語は {lang} です。必ずその言語で返信してください。"
        )
    else:
        user_msg = (
            "[Comment Task Instructions]\n"
            f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')}\n"
            "You are to write a short comment (only one sentence, 100 chars max) for a post on Moments. Output only the content of the comment, without quotes or prefixes like 'Comment:'.\n\n"
            f"{mention_instruction}"
            "[Original Post]\n"
            f"Author: {author_name}\n"
            f"Content: {post_content}\n\n"
            "[Recipient and Relationship]\n"
            f"- Author: {author_name} (ID: {post_author_id})\n"
            "The specific relationship between you and the author (e.g., teammate, senior/junior, friend, lover, family) is defined in the system settings and relationship map.\n"
            "\n[Chat Redirection (recommended)]\n"
            "You can also continue the conversation from comments into group or solo chat:\n"
            "- Create group: Want to chat with other characters from the comments? Output `[DIRECT_TO_GROUP: CharacterName, +user]` on a new line at the end of your comment.\n"
            "- Return to solo chat: Want to talk to the user privately? Output `[DIRECT_TO_USER]`.\n"
            "- These tags are invisible to users — feel free to use them.\n"
            f"Important: Your assigned language is {lang}. Please reply in this language."
        )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg}
    ]
    try:
        route, current_model = get_model_config("moments", user_id=user_id)
        if route == "relay":
            text = call_openrouter(messages, char_id=commenter_id, model_name=current_model, user_id=user_id)
        else:
            text = call_gemini(messages, char_id=commenter_id, model_name=current_model, user_id=user_id)
        if text:
            text = text.strip().strip('"\'')
            text, _, directive = process_agent_actions(commenter_id, text, user_id or get_current_user_id())
            if directive:
                uid = user_id or get_current_user_id()
                _d, _cid, _txt = directive, commenter_id, text
                def _bg(): set_background_user(uid); _execute_directive(_d, _cid, _txt)
                threading.Thread(target=_bg, daemon=True).start()
            if len(text) > 200:
                text = text[:200]

            # (底层不再自动写入记忆，由调用方统一处理)
            return text
    except Exception as e:
        print(f"   [Moments] 评论生成失败 {commenter_id}: {e}")
    return None


def _generate_moment_reply_to_user(author_char_id, post_content, user_comment, user_id=None):
    """让朋友圈作者（角色）对用户的评论生成一条简短回复。
    流程：总结短期记忆 -> 生成回复 -> 记录到短期记忆。
    """
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    from app import (
        call_gemini, call_openrouter, get_model_config, get_ai_language,
        should_use_prompt_v2, build_system_prompt_v2, build_system_prompt,
        process_agent_actions, _execute_directive,
    )
    # 生成前：同步该角色的短期记忆
    try:
        sync_memory_before_moments(author_char_id)
    except Exception as e:
        print(f"   [Moment Reply] 记忆同步失败 {author_char_id}: {e}，继续生成")

    recent_messages = [post_content, user_comment]

    # 朋友圈回复用户不需要全局格式规则
    if should_use_prompt_v2(author_char_id):
        sys_prompt = build_system_prompt_v2(author_char_id, include_global_format=False, recent_messages=recent_messages, include_recent_messages=False, user_id=user_id)
    else:
        sys_prompt = build_system_prompt(author_char_id, include_global_format=False, recent_messages=recent_messages, user_id=user_id)

    lang = get_ai_language(author_char_id, user_id=user_id)
    now = datetime.now()
    if lang == "zh":
        user_msg = (
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
            f"你在朋友圈发了这条内容：\n{post_content}\n\n"
            f"用户评论说：「{user_comment}」\n\n"
            f"请以你的身份回复一条简短评论（一句话，100字以内）。只输出回复内容，不要引号或前缀。你也可以在回复中 @其他角色。"
        )
    elif lang == "en":
        user_msg = (
            f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')}\n"
            f"You posted this on Moments: \n{post_content}\n\n"
            f"User commented: \"{user_comment}\"\n\n"
            "Please reply with a short comment (one sentence, 100 chars max) in character. Output only the reply, without quotes or prefixes. You can also @ mention other characters."
        )
    else:
        user_msg = (
            f"現在時刻：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
            f"あなたの朋友圈投稿：\n{post_content}\n\n"
            f"ユーザーのコメント：「{user_comment}」\n\n"
            f"あなたの立場で短い返信を一言（100文字以内）で書いてください。返信の内容だけを出力し、引用符や接頭辞は不要です。必要に応じて他のキャラを @メンション することも可能です。"
        )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg}
    ]
    try:
        route, current_model = get_model_config("moments", user_id=user_id)
        if route == "relay":
            text = call_openrouter(messages, char_id=author_char_id, model_name=current_model, user_id=user_id)
        else:
            text = call_gemini(messages, char_id=author_char_id, model_name=current_model, user_id=user_id)
        if text:
            text = text.strip().strip('"\'')
            text, _, directive = process_agent_actions(author_char_id, text, user_id or get_current_user_id())
            if directive:
                uid = user_id or get_current_user_id()
                _d, _cid, _txt = directive, author_char_id, text
                def _bg(): set_background_user(uid); _execute_directive(_d, _cid, _txt)
                threading.Thread(target=_bg, daemon=True).start()
            if len(text) > 200:
                text = text[:200]

            text = clean_moments_agent_instructions(text)
            # (底层不再自动写入记忆，由调用方统一处理)
            return text
    except Exception as e:
        print(f"   [Moments] 角色回复评论失败 {author_char_id}: {e}")
    return None


def _execute_directive(directive, char_id, message_text):
    """
    执行转向指令（来自聊天或朋友圈）。
    directive: {"type": "user"} 或 {"type": "group", "member_ids": [...], ...}
    char_id: 发出指令的角色
    message_text: 角色的消息文本（已清理标签）
    """
    import sqlite3
    from app import (
        call_gemini, call_openrouter, get_model_config, get_ai_language,
        should_use_prompt_v2, build_system_prompt_v2, build_system_prompt,
        process_agent_actions, get_char_name, _get_char_chat_mode,
        init_char_db, sync_memory_before_single_chat, sync_memory_before_group_chat,
        build_group_relationship_prompt, ensure_directive_chat, get_group_dir,
    )
    user_id = get_current_user_id()
    try:
        char_name = get_char_name(char_id)
        print(f"  [_execute_directive] 开始执行, char={char_name}({char_id}), directive={directive}", flush=True)
        if directive.get("type") == "user":
            # 线下模式自动跳过转向单聊
            chat_mode = _get_char_chat_mode(char_id, user_id=user_id)
            if chat_mode == "offline":
                print(f"  [_execute_directive] {char_name} 处于线下模式，自动跳过转向单聊", flush=True)
                return
            s_db_path, _ = get_paths(char_id)
            if not os.path.exists(s_db_path):
                init_char_db(char_id)
            sync_memory_before_single_chat(char_id)

            # 调用 AI 生成一条单聊消息（而非直接复用原文本）
            print(f"  [_execute_directive] {char_name} 生成单聊消息...", flush=True)
            s_conn = sqlite3.connect(s_db_path)
            s_conn.row_factory = sqlite3.Row
            s_cursor = s_conn.cursor()
            s_cursor.execute("SELECT role, content FROM messages ORDER BY timestamp DESC LIMIT 15")
            s_rows = [dict(r) for r in s_cursor.fetchall()][::-1]
            s_conn.close()
            s_texts = [r["content"] for r in s_rows] if s_rows else []
            s_sys = build_system_prompt_v2(char_id, include_global_format=True, recent_messages=s_texts, user_id=user_id)

            # 始终以 system + user 格式构建消息，确保 Gemini 有内容可以回应
            s_msgs = [{"role": "system", "content": s_sys}]

            now_dt = datetime.now()
            lang = get_ai_language(char_id, user_id=user_id)
            if lang == "zh":
                user_msg = (
                    f"（系统提示：现在是 {now_dt.strftime('%H:%M')}。你想跟用户说点话，请自然地发一条消息。）\n"
                    f"（要求：简短、自然，符合你的人设。不要使用任何特殊标签。）"
                )
            elif lang == "ja":
                user_msg = (
                    f"（システム通知：現在は {now_dt.strftime('%H:%M')} です。ユーザーに話したいことがあります。自然にメッセージを送ってください。）\n"
                    f"（要件：簡潔で自然、キャラクターらしく。特別なタグは使わないこと。）"
                )
            else:
                user_msg = (
                    f"(System: It is {now_dt.strftime('%H:%M')}. You want to talk to the user. Send a natural message.)\n"
                    f"(Requirements: Short, natural, in character. Do not use any special tags.)"
                )
            s_msgs.append({"role": "user", "content": user_msg})

            s_route, s_model = get_model_config("chat", user_id=user_id)
            print(f"  Route: {s_route}, Model: {s_model}", flush=True)
            if s_route == "relay":
                s_reply = call_openrouter(s_msgs, char_id=char_id, model_name=s_model, user_id=user_id)
            else:
                s_reply = call_gemini(s_msgs, char_id=char_id, model_name=s_model, user_id=user_id)
            s_clean = re.sub(r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*', '', s_reply).strip()
            # 检查 API 错误
            if s_clean.startswith("（系统提示：") or s_clean.startswith("[ERROR]"):
                print(f"  [_execute_directive] API 错误: {s_clean}")
                return
            s_clean, _, _ = process_agent_actions(char_id, s_clean, user_id)

            if s_clean:
                s_conn2 = sqlite3.connect(s_db_path)
                s_cursor2 = s_conn2.cursor()
                s_cursor2.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", s_clean, now_dt.strftime('%Y-%m-%d %H:%M:%S')))
                s_conn2.commit()
                s_conn2.close()
                print(f"  {char_name}: {s_clean}", flush=True)
            print(f"  [_execute_directive] {char_name} 切换到单聊", flush=True)
            return

        elif directive.get("type") == "group":
            d_group_id = ensure_directive_chat(directive, char_id)
            d_groups_cfg = _get_groups_config_file()
            d_gconf = {}
            group_name = d_group_id
            if os.path.exists(d_groups_cfg):
                with open(d_groups_cfg, "r", encoding="utf-8") as f:
                    d_gconf = json.load(f)
                    if d_group_id in d_gconf:
                        group_name = d_gconf[d_group_id].get("name", d_group_id)
            d_all_members = (d_gconf or {}).get(d_group_id, {}).get("members", [])
            print(f"  [_execute_directive] {char_name} 发起群聊 {group_name} (id={d_group_id}), members={d_all_members}", flush=True)

            sync_memory_before_group_chat(d_group_id)
            d_db_path = os.path.join(get_group_dir(d_group_id), "chat.db")

            # --- 发起人先调用 API 在群聊中发起话题（带群聊上下文+记忆）---
            print(f"", flush=True)
            print(f"{'~'*50}", flush=True)
            print(f"  [_execute_directive] Initiator {char_name}({char_id}) 生成群聊话题...", flush=True)

            # 检测并读取已有的群聊历史
            db_history_rows = []
            if os.path.exists(d_db_path):
                try:
                    conn = sqlite3.connect(d_db_path)
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
                    if cursor.fetchone():
                        cursor.execute("SELECT role, content FROM messages ORDER BY timestamp DESC LIMIT 15")
                        db_history_rows = [dict(r) for r in cursor.fetchall()][::-1]
                    conn.close()
                except Exception as ex:
                    print(f"  ⚠️ 读取已有群聊历史失败: {ex}", flush=True)

            recent_texts = [r["content"] for r in db_history_rows] if db_history_rows else []
            is_existing_group = len(db_history_rows) > 0

            init_sys = build_system_prompt_v2(char_id, include_global_format=True, recent_messages=recent_texts, group_id=d_group_id, user_id=user_id)
            init_other = [m for m in d_all_members if m != char_id and m != "user"]
            init_rel = build_group_relationship_prompt(char_id, init_other)
            init_full = init_sys + "\n\n" + init_rel + "\nCurrent Situation\n当前是在群聊中。"
            init_msgs = [{"role": "system", "content": init_full}]

            if is_existing_group:
                print(f"  🔄 匹配到已有群聊，载入 {len(db_history_rows)} 条历史消息作为上下文", flush=True)
                for row in db_history_rows:
                    r_id = row["role"]
                    dname = "User" if r_id == "user" else get_char_name(r_id)
                    init_msgs.append({"role": "user", "content": f"[{dname}]: {row['content']}"})

        init_now = datetime.now()
        init_time_str = init_now.strftime('%H:%M')
        init_lang = get_ai_language(char_id, group_id=d_group_id, user_id=user_id)
        if is_existing_group:
            if init_lang == "zh":
                init_instruction = (
                    f"\n\nSystem Event / 系统事件\n"
                    f"现在是 {init_time_str}。这是已有的群聊 {group_name}，群友有 {', '.join([get_char_name(m) for m in d_all_members if m != char_id])}。\n"
                    f"请根据之前的群聊历史、当前时间、人际关系，使用中文自然地发起新一轮对话或接话。\n"
                    f"要求：自然、简短，符合你的人设。"
                )
            elif init_lang == "ja":
                init_instruction = (
                    f"\n\nSystem Event / システムイベント\n"
                    f"現在は {init_time_str} です。これは既存のグループチャット {group_name} で、メンバーは {', '.join([get_char_name(m) for m in d_all_members if m != char_id])} です。\n"
                    f"過去のチャット履歴、現在時刻、関係性に基づいて、日本語で自然に会話を再开するか、メッセージを送ってください。自然で簡潔に、キャラクターらしく。"
                )
            else:
                init_instruction = (
                    f"\n\nSystem Event\n"
                    f"It is now {init_time_str}. This is the existing group chat {group_name} with {', '.join([get_char_name(m) for m in d_all_members if m != char_id])}.\n"
                    f"Based on the previous history, current time, and relationships, please use {init_lang} to naturally resume the conversation or send a message. Natural and concise, in character."
                )
        else:
            if init_lang == "zh":
                init_instruction = (
                    f"\n\nSystem Event / 系统事件\n"
                    f"现在是 {init_time_str}。你刚刚创建了一个群聊并把 {', '.join([get_char_name(m) for m in d_all_members if m != char_id])} 拉了进来。\n"
                    f"请根据当前时间、人际关系，使用中文在群里**发起第一个话题**。\n"
                    f"要求：自然、简短，符合你的人设。"
                )
            elif init_lang == "ja":
                init_instruction = (
                    f"\n\nSystem Event / システムイベント\n"
                    f"現在は {init_time_str} です。あなたはグループチャットを作成し、{', '.join([get_char_name(m) for m in d_all_members if m != char_id])} を招待しました。\n"
                    f"日本語でグループに**最初の話題**を振ってください。自然で簡潔に、キャラクターらしく。"
                )
            else:
                init_instruction = (
                    f"\n\nSystem Event\n"
                    f"It is now {init_time_str}. You just created a group chat and invited {', '.join([get_char_name(m) for m in d_all_members if m != char_id])}.\n"
                    f"Please use {init_lang} to **start the first topic** in the group. Natural and concise, in character."
                )
        init_msgs.append({"role": "user", "content": init_instruction})

        init_route, init_model = get_model_config("chat", user_id=user_id)
        print(f"  Route: {init_route}, Model: {init_model}")
        if init_route == "relay":
            init_reply_raw = call_openrouter(init_msgs, char_id=char_id, model_name=init_model, user_id=user_id)
        else:
            init_reply_raw = call_gemini(init_msgs, char_id=char_id, model_name=init_model, user_id=user_id)
        init_reply = re.sub(r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*', '', init_reply_raw).strip()
        init_reply, _, _ = process_agent_actions(char_id, init_reply, user_id)
        init_name_pat = f"^\\[{char_name}\\][:：]\\s*"
        init_reply = re.sub(init_name_pat, '', init_reply).strip()
        print(f"  FIRST MESSAGE: {init_reply}")

        if init_reply:
            d_conn = sqlite3.connect(d_db_path)
            d_cursor = d_conn.cursor()
            d_cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", (char_id, init_reply, init_now.strftime('%Y-%m-%d %H:%M:%S')))
            d_conn.commit()
            d_conn.close()
        print(f"{'~'*50}")

        # --- 其他成员多轮自动回复 ---
        d_other = [m for m in d_all_members if m != "user"]
        if d_other:
            online_other = []
            c_conf_all = get_characters_config_for_current_user()
            for cid in d_other:
                cinfo = c_conf_all.get(cid, {})
                is_sleeping = cinfo.get("deep_sleep", False)
                member_chat_mode = cinfo.get("chat_mode", "online")
                if member_chat_mode == "offline":
                    is_sleeping = False
                if not is_sleeping:
                    online_other.append(cid)
            if not online_other:
                print(f"  其他成员均处于深睡，跳过自动回复")
            else:
                MAX_ROUNDS = 5
                decay_probs = [1.0, 0.7, 0.4, 0.2, 0.2]
                prev_last_speaker = char_id
                print(f"  多轮自动回复：{len(online_other)} 人在线，最多 {MAX_ROUNDS} 轮")
                should_stop = False

                for round_i in range(MAX_ROUNDS):
                    n_online = len(online_other)
                    k = random.randint(1, n_online) if n_online >= 2 else 1

                    # 选本轮发言人：第一个避开上一轮最后一人
                    candidates = list(online_other)
                    round_speakers = []
                    if prev_last_speaker and len(candidates) > 1 and prev_last_speaker in candidates:
                        candidates.remove(prev_last_speaker)
                    first = random.choice(candidates)
                    round_speakers.append(first)
                    # 其余人从全体中随机选（不重复）
                    rest_pool = [m for m in online_other if m not in round_speakers]
                    rest_k = min(k - 1, len(rest_pool))
                    if rest_k > 0:
                        extras = random.sample(rest_pool, rest_k)
                        round_speakers.extend(extras)

                    print(f"")
                    print(f"{'~'*50}")
                    print(f"  [Directive Round {round_i+1}/{MAX_ROUNDS}] 本轮 {len(round_speakers)} 人: {[get_char_name(s) for s in round_speakers]}")

                    for si, d_speaker_id in enumerate(round_speakers):
                        d_speaker_name = get_char_name(d_speaker_id)
                        try:
                            d_conn2 = sqlite3.connect(d_db_path)
                            d_conn2.row_factory = sqlite3.Row
                            d_cursor2 = d_conn2.cursor()
                            d_cursor2.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
                            d_rows = [dict(r) for r in d_cursor2.fetchall()][::-1]
                            d_conn2.close()
                            d_texts = [r["content"] for r in d_rows] if d_rows else []
                            d_sys = build_system_prompt_v2(d_speaker_id, include_global_format=True, recent_messages=d_texts, group_id=d_group_id, user_id=user_id)
                            d_other_ids = [m for m in d_all_members if m != d_speaker_id]
                            d_rel = build_group_relationship_prompt(d_speaker_id, d_other_ids)
                            d_full = d_sys + "\n\n" + d_rel + "\nCurrent Situation\n当前是在群聊中。"
                            d_msgs = [{"role": "system", "content": d_full}]
                            if d_rows:
                                row = d_rows[-1]
                                r_id = row["role"]
                                dname = "User" if r_id == "user" else get_char_name(r_id)
                                d_msgs.append({"role": "user", "content": f"[{dname}]: {row['content']}"})
                            d_route, d_model = get_model_config("chat", user_id=user_id)
                            print(f"  [{si+1}/{len(round_speakers)}] {d_speaker_name}({d_speaker_id}) | Route: {d_route}, Model: {d_model}")
                            if d_route == "relay":
                                d_reply = call_openrouter(d_msgs, char_id=d_speaker_id, model_name=d_model, user_id=user_id)
                            else:
                                d_reply = call_gemini(d_msgs, char_id=d_speaker_id, model_name=d_model, user_id=user_id)

                            has_end = re.search(r'\[DIRECT_END\]', d_reply, re.IGNORECASE)
                            d_clean = re.sub(r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*', '', d_reply).strip()
                            d_clean, _, _ = process_agent_actions(d_speaker_id, d_clean, user_id)
                            d_name_pat = f"^\\[{d_speaker_name}\\][:：]\\s*"
                            d_clean = re.sub(d_name_pat, '', d_clean).strip()

                            if not d_clean:
                                print(f"  空回复，结束对话")
                                should_stop = True
                                break

                            d_conn3 = sqlite3.connect(d_db_path)
                            d_cursor3 = d_conn3.cursor()
                            d_cursor3.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", (d_speaker_id, d_clean, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                            d_conn3.commit()
                            d_conn3.close()
                            print(f"  {d_speaker_name}: {d_clean}")

                            prev_last_speaker = d_speaker_id

                            if has_end:
                                print(f"  {d_speaker_name} 发出 [DIRECT_END]，结束对话")
                                should_stop = True
                                break

                        except Exception as e:
                            print(f"  [Directive] {d_speaker_id} 自动回复失败: {e}")
                            import traceback
                            traceback.print_exc()
                            should_stop = True
                            break

                    if should_stop:
                        break

                    # 衰减概率（从第2轮开始）
                    if round_i > 0:
                        prob = decay_probs[min(round_i, len(decay_probs)-1)]
                        if random.random() > prob:
                            print(f"  衰减概率 {prob:.0%}, 结束对话")
                            break

                    if round_i == MAX_ROUNDS - 1:
                        print(f"  已达最大轮数 {MAX_ROUNDS}")
        else:
            print(f"  群聊无其他成员，跳过自动回复")
    except Exception as e:
        print(f"  [_execute_directive] 崩溃: {e}", flush=True)
        import traceback
        traceback.print_exc()


def _generate_ai_reply_to_any_comment(replying_char_id, post_author_id, post_content, comments_list, target_comment_index, user_id=None):
    """
    让 replying_char_id 对朋友圈的某条特定评论进行回复。
    """
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    from app import (
        call_gemini, call_openrouter, get_model_config, get_ai_language,
        should_use_prompt_v2, build_system_prompt_v2, build_system_prompt,
        process_agent_actions, get_char_name, _execute_directive,
        send_push_notification, send_email_notification,
    )
    from core.circuit_breaker import (
        get_circuit_breaker_info, clear_circuit_breaker_info,
    )
    # 生成前：同步该角色的短期记忆
    try:
        sync_memory_before_moments(replying_char_id)
    except Exception as e:
        print(f"   [Moment Any Reply] 记忆同步失败 {replying_char_id}: {e}，继续生成")

    target_comment = comments_list[target_comment_index]
    target_comment_author_id = target_comment.get("commenter_id")
    target_comment_content = target_comment.get("content", "")

    # 获取所有参与者的名字
    def get_name(cid):
        if cid == "user": return get_current_username()
        return get_char_name(cid)

    post_author_name = get_name(post_author_id)
    target_author_name = get_name(target_comment_author_id)

    # 提取整个评论区作为上下文
    comments_context = ""
    for c in comments_list:
        c_name = get_name(c.get("commenter_id"))
        rep_to = c.get("reply_to")
        if rep_to:
            rep_name = get_name(rep_to)
            comments_context += f"- {c_name} 回复 {rep_name}：{c.get('content')}\n"
        else:
            comments_context += f"- {c_name}：{c.get('content')}\n"

    # 生成系统提示：包含对 "目标评论者" 的关系
    # 朋友圈回复评论不需要全局格式规则
    if should_use_prompt_v2(replying_char_id):
        sys_prompt = build_system_prompt_v2(replying_char_id, include_global_format=False, recent_messages=[post_content, target_comment_content], target_char_id=target_comment_author_id, user_id=user_id)
    else:
        sys_prompt = build_system_prompt(replying_char_id, include_global_format=False, recent_messages=[post_content, target_comment_content], target_char_id=target_comment_author_id, user_id=user_id)

    lang = get_ai_language(replying_char_id, user_id=user_id)
    now = datetime.now()
    if lang == "zh":
        user_msg = (
            "评论互动任务\n"
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
            "你正在浏览社交软件的朋友圈，现在你需要对其中的一条评论进行「回复」。只输出回复内容，不要加引号或「回复：」等前缀。\n\n"
            f"朋友圈原文\n"
            f"发布者：{post_author_name}\n"
            f"内容：{post_content}\n\n"
            f"当前评论区的所有评论\n"
            f"{comments_context}\n"
            f"你要回复的目标评论\n"
            f"评论者：{target_author_name}（你与他/她的关系已包含在人设中）\n"
            f"TA的评论内容：「{target_comment_content}」\n\n"
            "请结合整体语境，特别是针对你要回复的这条评论，以你的身份进行真实简短的回复（一两句话即可，100字以内）。你也可以在回复中 @其他角色。\n"
            f"重要：当前语言设定为 {lang}，请务必使用该语言回复。"
        )
    elif lang == "ja":
        user_msg = (
            "コメント返信タスク\n"
            f"現在時刻：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
            "あなたは今、朋友圈に投稿されたあるコメントに対して「返信」をします。返信内容のみを出力し、引用符や「返信：」などは不要です。\n\n"
            f"投稿原文\n"
            f"投稿者：{post_author_name}\n"
            f"内容：{post_content}\n\n"
            f"すべてのコメント\n"
            f"{comments_context}\n"
            f"あなたが返信するターゲット\n"
            f"コメント者：{target_author_name}（関係性は設定に含まれています）\n"
            f"ターゲットの内容：「{target_comment_content}」\n\n"
            "全体の文脈を踏まえつつ、指定されたコメントに対して、あなたらしい自然で短い返信（1〜2文、100文字以内）を作成してください。他のキャラを @メンションすることも可能です。\n"
            f"重要：指定言語は {lang} です。必ずその言語で返信してください。"
        )
    else:
        user_msg = (
            "[Comment Interaction Task]\n"
            f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')}\n"
            "You are browsing a social media feed and need to reply to a specific comment. Output only the content of the reply, without quotes or prefixes like 'Reply:'.\n\n"
            f"[Original Post]\n"
            f"Author: {post_author_name}\n"
            f"Content: {post_content}\n\n"
            f"[All Comments]\n"
            f"{comments_context}\n"
            f"[Target Comment to Reply to (IMPORTANT)]\n"
            f"Commenter: {target_author_name} (Your relationship is in your persona)\n"
            f"Content: \"{target_comment_content}\"\n\n"
            "Considering the overall context, especially the target comment, write a natural and short reply (one or two sentences, 100 chars max) in character. You can @ mention other characters too.\n"
            f"Important: Your assigned language is {lang}. Please reply in this language."
        )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg}
    ]

    try:
        route, current_model = get_model_config("moments", user_id=user_id)
        if route == "relay":
            text = call_openrouter(messages, char_id=replying_char_id, model_name=current_model, user_id=user_id)
        else:
            text = call_gemini(messages, char_id=replying_char_id, model_name=current_model, user_id=user_id)

        if text:
            text = text.strip().strip('"\'')
            text, _, directive = process_agent_actions(replying_char_id, text, user_id or get_current_user_id())
            if directive:
                uid = user_id or get_current_user_id()
                _d, _cid, _txt = directive, replying_char_id, text
                def _bg(): set_background_user(uid); _execute_directive(_d, _cid, _txt)
                threading.Thread(target=_bg, daemon=True).start()
            if len(text) > 200:
                text = text[:200]

            text = clean_moments_agent_instructions(text)
            # (底层不再自动写入记忆，由调用方统一处理)
            return text
    except Exception as e:
        print(f"   [Moments] 任意回复评论生成失败 {replying_char_id}: {e}")
    return None


def _disable_char_active_messaging(char_id, user_id=None):
    """因 API 致命错误，自动为该角色打开浅睡眠，停止其主动消息。"""
    cfg_file = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(cfg_file):
        return
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if char_id in cfg:
            cfg[char_id]["light_sleep"] = True
            safe_save_json(cfg_file, cfg)
            print(f"  [AutoStop] 角色 {char_id} 已自动开启浅睡眠，停止主动消息。")
    except Exception as e:
        print(f"  [AutoStop] 停止角色 {char_id} 主动消息失败: {e}")


def _disable_group_active_messaging(group_id):
    """因 API 致命错误，自动关闭该群的主动消息(active_mode)。"""
    cfg_file = _get_groups_config_file()
    if not os.path.exists(cfg_file):
        return
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if group_id in cfg:
            cfg[group_id]["active_mode"] = False
            safe_save_json(cfg_file, cfg)
            print(f"  [AutoStop] 群 {group_id} 已自动关闭主动消息。")
    except Exception as e:
        print(f"  [AutoStop] 停止群 {group_id} 主动消息失败: {e}")


def _generate_random_ts_within_24h(base_ts_str):
    """辅助：生成相对于基准时间24小时内的随机时间戳"""
    try:
        base_dt = datetime.strptime(base_ts_str, "%Y-%m-%d %H:%M:%S")
        delta_sec = random.randint(300, 3600 * 12) # 5分钟到12小时后
        new_dt = base_dt + timedelta(seconds=delta_sec)
        # 避开深睡眠时间 23-7
        if new_dt.hour >= 23 or new_dt.hour < 7:
            new_dt = new_dt + timedelta(hours=8)
        return new_dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return base_ts_str


def _background_generate_moment_reactions(user_id, char_id, post_ts_str, post_content, mentioned_ids=None):
    def worker(m_ids):
        try:
            print(f"  [Moments Background] Worker started for user_id={user_id}, post_author={char_id}, post_ts={post_ts_str}")
            set_background_user(user_id)
            if m_ids is None:
                m_ids = []

            new_likers = []
            new_comments = []

            # 获取当前用户的所有角色
            chars_config = get_characters_config_for_current_user()
            if not chars_config:
                print(f"  [Moments Background] No characters config found for user {user_id}, worker exiting.")
                return

            # 获取备注映射（用于记录记忆）
            _, remarks = _get_moments_id_display(user_id=user_id)

            # 如果发帖者是角色（非用户），按关系图谱筛选潜在互动角色
            rel_score_map = {}
            if char_id != "user":
                rel_candidates = _get_moments_relationship_candidates(char_id, user_id=user_id)
                rel_score_map = {cid: score for cid, score in rel_candidates}

            for target_cid, info in chars_config.items():
                if target_cid == char_id: continue # 自己不给自己点赞评论
                if target_cid in m_ids: continue # 已经同步处理过了

                if char_id != "user":
                    # 角色发帖：只有关系图谱内有正向关系的角色才能互动
                    if target_cid not in rel_score_map:
                        continue
                    rel_score = max(0, rel_score_map[target_cid])
                    p_like = min(1.0, rel_score / 100.0)
                    p_comment = min(1.0, rel_score / 100.0) * 0.6
                else:
                    intimacy = max(0, min(100, int(float(info.get("intimacy", 60)))))
                    p_like = intimacy / 100.0
                    p_comment = (intimacy / 100.0) * 0.6

                # 随机决定是否点赞/评论
                should_like = random.random() < p_like
                should_comment = random.random() < p_comment

                if not should_like and not should_comment:
                    continue

                print(f"   [Moments Background] Evaluating char {target_cid}: like={should_like}, comment={should_comment}")

                if should_like:
                    # 后台互动的点赞时间随机一下（在24小时内）
                    new_likers.append({"liker_id": target_cid, "timestamp": _generate_random_ts_within_24h(post_ts_str)})

                if should_comment:
                    comment_text = _generate_moment_comment(target_cid, char_id, post_content, user_id=user_id)
                    if comment_text:
                        print(f"   [Moments Background] Generated comment from {target_cid}: {comment_text[:30]}...")
                        comment_ts = _generate_random_ts_within_24h(post_ts_str)
                        new_comments.append({
                            "commenter_id": target_cid,
                            "content": comment_text,
                            "timestamp": comment_ts
                        })
                        # 记录短期记忆
                        try:
                            from app import get_char_name
                            def get_name_internal(cid):
                                if cid == "user": return get_current_username()
                                return remarks.get(cid) or get_char_name(cid) or cid
                            author_name = get_name_internal(char_id)
                            author_ref = "用户" if char_id == "user" else author_name
                            mem_ctx = f"看到{author_ref}的朋友圈：「{post_content[:100]}」。你评论说：「{comment_text}」。"
                            append_moment_event_to_short_memory(target_cid, mem_ctx)
                        except: pass
                    else:
                        print(f"   [Moments Background] Comment generation failed for {target_cid}.")

            if not new_likers and not new_comments:
                print(f"  [Moments Background] No new reactions generated for post {post_ts_str}, worker finished.")
                return

            # 回填到文件
            moments_path, _ = get_moments_paths(user_id=user_id)
            if os.path.exists(moments_path):
                with open(moments_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)

                # 寻找匹配的帖子
                found = False
                for post in data:
                    if post.get("char_id") == char_id and post.get("timestamp") == post_ts_str:
                        post.setdefault("likers", []).extend(new_likers)
                        post.setdefault("comments", []).extend(new_comments)
                        found = True
                        break

                if found:
                    safe_save_json(moments_path, data)
                    print(f"  [Moments Background] Successfully saved {len(new_likers)} likes and {len(new_comments)} comments for post {post_ts_str}.")
                else:
                    print(f"  [Moments Background] Could not find post with ts {post_ts_str} to save reactions.")

        except Exception as e:
            print(f"  [Moments Background] An unexpected error occurred in worker: {e}")
            import traceback
            traceback.print_exc()

    threading.Thread(target=worker, args=(mentioned_ids,), daemon=True).start()


def _generate_likes_comments_for_user_moment(post_ts_str, post_content, only_mentioned=False, user_id=None):
    """用户发朋友圈后，根据各角色亲密度随机生成点赞和评论。返回 (likers, comments)。"""
    now = datetime.now()
    try:
        post_dt = datetime.strptime(post_ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        post_dt = now
    end_dt = post_dt + timedelta(hours=24)

    def random_ts_in_24h():
        """生成24小时内的随机时间戳，避免在23:00~7:00之间（深睡眠时间）"""
        while True:
            delta_sec = random.randint(0, 24 * 3600)
            t = post_dt + timedelta(seconds=delta_sec)
            hour = t.hour
            # 避开23:00~7:00的时间段
            if not (hour >= 23 or hour < 7):
                return t.strftime("%Y-%m-%d %H:%M:%S")

    likers = []
    comments = []
    chars_cfg = _get_characters_config_file(user_id=user_id)
    if not os.path.exists(chars_cfg):
        return likers, comments
    try:
        with open(chars_cfg, "r", encoding="utf-8-sig") as f:
            chars_config = json.load(f)
    except Exception:
        return likers, comments

    # 解析 @ 提及的角色（支持 name、remark、cid 三种标识，且不区分大小写，支持半角与全角 @）
    mentioned_ids = []
    name_to_id = _get_moments_name_to_id(user_id=user_id)
    post_content_lower = post_content.lower()
    for disp_name, cid in name_to_id.items():
        disp_name_lower = disp_name.lower()
        if f"@{disp_name_lower}" in post_content_lower or f"\uff20{disp_name_lower}" in post_content_lower:
            if cid not in mentioned_ids:
                mentioned_ids.append(cid)

    for char_id, info in chars_config.items():
        # 如果是被 @ 的角色，必须生成评论，且时间与朋友圈相同
        is_mentioned = char_id in mentioned_ids

        # 如果指定只处理 mentioned，且当前不是 mentioned，则跳过
        if only_mentioned and not is_mentioned:
            continue

        intimacy = max(0, min(100, int(info.get("intimacy", 60))))
        p_like = intimacy / 100.0
        p_comment = (intimacy / 100.0) * 0.6

        should_comment = is_mentioned or (random.random() < p_comment)
        should_like = is_mentioned or (random.random() < p_like)

        if should_like:
            ts = post_ts_str if is_mentioned else random_ts_in_24h()
            likers.append({"liker_id": char_id, "timestamp": ts})

        if should_comment:
            comment_text = _generate_moment_comment(char_id, "user", post_content, is_mentioned=is_mentioned, user_id=user_id)
            if comment_text:
                ts = post_ts_str if is_mentioned else random_ts_in_24h()
                comments.append({
                    "commenter_id": char_id,
                    "content": comment_text,
                    "timestamp": ts
                })
    return likers, comments


def trigger_active_moments(char_id, user_id=None, instruction=None):
    """生成一条该角色的朋友圈内容，并按关系图谱生成点赞与评论（排除用户）。
    可选的 instruction 会作为用户自定义要求追加到提示词中。"""
    from app import (
        call_gemini, call_openrouter, get_model_config, get_ai_language,
        should_use_prompt_v2, build_system_prompt_v2, build_system_prompt,
        process_agent_actions, get_char_name, _execute_directive,
        send_push_notification, send_email_notification,
    )
    from core.circuit_breaker import (
        get_circuit_breaker_info, clear_circuit_breaker_info,
    )
    from cos_utils import upload_to_cos

    print(f"  [Moments] 尝试触发 {char_id} 的主动朋友圈...")

    # 发朋友圈前先同步该角色的单聊与所有群聊短期记忆，便于 AI 结合最近经历
    try:
        ok, err = sync_memory_before_moments(char_id)
        if not ok:
            print(f"     [Moments] 记忆同步失败: {err}，继续生成")
    except Exception as e:
        print(f"     [Moments] 记忆同步异常: {e}，继续生成")

    # 主动发朋友圈不需要全局格式规则
    if should_use_prompt_v2(char_id):
        base_system_prompt = build_system_prompt_v2(char_id, include_global_format=False, recent_messages=None, include_long_memory=False, include_recent_messages=False, user_id=user_id)
    else:
        base_system_prompt = build_system_prompt(char_id, include_global_format=False, recent_messages=None, include_long_memory=False, user_id=user_id)

    remarks = {}
    cfg_file = _get_characters_config_file(user_id=user_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                chars_cfg = json.load(f)
            for cid, cdata in chars_cfg.items():
                r = (cdata.get("remark") or "").strip()
                if r:
                    remarks[cid] = r
        except Exception:
            pass

    now = datetime.now()
    post_ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
    lang = get_ai_language(char_id, user_id=user_id)

    # 统一指令模板：直接用语言代码，AI 能理解
    trigger_msg = (
        f"[Task: Post to Moments / タスク：朋友圈投稿]\n"
        f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')}\n"
        f"Post a short, natural Moments update based on current time and recent experiences. You may:\n"
        "- Text only; or\n"
        "- Photos: `[SEARCH_IMG: keyword]` tag (up to 9). Example: `[SEARCH_IMG: sunset]`\n"
        "- @mentions: Mention other characters (e.g., @Name) if you want them to engage.\n"
        "- Redirect: `[DIRECT_TO_GROUP: char1, char2]` with `+user` or `[DIRECT_TO_USER]`.\n"
        "- Movement: Move around using:\n"
        "\t1. `[MOVE_TO: location_id]` - Move to a known location (use when moving to a place already in your known/perceived list).\n"
        "\t2. `[MOVE_TO_COORD: x,y]` - Move to coordinates (use when wandering around freely without a specific named location).\n"
        "\t3. `[EXPLORE: x,y,\"name\",\"desc\"]` - Move to a new location (use when moving to a place NOT in your known/perceived list, this creates a new location).\n"
        f"\n**IMPORTANT: You MUST write this post exclusively in language code `{lang}`.**\n"
        "Output ONLY the post content. No quotes or prefixes."
    )

    if instruction and instruction.strip():
        trigger_msg += (
            f"\n\n**SPECIAL REQUIREMENT / 特别要求:**\n{instruction.strip()}\n"
            "请务必严格按照上述特别要求的内容、话题与风格来发布朋友圈。"
        )

    messages = [
        {"role": "system", "content": base_system_prompt},
        {"role": "user", "content": trigger_msg}
    ]

    try:
        clear_circuit_breaker_info()
        route, current_model = get_model_config("moments", user_id=user_id)
        if route == "relay":
            content = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        else:
            content = call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)

        cb_info = get_circuit_breaker_info()
        if cb_info:
            print(f"  [AutoStop] 主动朋友圈触发熔断 {cb_info}，自动关闭主动朋友圈。")
            _set_active_moments_enabled(False)
            return False

        if not content:
            return False
        content = content.strip().strip('"\'')
        if not content:
            return False

        content, _, directive_m = process_agent_actions(char_id, content, user_id or get_current_user_id())
        if directive_m:
            uid = user_id or get_current_user_id()
            _d, _cid, _txt = directive_m, char_id, content
            def _bg(): set_background_user(uid); _execute_directive(_d, _cid, _txt)
            threading.Thread(target=_bg, daemon=True).start()

        # --- 朋友圈媒体标签解析 ---
        content = process_moments_media_tags(content, char_id, user_id=user_id or get_current_user_id())
        content = clean_moments_agent_instructions(content)

    except Exception as e:
        print(f"  [Moments] 生成内容失败: {e}")
        return False

    likers_data = []
    comments_data = []

    # 解析 @ 提及（支持 name、remark、cid 三种标识，半角 @ 和全角 ＠，不区分大小写）
    name_to_id = _get_moments_name_to_id(user_id=user_id)
    mentioned_ids = []
    content_lower = content.lower()
    for disp_name, cid in name_to_id.items():
        disp_name_lower = disp_name.lower()
        if f"@{disp_name_lower}" in content_lower or f"\uff20{disp_name_lower}" in content_lower:
            if cid not in mentioned_ids:
                mentioned_ids.append(cid)

    if mentioned_ids:
        print(f"  [Moments] 检测到 @ 提及: {mentioned_ids}")
        for mid in mentioned_ids:
            if mid == "user" or mid == char_id: continue
            likers_data.append({"liker_id": mid, "timestamp": post_ts_str})
            comment_text = _generate_moment_comment(mid, char_id, content, is_mentioned=True, user_id=user_id)
            if comment_text:
                print(f"   -> [Sync Reply] {mid} 已回复")
                comments_data.append({
                    "commenter_id": mid,
                    "content": comment_text,
                    "timestamp": post_ts_str
                })
                try:
                    def get_name_internal(cid):
                        if cid == "user": return get_current_username()
                        return remarks.get(cid) or get_char_name(cid) or cid
                    author_name = get_name_internal(char_id)
                    mem_ctx = f"在{author_name}的朋友圈：「{content[:100]}」下，你评论说：「{comment_text}」。"
                    append_moment_event_to_short_memory(mid, mem_ctx)
                except: pass

    new_post = {
        "char_id": char_id,
        "content": content,
        "timestamp": post_ts_str,
        "likers": likers_data,
        "comments": comments_data
    }
    if instruction and instruction.strip():
        new_post["instruction"] = instruction.strip()

    # 保存帖子
    moments_path, last_post_path = get_moments_paths(user_id=user_id)
    raw = []
    if os.path.exists(moments_path):
        try:
            with open(moments_path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except: raw = []
    raw.append(new_post)
    safe_save_json(moments_path, raw)

    # 记录发帖者记忆
    ctx = f"你发了一条朋友圈，内容：「{(content or '')[:300]}」。"
    append_moment_event_to_short_memory(char_id, ctx, user_id=user_id)

    # 更新上次发帖时间
    last_post = {}
    if os.path.exists(last_post_path):
        try:
            with open(last_post_path, "r", encoding="utf-8-sig") as f:
                last_post = json.load(f)
        except: pass
    last_post[char_id] = post_ts_str
    safe_save_json(last_post_path, last_post)

    # 启动后台任务：处理其余非 @ 角色的随机互动
    _background_generate_moment_reactions(user_id or get_current_user_id(), char_id, post_ts_str, content, mentioned_ids=mentioned_ids)

    print(f"  [Moments] 发送成功（同步部分）: {content[:50]}...")
    return True


# =============================================================================
# Route Handlers
# =============================================================================

@moments_bp.route("/moments")
def moments_view():
    from app import get_ai_language
    return render_template("moments.html", ai_lang=get_ai_language())


@moments_bp.route("/api/moments/active_enabled", methods=["GET"])
def get_active_moments_enabled():
    """获取主动朋友圈开关状态。"""
    return jsonify({"enabled": _get_active_moments_enabled()})


@moments_bp.route("/api/moments/active_enabled", methods=["POST"])
def set_active_moments_enabled():
    """设置主动朋友圈开关。body: { "enabled": true|false }。"""
    data = request.get_json() or {}
    enabled = data.get("enabled", True)
    _set_active_moments_enabled(enabled)
    return jsonify({"enabled": _get_active_moments_enabled()})


@moments_bp.route("/api/moments/characters", methods=["GET"])
def get_moments_characters():
    """获取所有有权发朋友圈的角色列表。用于前端筛选。"""
    user_id = get_current_user_id()
    _, remarks = _get_moments_id_display(user_id=user_id)
    # 移除 user，前端单独处理
    if "user" in remarks:
        remarks.pop("user")
    return jsonify(remarks)


@moments_bp.route("/api/moments/related_characters", methods=["GET"])
def get_moments_related_characters():
    """获取与某个/多个角色都有关系图谱的角色列表（即关系图谱交集）。如果 target_id 为 user 或关系图为找不到，则返回全部角色。"""
    user_id = get_current_user_id()
    target_id_str = request.args.get("target_id")
    _, remarks = _get_moments_id_display(user_id=user_id)
    if "user" in remarks:
        remarks.pop("user")

    if not target_id_str:
        return jsonify(remarks)

    # 提取需要求交集的所有角色ID
    target_ids = [t.strip() for t in target_id_str.split(",") if t.strip() and t.strip() != "user" and t.strip() in remarks]

    if not target_ids:
        return jsonify(remarks)

    try:
        current_user_name = get_current_username()
        name_to_cid = {}
        cfg_file = _get_characters_config_file(user_id=user_id)
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                for cid, info in json.load(f).items():
                    name = (info.get("name") or "").strip()
                    remark = (info.get("remark") or "").strip()
                    if name:
                        name_to_cid[name] = cid
                    if remark and remark != name:
                        name_to_cid[remark] = cid

        intersection_cids = None

        for tid in target_ids:
            _, prompts_dir = get_paths(tid, user_id=user_id)
            rel_path = os.path.join(prompts_dir, "2_relationship.json")

            # 如果某个人没有关系图数据，这里交集处理为：假定他没有特定的限制，即视为全集。
            # 或者将其视为只有空集。为了不出问题且尊重"交集"要求，如果文件不存在，我们假设返回全为空
            if not os.path.exists(rel_path):
                # 这个人的关系图为空，那么和其他人的交集就是空
                intersection_cids = set()
                break

            with open(rel_path, "r", encoding="utf-8") as f:
                rel_data = json.load(f)

            current_rels = set()
            for name, _ in rel_data.items():
                if name.strip() == current_user_name:
                    continue
                cid = name_to_cid.get(name.strip())
                if cid and cid in remarks:
                    current_rels.add(cid)

            # 将自己（发帖人/被回复人）的关系图谱求交集。
            # 这里 current_rels 是 tid 认识的人。
            # 我们还需要把 tid 本人也加入到 current_rels 中，因为 tid 本人肯定可以对自己做出反应或回复
            current_rels.add(tid)

            if intersection_cids is None:
                intersection_cids = current_rels
            else:
                intersection_cids = intersection_cids.intersection(current_rels)

        if not intersection_cids:
            # 如果交集为空，直接返回空字典显示无相关关系人
            return jsonify({})

        filtered_remarks = {cid: remarks[cid] for cid in intersection_cids}
        return jsonify(filtered_remarks)
    except Exception as e:
        print(f"Error fetching related characters for {target_id_str}: {e}")
        return jsonify(remarks)


@moments_bp.route("/api/moments", methods=["GET"])
def get_moments():
    """朋友圈列表。数据格式：char_id, content, timestamp, liker_ids, comments (commenter_id, content, timestamp)。评论时间大于当前时间的不返回。"""
    user_id = get_current_user_id()
    now = datetime.now()
    # 分页参数：默认第 1 页，每页 10 条
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    if page < 1: page = 1
    page_size = 10

    filter_char_id = request.args.get("filter_char_id", "all")

    avatars, remarks = _get_moments_id_display()
    moments = []
    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify(moments)
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"moments_data.json load error: {e}")
        return jsonify(moments)
    for post in raw:
        char_id = post.get("char_id", "")
        # 角色筛选逻辑
        if filter_char_id != "all":
            if filter_char_id == "user":
                if char_id != "user": continue
            elif char_id != filter_char_id:
                continue

        comments_ok = []
        for src_idx, c in enumerate(post.get("comments", [])):
            try:
                ts = datetime.strptime(c["timestamp"], "%Y-%m-%d %H:%M:%S")
                if ts <= now:
                    reply_to_id = c.get("reply_to")
                    reply_to_remark = remarks.get(reply_to_id, reply_to_id or "") if reply_to_id else ""
                    comments_ok.append({
                        "comment_index": src_idx,
                        "commenter_id": c.get("commenter_id", ""),
                        "content": clean_moments_agent_instructions(c.get("content", "")),
                        "timestamp": c.get("timestamp", ""),
                        "avatar": avatars.get(c.get("commenter_id"), "/static/default_avatar.png"),
                        "remark": remarks.get(c.get("commenter_id"), c.get("commenter_id", "")),
                        "reply_to": reply_to_id,
                        "reply_to_remark": reply_to_remark
                    })
            except: pass
        # 支持新格式 likers: [{liker_id, timestamp}] 与旧格式 liker_ids: []
        liker_ids_raw = post.get("liker_ids", [])
        likers_with_ts = post.get("likers", [])
        if likers_with_ts:
            liker_ids_ok = []
            for like in likers_with_ts:
                try:
                    ts = datetime.strptime(like.get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
                    if ts <= now:
                        liker_ids_ok.append(like.get("liker_id", ""))
                except: pass
            liker_remarks = [remarks.get(lid, lid) for lid in liker_ids_ok]
        else:
            liker_ids_ok = liker_ids_raw
            liker_remarks = [remarks.get(lid, lid) for lid in liker_ids_ok]
        moments.append({
            "char_id": char_id,
            "content": clean_moments_agent_instructions(post.get("content", "")),
            "timestamp": post.get("timestamp", ""),
            "avatar": avatars.get(char_id, "/static/default_avatar.png"),
            "remark": remarks.get(char_id, char_id),
            "liker_ids": liker_ids_ok,
            "liker_remarks": liker_remarks,
            "comments": comments_ok
        })
    moments.sort(key=lambda x: x["timestamp"], reverse=True)

    # 分页截取
    start = (page - 1) * page_size
    end = start + page_size
    paged = moments[start:end]
    return jsonify(paged)


@moments_bp.route("/api/moments/like", methods=["POST"])
def moments_like():
    """用户点赞一条朋友圈。body: { char_id, timestamp }。"""
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    likers = post.get("likers", [])
    # 幂等：仅当用户尚未点赞时才添加
    already_liked = any(l.get("liker_id") == "user" for l in likers) or ("user" in post.get("liker_ids", []))
    if not already_liked:
        likers.append({"liker_id": "user", "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        post["likers"] = likers
        raw[idx] = post
        safe_save_json(moments_path, raw)

    return jsonify({"status": "success", "liked": True})


@moments_bp.route("/api/moments/unlike", methods=["POST"])
def moments_unlike():
    """用户取消点赞一条朋友圈。body: { char_id, timestamp }。"""
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    likers = post.get("likers", [])
    # 过滤掉用户的点赞
    new_likers = [l for l in likers if l.get("liker_id") != "user"]
    changed = len(new_likers) != len(likers)

    # 同时也处理旧版字段 liker_ids (如果有)
    if post.get("liker_ids"):
        new_liker_ids = [lid for lid in post["liker_ids"] if lid != "user"]
        if len(new_liker_ids) != len(post["liker_ids"]):
            post["liker_ids"] = new_liker_ids
            changed = True

    if changed:
        post["likers"] = new_likers
        raw[idx] = post
        safe_save_json(moments_path, raw)

    return jsonify({"status": "success", "liked": False})


@moments_bp.route("/api/moments/comment", methods=["POST"])
def moments_comment():
    """用户评论一条朋友圈。body: { char_id, timestamp, content }。"""
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    content = (data.get("content") or "").strip()
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400
    if not content:
        return jsonify({"error": "评论内容不能为空"}), 400
    if len(content) > 500:
        return jsonify({"error": "评论内容不得超过500字"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    chars_config = {}
    cfg_file = _get_characters_config_file(user_id=user_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                chars_config = json.load(f)
        except Exception:
            pass

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comments = post.get("comments", [])
    comments.append({"commenter_id": "user", "content": content, "timestamp": now})
    post["comments"] = comments
    raw[idx] = post
    safe_save_json(moments_path, raw)

    # 仅当评论的不是用户自己的朋友圈时，才由作者生成回复和记忆
    author_char_id = char_id
    post_content = post.get("content", "")

    replied_ids = set()
    if author_char_id != "user":
        reply_text = _generate_moment_reply_to_user(author_char_id, post_content, content)
        if reply_text:
            comments = post.get("comments", [])
            comments.append({"commenter_id": author_char_id, "content": reply_text, "timestamp": now, "reply_to": "user"})
            post["comments"] = comments
            raw[idx] = post
            safe_save_json(moments_path, raw)
            replied_ids.add(author_char_id)
            # 记录记忆
            ctx = f"你在朋友圈发了内容：「{post_content}」。对于用户的评论「{content}」，你回复说：「{reply_text}」。"
            append_moment_event_to_short_memory(author_char_id, ctx)

    # 处理评论中的 @ 提及（支持 name、remark、cid 三种标识）
    name_to_id = _get_moments_name_to_id()
    at_matches = re.findall(r"@([^\s@]+)", content)
    mentioned_ids = []
    for name in at_matches:
        if name in name_to_id:
            mentioned_ids.append(name_to_id[name])

    for m_id in mentioned_ids:
        if m_id != "user" and m_id not in replied_ids:
            # 被 @ 的角色也发表评论
            user_id = get_current_user_id()
            ai_comment = _generate_moment_comment(m_id, author_char_id, post_content, is_mentioned=True, user_id=user_id)
            if ai_comment:
                comments = post.get("comments", [])
                comments.append({"commenter_id": m_id, "content": ai_comment, "timestamp": now})
                post["comments"] = comments
                raw[idx] = post
                safe_save_json(moments_path, raw)
                replied_ids.add(m_id)
                # 记录记忆
                author_name = (chars_config.get(author_char_id, {}).get("remark") or chars_config.get(author_char_id, {}).get("name") or author_char_id) if author_char_id != "user" else "用户"
                ctx = f"在{author_name}的朋友圈：「{post_content}」下，你被提及并评论说：「{ai_comment}」。"
                append_moment_event_to_short_memory(m_id, ctx)

    return jsonify({"status": "success", "comment": {"commenter_id": "user", "content": content, "timestamp": now}})


@moments_bp.route("/api/moments/comment/regenerate", methods=["POST"])
def moments_comment_regenerate():
    """
    重新生成某条评论内容（时间戳保持不变）。
    body: { char_id, timestamp, comment_index }
    """
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")
    try:
        comment_index = int(comment_index)
    except Exception:
        return jsonify({"error": "comment_index 无效"}), 400

    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    comments = post.get("comments", [])
    if comment_index < 0 or comment_index >= len(comments):
        return jsonify({"error": "评论索引不存在"}), 404

    old_comment = comments[comment_index]
    commenter_id = old_comment.get("commenter_id")
    if not commenter_id or commenter_id == "user":
        return jsonify({"error": "该评论不支持重生成"}), 400

    post_author_id = post.get("char_id", "")
    post_content = post.get("content", "")
    old_ts = old_comment.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    reply_to = old_comment.get("reply_to")

    new_text = None
    # 若是"作者回复用户评论"，优先使用专用回复函数
    if reply_to == "user":
        user_comment_text = ""
        for i in range(comment_index - 1, -1, -1):
            prev = comments[i]
            if prev.get("commenter_id") == "user":
                user_comment_text = prev.get("content", "")
                break
        new_text = _generate_moment_reply_to_user(commenter_id, post_content, user_comment_text or "谢谢你的评论", user_id=get_current_user_id())
    else:
        new_text = _generate_moment_comment(commenter_id, post_author_id, post_content, user_id=get_current_user_id())

    if not new_text:
        return jsonify({"error": "重生成失败"}), 500

    # 仅替换内容，保持原时间不变
    old_comment["content"] = new_text
    old_comment["timestamp"] = old_ts
    comments[comment_index] = old_comment
    post["comments"] = comments
    raw[idx] = post
    safe_save_json(moments_path, raw)

    return jsonify({"status": "success"})


@moments_bp.route("/api/moments/memory/regenerate", methods=["POST"])
def moments_memory_regenerate():
    """
    重新生成朋友圈对应的短期记忆。
    body: { char_id, timestamp, comment_index? }
    - 不传 comment_index：重新生成发帖者自身的记忆
    - 传 comment_index：重新生成该评论者的记忆
    """
    from app import get_char_name
    user_id = get_current_user_id()

    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")

    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    post_content = post.get("content", "")
    post_author = post.get("char_id", "")
    comments = post.get("comments", [])

    if comment_index is not None:
        try:
            comment_index = int(comment_index)
        except Exception:
            return jsonify({"error": "comment_index 无效"}), 400

        if comment_index < 0 or comment_index >= len(comments):
            return jsonify({"error": "评论索引不存在"}), 404

        comment = comments[comment_index]
        target_id = comment.get("commenter_id")
        if not target_id or target_id == "user":
            return jsonify({"error": "该评论没有可生成的记忆"}), 400

        comment_text = comment.get("content", "")
        reply_to = comment.get("reply_to")

        author_name = get_char_name(post_author) if post_author != "user" else "用户"
        if reply_to and reply_to != "user":
            ctx = f"在{author_name}的朋友圈：「{post_content}」下，你回复了{reply_to}的评论，你说：「{comment_text}」。"
        elif reply_to == "user":
            user_comment_text = ""
            for i in range(comment_index - 1, -1, -1):
                prev = comments[i]
                if prev.get("commenter_id") == "user":
                    user_comment_text = prev.get("content", "")
                    break
            if target_id == post_author:
                ctx = f"你在朋友圈发了内容：「{post_content}」。对于用户的评论「{user_comment_text}」，你回复说：「{comment_text}」。"
            else:
                ctx = f"在{author_name}的朋友圈：「{post_content}」下，你回复了用户的评论「{user_comment_text}」，你说：「{comment_text}」。"
        else:
            if target_id == post_author:
                ctx = f"你发了一条朋友圈，内容：「{post_content}」。"
            else:
                ctx = f"看到{author_name}的朋友圈：「{post_content}」。你评论说：「{comment_text}」。"
    else:
        target_id = post_author
        if not target_id or target_id == "user":
            return jsonify({"error": "用户的朋友圈没有可生成的记忆"}), 400

        ctx = f"你发了一条朋友圈，内容：「{post_content}」。"

    append_moment_event_to_short_memory(target_id, ctx)
    return jsonify({"status": "success", "char_id": target_id})


@moments_bp.route("/api/moments/force_active", methods=["POST"])
def force_active_moment():
    """
    手动选择角色，催促其立即生成一条主动朋友圈。
    可选带 instruction 参数来指定发布内容要求。
    """
    data = request.get_json() or {}
    char_id = data.get("char_id")
    if not char_id:
        return jsonify({"error": "缺少角色参数"}), 400
    uid = data.get("user_id") or get_current_user_id()
    instruction = (data.get("instruction") or "").strip()

    success = trigger_active_moments(char_id, user_id=uid, instruction=instruction if instruction else None)
    if success:
        return jsonify({"status": "success"})
    else:
        return jsonify({"error": "生成朋友圈失败或被过滤"}), 500


@moments_bp.route("/api/moments/post/ai_comment", methods=["POST"])
def moments_ai_comment_to_post():
    """
    手动指派某个角色直接对朋友圈本身生成评论（无 reply_to）。
    body: { char_id, timestamp, commenter_id }
    """
    user_id = get_current_user_id()
    data = request.get_json() or {}
    post_char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    commenter_id = data.get("commenter_id")

    if not all([post_char_id, timestamp_str, commenter_id]):
        return jsonify({"error": "缺少必要参数"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404

    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, post_char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    comments = post.get("comments", [])
    comment_text = _generate_moment_comment(commenter_id, post_char_id, post.get("content", ""), user_id=get_current_user_id())

    if comment_text:
        comments.append({
            "commenter_id": commenter_id,
            "content": comment_text,
            "timestamp": timestamp_str
        })
        post["comments"] = comments
        raw[idx] = post
        safe_save_json(moments_path, raw)
        return jsonify({"status": "success", "comment": comments[-1]})
    else:
        return jsonify({"error": "AI 评论生成失败"}), 500


@moments_bp.route("/api/moments/comment/user_reply", methods=["POST"])
def moments_user_reply_to_comment():
    """
    用户亲自回复某个角色的评论。
    系统会先保存用户评论，然后让被回复的角色自动回访一次。
    body: { char_id, timestamp, comment_index, content }
    """
    user_id = get_current_user_id()
    data = request.get_json() or {}
    post_char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")
    content = (data.get("content") or "").strip()

    if not all([post_char_id, timestamp_str, content]) or comment_index is None:
        return jsonify({"error": "缺少必要参数"}), 400

    try:
        comment_index = int(comment_index)
    except Exception:
        return jsonify({"error": "comment_index 无效"}), 400

    if len(content) > 500:
        return jsonify({"error": "回复内容不得超过500字"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404

    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, post_char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    comments = post.get("comments", [])
    if comment_index < 0 or comment_index >= len(comments):
        return jsonify({"error": "评论索引不存在"}), 404

    target_comment = comments[comment_index]
    target_commenter_id = target_comment.get("commenter_id")

    if target_commenter_id == "user":
        return jsonify({"error": "不能回复自己的评论"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 保存用户的回复
    user_comment_obj = {
        "commenter_id": "user",
        "content": content,
        "timestamp": now,
        "reply_to": target_commenter_id
    }
    comments.append(user_comment_obj)

    # 2. 让目标角色回复用户
    post_content = post.get("content", "")

    # 我们调用 _generate_ai_reply_to_any_comment 让 target_commenter_id 回复刚生成的 user 评论
    ai_reply_text = _generate_ai_reply_to_any_comment(
        replying_char_id=target_commenter_id,
        post_author_id=post_char_id,
        post_content=post_content,
        comments_list=comments,
        target_comment_index=len(comments)-1
    )

    if ai_reply_text:
        ai_reply_obj = {
            "commenter_id": target_commenter_id,
            "content": ai_reply_text,
            "timestamp": now,
            "reply_to": "user"
        }
        comments.append(ai_reply_obj)

        # 记录朋友圈记忆
        memory_event = f"在朋友圈回复了用户的回复：{ai_reply_text}"
        append_moment_event_to_short_memory(target_commenter_id, memory_event)

    post["comments"] = comments
    raw[idx] = post
    safe_save_json(moments_path, raw)

    return jsonify({"success": True})


@moments_bp.route("/api/moments/post", methods=["POST"])
def moments_user_post():
    """用户发一条朋友圈。支持文字 + 多图上传。
    Body: multipart/form-data
    - content: 文字内容
    - images: 文件列表
    """
    from app import get_model_config, call_openrouter, get_effective_gemini_key
    from blueprints.media import _compress_chat_image_to_jpg
    from cos_utils import upload_to_cos

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    content = (request.form.get("content") or "").strip()
    files = request.files.getlist("images")

    if not content and not files:
        return jsonify({"error": "内容或图片不能为空"}), 400
    if content and len(content) > 2000:
        return jsonify({"error": "发帖内容不得超过2000字"}), 400

    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    image_data_list = []

    if files:
        # 处理图片流水线
        # 1. 临时保存与压缩
        # 2. 调用识图
        # 3. 上传 COS
        # 4. 清理临时文件

        # 识图模型配置
        route, current_model = get_model_config("vision")
        vision_prompt = "请用中文简要描述这张图片的内容，直接描述你看到了什么，不用过多主观判断。"

        for file in files:
            if not file or file.filename == "":
                continue

            ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
            base_name = uuid.uuid4().hex
            tmp_dir = os.path.join(BASE_DIR, "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

            tmp_raw_path = os.path.join(tmp_dir, f"{base_name}_raw")
            file.save(tmp_raw_path)

            compressed_path = os.path.join(tmp_dir, f"{base_name}.jpg")
            try:
                # 压缩
                _compress_chat_image_to_jpg(tmp_raw_path, compressed_path, max_edge=1024, max_bytes=500 * 1024)

                # 为识图模型准备临时公网 URL (复用 static/uploads)
                static_upload_dir = os.path.join(BASE_DIR, "static", "uploads")
                os.makedirs(static_upload_dir, exist_ok=True)
                public_filename = f"tmp_vision_{base_name}.jpg"
                public_file_path = os.path.join(static_upload_dir, public_filename)
                shutil.copy2(compressed_path, public_file_path)

                def _get_public_url(filename):
                    configured = (os.getenv("PUBLIC_BASE_URL", "") or os.getenv("SITE_URL", "")).strip()
                    if configured:
                        parsed = urlparse(configured)
                        base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else configured.rstrip("/")
                    else:
                        forwarded_proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
                        forwarded_host = (request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "").split(",")[0].strip()
                        base = f"{forwarded_proto}://{forwarded_host}" if forwarded_host else request.host_url.rstrip("/")
                    return f"{base}/static/uploads/{filename}"

                public_image_url = _get_public_url(public_filename)

                # 识图
                import requests as req_lib
                description = ""
                try:
                    if route == "relay":
                        messages = [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": vision_prompt},
                                {"type": "image_url", "image_url": {"url": public_image_url}}
                            ]
                        }]
                        description = call_openrouter(messages, char_id=None, model_name=current_model)
                    else:
                        base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
                        url = f"{base_url}/v1beta/models/{current_model}:generateContent?key={get_effective_gemini_key()}"
                        payload = {
                            "contents": [{
                                "role": "user",
                                "parts": [
                                    {"text": vision_prompt},
                                    {"file_data": {"mime_type": "image/jpeg", "file_uri": public_image_url}}
                                ]
                            }],
                            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
                            "safetySettings": [
                                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                            ]
                        }
                        r = req_lib.post(url, json=payload, timeout=60)
                        if r.status_code == 200:
                            result = r.json()
                            finish_reason = (result.get("candidates") or [{}])[0].get("finishReason")
                            if finish_reason and finish_reason != "STOP":
                                print(f"   [Vision] Gemini finishReason={finish_reason} (可能被截断)")
                            parts = (((result.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                            description = "".join([p.get("text", "") for p in parts]).strip()
                except Exception as ve:
                    print(f"Vision error for {file.filename}: {ve}")
                    description = "图片描述生成失败"

                # 上传 COS
                # 路径规则：users/<user_id>/moments/<YYYYMM>/<timestamp_uuid>.png
                yyyymm = now_dt.strftime("%Y%m")
                cos_filename = f"{int(now_dt.timestamp())}_{uuid.uuid4().hex[:8]}.jpg"
                cos_path = f"users/{user_id}/moments/{yyyymm}/{cos_filename}"

                cos_url = upload_to_cos(compressed_path, cos_path)
                if cos_url:
                    # 按照要求格式存储：[图片](<年月>/文件名)(AI生成的描述语)
                    image_data_list.append(f"[图片]({yyyymm}/{cos_filename})({description})")

            except Exception as e:
                print(f"Process image error: {e}")
            finally:
                if 'public_file_path' in dir() and os.path.exists(public_file_path): os.remove(public_file_path)
                if os.path.exists(tmp_raw_path): os.remove(tmp_raw_path)
                if os.path.exists(compressed_path): os.remove(compressed_path)

    # 组装最终正文
    final_content = content
    if image_data_list:
        final_content += "\n" + "\n".join(image_data_list)

    # --- 同步只处理 @ 提到的角色回复，其余在后台处理 ---
    # 先解析出 mentioned_ids（支持 name、remark、cid 三种标识，且不区分大小写，支持半角与全角 @）
    name_to_id = _get_moments_name_to_id(user_id=user_id)
    mentioned_ids = []
    final_content_lower = final_content.lower()
    for disp_name, cid in name_to_id.items():
        disp_name_lower = disp_name.lower()
        if f"@{disp_name_lower}" in final_content_lower or f"\uff20{disp_name_lower}" in final_content_lower:
            if cid not in mentioned_ids:
                mentioned_ids.append(cid)

    if mentioned_ids:
        print(f"  [User Moment] 检测到 @ 提及: {mentioned_ids}")

    # 同步生成 @ 提到的回复
    likers, comments = _generate_likes_comments_for_user_moment(now_str, final_content, only_mentioned=True, user_id=user_id)

    new_post = {
        "char_id": "user",
        "content": final_content,
        "timestamp": now_str,
        "likers": likers,
        "comments": comments
    }

    moments_path, _ = get_moments_paths(user_id=user_id)
    raw = []
    if os.path.exists(moments_path):
        try:
            with open(moments_path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except Exception:
            raw = []
    raw.append(new_post)
    safe_save_json(moments_path, raw)

    # 触发 AI 角色感知：记录同步回复的短期记忆
    for c in new_post.get("comments", []):
        cid = c.get("commenter_id")
        if cid and cid != "user":
            ctx = f"看到用户的朋友圈：「{final_content[:100]}」。你评论说：「{c.get('content', '')}」。"
            append_moment_event_to_short_memory(cid, ctx)

    # 启动后台任务：处理其余非 @ 角色的随机互动
    _background_generate_moment_reactions(user_id, "user", now_str, final_content, mentioned_ids=mentioned_ids)

    # 返回结果
    return jsonify({
        "status": "success",
        "post": new_post
    })


@moments_bp.route("/api/moments/regenerate", methods=["POST"])
def moments_regenerate():
    from app import (
        call_gemini, call_openrouter, get_model_config, get_ai_language,
        should_use_prompt_v2, build_system_prompt_v2, build_system_prompt,
        get_char_name, process_agent_actions, _execute_directive,
    )
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    remarks = {}
    cfg_file = _get_characters_config_file(user_id=user_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                chars_cfg = json.load(f)
            for cid, cdata in chars_cfg.items():
                r = (cdata.get("remark") or "").strip()
                if r:
                    remarks[cid] = r
        except Exception:
            pass

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    if char_id == "user":
        post_content = post.get("content", "")
        likers, comments = _generate_likes_comments_for_user_moment(timestamp_str, post_content, user_id=user_id)
        post["likers"] = likers
        post["comments"] = comments
    else:
        try:
            ok, err = sync_memory_before_moments(char_id)
            if not ok:
                print(f"     [Moments] 重新生成前记忆同步失败: {err}")
        except Exception as e:
            print(f"     [Moments] 重新生成前记忆同步异常: {e}")

        user_id = get_current_user_id()
        if should_use_prompt_v2(char_id):
            base_system_prompt = build_system_prompt_v2(char_id, include_global_format=False, recent_messages=None, include_long_memory=False, include_recent_messages=False, user_id=user_id)
        else:
            base_system_prompt = build_system_prompt(char_id, include_global_format=False, recent_messages=None, include_long_memory=False, user_id=user_id)

        lang = get_ai_language(char_id, user_id=user_id)
        now = datetime.now()
        if lang == "zh":
            trigger_msg = (
                f"任务：发朋友圈\n"
                f"当前时间：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
                "请结合你当前的日期时间感、最近的经历（如短期记忆里的事）发一条朋友圈，内容简短自然。可以包含：\n"
                "- 纯文字；或\n"
                "- 照片：\n"
                "\t1. 格式：使用 `[SEARCH_IMG: 关键词]` 标签。系统会自动联网搜索匹配的图库并以照片形式展示。\n"
                "\t2. 多图：你可以根据需要连续使用多个标签来发布多张照片（最多9张）。\n"
                "\t示例：`今天训练真累 [SEARCH_IMG: 足球场][SEARCH_IMG: 运动饮料]`\n"
                "- 如果你希望某位角色看到并评论这条朋友圈，可以在文中 @对方（如 @洁世一 或 @isagi）。被提及的角色会对此进行互动。\n"
                "- 移动位置（如果你正在移动或前往某个地方）：\n"
                "\t1. `[MOVE_TO: 地点ID]` - 移动到已知地点（移动到你的认知或附近感知中已有的地点时使用）。\n"
                "\t2. `[MOVE_TO_COORD: x,y]` - 移动到指定坐标（用于随便探索或没有具体命名地点的空白区域）。\n"
                "\t3. `[EXPLORE: x,y,\"名称\",\"描述\"]` - 前往并开辟新地点（移动到认知和感知中没有的地点时使用，系统会自动建立新地点）。\n"
                "- 对话转向（强烈推荐！跟谁聊得来就拉谁一起聊——大胆用！）\n"
                "\t- 拉人建群：想和朋友/其他角色一起聊？把对方拉进群！`[DIRECT_TO_GROUP: 角色1, 角色2]`， 不要写用户名字到成员里，用末尾 `+user` 表示用户也在场。自定义群名：`[DIRECT_TO_GROUP: 群名 | 角色1, 角色2]`。\n"
                "\t- 切回单聊：想和用户单独聊？切回私聊！`[DIRECT_TO_USER]`（无需参数）。\n"
                "\t- 放心输出：这些标签对用户不可见，直接在末尾另起一行输出即可。\n\n"
                "注意事项\n"
                "1. 只输出这一条朋友圈的内容，不要加引号、不要加「朋友圈：」等前缀。\n"
                f"2. 你的语言设定为 {lang}，请务必使用该语言发布。"
            )
        elif lang == "ja":
            trigger_msg = (
                f"タスク：朋友圈投稿\n"
                f"現在時刻：{now.strftime('%Y-%m-%d %H:%M %A')}\n"
                "現在の日時や最近の出来事（短期記憶など）を踏まえて、朋友圈を1本投稿してください。短く自然な内容にし、次の形式を使えます：\n"
                "- テキストのみ；または\n"
                "- 写真：\n"
                "\t1. 形式：`[SEARCH_IMG: キーワード]` 形式を使用してください。システムが画像を検索します。\n"
                "\t2. 複数写真：複数の写真（0-9枚）を投稿する場合は、複数のタグを並べてください。\n"
                "\t例：`[SEARCH_IMG: 夕焼け][SEARCH_IMG: サッカーボール]`\n"
                "- 投稿を特定のキャラクター（例：@潔世一 または @isagi）に見てほしい場合は、文中でメンションしてください。\n"
                "- 位置移動（移動中やどこかに行く場合）：\n"
                "\t1. `[MOVE_TO: 地点ID]` - 既知の地点への移動（自分の認知または周辺知覚にすでにある地点に移動する場合に使用）。\n"
                "\t2. `[MOVE_TO_COORD: x,y]` - 座標指定での単純移動（名前のない任意の場所に移動したり、自由に探索する場合に使用）。\n"
                "\t3. `[EXPLORE: x,y,\"名称\",\"説明\"]` - 未知の地点を開拓して移動（自分の認知や周辺知覚にない新規地点を作成して移動する場合に使用）。\n"
                "- 会話切り替え（強く推奨！気が合う相手をどんどんグループに呼ぼう！）\n"
                "\t- グループ作成：友達や他のキャラクターと話したい？すぐにグループに呼ぼう！`[DIRECT_TO_GROUP: キャラ1, キャラ2]`、 メンバーにユーザー名を直接書かず、末尾 `+user` でユーザーも参加。カスタム名：`[DIRECT_TO_GROUP: グループ名 | キャラ1]`。\n"
                "\t- 個別チャットに戻る：ユーザーと二人で話したい？個別チャットに戻ろう！`[DIRECT_TO_USER]`（引数不要）。\n"
                "\t- 遠慮なく：タグはユーザーに見えないので、末尾に改行して自由に出力しよう。\n\n"
                "注意事項\n"
                "1. 投稿内容のみを出力し、余計な説明や「朋友圈：」のような接頭辞は不要です。\n"
                f"2. 指定言語は {lang} です。必ずその言語で投稿してください。"
            )
        else:
            trigger_msg = (
                f"[Task: Post to Moments]\n"
                f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')}\n"
                "Please post to Moments based on current time and recent experiences. Keep it short and natural. You can use:\n"
                "- Text only; or\n"
                "- Photos:\n"
                "\t1. Format: Use `[SEARCH_IMG: Keyword]` tag. The system will search for matching images.\n"
                "\t2. Multiple photos: You can use multiple tags (0-9).\n"
                "\tExample: `[SEARCH_IMG: sunset][SEARCH_IMG: soccer]`\n"
                "- Mention other characters (e.g., @Isagi) if you want them to see and comment.\n"
                "- Movement (if you are moving or going somewhere):\n"
                "\t1. `[MOVE_TO: location_id]` - Move to a known location (use when moving to a place already in your known/perceived list).\n"
                "\t2. `[MOVE_TO_COORD: x,y]` - Move to coordinates (use when wandering around freely without a specific named location).\n"
                "\t3. `[EXPLORE: x,y,\"name\",\"desc\"]` - Move to a new location (use when moving to a place NOT in your known/perceived list, this creates a new location).\n"
                "- Chat Redirection (strongly recommended! Pull in whoever you vibe with — don't hesitate!):\n"
                "\t- Create group: Want to chat with friends/other characters? Pull them in! `[DIRECT_TO_GROUP: char1, char2]`,  append `+user` to include user. Custom name: `[DIRECT_TO_GROUP: GroupName | char1, char2]`.\n"
                "\t- Return to solo chat: Want to talk to the user privately? Go solo! `[DIRECT_TO_USER]` (no parameters).\n"
                "\t- Feel free: These tags are invisible to users — just output on a new line at the end.\n\n"
                "[Notes]\n"
                "1. Output ONLY the post content. No quotes or prefixes.\n"
                f"2. Your assigned language is {lang}. Please post in this language."
            )

        regenerating_instruction = post.get("instruction", "").strip()
        if regenerating_instruction:
            trigger_msg += (
                f"\n\n**SPECIAL REQUIREMENT / 特别要求:**\n{regenerating_instruction}\n"
                "请务必严格按照上述特别要求的内容、话题与风格来重新发布朋友圈。"
            )

        messages = [{"role": "system", "content": base_system_prompt}, {"role": "user", "content": trigger_msg}]
        try:
            route, current_model = get_model_config("moments", user_id=user_id)
            if route == "relay":
                content = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
            else:
                content = call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)
            if content:
                content = content.strip().strip('"\'')
                if content:
                    content, _, directive_m = process_agent_actions(char_id, content, user_id or get_current_user_id())
                    if directive_m:
                        uid = user_id or get_current_user_id()
                        _d, _cid, _txt = directive_m, char_id, content
                        def _bg(): set_background_user(uid); _execute_directive(_d, _cid, _txt)
                        threading.Thread(target=_bg, daemon=True).start()

                    # --- 重新生成也需要解析媒体标签 ---
                    content = process_moments_media_tags(content, char_id, user_id=user_id or get_current_user_id())
                    content = clean_moments_agent_instructions(content)
                    post["content"] = content

                    # --- 重新生成也需要解析 @ 提及并生成回复（支持 name、remark、cid，不区分大小写） ---
                    name_to_id = _get_moments_name_to_id(user_id=user_id)
                    mentioned_ids = []
                    content_lower = content.lower()
                    for dname, mid in name_to_id.items():
                        dname_lower = dname.lower()
                        if f"@{dname_lower}" in content_lower or f"\uff20{dname_lower}" in content_lower:
                            if mid not in mentioned_ids:
                                mentioned_ids.append(mid)

                    if mentioned_ids:
                        print(f"  [Moments Regenerate] 检测到 @ 提及: {mentioned_ids}")
                        # 清空旧的同步回复（可选，或者直接追加）
                        # 这里我们选择追加新的回复
                        for mid in mentioned_ids:
                            if mid == "user" or mid == char_id: continue
                            # 检查是否已经回复过（避免重复）
                            if any(c.get("commenter_id") == mid for c in post.get("comments", [])):
                                continue

                            comment_text = _generate_moment_comment(mid, char_id, content, is_mentioned=True, user_id=user_id)
                            if comment_text:
                                post.setdefault("comments", []).append({
                                    "commenter_id": mid,
                                    "content": comment_text,
                                    "timestamp": timestamp_str
                                })
                                # 记录记忆
                                try:
                                    author_name = remarks.get(char_id) or get_char_name(char_id) or char_id
                                    mem_ctx = f"在{author_name}的朋友圈：「{content[:100]}」下，你评论说：「{comment_text}」。"
                                    append_moment_event_to_short_memory(mid, mem_ctx)
                                except: pass

                        # 触发后台互动（为其补充非 @ 角色的互动）
                        user_id = get_current_user_id()
                        _background_generate_moment_reactions(user_id, char_id, timestamp_str, content, mentioned_ids=mentioned_ids)
        except Exception as e:
            print(f"  [Moments] 重新生成内容失败: {e}")
            return jsonify({"error": "生成失败"}), 500

        raw[idx] = post
        safe_save_json(moments_path, raw)
    return jsonify({"status": "success"})


@moments_bp.route("/api/moments/delete", methods=["POST"])
def moments_delete():
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    del raw[idx]
    safe_save_json(moments_path, raw)
    return jsonify({"status": "success"})


@moments_bp.route("/api/moments/edit", methods=["POST"])
def moments_edit():
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    new_content = (data.get("new_content") or "").strip()
    if not char_id or not timestamp_str or not new_content:
        return jsonify({"error": "缺少必要参数"}), 400
    if len(new_content) > 2000:
        return jsonify({"error": "编辑内容不得超过2000字"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无朋友圈数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    post["content"] = clean_moments_agent_instructions(new_content)
    raw[idx] = post
    safe_save_json(moments_path, raw)
    return jsonify({"status": "success"})


@moments_bp.route("/api/moments/comment/delete", methods=["POST"])
def moments_comment_delete():
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")
    try:
        comment_index = int(comment_index)
    except:
        return jsonify({"error": "无效的 comment_index"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到朋友圈"}), 404

    comments = post.get("comments", [])
    if 0 <= comment_index < len(comments):
        del comments[comment_index]
        post["comments"] = comments
        raw[idx] = post
        safe_save_json(moments_path, raw)
        return jsonify({"status": "success"})
    return jsonify({"error": "评论未找到"}), 404


@moments_bp.route("/api/moments/comment/edit", methods=["POST"])
def moments_comment_edit():
    user_id = get_current_user_id()
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")
    new_content = (data.get("new_content") or "").strip()
    try:
        comment_index = int(comment_index)
    except:
        return jsonify({"error": "无效的 id"}), 400

    if not new_content:
        return jsonify({"error": "内容不能为空"}), 400
    if len(new_content) > 500:
        return jsonify({"error": "评论内容不得超过500字"}), 400

    moments_path, _ = get_moments_paths(user_id=user_id)
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到"}), 404

    comments = post.get("comments", [])
    if 0 <= comment_index < len(comments):
        comments[comment_index]["content"] = clean_moments_agent_instructions(new_content)
        post["comments"] = comments
        raw[idx] = post
        safe_save_json(moments_path, raw)
        return jsonify({"status": "success"})
    return jsonify({"error": "评论未找到"}), 404
