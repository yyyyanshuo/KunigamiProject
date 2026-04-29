import os
import time
import re
import json
import sqlite3 # 导入 sqlite3 库
from datetime import datetime, timedelta, time as dt_time
from flask import Flask, request, jsonify, send_from_directory, send_file, render_template, session, redirect, url_for # <--- 加上这个
from dotenv import load_dotenv
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler # 新增
import memory_jobs # 导入刚才那个模块
import shutil # 如果以后需要创建新角色用
import random
from pywebpush import webpush, WebPushException # 记得导入
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import threading # 用于异步发送，防止卡顿
from contextvars import ContextVar
from email.utils import formataddr # <--- 新增这个导入
import tempfile # <--- 记得在最上面加这个 import
import io
from urllib.parse import quote as url_quote, urlparse
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
import pykakasi
from PIL import Image, ImageOps

# 初始化 kakasi (用于日语注音)
kks = pykakasi.kakasi()
# 常见 emoji / 表情符号范围（用于避免 pykakasi 误分词）
EMOJI_SPLIT_RE = re.compile(
    r'('
    r'[\U0001F1E6-\U0001F1FF]'     # flags
    r'|[\U0001F300-\U0001FAFF]'    # symbols & pictographs
    r'|[\u2600-\u26FF]'            # misc symbols
    r'|[\u2700-\u27BF]'            # dingbats
    r'|[\uFE0F]'                   # variation selector
    r')+'
)

def _add_furigana_to_japanese(text: str) -> str:
    """给日语文本中的汉字注音。跳过 [表情]、[图片] 等功能性标签，保留 emoji。"""
    if not text: return text
    # 跳过特定的标签段落
    pattern = r'(\[表情\][^\s/]+|\[图片\]\([^)]+\)\([\s\S]*?\)|\[recall\])'
    parts = re.split(pattern, text)
    
    out = ""
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # tag 保持原样
            out += part
        else:
            # 用占位符替换 emoji，避免 pykakasi 误处理
            emoji_map = {}
            def replace_emoji(match):
                emoji_key = f"__EMOJI_{len(emoji_map)}__"
                emoji_map[emoji_key] = match.group(0)
                return emoji_key
            
            part_with_placeholders = re.sub(EMOJI_SPLIT_RE, replace_emoji, part)
            
            # 按换行拆分再注音
            line_parts = re.split(r'(\r\n|\n|\r)', part_with_placeholders)
            for line_part in line_parts:
                if not line_part or line_part in ("\r\n", "\n", "\r"):
                    out += line_part
                    continue
                
                # 普通文本进行分词和注音
                result = kks.convert(line_part)
                # 安全回退
                joined_orig = "".join((it.get("orig") or "") for it in result)
                if joined_orig and joined_orig != line_part and not re.search(r'[\u3040-\u30ff\u4e00-\u9faf]', line_part):
                    out += line_part
                    continue
                
                for item in result:
                    orig = item['orig']
                    hira = item['hira']
                    
                    # 如果不含汉字，直接追加
                    if not re.search(r'[\u4e00-\u9faf]', orig):
                        out += orig
                        continue
                    
                    # 去除末尾相同的假名
                    suf = ''
                    while orig and hira and orig[-1] == hira[-1]:
                        suf = orig[-1] + suf
                        orig = orig[:-1]
                        hira = hira[:-1]
                    
                    # 去除开头相同的假名
                    pre = ''
                    while orig and hira and orig[0] == hira[0]:
                        pre += orig[0]
                        orig = orig[1:]
                        hira = hira[1:]
                    
                    # 如果中间有汉字，加 <ruby> 注音
                    if orig and hira:
                        out += f"{pre}<ruby>{orig}<rt>{hira}</rt></ruby>{suf}"
                    else:
                        out += pre + suf
            
            # 【新增】把 emoji 占位符替换回原始 emoji
            for emoji_key, emoji_char in emoji_map.items():
                out = out.replace(emoji_key, emoji_char)
    
    return out

load_dotenv()  # 从 .env 读取环境变量

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
# 新增下面这行，来读取我们配置的 API 地址
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://oa.api2d.net/v1")
# 【新增】读取旧的中转商地址
OPENROUTER_BASE_URL_OLD = os.getenv("OPENROUTER_BASE_URL_OLD", "https://vg.v1api.cc/v1")

app = Flask(__name__, static_folder='static', template_folder='templates')

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = "kunigami_secret_key_change_this" # 【新增】用于加密 Session，随便写
app.permanent_session_lifetime = timedelta(days=30) # 记住登录状态 30 天

# 配置项
MAX_CONTEXT_LINES = 10
DATABASE_FILE = "chat_history.db"

# 定义基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHARACTERS_DIR = os.path.join(BASE_DIR, "characters")
CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
# 【新增】群聊配置路径
GROUPS_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "groups.json")
GROUPS_DIR = os.path.join(BASE_DIR, "groups")

USER_SETTINGS_FILE = os.path.join(BASE_DIR, "configs", "user_settings.json")
USERS_DB = os.path.join(BASE_DIR, "configs", "users.db")
USERS_ROOT = os.path.join(BASE_DIR, "users")
DEVICE_ACCOUNTS_FILE = os.path.join(BASE_DIR, "configs", "device_accounts.json")


def _get_characters_config_file() -> str:
    """
    返回当前应使用的 characters.json 配置路径：
    - 已登录用户：users/<user_id>/configs/characters.json
    - 未登录：退回全局 configs/characters.json（仅用于调试/兼容）
    """
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "characters.json")
    return CONFIG_FILE


def _get_groups_config_file() -> str:
    """
    返回当前应使用的 groups.json 配置路径：
    - 已登录用户：users/<user_id>/configs/groups.json
    - 未登录：退回全局 configs/groups.json
    """
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "groups.json")
    return GROUPS_CONFIG_FILE


def get_all_char_ids_for_current_user() -> list:
    """返回当前用户 characters.json 中的角色 ID 列表（供定时任务用）"""
    d = get_characters_config_for_current_user()
    return list(d.keys())


def get_characters_config_for_current_user() -> dict:
    """返回当前用户 characters.json 的完整配置（供定时任务用）"""
    cfg = _get_characters_config_file()
    if not os.path.exists(cfg):
        return {}
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_groups_config_for_current_user() -> dict:
    """返回当前用户 groups.json 的完整配置（供定时任务用）"""
    cfg = _get_groups_config_file()
    if not os.path.exists(cfg):
        return {}
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_all_group_ids_for_current_user() -> list:
    """返回当前用户 groups.json 中的群聊 ID 列表（供定时任务用）"""
    cfg = _get_groups_config_file()
    if not os.path.exists(cfg):
        return []
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return list(json.load(f).keys())
    except Exception:
        return []
MOMENTS_DATA_FILE = os.path.join(BASE_DIR, "configs", "moments_data.json")
MOMENTS_LAST_POST_FILE = os.path.join(BASE_DIR, "configs", "moments_last_post.json")
ACTIVE_MOMENTS_ENABLED_FILE = os.path.join(BASE_DIR, "configs", "active_moments_enabled.json")

# --- 【新增】已读状态管理 ---
READ_STATUS_FILE = os.path.join(BASE_DIR, "configs", "read_status.json")


def _get_read_status_file() -> str:
    """
    返回当前用户的已读状态文件路径：
    - 已登录: users/<user_id>/configs/read_status.json
    - 未登录: 退回全局 configs/read_status.json
    """
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "read_status.json")
    return READ_STATUS_FILE

# --- 常用语 (per-user) ---
QUICK_PHRASES_FILE = os.path.join(BASE_DIR, "configs", "quick_phrases.json")

# --- 表情库 (官方: stickers/; 用户: users/<id>/sticker_uploads/; 喜欢: users/<id>/configs/stickers_favorites.json) ---
STICKERS_ROOT = os.path.join(BASE_DIR, "stickers")
STICKER_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# 表情描述列表来源：configs/sticker_descriptions_sorted.txt（由 scripts/export_sticker_descriptions.py 生成）
STICKER_DESCRIPTIONS_FILE = os.path.join(BASE_DIR, "configs", "sticker_descriptions_sorted.txt")


def _get_sticker_allowed_descriptions():
    """AI 与上传页统一使用的表情描述列表：从导出结果文件读取，按出现次数排序；无文件时退回默认短列表。"""
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


def _get_stickers_upload_dir() -> str:
    """当前用户的个人上传表情目录"""
    uid = get_current_user_id()
    if not uid:
        return ""
    d = os.path.join(USERS_ROOT, str(uid), "sticker_uploads")
    os.makedirs(d, exist_ok=True)
    return d


def _get_stickers_favorites_file() -> str:
    """当前用户喜欢表情列表 JSON 路径"""
    uid = get_current_user_id()
    if not uid:
        return ""
    cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "stickers_favorites.json")


def _get_added_sticker_packs_file() -> str:
    """当前用户已添加的表情包 ID 列表 JSON 路径"""
    uid = get_current_user_id()
    if not uid:
        return ""
    cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "added_sticker_packs.json")


def _load_added_sticker_packs() -> list:
    """当前用户已添加的表情包 ID 列表"""
    path = _get_added_sticker_packs_file()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return []


def _save_added_sticker_packs(pack_ids: list) -> bool:
    path = _get_added_sticker_packs_file()
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pack_ids, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _stickers_path_to_relative(path: str) -> str:
    """存储用相对标识：official:pack_id:filename 或 user:filename"""
    return path


def _stickers_relative_to_url(path: str) -> str:
    """相对标识转成前端可请求的 URL"""
    if not path:
        return ""
    if path.startswith("official:"):
        parts = path.split(":", 2)
        if len(parts) >= 3:
            return f"/api/stickers/file?path={url_quote(path)}"
    if path.startswith("user:"):
        return f"/api/stickers/file?path={url_quote(path)}"
    return ""


def _stickers_path_to_abs(path: str) -> str:
    """相对标识转成服务器绝对路径（仅允许 stickers 或用户 sticker_uploads）"""
    if not path:
        return ""
    if path.startswith("official:"):
        parts = path.split(":", 2)
        if len(parts) >= 3:
            pack_id, filename = parts[1], parts[2]
            if ".." in pack_id or ".." in filename or "/" in pack_id or "\\" in pack_id:
                return ""
            return os.path.join(STICKERS_ROOT, pack_id, filename)
    if path.startswith("user:"):
        filename = path[5:].lstrip(":")
        if ".." in filename or "/" in filename or "\\" in filename:
            return ""
        ud = _get_stickers_upload_dir()
        if not ud:
            return ""
        return os.path.join(ud, filename)
    return ""


def _sticker_path_to_name(path: str) -> str:
    """从 path 提取显示用名称（文件名无扩展名）"""
    if not path:
        return ""
    if path.startswith("official:") and path.count(":") >= 2:
        filename = path.split(":", 2)[2]
        return os.path.splitext(filename)[0]
    if path.startswith("user:"):
        filename = path[5:].lstrip(":")
        return os.path.splitext(filename)[0]
    return path


def _sticker_content_for_ai(content: str) -> str:
    """把消息里的 [表情]path 转成 [表情]name，供 AI 理解"""
    if not content or "[表情]" not in content:
        return content
    def repl(m):
        path = m.group(1).strip()
        name = _sticker_path_to_name(path)
        return f"[表情]{name}" if name else m.group(0)
    return re.sub(r"\[表情\]([^\]]+)", repl, content)


def _resolve_sticker_name_to_path(name: str) -> str:
    """【写时随机】仅用于 LLM 输出入库前：检索名称含有该关键词的表情（如「开心」匹配 开心、开心（1）、开心一 等），在匹配结果中随机选一个 path 写入 DB。"""
    name = (name or "").strip()
    items = _search_stickers(name)  # 已为包含匹配：q in s["name"].lower()
    if not items:
        return ""
    return random.choice(items)["path"]


def _resolve_sticker_name_to_path_deterministic(name: str) -> str:
    """【读时确定性】按名称搜索后固定返回第一条匹配的 path。用于 /file、/resolve、收藏等，避免历史记录每次刷新变脸。"""
    name = (name or "").strip()
    items = _search_stickers(name)
    if not items:
        return ""
    name_lower = name.lower()
    exact = [i for i in items if (i.get("name") or "").lower() == name_lower]
    if exact:
        return exact[0]["path"]
    return items[0]["path"]


def _sticker_content_from_ai(content: str) -> str:
    """【写时随机】拦截 LLM 文本，将 [表情]纯名称 随机替换为 [表情]精确 path；已为 path 则放行。使用非贪婪+先行断言，避免吞掉 ' / ' 后文字。"""
    if not content or "[表情]" not in content:
        return content
    pattern = r"\[表情\](.*?)(?=\s*/\s*|$)"
    def repl(m):
        name_or_path = (m.group(1) or "").strip()
        if name_or_path.startswith("official:") or name_or_path.startswith("user:"):
            return m.group(0)
        path = _resolve_sticker_name_to_path(name_or_path)
        return f"[表情]{path}" if path else m.group(0)
    return re.sub(pattern, repl, content)


def _get_quick_phrases_file() -> str:
    """
    返回当前用户的常用语文件路径：
    - 已登录: users/<user_id>/configs/quick_phrases.json
    - 未登录: 退回全局 configs/quick_phrases.json
    """
    uid = get_current_user_id()
    if uid:
        cfg_dir = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "quick_phrases.json")
    return QUICK_PHRASES_FILE


def _load_device_accounts() -> dict:
    """读取设备账号映射表：device_id -> { user_id: {email, display_name, last_login} }"""
    if not os.path.exists(DEVICE_ACCOUNTS_FILE):
        return {}
    try:
        with open(DEVICE_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_device_accounts(data: dict) -> None:
    """保存设备账号映射表（使用安全写入）"""
    safe_save_json(DEVICE_ACCOUNTS_FILE, data or {})


def _track_device_login(user_id: int, email: str, display_name: str) -> str:
    """
    在当前设备上记录一次登录：
    - 生成/读取 device_id（存入 cookie）
    - 在 DEVICE_ACCOUNTS_FILE 中记录该 device_id 下最近登录过的账号列表
    """
    device_id = request.cookies.get("device_id") or uuid.uuid4().hex
    data = _load_device_accounts()
    entry = data.get(device_id, {})
    now_str = datetime.now().isoformat()
    entry[str(user_id)] = {
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
        "last_login": now_str,
    }
    data[device_id] = entry
    _save_device_accounts(data)
    return device_id


def _get_recent_device_accounts(max_age_days: int = 30) -> list[dict]:
    """
    返回当前设备在最近 max_age_days 天内登录过的账号列表。
    仅按 device_id 区分“同一设备”。
    """
    device_id = request.cookies.get("device_id")
    if not device_id or not os.path.exists(DEVICE_ACCOUNTS_FILE):
        return []

    data = _load_device_accounts()
    entries = data.get(device_id, {})
    if not isinstance(entries, dict):
        return []

    now = datetime.now()
    results: list[dict] = []
    for uid_str, info in entries.items():
        try:
            ts = info.get("last_login")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts)
            if (now - dt).days > max_age_days:
                continue
        except Exception:
            continue
        # 归一化 user_id 类型
        try:
            info["user_id"] = int(uid_str)
        except Exception:
            pass
        results.append(info)

    # 按 last_login 逆序
    results.sort(key=lambda x: x.get("last_login", ""), reverse=True)
    return results

# --- 【新增】推送订阅管理 ---
SUBSCRIPTIONS_FILE = os.path.join(BASE_DIR, "configs", "subscriptions.json")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
# 这里的邮箱随便填一个您的，用于标识发送者
VAPID_CLAIMS = {"sub": "mailto:yyyyanshuo@foxmail.com"}

# --- 后台用户上下文（定时任务用，无 request 时替代 session） ---
_background_user_var: ContextVar[int | None] = ContextVar("background_user_id", default=None)


def set_background_user(user_id: int | None) -> None:
    """定时任务中设置当前操作用户，使 get_paths、get_moments_paths 等指向 users/<user_id>/..."""
    _background_user_var.set(user_id)


def clear_background_user() -> None:
    """清除后台用户上下文"""
    _background_user_var.set(None)


def list_all_user_ids() -> list[int]:
    """从 users.db 返回所有用户 ID，供定时任务遍历"""
    if not os.path.exists(USERS_DB):
        return []
    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"[list_all_user_ids] 错误: {e}")
        return []


# --- 用户/会话相关辅助 ---
def init_users_db():
    """初始化 users.db，用于多用户账号系统。"""
    os.makedirs(os.path.dirname(USERS_DB), exist_ok=True)
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT,
            provider TEXT,
            provider_user_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_current_user_id():
    """
    返回当前操作用户 ID。
    - 定时任务中：优先返回 set_background_user() 设置的后台用户
    - HTTP 请求中：返回 session["user_id"]
    """
    try:
        bg = _background_user_var.get()
        if bg is not None:
            return bg
    except LookupError:
        pass
    try:
        return session.get("user_id")
    except Exception:
        return None


# --- 旧数据迁移：将单用户数据迁移到默认用户空间 ---
def migrate_single_user_data_to_default_user():
    """
    将现有全局数据 (characters/, groups/, configs/moments_*.json 等)
    迁移到第一个用户的命名空间 users/<user_id>/...。
    仅在该用户目录尚不存在时执行一次。
    """
    try:
        # 1. 找到或创建默认用户
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT id, email FROM users ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()

        if row:
            default_user_id = row[0]
            default_email = row[1]
        else:
            # users 表为空：根据旧 user_settings.json 创建一个默认用户
            email = "admin@local"
            display_name = "admin"
            password = "123456"
            if os.path.exists(USER_SETTINGS_FILE):
                try:
                    with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                        udata = json.load(f)
                    display_name = udata.get("current_user_name", display_name)
                    email = (udata.get("email") or f"{display_name}@local").lower()
                    password = udata.get("password") or password
                except Exception:
                    pass
            from datetime import datetime
            cur.execute(
                "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                (email.lower(), generate_password_hash(password), display_name, datetime.now().isoformat())
            )
            default_user_id = cur.lastrowid
            default_email = email
            conn.commit()

        conn.close()

        user_root = os.path.join(USERS_ROOT, str(default_user_id))

        # 如果用户目录已经存在，认为迁移已完成或由用户手动创建，避免重复覆盖
        if os.path.exists(user_root):
            print(f"[Migrate] users/{default_user_id} 已存在，跳过全局数据迁移。")
            return

        print(f"[Migrate] 开始将全局数据迁移到用户 {default_user_id} ({default_email}) ...")
        os.makedirs(user_root, exist_ok=True)

        # 2. 迁移角色数据：characters/<char_id> -> users/<user_id>/characters/<char_id>
        chars_root = os.path.join(user_root, "characters")
        os.makedirs(chars_root, exist_ok=True)
        if os.path.exists(CHARACTERS_DIR):
            try:
                for char_id in os.listdir(CHARACTERS_DIR):
                    src_char_dir = os.path.join(CHARACTERS_DIR, char_id)
                    if not os.path.isdir(src_char_dir):
                        continue
                    dst_char_dir = os.path.join(chars_root, char_id)
                    if os.path.exists(dst_char_dir):
                        continue
                    try:
                        shutil.copytree(src_char_dir, dst_char_dir)
                        print(f"[Migrate] 角色 {char_id} -> users/{default_user_id}/characters/")
                    except Exception as e:
                        print(f"[Migrate] 角色 {char_id} 迁移失败: {e}")
            except Exception as e:
                print(f"[Migrate] 遍历 characters/ 失败: {e}")

        # 3. 迁移群聊数据：groups/<group_id> -> users/<user_id>/groups/<group_id>
        groups_root = os.path.join(user_root, "groups")
        os.makedirs(groups_root, exist_ok=True)
        if os.path.exists(GROUPS_DIR):
            try:
                for group_id in os.listdir(GROUPS_DIR):
                    src_group_dir = os.path.join(GROUPS_DIR, group_id)
                    if not os.path.isdir(src_group_dir):
                        continue
                    dst_group_dir = os.path.join(groups_root, group_id)
                    if os.path.exists(dst_group_dir):
                        continue
                    try:
                        shutil.copytree(src_group_dir, dst_group_dir)
                        print(f"[Migrate] 群聊 {group_id} -> users/{default_user_id}/groups/")
                    except Exception as e:
                        print(f"[Migrate] 群聊 {group_id} 迁移失败: {e}")
            except Exception as e:
                print(f"[Migrate] 遍历 groups/ 失败: {e}")

        # 4. 迁移朋友圈数据：configs/moments_*.json -> users/<user_id>/configs/
        user_configs = os.path.join(user_root, "configs")
        os.makedirs(user_configs, exist_ok=True)
        try:
            if os.path.exists(MOMENTS_DATA_FILE):
                dst = os.path.join(user_configs, "moments_data.json")
                if not os.path.exists(dst):
                    shutil.copy2(MOMENTS_DATA_FILE, dst)
                    print(f"[Migrate] moments_data.json -> users/{default_user_id}/configs/")
            if os.path.exists(MOMENTS_LAST_POST_FILE):
                dst = os.path.join(user_configs, "moments_last_post.json")
                if not os.path.exists(dst):
                    shutil.copy2(MOMENTS_LAST_POST_FILE, dst)
                    print(f"[Migrate] moments_last_post.json -> users/{default_user_id}/configs/")
            if os.path.exists(READ_STATUS_FILE):
                dst = os.path.join(user_configs, "read_status.json")
                if not os.path.exists(dst):
                    shutil.copy2(READ_STATUS_FILE, dst)
                    print(f"[Migrate] read_status.json -> users/{default_user_id}/configs/")
            if os.path.exists(QUICK_PHRASES_FILE):
                dst = os.path.join(user_configs, "quick_phrases.json")
                if not os.path.exists(dst):
                    shutil.copy2(QUICK_PHRASES_FILE, dst)
                    print(f"[Migrate] quick_phrases.json -> users/{default_user_id}/configs/")
        except Exception as e:
            print(f"[Migrate] 朋友圈/已读/常用语数据迁移失败: {e}")

        print("[Migrate] 全局数据迁移完成。")
    except Exception as e:
        print(f"[Migrate] 迁移过程中出现异常: {e}")


def init_user_workspace(user_id: int) -> None:
    """
    为新注册的用户创建 users/<user_id>/ 下的基础结构：
    - characters/ : 暂不主动复制，按需由 get_paths 懒加载模板
    - groups/     : 暂不主动复制，全局 groups.json 仍作为模板
    - configs/    : 复制当前 configs/ 目录下的配置文件快照（排除 users.db）
    - logs/       : 预建空日志目录

    这样每个用户都有独立的 configs 和 logs，不再依赖全局文件。
    """
    try:
        user_root = os.path.join(USERS_ROOT, str(user_id))
        os.makedirs(user_root, exist_ok=True)

        # 1. 预建目录
        chars_root = os.path.join(user_root, "characters")
        groups_root = os.path.join(user_root, "groups")
        configs_root = os.path.join(user_root, "configs")
        logs_root = os.path.join(user_root, "logs")
        for d in (chars_root, groups_root, configs_root, logs_root):
            os.makedirs(d, exist_ok=True)

        # 2. 拷贝 configs 目录下的当前配置快照（排除 users.db）
        global_configs = os.path.join(BASE_DIR, "configs")
        if os.path.exists(global_configs):
            for name in os.listdir(global_configs):
                if name == "users.db":
                    continue
                src = os.path.join(global_configs, name)
                dst = os.path.join(configs_root, name)
                try:
                    if os.path.isdir(src):
                        if not os.path.exists(dst):
                            shutil.copytree(src, dst)
                    else:
                        if not os.path.exists(dst):
                            shutil.copy2(src, dst)
                except Exception as e:
                    print(f"[InitUser] 拷贝 configs/{name} 给用户 {user_id} 失败: {e}")
    except Exception as e:
        print(f"[InitUser] 初始化用户 {user_id} 工作区失败: {e}")


# 初始化多用户账号数据库并尝试迁移旧数据
init_users_db()
migrate_single_user_data_to_default_user()
# --- 用户级配置辅助函数（API Key / 邮箱等） ---
def _get_user_settings_file() -> str:
    """
    返回当前用户的设置文件路径：
    - 已登录: users/<user_id>/configs/user_settings.json
    - 未登录: 退回全局 USER_SETTINGS_FILE（兼容旧逻辑）
    """
    uid = get_current_user_id()
    if uid:
        base = os.path.join(USERS_ROOT, str(uid), "configs")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "user_settings.json")
    return USER_SETTINGS_FILE


def _load_user_settings() -> dict:
    """读取当前用户的设置文件，出错时返回空 dict。"""
    path = _get_user_settings_file()
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    return data


def get_effective_gemini_key():
    """优先使用用户在个人主页配置的 Gemini API Key，否则退回 .env。"""
    data = _load_user_settings()
    return data.get("gemini_api_key") or GEMINI_KEY


def get_effective_openrouter_key():
    """优先使用用户在个人主页配置的 OpenRouter API Key，否则退回 .env。"""
    data = _load_user_settings()
    return data.get("openrouter_api_key") or OPENROUTER_KEY

# --- 【新增】安全保存 JSON (防止文件损坏) ---
def safe_save_json(filepath, data):
    """
    原子化写入：先写临时文件，再重命名。
    防止多线程写入导致文件损坏 (Extra data 错误)。
    """
    dir_name = os.path.dirname(filepath)
    # 创建临时文件
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)

    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 瞬间替换 (Atomic Operation)
        os.replace(temp_path, filepath)
    except Exception as e:
        print(f"❌ Save JSON Error: {e}")
        os.remove(temp_path) # 出错则删掉临时文件

def get_current_username():
    """获取当前设置的用户名"""
    default_name = "User"
    data = _load_user_settings()
    return data.get("current_user_name", default_name)

def get_ai_language():
    """获取当前的 AI 回复语言设置 (默认中文 zh)"""
    default_lang = "zh"
    data = _load_user_settings()
    return data.get("ai_language", default_lang)

def get_user_age():
    """获取用户年龄，默认 None 表示未设置"""
    data = _load_user_settings()
    age = data.get("user_age")
    try:
        return int(age) if age is not None else None
    except Exception:
        return None

def get_char_tickle_suffix(char_id):
    """获取角色的拍一拍后缀，默认空字符串"""
    cfg_file = _get_characters_config_file()
    if not os.path.exists(cfg_file):
        return ""
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get(char_id, {}).get("tickle_suffix", "")
    except:
        return ""

def get_user_tickle_suffix():
    """获取用户的拍一拍后缀（被拍时的描述），默认空字符串）"""
    data = _load_user_settings()
    return data.get("tickle_suffix", "")

def _extract_tickle_target(content):
    """从消息内容解析拍一拍目标，返回 (is_tickle, target)。
    target: 'self' | 'user' | char_id | None
    """
    if not content or not isinstance(content, str):
        return False, None
    c = content.strip()
    if c == "[tickle_self]":
        return True, "self"
    if c == "[tickle_user]":
        return True, "user"
    if c == "[tickle]":
        return True, "assistant"  # 单聊时对方是 assistant
    m = re.match(r'^\[tickle_(\w+)\]$', c)
    if m:
        return True, m.group(1)  # 群聊 [tickle_xxx]
    return False, None

def _check_consecutive_tickle(db_path, new_target, assistant_char_id=None):
    """检查是否连续拍同一人。new_target: 'self'|'user'|char_id。
    assistant_char_id: 单聊时 [tickle] 的对象，群聊可 None。返回 (ok, last_content)"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM messages ORDER BY id DESC LIMIT 2")
        rows = cursor.fetchall()
        conn.close()
    except:
        return True, None

    for row in rows:
        role, content = row[0], (row[1] or "")
        is_tickle, target = _extract_tickle_target(content)
        if not is_tickle:
            continue
        if role == "user":
            initiator = "user"
        else:
            initiator = role
        if target == "self":
            obj = initiator
        elif target == "user":
            obj = "user"
        elif target == "assistant" and assistant_char_id:
            obj = assistant_char_id
        else:
            obj = target
        if str(obj) == str(new_target):
            return False, content
    return True, None

def _strip_consecutive_tickle(text):
    """从 AI 回复中移除连续重复的 [tickle] 或 [tickle_user]。同目标连续出现则删后者。"""
    if not text:
        return text
    parts = [p.strip() for p in text.split('/')]
    last_tickle_target = None
    result = []
    for p in parts:
        is_t, tgt = _extract_tickle_target(p)
        if is_t:
            # assistant/self 视为同一类（拍自己），user 为另一类
            norm = "self" if tgt in ("assistant", "self") else tgt
            if norm == last_tickle_target:
                continue
            last_tickle_target = norm
        else:
            last_tickle_target = None
        result.append(p)
    return '/'.join(result)

# ... (之前的 imports 和 常用语接口 保持不变) ...

# --- 工具：获取路径（支持每用户命名空间） ---
def get_paths(char_id):
    """
    根据角色ID生成 数据库路径 和 Prompt文件夹路径。
    如果存在登录用户，则使用 users/<user_id>/characters/<char_id>/ 作为实际工作目录；
    否则退回全局 characters/<char_id>/。
    """
    user_id = get_current_user_id()

    if user_id:
        # 当前登录用户的角色根目录
        user_char_root = os.path.join(USERS_ROOT, str(user_id), "characters")
        template_dir = os.path.join(CHARACTERS_DIR, char_id)
        char_dir = os.path.join(user_char_root, char_id)

        # 若该用户下还没有该角色目录，而模板存在，则从全局模板复制一份（不复制 chat.db）
        if not os.path.exists(char_dir) and os.path.exists(template_dir):
            os.makedirs(char_dir, exist_ok=True)
            try:
                for name in os.listdir(template_dir):
                    if name == "chat.db":
                        continue  # 每个用户自己的聊天记录单独生成
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
        # 未登录时退回全局目录（兼容老逻辑）
        char_dir = os.path.join(CHARACTERS_DIR, char_id)

    db_path = os.path.join(char_dir, "chat.db")
    prompts_dir = os.path.join(char_dir, "prompts")
    return db_path, prompts_dir

# --- 工具：初始化指定角色的数据库 ---
def init_char_db(char_id):
    db_path, _ = get_paths(char_id)
    # 确保文件夹存在
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
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

from typing import Tuple


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

@app.route("/api/<char_id>/mark_read", methods=["POST"])
def mark_read_api(char_id):
    mark_char_as_read(char_id)
    return jsonify({"status": "success"})


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

# ---------------------- 核心：Prompt 构建系统 ----------------------

def get_char_name(char_id):
    """从 characters.json 获取角色姓名，默认用 char_id"""
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
    """从 characters.json 获取角色年龄，默认 None 表示未设置"""
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

def migrate_persona_extract_age(char_id):
    """
    迁移旧版人设：从 1_base_persona.md 中提取年龄，移除姓名和年龄行，写入 characters.json。
    若已迁移过（config 中已有 age 且 persona 已无姓名行），则跳过。
    """
    _, prompts_dir = get_paths(char_id)
    persona_path = os.path.join(prompts_dir, "1_base_persona.md")
    if not os.path.exists(persona_path):
        return

    try:
        with open(persona_path, "r", encoding="utf-8-sig") as f:
            content = f.read()

        if not content.strip():
            return

        # 检查 config 是否已有 age（可能已迁移）
        existing_age = get_char_age(char_id)
        if existing_age is not None:
            # 已有年龄，只做清理：移除姓名、年龄相关行（防止重复写入）
            cleaned = _strip_name_age_from_persona(content)
            if cleaned != content and cleaned.strip():
                with open(persona_path, "w", encoding="utf-8") as f:
                    f.write(cleaned)
            return

        # 提取年龄（多种格式）
        extracted_age = _extract_age_from_text(content)
        cleaned = _strip_name_age_from_persona(content)

        # 仅当清理后非空时才覆盖，否则保留原文避免数据丢失
        if cleaned.strip():
            with open(persona_path, "w", encoding="utf-8") as f:
                f.write(cleaned)

        # 将年龄写入 characters.json
        if extracted_age is not None and os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            if char_id in all_config:
                all_config[char_id]["age"] = extracted_age
                all_config[char_id]["age_last_incremented"] = datetime.now().strftime("%Y")
                safe_save_json(CONFIG_FILE, all_config)
                print(f"   ✅ [Migration] {char_id} 已迁移：提取年龄 {extracted_age}，已清理人设中的姓名/年龄")
    except Exception as e:
        print(f"   ❌ [Migration] {char_id} 迁移失败: {e}")

def _extract_age_from_text(text):
    """从文本中提取年龄数字，支持 年齢：18、18歳、年龄：18、18岁 等"""
    import re
    patterns = [
        r'年齢[：:\s]*(\d+)',
        r'年龄[：:\s]*(\d+)',
        r'(\d+)[歳岁]',
        r'#\s*役割\s*\([^)]*\)\s*\((\d+)',  # # 役割 (名前) (18/身長...
        r'#\s*角色\s*\([^)]*\)\s*\((\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None

def _strip_name_age_from_persona(text):
    """移除人设中的姓名、年龄相关行，返回清理后的内容"""
    import re
    lines = text.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        # 跳过：名前はxxx、名前：xxx、名前がxxx
        if re.match(r'^名前[はが：:]\s*', stripped) or re.match(r'^姓名[：:]\s*', stripped):
            continue
        # 跳过：年齢：18、年龄：18、18歳 等 standalone
        if re.match(r'^年齢[：:\s]*\d+\s*$', stripped) or re.match(r'^年龄[：:\s]*\d+\s*$', stripped):
            continue
        if re.match(r'^(\d+)[歳岁]\s*$', stripped):
            continue
        # 跳过 # 役割 下的 (名前) (年齢/身長/誕生日) 整行
        if re.match(r'^\s*\([^)]+\)\s*\(\d+[/／]', stripped):
            continue
        result.append(line)
    return '\n'.join(result).strip()

def run_persona_migration_all():
    """对所有角色执行人设迁移"""
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            char_ids = list(json.load(f).keys())
        for cid in char_ids:
            migrate_persona_extract_age(cid)
    except Exception as e:
        print(f"❌ [Migration] 批量迁移失败: {e}")


def _is_mainly_japanese(text):
    """粗略判断文本是否以日语为主（含一定量平假名/片假名）。"""
    if not text or not text.strip():
        return False
    hira_kata = re.findall(r"[\u3040-\u309f\u30a0-\u30ff]+", text)
    return len("".join(hira_kata)) >= 3


def _is_mainly_chinese(text):
    """粗略判断文本是否以中文为主（含一定量汉字）。"""
    if not text or not text.strip():
        return False
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return len(cjk) >= 3


def _extract_keywords_jieba(text, stop, max_tokens=8, nouns_only=False):
    """
    使用 jieba 分词抽取中文关键词。
    nouns_only=True 时只保留名词（词性以 n 开头：n, nr, ns, nt, nz 等）；否则保留名词、动词、形容词。
    返回 dict[str, int]，长度 2~max_tokens 且不在 stop 中的词。
    """
    try:
        import jieba.posseg as pseg  # type: ignore[reportMissingImports]
        words = pseg.cut(text)
    except Exception:
        return {}
    freq = {}
    # jieba 词性：n=名词 nr=人名 ns=地名 nt=机构 nz=其他专名 等；v=动词 a=形容词
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
    """
    使用 Janome 形态分析抽取日语关键词。
    nouns_only=True 时只保留名词；否则保留名词、形容词、动词（不含副词）。
    单字假名过滤；称谓由 stop 过滤。
    返回 dict[str, int]，长度 2~max_tokens 且不在 stop 中的 surface 形式。
    """
    try:
        from janome.tokenizer import Tokenizer
        tokenizer = Tokenizer()
        tokens = list(tokenizer.tokenize(text))
    except Exception:
        return {}
    freq = {}
    keep_pos = ("名詞",) if nouns_only else ("名詞", "形容詞", "動詞")
    min_len = 2  # 单个假名不要
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


def select_relevant_long_memory(long_mem, recent_messages=None, user_latest_input=None):
    """
    RAI 式长期记忆筛选：根据「最近对话」从长期记忆中选出相关条目，避免整份注入。

    若有 user_latest_input：先对用户最新一条消息做关键词分析（仅名词），用该关键词在长期记忆中检索，匹配到的事件无条件加入；
    再对上下文 recent_messages 做关键词分析（仅名词），在剩余长期记忆中按关键词+时间打分筛选，补足条数。
    日语用 Janome 形态分析只保留名词；中文用 jieba 分词只保留名词。

    输入:
        long_mem: dict[str, str]，key 为周/月标识，value 为该段总结文本。
        recent_messages: list[str] 或 None。最近对话纯文本，用于抽取关键词筛选。
        user_latest_input: str 或 None。用户最新一条输入；若提供则先据此无条件入选匹配的长期记忆，再从剩余中按上下文筛选。

    输出:
        list[tuple[str, str]]：[(key, text_block), ...]，用于拼入 【Long-term Memory】 段落。
    """
    if not long_mem:
        return []
    if not recent_messages:
        # 无上下文，直接注入全部长期记忆（兼容旧行为）
        print("--- [Long Memory RAI] 无上下文，注入全部长期记忆 ---")
        return [(k, v) for k, v in long_mem.items()]

    print(f"--- [Long Memory RAI] 开始筛选，共 {len(long_mem)} 条长期记忆，上下文 {len(recent_messages)} 段 ---")

    # --- 1. 从 recent_messages 抽取关键词（形态分析只保留名词）---
    text = " ".join(str(s) for s in recent_messages if s)
    text = text.replace("/", " ")
    # 停用词：无实义、语气、连接词、常见应答 / 代词 / 助动词等
    stop = {
        # 中文常见虚词
        "今天", "明天", "昨天", "然后", "但是", "所以", "而且", "可以", "已经", "还是", "就是", "感觉", "真的", "有点", "什么", "怎么", "为什么", "这个", "那个",
        "的", "了", "吗", "呢", "啊", "哦", "嗯", "好", "对", "是", "有", "在", "不", "没", "很", "都", "也", "就", "还", "会", "能", "要", "说", "想", "看", "做",
        # 日语：助词 / 助动词 / 代词 / 结构名词 / 常见副词 / 连接词 / 频繁但信息量低的动词
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
        # 过于一般的动作动词
        "思う", "言う", "見る", "行く", "来る",
    }
    now = datetime.now()
    current_year, current_month = now.year, now.month
    TOP_K = 3
    A, B = 3, 1  # keyword_weight, recency_weight
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
        # 非日/中或分词失败：规则分词（混合）
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
        print(f"  关键词(名词): {keywords}")

    # 有用户最新消息时：先对用户最新消息做关键词分析→匹配的长期记忆无条件加入；再对上下文做关键词分析→筛选剩余长期记忆
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

        remaining_scored = [(A * keyword_score_ctx(ev) + B * recency_score(k), k, ev) for k, ev in remaining_events]
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

    # --- 2. 事件级打分并排序 ---
    event_scored = []  # (total, key, event_text, kw_score, r_score)
    for k, v in long_mem.items():
        r_score = recency_score(k)
        events = split_events(v)
        if not events:
            continue
        for ev in events:
            kw_score = keyword_score(ev)
            total = A * kw_score + B * r_score
            event_scored.append((total, k, ev, kw_score, r_score))

    if not event_scored:
        # 没有可用事件，退回按 key 级别仅按时间选 TOP_K
        print("  无可用事件，退回按时间选择 key。")
        scored_keys = [(recency_score(k), k, v) for k, v in long_mem.items()]
        scored_keys.sort(key=lambda x: -x[0])
        result = []
        for _, k, v in scored_keys[:TOP_K]:
            print(f"  回退入选: {k} (仅按时间)")
            result.append((k, v))
        print("--- [Long Memory RAI] 筛选结束 ---")
        return result

    # 按总分排序事件
    event_scored.sort(key=lambda x: -x[0])

    # 打印前若干条事件的得分（用于调试）
    for idx, (total, k, ev, kw_s, r_s) in enumerate(event_scored[:20]):
        print(f"    事件候选[{idx}]: {k} kw={kw_s}, time={r_s}, total={total}, text={ev[:40]}...")

    # --- 3. 按事件选取：全量保留被选中事件文本，不再裁剪 ---
    selected_by_key = {}  # key -> [event_text, ...]
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
        # 所有事件得分都 <=0，再次按时间回退
        print("  所有事件得分过低，退回按时间选择 key。")
        scored_keys = [(recency_score(k), k, v) for k, v in long_mem.items()]
        scored_keys.sort(key=lambda x: -x[0])
        result = []
        for _, k, v in scored_keys[:TOP_K]:
            print(f"  回退入选: {k} (仅按时间)")
            result.append((k, v))
        print("--- [Long Memory RAI] 筛选结束 ---")
        return result

    # 将事件按 key 聚合，拼成每个 key 的文本块（不再裁剪）
    result = []
    for k, events in selected_by_key.items():
        block = "\n".join(events)
        result.append((k, block))

    print("--- [Long Memory RAI] 筛选结束 ---")
    return result


def get_short_memory_text_for_rai(char_id, include_yesterday=True):
    """
    读取角色的短期记忆（当天，可选昨天），格式化为一段文本，用于作为 RAI 的 recent_messages。
    返回 list[str]，可直接传入 build_system_prompt(..., recent_messages=...)。
    """
    try:
        _, prompts_dir = get_paths(char_id)
        short_file = os.path.join(prompts_dir, "6_memory_short.json")
        if not os.path.exists(short_file):
            return []
        with open(short_file, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        dates = [today_str]
        if include_yesterday:
            yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            dates.insert(0, yesterday_str)
        lines = []
        for date_str in dates:
            day_data = data.get(date_str)
            events = []
            if isinstance(day_data, list):
                events = day_data
            elif isinstance(day_data, dict):
                events = day_data.get("events", [])
            for e in events:
                t = e.get("time", "")
                ev = e.get("event", "")
                if ev:
                    lines.append(f"- [{t}] {ev}")
        if not lines:
            return []
        return ["\n".join(lines)]
    except Exception:
        return []


def should_use_prompt_v2(char_id=None) -> bool:
    """系统全局采用 System Prompt v2 版本。
    
    v2 特点：
    - 4层时间线聚合（长期 + 中期 + 短期 + 最近消息）
    - 更高效的上下文组织
    - 更好的词元利用率
    """
    return True


# ===================== 【新增】System Prompt v2：时间线聚合版本 =====================

def parse_week_key_to_dates(week_key: str) -> tuple:
    """解析长期记忆的周期 key (YYYY-MM-WeekN 或 YYYY-MM) 为 (start_date, end_date)。
    
    返回: (start_date, end_date) 都是 datetime.date 对象，end_date 为该周/该月最后一天。
    【重要修改】WeekN 的最后一天应为该周的周日（维持时间线逻辑一致性）。
    """
    try:
        from datetime import date, timedelta
        import calendar
        if '-Week' in week_key:
            parts = week_key.split('-Week')
            ym_str = parts[0]
            week_num = int(parts[1])
            year, month = map(int, ym_str.split('-'))
            
            # 计算该月 1 号
            first_day_of_month = date(year, month, 1)
            # 计算 1 号是周几 (0=Mon, 6=Sun)
            first_weekday = first_day_of_month.weekday()
            
            # 第一周的周日日期: 1号 + (6 - first_weekday)
            first_sunday = first_day_of_month + timedelta(days=(6 - first_weekday))
            
            # 第 N 周的周日
            target_sunday = first_sunday + timedelta(weeks=(week_num - 1))
            
            # 确保不跨月 (如果是最后一周，取月底和周日的最小值)
            _, last_day_num = calendar.monthrange(year, month)
            last_day_of_month = date(year, month, last_day_num)
            
            end_date = min(target_sunday, last_day_of_month)
            # start_date 简单设为周日往前 6 天
            start_date = end_date - timedelta(days=6)
            if start_date.month != month:
                start_date = first_day_of_month
                
            return (start_date, end_date)
        else:
            # 处理 YYYY-MM 格式
            year, month = map(int, week_key.split('-'))
            _, last_day = calendar.monthrange(year, month)
            return (date(year, month, 1), date(year, month, last_day))
    except Exception:
        return None


def extract_long_memory_with_timeline_ts(char_id, recent_messages=None, user_latest_input=None) -> list:
    """提取长期记忆，为每条计算排序用的时间戳（该周最后一天的23:59）。
    
    返回: [(content_text, last_date, datetime_23_59)]
    """
    _, prompts_dir = get_paths(char_id)
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
    
    # 使用既有的筛选函数获取相关长期记忆
    selected = select_relevant_long_memory(long_mem, recent_messages, user_latest_input=user_latest_input)
    print(f"[DEBUG] extract_long_memory: 筛选后得到 {len(selected)} 条有效记忆")
    if not selected:
        print(f"[DEBUG] extract_long_memory: 筛选结果为空")
        return result
    
    # 为每条记忆计算时间戳
    for week_key, content in selected:
        date_range = parse_week_key_to_dates(week_key)
        print(f"[DEBUG] extract_long_memory: week_key={week_key}, date_range={date_range}")
        if date_range:
            _, last_date = date_range
            # 该周最后一天的23:59
            ts_23_59 = datetime.combine(last_date, dt_time(23, 59))
            result.append((content, last_date, ts_23_59))
            print(f"[DEBUG] extract_long_memory: 添加事件 - {ts_23_59.strftime('%Y-%m-%d %H:%M')}")
    
    print(f"[DEBUG] extract_long_memory: 最终返回 {len(result)} 条事件")
    return result


def extract_medium_memory_with_timeline_ts(char_id) -> list:
    """提取中期记忆，为每条计算排序用的时间戳（该天的23:59）。
    
    返回: [(content_with_date_label, date_obj, datetime_23_59)]
    """
    _, prompts_dir = get_paths(char_id)
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
    
    # 倒推7天读取中期记忆
    now = datetime.now()
    for i in range(7, 0, -1):
        day_date = (now - timedelta(days=i)).date()
        day_key = day_date.strftime("%Y-%m-%d")
        
        if day_key in med_mem:
            content = str(med_mem[day_key]).strip()
            print(f"[DEBUG] extract_medium_memory: 找到 {day_key} 的记忆 - {content[:50]}")
            if content:
                # 该天的23:59作为排序时间戳
                ts_23_59 = datetime.combine(day_date, dt_time(23, 59))
                result.append((content, day_date, ts_23_59))
        else:
            print(f"[DEBUG] extract_medium_memory: {day_key} 没有记忆")
    
    print(f"[DEBUG] extract_medium_memory: 最终返回 {len(result)} 条事件")
    return result


def extract_short_memory_with_timeline_ts(char_id) -> list:
    """提取短期记忆，每条事件作为一个独立个体参与排序。
    
    返回: [(content_with_label, base_date, datetime_ts)]
    """
    _, prompts_dir = get_paths(char_id)
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
    
    # 包含今天和昨天（凌晨4点前）
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
                
                # 为每条事件单独生成内容和时间戳
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


def append_short_memory_event(char_id, event_content, date_str, time_str):
    """往 6_memory_short.json 中追加一条短期记忆。"""
    try:
        _, prompts_dir = get_paths(char_id)
        short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")
        
        # 1. 加载现有数据
        current_data = {}
        if os.path.exists(short_mem_path):
            with open(short_mem_path, "r", encoding="utf-8-sig") as f:
                try: 
                    current_data = json.load(f) or {}
                except: 
                    pass
        
        # 2. 格式化数据结构 (兼容 list/dict)
        day_data = current_data.get(date_str, {})
        existing_events = []
        last_id = 0
        
        if isinstance(day_data, list):
            existing_events = day_data
        elif isinstance(day_data, dict):
            existing_events = day_data.get("events", [])
            last_id = day_data.get("last_id", 0)
        
        # 3. 追加新事件 (去重: 如果同一时间有相同的内容，则不添加)
        is_duplicate = any(e.get("time") == time_str and e.get("event") == event_content for e in existing_events)
        if not is_duplicate:
            existing_events.append({
                "time": time_str,
                "event": event_content
            })
            # 按时间排序
            existing_events.sort(key=lambda x: x.get("time", ""))
            
            # 4. 写回文件
            current_data[date_str] = {"events": existing_events, "last_id": last_id}
            with open(short_mem_path, "w", encoding="utf-8") as f:
                json.dump(current_data, f, ensure_ascii=False, indent=2)
            print(f"[DEBUG] append_short_memory: 已保存事件到 {char_id} 的短期记忆")
        else:
            print(f"[DEBUG] append_short_memory: 事件重复，跳过写入")
            
    except Exception as e:
        print(f"[DEBUG] append_short_memory Error: {e}")


def extract_recent_messages_with_labels(char_id, limit=20) -> list:
    """提取最近消息，为每条标注角色和时间戳。
    
    返回: [(role, content_with_label, datetime_ts)]
    """
    db_path = get_char_db_path(char_id)
    result = []
    
    print(f"[DEBUG] extract_recent_messages: DB路径 = {db_path}")
    print(f"[DEBUG] extract_recent_messages: DB存在 = {os.path.exists(db_path)}")
    
    if not os.path.exists(db_path):
        print(f"[DEBUG] extract_recent_messages: 数据库不存在")
        return result
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 获取最近的消息（不限日期）
        cursor.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        print(f"[DEBUG] extract_recent_messages: 查询到 {len(rows)} 条消息")
        
        # 逆序处理，使其按时间升序排列
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
            
            # 构建显示标签
            role_label = "user" if role == "user" else "你"
            time_label = msg_dt.strftime("%H:%M")
            content_display = f"[{time_label}] 【{role_label}】{content}"
            
            result.append((role, content_display, msg_dt))
    except Exception as e:
        print(f"[DEBUG] extract_recent_messages: 数据库操作失败 - {e}")
        import traceback
        print(traceback.format_exc())
    
    print(f"[DEBUG] extract_recent_messages: 最终返回 {len(result)} 条消息")
    return result


def build_timeline_section(timeline_events) -> str:
    """构建格式化的时间线文本。
    
    timeline_events: [(layer_type, content_display, timestamp_dt)]
    layer_type: "long_memory" | "medium_memory" | "short_memory" | "message"
    
    返回: 按timestamp升序排列的时间线Prompt文本
    """
    if not timeline_events:
        return ""
    
    # 按时间戳排序
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
            # 短期记忆可能已包含日期标签，但统一加上时间头
            lines.append(f"[{ts_str}] {layer_label}{content}")
            continue
        elif layer_type == "message":
            # 消息已包含标签
            lines.append(content)
            continue
        else:
            layer_label = "【事件】"
        
        # 简化日期显示
        lines.append(f"[{ts_str}] {layer_label}{content[:500]}")  # 截断过长内容
    
    timeline_text = "\n".join(lines)
    return f"【时间线 / タイムライン】\n{timeline_text}"


def build_system_prompt_v2(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, target_char_id=None):
    """【新版】System Prompt（6个组件+时间线聚合）。
    
    组件：
    1. 角色人设
    2. 用户人设
    3. 关系设定
    4. 日程表
    5. 系统规则
    6. 时间线（长期记忆+中期记忆+短期记忆+最近消息）
    """
    prompt_parts = []
    
    # 路径准备
    _, prompts_dir = get_paths(char_id)
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    
    # ===== 【1】角色人设 =====
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

    path = os.path.join(prompts_dir, "1_base_persona.md")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
                if content:
                    if name_age_prefix:
                        content = name_age_prefix + content
                    prompt_parts.append(f"【キャラクター / 角色人设】\n{content}")
        except Exception:
            pass
    
    # ===== 【2】用户人设 =====
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
            
        if include_global_format:
            # 优先从用户配置读取，其次从全局配置读取
            user_persona_file = None
            # 先尝试用户目录
            uid = get_current_user_id()
            if uid:
                user_dir = os.path.join(BASE_DIR, "users", str(uid), "configs")
                potential_file = os.path.join(user_dir, "global_user_persona.md")
                if os.path.exists(potential_file):
                    user_persona_file = potential_file
            # 再试全局目录
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
            elif user_prefix.strip():
                prompt_parts.append(f"【ユーザー / 用户人设】\n{user_prefix.strip()}")
    except:
        pass
    
    # ===== 【3】关系设定 =====
    try:
        current_user_name = get_current_username()
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                rel_data = json.load(f) or {}
            
            # 如果传入了特定的目标角色ID（用于朋友圈互相评论等场景）
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
                # 如果没找到特定匹配，且是在群聊或没有明确匹配时，展示列表（原有逻辑）
                rel_lines = []
                
                # 尝试加载名字映射，如果是 ID 存的就转成名字
                id_to_name = {}
                try:
                    with open(_get_characters_config_file(), "r", encoding="utf-8") as cf:
                        c_data = json.load(cf)
                        id_to_name = {str(k): v.get("name", str(k)) for k, v in c_data.items()}
                except: pass
                
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
    
    # ===== 【4】日程表 =====
    path = os.path.join(prompts_dir, "7_schedule.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                schedule = json.load(f) or {}
            if schedule:
                # 过滤为近7天的日程
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
    
    # ===== 【5】系统规则 =====
    if include_global_format:
        global_dir = os.path.join(BASE_DIR, "configs")
        global_format_file = os.path.join(global_dir, "global_format.md")
        if os.path.exists(global_format_file):
            try:
                with open(global_format_file, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()
                    if content:
                        prompt_parts.append(f"【システムルール / 系统规则】\n{content}")
            except Exception:
                pass
    
    # ===== 【6】时间线 =====
    timeline_events = []
    
    # 收集长期记忆事件
    long_mem_events = extract_long_memory_with_timeline_ts(char_id, recent_messages, user_latest_input)
    print(f"[DEBUG v2] extract_long_memory_with_timeline_ts() 返回 {len(long_mem_events)} 条事件")
    for i, (content, _, ts) in enumerate(long_mem_events):
        print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 长期记忆: {content[:100]}")
        timeline_events.append(("long_memory", content, ts))
    
    # 收集中期记忆事件
    med_mem_events = extract_medium_memory_with_timeline_ts(char_id)
    print(f"[DEBUG v2] extract_medium_memory_with_timeline_ts() 返回 {len(med_mem_events)} 条事件")
    for i, (content, _, ts) in enumerate(med_mem_events):
        print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 中期记忆: {content[:100]}")
        timeline_events.append(("medium_memory", content, ts))
    
    # 收集短期记忆事件
    short_mem_events = extract_short_memory_with_timeline_ts(char_id)
    print(f"[DEBUG v2] extract_short_memory_with_timeline_ts() 返回 {len(short_mem_events)} 条事件")
    for i, (content, _, ts) in enumerate(short_mem_events):
        print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 短期记忆: {content[:100]}")
        timeline_events.append(("short_memory", content, ts))
    
    # 收集最近消息
    msg_events = extract_recent_messages_with_labels(char_id, limit=20)
    print(f"[DEBUG v2] extract_recent_messages_with_labels() 返回 {len(msg_events)} 条事件")
    for i, (_, content, ts) in enumerate(msg_events):
        print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} - 消息: {content[:100]}")
        timeline_events.append(("message", content, ts))
    
    print(f"[DEBUG v2] 时间线总计: {len(timeline_events)} 条事件")
    
    # 构建时间线文本
    if timeline_events:
        timeline_text = build_timeline_section(timeline_events)
        prompt_parts.append(timeline_text)
    else:
        print(f"[DEBUG v2] WARNING: timeline_events 为空！")
    
    # ===== 【当前时间】=====
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
    
    # ===== 【身份提示】=====
    if char_name:
        prompt_parts.append(f"【あなたの正体】\nあなたは {char_name} です。")
    
    # ===== 【语言控制】=====
    lang = get_ai_language()
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

    return "\n\n".join(prompt_parts)


def build_messages_for_chat_v2(char_id, user_input, recent_messages=None) -> list:
    """【新版】为聊天构建消息列表（仅System + 最新User消息）。
    
    返回: [
        {"role": "system", "content": system_prompt_v2},
        {"role": "user", "content": user_latest_message}
    ]
    """
    system_prompt = build_system_prompt_v2(char_id, include_global_format=True, recent_messages=recent_messages)
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]
    
    return messages


def build_system_prompt(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, include_long_memory=True, target_char_id=None):
    """
    根据 prompts/ 文件夹下的文件，动态组装 System Prompt。
    包含：人设、关系、用户档案、格式要求、长/中/短期记忆、日程表、当前时间。
    include_global_format=False 时不拼接全局 system 规则（如朋友圈生成时使用）。
    recent_messages: 可选，最近对话文本列表，用于长期记忆的 RAI 筛选；不传则仍注入全部长期记忆。
    user_latest_input: 可选，用户最新一条输入；单聊/群聊时传入，其关键词会无条件参与长期记忆筛选。
    include_long_memory: 为 False 时不往 prompt 里加长期记忆段落（如主动发朋友圈时使用）。
    """
    prompt_parts = []

    # 获取当前日期对象，用于筛选记忆
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 路径准备
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = os.path.join(BASE_DIR, "configs")
    # 获取该角色的 Prompt 目录
    _, prompts_dir = get_paths(char_id)

    # 【关键修改】获取当前动态用户名
    current_user_name = get_current_username()

    print(f"--- [Debug] 正在为 [{char_id}] 构建 Prompt，路径: {prompts_dir} ---") # <--- 加这行调试

    # ========== 新顺序：1. 角色人设 ==========
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

    try:
        path = os.path.join(prompts_dir, "1_base_persona.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
                if content:
                    if name_age_prefix:
                        content = name_age_prefix + content
                    prompt_parts.append(f"【Role / キャラクター設定】\n{content}")
    except Exception: pass

    # ========== 新顺序：2. 用户人设 ==========
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

        # 已登录用户：只读取自己 users/<user_id>/configs/ 下的 user_persona
        # 未登录（理论上不会走到）：退回全局 CONFIG_DIR
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
            # 没有人设文件时，仅用昵称/年龄信息
            prompt_parts.append(f"【User / ユーザー情報】\n{user_prefix.strip()}")
    except: pass

    # ========== 新顺序：3. 关系设定 ==========
    try:
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                rel_data = json.load(f)
                
                # 如果传入了特定的目标角色ID（用于朋友圈互相评论等场景）
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
                    # 【修改】拼装文本改成日语
                    rel_str = (f"対話相手：{display_name}\n"
                           f"関係性：{target_rel.get('role', '不明')}\n"
                           f"関係度：{target_rel.get('score', 1)}\n"
                           f"詳細：{target_rel.get('description', '')}")
                    # 【修正】append 必须在 if 内部，避免 rel_str 未定义错误
                    prompt_parts.append(f"【Relationship / 関係設定】\n{rel_str}")
                elif rel_data:
                    # v1 版本中处理无明确匹配的列表 (群聊或全局)
                    rel_lines = []
                    id_to_name = {}
                    try:
                        with open(_get_characters_config_file(), "r", encoding="utf-8") as cf:
                            c_data = json.load(cf)
                            id_to_name = {str(k): v.get("name", str(k)) for k, v in c_data.items()}
                    except: pass
                    for key_name, info in rel_data.items():
                        disp_name = id_to_name.get(key_name, key_name)
                        role = info.get('role', '未知')
                        desc = info.get('description', '特になし')
                        score = info.get('score', 1)
                        rel_lines.append(f"- {disp_name}: {role} (关系度:{score}) {desc}")
                    if rel_lines:
                        rel_text = "\n".join(rel_lines)
                        prompt_parts.append(f"【Relationship / 関係設定】\n{rel_text}")
    except Exception: pass

    # ========== 新顺序：4. 长期记忆 ==========
    # include_long_memory=False 时不加入长期记忆（如主动发朋友圈）
    if include_long_memory:
        try:
            path = os.path.join(prompts_dir, "4_memory_long.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    long_mem = json.load(f)
                    if long_mem:
                        selected = select_relevant_long_memory(long_mem, recent_messages, user_latest_input=user_latest_input)
                        if selected:
                            mem_list = [f"- {k}: {v}" for k, v in selected]
                            prompt_parts.append(f"【Long-term Memory / 長期記憶】\n" + "\n".join(mem_list))
        except Exception:
            pass

    # ========== 新顺序：5. 中期记忆 ==========
    try:
        path = os.path.join(prompts_dir, "5_memory_medium.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                med_mem = json.load(f)
                # 倒推7天，收集内容
                summary_texts = []
                for i in range(7, 0, -1):
                    day_key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                    if day_key in med_mem:
                        summary_texts.append(str(med_mem[day_key]))
                if summary_texts:
                    # 合并为一段，限制总长度（如200字以内）
                    combined = " ".join(summary_texts)
                    # 截断为200字以内（可调整）
                    max_len = 200
                    if len(combined) > max_len:
                        combined = combined[:max_len] + "..."
                    prompt_parts.append(f"【Medium-term Memory / 最近一週間の出来事】\n{combined}")
    except Exception: pass

    # ========== 新顺序：6. 短期记忆 ==========
    try:
        path = os.path.join(prompts_dir, "6_memory_short.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                short_mem = json.load(f)

                # 获取当天的数据
                dates_to_load = [today_str]

                # 【关键逻辑】如果现在是凌晨 4 点之前，说明昨天还没日结，必须把昨天的也带上
                if now.hour < 4:
                    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    # 把昨天插在前面，按时间顺序读取
                    dates_to_load.insert(0, yesterday_str)

                # 2. 循环读取并拼接
                combined_events_str = ""
                for date_key in dates_to_load:
                    day_data = short_mem.get(date_key)
                    # 兼容格式
                    today_events = []
                    if isinstance(day_data, list): today_events = day_data
                    elif isinstance(day_data, dict): today_events = day_data.get("events", [])

                    if today_events:
                        # 加个日期头，让 AI 分得清
                        combined_events_str += f"\n--- {date_key} ---\n"
                        combined_events_str += "\n".join([f"- [{e.get('time')}] {e.get('event')}" for e in today_events])

                if combined_events_str:
                    prompt_parts.append(f"【Short-term Memory / 最近の出来事】{combined_events_str}")
    except Exception: pass

    # ========== 新顺序：7. 计划 ==========
    try:
        path = os.path.join(prompts_dir, "7_schedule.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f: # 记得用 utf-8-sig
                schedule = json.load(f)
                future_plans = []

                # 计算日期范围: 今天 ~ 7天后
                limit_date = now + timedelta(days=7)
                limit_date_str = limit_date.strftime("%Y-%m-%d")

                sorted_dates = sorted(schedule.keys())
                for date_key in sorted_dates:
                    # 【修改】只选取：今天 <= 日期 <= 7天后
                    if today_str <= date_key <= limit_date_str:
                        future_plans.append(f"- {date_key}: {schedule[date_key]}")

                if future_plans:
                    prompt_parts.append(f"【Schedule / 今後の予定】\n" + "\n".join(future_plans))
    except Exception: pass

    # ========== 新顺序：8. 系统设定+表情 ==========
    if include_global_format:
        try:
            user_id = get_current_user_id()
            if user_id:
                cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
                path = os.path.join(cfg_dir, "global_format.md")
            else:
                path = os.path.join(CONFIG_DIR, "global_format.md")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    prompt_parts.append(f"【System Rules / 出力ルール】\n{f.read().strip()}")
        except Exception:
            pass
        # 表情格式说明：只能用固定描述列表（来自 sticker_descriptions_sorted.txt），系统按「名称包含描述」匹配
        desc_list = "、".join(_get_sticker_allowed_descriptions())
        prompt_parts.append(
            "【Sticker / 表情】\n"
            "在分段回复中若要发送表情，请**仅使用**以下描述之一，格式为 [表情]描述：\n"
            f"{desc_list}\n"
            "系统会按「表情名称包含该描述」匹配表情库并随机展示一张（同一描述可对应多张图）。勿使用列表外的词，否则将原文显示。"
        )

    # ========== 新顺序：9. 今日时间 ==========
    # 格式示例: 2025-11-29 Saturday

    # 简单的星期几映射
    week_map = ["月", "火", "水", "木", "金", "土", "日"]
    week_str = week_map[now.weekday()]

    current_date_str = now.strftime('%Y-%m-%d %A')

    # 【修改】说明文字改成日语
    prompt_parts.append(f"【Current Date / 現在の日付】\n今日は: {current_date_str}\n(以下の会話履歴には時間 [HH:MM] のみが含まれています。现在の日付に基づいて理解してください)")

    # ========== 新顺序：10. 上下文（语言控制） ==========
    lang = get_ai_language()
    if lang == "zh":
        # 强力指令：即使人设是日文，也要用中文回复
        lang_instruction = (
            "\n\n【Language Control / 语言控制】\n"
            "请注意：无论上述设定使用何种语言，你**必须使用中文**进行回复。\n"
            "在保留角色语气、口癖和性格特征的前提下，自然地转化为中文表达。"
        )
        prompt_parts.append(lang_instruction)
    elif lang == "ja":
        # 强力指令：必须用日语回复
        lang_instruction = (
            "\n\n【Language Control / 言語制御】\n"
            "ご注意：設定やユーザーの入力に関わらず、あなたは**必ず日本語**で返答してください。\n"
            "キャラクターの性格や口調を維持したまま、自然な日本語で表現してください。"
        )
        prompt_parts.append(lang_instruction)
    
    return "\n\n".join(prompt_parts)

# --- 工具：构建群聊时的关系 Prompt (ID -> Name 映射版) ---
def build_group_relationship_prompt(current_char_id, other_member_ids):
    """
    当 current_char_id 说话时，注入他对群里其他人的看法。
    关键：需要把 other_member_ids (如 isagi) 转换为 关系JSON里的 Key (如 洁世一)
    """
    # 1. 读取全局角色配置，建立 ID -> Name 的映射表
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")

    id_to_name_map = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            for cid, cinfo in chars_config.items():
                id_to_name_map[cid] = cinfo.get("name", cid) # 没名字就用ID兜底
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

# --- 【修正版】AI 总结专用函数 (双语支持) ---
def call_ai_to_summarize(text_content, prompt_type="short", char_id="kunigami"):
    if not text_content: return None

    # 获取角色名和语言
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
    char_name = "私"
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            if char_id in chars_config:
                char_name = chars_config[char_id]["name"]
    except: pass

    lang = get_ai_language()

    # --- Prompt 字典 ---
    prompts = {
        "ja": {
            "short": (
                f"あなたは{char_name}本人として、自身の記憶を整理しています。"
                "【人称の区別】「私」= あなた（{char_name}）、「相手」= チャット相手（ユーザー）。混同しないでください。"
                "以下の会話ログから、重要な出来事を抽出してください。"
                "出力フォーマット：\n- [HH:MM] (自分または相手の行動・会話の要点、一言で)"
            ),
            "medium": (
                f"あなたは{char_name}本人です。【人称の区別】「私」= あなた（{char_name}）、「相手」= チャット相手。"
                "この一日の記録を振り返り、**出来事ごとに一行ずつ**まとめてください。"
                "**要件**：\n1. 出力は必ず「- 」で始まる箇条書き（1行1事件）。\n2. 時間表記は不要。\n3. **一人称視点**で、事実のみを淡々と記述。自分と相手を明確に区別。\n4. 5〜15件程度。"
            ),
            "long": (
                f"あなたは{char_name}本人です。【人称の区別】「私」= あなた、「相手」= チャット相手。"
                "この一週間の記録を振り返り、**出来事ごとに一行ずつ**まとめてください。"
                "**要件**：\n1. 出力は必ず「- 」で始まる箇条書き（1行1事件）。\n2. 時間表記は不要。\n3. 事実ベースで記述。自分と相手を明確に区別。\n4. 10〜25件程度。"
            ),
            "group_log": (
                "あなたはグループチャットの書記係（第三者）です。"
                "以下の会話ログから、重要なトピックや出来事を**客観的に**抽出してください。"
                "出力フォーマット：\n- [HH:MM] 出来事の内容"
            ),
            "moment": (
                f"あなたは{char_name}本人です。【人称の区別】「私」= あなた、「相手」= 他者。"
                "以下の朋友圈（Moments）に関するやり取りを、**一行だけ**で自分の記憶として要約してください。一人称で、事実を簡潔に。時間表記・箇条書き・引用符は不要。出力はその一文のみ。"
            )
        },
        "zh": {
            "short": (
                f"你现在是{char_name}本人，正在整理自己的记忆。"
                "【人称区分】「我」= 你本人（{char_name}），「你/对方」= 聊天对象（用户）。请严格区分，不要混淆。"
                "请从以下的对话记录中提取重要的事件。"
                "输出格式：\n- [HH:MM] (自己或对方的行动/对话要点，一句话)"
            ),
            "medium": (
                f"你现在是{char_name}本人。【人称区分】「我」= 你本人，「你/对方」= 聊天对象。"
                "请回顾这一天的记录，**按事件逐条**总结，每条一行。"
                "**要求**：\n1. 输出必须是「- 」开头的条列（一行一事）。\n2. 不要写具体时间点。\n3. 使用**第一人称**，只平实记录事实。明确区分「我」和「对方」。\n4. 约5～15条。"
            ),
            "long": (
                f"你现在是{char_name}本人。【人称区分】「我」= 你本人，「你/对方」= 聊天对象。"
                "请回顾这一周的记录，**按事件逐条**总结，每条一行。"
                "**要求**：\n1. 输出必须是「- 」开头的条列（一行一事）。\n2. 不要写具体时间点。\n3. 基于事实。明确区分「我」和「对方」。\n4. 约10～25条。"
            ),
            "group_log": (
                "你是群聊的书记员（第三方视角）。"
                "请从以下的对话记录中，**客观地**提取重要的话题或事件。"
                "要求：\n1. 不要使用第一人称。\n2. 明确主语（如“[名字]说了...”、“大家决定...”）。\n"
                "输出格式：\n- [HH:MM] 事件内容"
            ),
            "moment": (
                f"你现在是{char_name}本人。【人称区分】「我」= 你本人，「你/对方」= 互动对象。"
                "请将以下朋友圈相关的一件互动，用**一句话**总结为自己的记忆。第一人称，只写事实、简洁。不要时间前缀、不要列表、不要引号。只输出这一句话。"
            )
        }
    }

    # 选择对应语言和类型的 Prompt
    system_instruction = prompts.get(lang, prompts["ja"]).get(prompt_type, "")

    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Log:\n{text_content}"}
    ]

    print(f"--- Memory Summary ({prompt_type}) [Lang:{lang}] ---")

    # 1. 获取当前配置
    route, current_model = get_model_config("summary") # 任务类型是 chat

    print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

    if route == "relay":
        return call_openrouter(messages, char_id=char_id, model_name=current_model)
    else:
        return call_gemini(messages, char_id=char_id, model_name=current_model)

# --- 【修正版】核心逻辑：增量更新 (支持强制重置) ---
def update_short_memory_for_date(char_id, target_date_str, force_reset=False): # <--- 增加参数
    # 1. 动态获取路径
    db_path, prompts_dir = get_paths(char_id)
    short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")

    # 2. 读取现有记忆
    current_data = {}
    if os.path.exists(short_mem_path):
        with open(short_mem_path, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    day_data = current_data.get(target_date_str)
    existing_events = []
    last_id = 0

    # 如果不是强制重置，才去读旧数据
    if not force_reset:
        if isinstance(day_data, list):
            existing_events = day_data
            last_id = 0
        elif isinstance(day_data, dict):
            existing_events = day_data.get("events", [])
            last_id = day_data.get("last_id", 0)
    else:
        print(f"   -> [Force Reset] 强制重置 {target_date_str}，从头开始扫描")

    # 3. 查询数据库 (私聊 DB)
    if not os.path.exists(db_path):
        return 0, []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    # 读取新消息
    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"[{target_date_str}] 没有新增私聊消息需要总结。")
        return 0, []

    new_max_id = rows[-1][0]

    # 4. 拼接文本
    # 加载名字映射
    id_to_name = {}
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        CHAR_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
        with open(CHAR_CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for k, v in c_conf.items(): id_to_name[k] = v["name"]
    except: pass

    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        name = "ユーザー" if role == "user" else id_to_name.get(role, role)
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 5. 调用 AI 总结
    try:
        # 传入 char_id 以匹配人设
        summary_text = call_ai_to_summarize(chat_log, "short", char_id)
        if not summary_text: return 0, []

        new_events_raw = []
        import re
        for line in summary_text.split('\n'):
            line = line.strip()
            if line:
                match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
                event_time = match_time.group(1) if match_time else datetime.now().strftime("%H:%M")
                event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
                new_events_raw.append({"time": event_time, "event": event_text})

        if not new_events_raw: return 0, []

        # --- 【关键修改】合并逻辑 (群聊记忆保护) ---

        all_events = []

        if last_id > 0:
            # A. 增量模式 (Append)：直接追加
            print(f"   -> [增量模式] 追加 {len(new_events_raw)} 条私聊记忆")
            all_events = existing_events + new_events_raw
        else:
            # B. 覆盖模式 (Reset/First Run)：
            # 以前是直接 all_events = new_events_raw (导致群聊丢失)
            # 现在我们要：保留旧数据里的【群聊】条目，只覆盖【私聊】条目

            print(f"   -> [覆盖模式] 正在保护群聊记忆...")

            # 筛选出旧数据里的群聊记忆 (特征：包含 "[群聊:")
            # 或者更严谨：我们假设 AI 总结的私聊不会自己加 [群聊:...] 前缀
            protected_group_events = [e for e in existing_events if "[群聊:" in e.get('event', '')]

            # 合并：旧的群聊 + 新总结的私聊
            all_events = protected_group_events + new_events_raw

        # 按时间重新排序 (让群聊和私聊按时间线穿插)
        all_events.sort(key=lambda x: x['time'])

        # 保存
        current_data[target_date_str] = {
            "events": all_events,
            "last_id": new_max_id
        }

        with open(short_mem_path, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)

        return len(new_events_raw), new_events_raw

    except Exception as e:
        print(f"增量总结出错: {e}")
        return 0, []

# --- 【修正版】分发群聊记忆给成员 ---
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


def append_moment_event_to_short_memory(char_id, context_text):
    """
    将朋友圈互动用 AI 总结为一句话，追加到角色的当日短期记忆中。
    使用与记忆总结相同的模型（summary），context_text 为互动描述。
    """
    if not char_id or char_id == "user" or not (context_text or "").strip():
        return
    import re
    try:
        summary = call_ai_to_summarize((context_text or "").strip(), "moment", char_id)
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


# --- 【新增】对话前自动记忆同步，保持单聊与群聊记忆连贯 ---
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


def sync_memory_before_single_chat(char_id):
    """
    单聊前：先总结该角色参与的所有群聊的短期记忆，并入其 6_memory_short。
    返回 (success: bool, error_msg: str|None)
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    dates = [today_str]
    if now.hour < 4:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates.insert(0, yesterday_str)

    group_ids = _get_groups_for_char(char_id)
    if not group_ids:
        return True, None

    try:
        for gid in group_ids:
            for d in dates:
                try:
                    update_group_short_memory(gid, d)
                except Exception as e:
                    print(f"   [Sync] 群聊 {gid} 日期 {d} 同步失败: {e}")
                    return False, f"群聊记忆同步失败: {e}"
        return True, None
    except Exception as e:
        print(f"   [Sync] 单聊前记忆同步失败: {e}")
        return False, str(e)


def sync_memory_before_group_chat(group_id):
    """
    群聊前：总结群成员的单聊 + 群成员参与的其他群聊（跳过当前群）的短期记忆。
    不包含本群群聊记忆。
    返回 (success: bool, error_msg: str|None)
    """
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


def sync_memory_before_moments(char_id):
    """
    发朋友圈前：先总结该角色的单聊短期记忆，以及其参与的所有群聊短期记忆，
    确保 6_memory_short 已包含最新单聊与群聊内容，再生成朋友圈时 AI 能结合最近经历。
    返回 (success: bool, error_msg: str|None)
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    dates = [today_str]
    if now.hour < 4:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates.insert(0, yesterday_str)
    try:
        ok, err = sync_memory_before_single_chat(char_id)
        if not ok:
            return ok, err
        for d in dates:
            try:
                update_short_memory_for_date(char_id, d)
            except Exception as e:
                print(f"   [Sync] 发朋友圈前单聊记忆 {char_id} 日期 {d} 同步失败: {e}")
        return True, None
    except Exception as e:
        print(f"   [Sync] 发朋友圈前记忆同步失败: {e}")
        return False, str(e)


# ---------------------- 工具函数 ----------------------

def get_timestamp():
    """生成时间戳"""
    return time.strftime("[%Y-%m-%d %A %H:%M:%S]", time.localtime())

def init_db():
    """初始化数据库，创建 messages 表"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    # 创建一个表来存储消息，有 id、角色、内容和时间戳
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

# ---------------------- 主页面 ----------------------

# --- 【新增】全局登录校验 ---
@app.before_request
def require_login():
    # 定义不需要登录就能访问的白名单
    allowed_routes = [
        'login_page', 'login_api', 'register_page', 'register_api',  # 登录 / 注册相关
        'forgot_password_page', 'forgot_password_send_code', 'forgot_password_reset', # 忘记密码相关
        'static', 'manifest', 'service_worker', 'app_logo', # 静态资源 & PWA
        'handle_theme_settings' # 允许未登录时访问主题设置，防止登录页被加载屏卡死
    ]

    # 如果当前请求的 endpoint 不在白名单，且没有有效登录态，则跳转登录页
    if request.endpoint and request.endpoint not in allowed_routes and 'user_id' not in session and 'logged_in' not in session:
        return redirect('/login')

# --- 【新增】登录页面 ---
@app.route("/login")
def login_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("login.html")

@app.route("/register")
def register_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("register.html")

@app.route("/forgot_password")
def forgot_password_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("forgot_password.html")


@app.route("/api/register", methods=["POST"])
def register_api():
    """注册新用户：email + password (+ display_name)，成功后自动登录"""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip()

    if not email or not password:
        return jsonify({"status": "error", "message": "邮箱和密码不能为空"}), 400

    if not display_name:
        display_name = email

    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()

        # 检查邮箱是否已存在
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"status": "error", "message": "该邮箱已被注册"}), 400

        from datetime import datetime
        cur.execute(
            "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
            (email, generate_password_hash(password), display_name, datetime.now().isoformat())
        )
        user_id = cur.lastrowid
        conn.commit()
        conn.close()

        # 为新用户初始化 users/<user_id>/ 文件夹结构与配置快照
        init_user_workspace(user_id)

        # 自动登录
        session['user_id'] = user_id
        session['logged_in'] = True
        session.permanent = True

        # 在当前设备记录本次登录账号
        device_id = _track_device_login(user_id, email=email, display_name=display_name)
        resp = jsonify({"status": "success"})
        # 设备标识 cookie，30 天内可用于账号快速切换
        resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
        return resp
    except Exception as e:
        print(f"[Register] 注册失败: {e}")
        return jsonify({"status": "error", "message": "服务器错误"}), 500

# --- 登录 API（支持多用户 + 兼容旧单用户逻辑） ---
@app.route("/api/login", methods=["POST"])
def login_api():
    data = request.json or {}
    input_user = (data.get("username") or "").strip()
    input_pass = data.get("password") or ""

    # 1. 优先按邮箱在 users.db 里查找（新多用户逻辑）
    if input_user and input_pass:
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            cur.execute("SELECT id, email, password_hash, display_name FROM users WHERE email = ?", (input_user.lower(),))
            row = cur.fetchone()
            conn.close()
            if row and check_password_hash(row[2], input_pass):
                user_id = row[0]
                email = row[1]
                display_name = row[3] or email

                session['user_id'] = user_id
                session['logged_in'] = True
                session.permanent = True

                device_id = _track_device_login(user_id, email=email, display_name=display_name)
                resp = jsonify({"status": "success"})
                resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
                return resp
        except Exception as e:
            print(f"[Login] users.db 查询失败: {e}")

    # 2. 向下兼容：如果没有在 users 表中找到，则读取旧的 user_settings.json
    saved_user = "admin"
    saved_pass = "123456"
    user_data = {}
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                saved_user = user_data.get("current_user_name", "admin")
                saved_pass = user_data.get("password", "123456")  # 旧逻辑中的明文密码
        except Exception:
            pass

    if input_user == saved_user and input_pass == saved_pass:
        # 如果老账号登录成功，则在 users.db 中自动迁移/创建一个默认用户记录
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            email = (user_data.get("email") or f"{saved_user}@local").lower()
            cur.execute("SELECT id FROM users WHERE email = ?", (email,))
            row = cur.fetchone()
            if row:
                user_id = row[0]
            else:
                from datetime import datetime
                cur.execute(
                    "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                    (email, generate_password_hash(saved_pass), saved_user, datetime.now().isoformat())
                )
                user_id = cur.lastrowid
                conn.commit()
            conn.close()

            session['user_id'] = user_id
            session['logged_in'] = True
            session.permanent = True

            device_id = _track_device_login(user_id, email=email, display_name=saved_user)
            resp = jsonify({"status": "success"})
            resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
            return resp
        except Exception as e:
            print(f"[Login] 旧账号迁移失败: {e}")
            # 退回老的单用户登录标记
            session['logged_in'] = True
            session.permanent = True
            return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "用户名或密码错误"}), 401

# --- 【新增】忘记密码相关接口 ---
# 存储验证码的全局字典 (生产环境应使用 Redis 或带有过期时间的缓存)
# 格式: { "email": {"code": "123456", "expire": timestamp} }
reset_codes = {}

@app.route("/api/forgot_password/send_code", methods=["POST"])
def forgot_password_send_code():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    
    if not email or "@" not in email:
        return jsonify({"status": "error", "message": "请输入有效的邮箱"}), 400

    # 1. 检查用户是否存在
    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        conn.close()
        
        if not user:
            return jsonify({"status": "error", "message": "该邮箱未注册", "error_type": "email"}), 404
            
        # 2. 生成验证码
        import random
        code = str(random.randint(100000, 999999))
        
        # 3. 记录验证码 (有效期 10 分钟)
        reset_codes[email] = {
            "code": code,
            "expire": time.time() + 600
        }
        
        # 4. 发送邮件 (异步)
        subject = "Kunigami AI - 重置密码验证码"
        content = f"您好！您正尝试为 Kunigami AI 账号重置密码。\n\n您的验证码是：{code}\n\n该验证码 10 分钟内有效。如果不是您本人操作，请忽略此邮件。"
        
        # 调试输出
        print(f"DEBUG: Attempting to send reset code {code} to {email}")

        # 直接利用已有的 send_email_notification
        # 修改 receiver 获取逻辑，因为 reset 场景下无法依赖 _load_user_settings (未登录)
        # 所以我们暂时需要一个通用的发送函数或调整 _send_email_thread
        
        # 为了兼容 _send_email_thread 的逻辑，我们临时修改它的行为或在这里通过 threading 发送
        def _send_reset_email(addr, sub, body):
            sender = os.getenv("MAIL_SENDER")
            pwd = os.getenv("MAIL_PASSWORD")
            host = os.getenv("MAIL_SERVER", "smtp.qq.com")
            
            # 【修复】新加坡服务器连接 Gmail/外部 SMTP 可能存在的端口连通性问题
            # SMTP_SSL (465) 在某些云厂商会被拦截，尝试使用 SMTP + STARTTLS (587)
            # 或者如果 465 报错，增加重试逻辑
            port = int(os.getenv("MAIL_PORT", 465))
            
            if not sender or not pwd:
                print("❌ [Reset] 配置缺失: MAIL_SENDER 或 MAIL_PASSWORD")
                return
            
            try:
                msg = MIMEText(body, 'plain', 'utf-8')
                msg['From'] = formataddr(["Kunigami AI", sender])
                msg['To'] = formataddr(["User", addr])
                msg['Subject'] = Header(sub, 'utf-8')

                if port == 465:
                    server = smtplib.SMTP_SSL(host, port, timeout=10)
                else:
                    server = smtplib.SMTP(host, port, timeout=10)
                    if port == 587:
                        server.starttls()
                
                server.login(sender, pwd)
                server.sendmail(sender, [addr], msg.as_string())
                server.quit()
                print(f"📧 [Reset] 成功发送验证码到 {addr}")
            except Exception as e:
                print(f"❌ [Reset] 验证码发送失败: {e}")
                # 尝试备用方案：如果 465 失败且是 QQ/Foxmail，尝试 587 端口
                if "Connection unexpectedly closed" in str(e) and port == 465:
                    print("🔄 [Reset] 尝试使用 STARTTLS (587端口) 重试...")
                    try:
                        server = smtplib.SMTP(host, 587, timeout=10)
                        server.starttls()
                        server.login(sender, pwd)
                        server.sendmail(sender, [addr], msg.as_string())
                        server.quit()
                        print(f"📧 [Reset] 通过 587 端口重试成功！")
                    except Exception as e2:
                        print(f"❌ [Reset] 587 端口重试也失败: {e2}")
                
        threading.Thread(target=_send_reset_email, args=(email, subject, content)).start()
        
        return jsonify({"status": "success", "message": "验证码已发送至您的邮箱"})
        
    except Exception as e:
        print(f"[Reset] 数据库异常: {e}")
        return jsonify({"status": "error", "message": "系统繁忙，请重试"}), 500

@app.route("/api/forgot_password/reset", methods=["POST"])
def forgot_password_reset():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = data.get("password") or ""
    
    if not email or not code or not new_password:
        return jsonify({"status": "error", "message": "请填写完整信息"}), 400

    # 1. 验证码校验
    record = reset_codes.get(email)
    if not record:
        return jsonify({"status": "error", "message": "验证码错误或已失效", "error_type": "code"}), 400
        
    if time.time() > record["expire"]:
        del reset_codes[email]
        return jsonify({"status": "error", "message": "验证码已过期，请重新发送", "error_type": "code"}), 400
        
    # 【修复】强制清理两端的空白字符，解决“需要加空格”或“复制带空格”的问题
    target_code = str(record["code"]).strip()
    input_code = str(code).strip()
    
    if target_code != input_code:
        return jsonify({"status": "error", "message": "验证码输入错误", "error_type": "code"}), 400

    # 2. 更新数据库
    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        
        # 先确认用户还在
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"status": "error", "message": "找回失败：该邮箱账号不存在", "error_type": "email"}), 404
            
        # 更新密码
        password_hash = generate_password_hash(new_password)
        cur.execute("UPDATE users SET password_hash = ? WHERE email = ?", (password_hash, email))
        conn.commit()
        conn.close()
        
        # 成功后清除验证码
        del reset_codes[email]
        
        return jsonify({"status": "success", "message": "密码重置成功"})
        
    except Exception as e:
        print(f"[Reset] 更新密码失败: {e}")
        return jsonify({"status": "error", "message": "系统错误，请联系管理员"}), 500
    data = request.json or {}
    input_user = (data.get("username") or "").strip()
    input_pass = data.get("password") or ""

    # 1. 优先按邮箱在 users.db 里查找（新多用户逻辑）
    if input_user and input_pass:
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            cur.execute("SELECT id, email, password_hash, display_name FROM users WHERE email = ?", (input_user.lower(),))
            row = cur.fetchone()
            conn.close()
            if row and check_password_hash(row[2], input_pass):
                user_id = row[0]
                email = row[1]
                display_name = row[3] or email

                session['user_id'] = user_id
                session['logged_in'] = True
                session.permanent = True

                device_id = _track_device_login(user_id, email=email, display_name=display_name)
                resp = jsonify({"status": "success"})
                resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
                return resp
        except Exception as e:
            print(f"[Login] users.db 查询失败: {e}")

    # 2. 向下兼容：如果没有在 users 表中找到，则读取旧的 user_settings.json
    saved_user = "admin"
    saved_pass = "123456"
    user_data = {}
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                saved_user = user_data.get("current_user_name", "admin")
                saved_pass = user_data.get("password", "123456")  # 旧逻辑中的明文密码
        except Exception:
            pass

    if input_user == saved_user and input_pass == saved_pass:
        # 如果老账号登录成功，则在 users.db 中自动迁移/创建一个默认用户记录
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            email = (user_data.get("email") or f"{saved_user}@local").lower()
            cur.execute("SELECT id FROM users WHERE email = ?", (email,))
            row = cur.fetchone()
            if row:
                user_id = row[0]
            else:
                from datetime import datetime
                cur.execute(
                    "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                    (email, generate_password_hash(saved_pass), saved_user, datetime.now().isoformat())
                )
                user_id = cur.lastrowid
                conn.commit()
            conn.close()

            session['user_id'] = user_id
            session['logged_in'] = True
            session.permanent = True

            device_id = _track_device_login(user_id, email=email, display_name=saved_user)
            resp = jsonify({"status": "success"})
            resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
            return resp
        except Exception as e:
            print(f"[Login] 旧账号迁移失败: {e}")
            # 退回老的单用户登录标记
            session['logged_in'] = True
            session.permanent = True
            return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "用户名或密码错误"}), 401

# --- 【新增】退出登录 (可选) ---
@app.route("/logout")
def logout():
    session.pop('user_id', None)
    session.pop('logged_in', None)
    return redirect('/login')


@app.route("/api/accounts/recent", methods=["GET"])
def get_recent_accounts():
    """
    返回当前设备最近 30 天内登录过的账号列表，用于前端「切换账号」下拉。
    只在当前已登录时可用。
    """
    current_uid = get_current_user_id()
    if not current_uid:
        return jsonify([]), 401

    accounts = _get_recent_device_accounts(max_age_days=30)

    # 确保当前账号一定在列表中（哪怕设备表丢失）
    seen_ids = {acc.get("user_id") for acc in accounts}
    if current_uid not in seen_ids:
        # 从 users.db 里补一条
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            cur.execute("SELECT email, display_name FROM users WHERE id = ?", (current_uid,))
            row = cur.fetchone()
            conn.close()
            if row:
                accounts.append(
                    {
                        "user_id": current_uid,
                        "email": row[0],
                        "display_name": row[1] or row[0],
                        "last_login": datetime.now().isoformat(),
                    }
                )
        except Exception:
            pass

    for acc in accounts:
        acc["is_current"] = acc.get("user_id") == current_uid

    return jsonify(accounts)


@app.route("/api/accounts/switch", methods=["POST"])
def switch_account():
    """
    在同一设备上，在最近 30 天内登录过的账号之间进行切换，而无需重新输入密码。
    通过 device_id + DEVICE_ACCOUNTS_FILE 校验权限。
    """
    current_uid = get_current_user_id()
    if not current_uid:
        return jsonify({"status": "error", "message": "尚未登录"}), 401

    data = request.get_json() or {}
    target_id = data.get("user_id")
    try:
        target_id = int(target_id)
    except Exception:
        return jsonify({"status": "error", "message": "无效的目标账号"}), 400

    device_id = request.cookies.get("device_id")
    if not device_id or not os.path.exists(DEVICE_ACCOUNTS_FILE):
        return jsonify({"status": "error", "message": "当前设备暂无可切换账号"}), 403

    all_devices = _load_device_accounts()
    entries = all_devices.get(device_id, {})
    info = entries.get(str(target_id))
    if not info:
        return jsonify({"status": "error", "message": "目标账号未在本设备登录过"}), 403

    # 校验 30 天内
    try:
        ts = info.get("last_login")
        if not ts:
            raise ValueError("no ts")
        dt = datetime.fromisoformat(ts)
        if (datetime.now() - dt).days > 30:
            return jsonify({"status": "error", "message": "该账号登录已超过 30 天，请重新登录"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "无法确认登录时间，请重新登录该账号"}), 403

    # 切换会话
    session['user_id'] = target_id
    session['logged_in'] = True
    session.permanent = True

    # 更新该账号的最后登录时间
    info["last_login"] = datetime.now().isoformat()
    entries[str(target_id)] = info
    all_devices[device_id] = entries
    _save_device_accounts(all_devices)

    return jsonify(
        {
            "status": "success",
            "user_id": target_id,
            "email": info.get("email"),
            "display_name": info.get("display_name"),
        }
    )


def get_moments_paths() -> Tuple[str, str]:
    """
    获取当前用户的朋友圈数据文件路径。
    如有登录用户，则使用 users/<user_id>/configs/moments_*.json；
    否则退回全局 MOMENTS_DATA_FILE / MOMENTS_LAST_POST_FILE。
    """
    user_id = get_current_user_id()
    if user_id:
        base = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(base, exist_ok=True)
        return (
            os.path.join(base, "moments_data.json"),
            os.path.join(base, "moments_last_post.json"),
        )
    return MOMENTS_DATA_FILE, MOMENTS_LAST_POST_FILE

# --- 【新增】PWA 支持文件路由 ---
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    # 必须设置 Header 确保 Service Worker 权限正确
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route("/")
def contact_list_view():
    # 改用 render_template，这样 html 里的 {% include %} 才会生效
    return render_template("contacts.html")

@app.route("/moments")
def moments_view():
    return render_template("moments.html", ai_lang=get_ai_language())


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


@app.route("/api/moments/active_enabled", methods=["GET"])
def get_active_moments_enabled():
    """获取主动朋友圈开关状态。"""
    return jsonify({"enabled": _get_active_moments_enabled()})


@app.route("/api/moments/active_enabled", methods=["POST"])
def set_active_moments_enabled():
    """设置主动朋友圈开关。body: { "enabled": true|false }。"""
    data = request.get_json() or {}
    enabled = data.get("enabled", True)
    _set_active_moments_enabled(enabled)
    return jsonify({"enabled": _get_active_moments_enabled()})


def _get_moments_id_display():
    """返回 (id -> avatar, id -> remark) 用于朋友圈展示。含 user 与所有角色。"""
    avatars, remarks = {}, {}
    # 当前登录用户的头像与昵称
    try:
        user_cfg = _load_user_settings()
        avatars["user"] = user_cfg.get("avatar") or "/user_avatar"
        remarks["user"] = user_cfg.get("current_user_name") or "我"
    except Exception:
        pass
    if not avatars.get("user"): avatars["user"] = "/user_avatar"
    if not remarks.get("user"): remarks["user"] = "我"
    cfg_file = _get_characters_config_file()
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                for cid, info in json.load(f).items():
                    avatars[cid] = info.get("avatar") or "/static/default_avatar.png"
                    remarks[cid] = info.get("remark") or info.get("name") or cid
        except: pass
    return avatars, remarks

@app.route("/api/moments/characters", methods=["GET"])
def get_moments_characters():
    """获取所有有权发朋友圈的角色列表。用于前端筛选。"""
    _, remarks = _get_moments_id_display()
    # 移除 user，前端单独处理
    if "user" in remarks:
        remarks.pop("user")
    return jsonify(remarks)

@app.route("/api/moments/related_characters", methods=["GET"])
def get_moments_related_characters():
    """获取与某个角色有关系图谱的角色列表。如果 target_id 为 user 或关系图为找不到，则返回全部角色。"""
    target_id = request.args.get("target_id")
    _, remarks = _get_moments_id_display()
    if "user" in remarks:
        remarks.pop("user")

    if not target_id or target_id == "user" or target_id not in remarks:
        return jsonify(remarks)

    try:
        _, prompts_dir = get_paths(target_id)
        rel_path = os.path.join(prompts_dir, "2_relationship.json")
        if not os.path.exists(rel_path):
            return jsonify(remarks)

        with open(rel_path, "r", encoding="utf-8") as f:
            rel_data = json.load(f)

        current_user_name = get_current_username()
        name_to_cid = {}
        cfg_file = _get_characters_config_file()
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                for cid, info in json.load(f).items():
                    name = (info.get("name") or "").strip()
                    remark = (info.get("remark") or "").strip()
                    if name:
                        name_to_cid[name] = cid
                    if remark and remark != name:
                        name_to_cid[remark] = cid

        filtered_remarks = {}
        for name, _ in rel_data.items():
            if name.strip() == current_user_name:
                continue
            cid = name_to_cid.get(name.strip())
            if cid and cid in remarks:
                filtered_remarks[cid] = remarks[cid]

        if not filtered_remarks:
            # 如果关系图没有匹配到任何系统中实际存在的角色，退化到全列表，避免没角色可选
            return jsonify(remarks)
            
        return jsonify(filtered_remarks)
    except Exception as e:
        print(f"Error fetching related characters for {target_id}: {e}")
        return jsonify(remarks)

@app.route("/api/moments", methods=["GET"])
def get_moments():
    """朋友圈列表。数据格式：char_id, content, timestamp, liker_ids, comments (commenter_id, content, timestamp)。评论时间大于当前时间的不返回。"""
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
    moments_path, _ = get_moments_paths()
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
                        "content": c.get("content", ""),
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
            "content": post.get("content", ""),
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


def _find_moment_post(raw, char_id, timestamp_str):
    """在 raw 列表中找到 char_id + timestamp 匹配的一条，返回 (index, post) 或 (None, None)。"""
    for i, post in enumerate(raw):
        if post.get("char_id") == char_id and post.get("timestamp") == timestamp_str:
            return i, post
    return None, None


@app.route("/api/moments/like", methods=["POST"])
def moments_like():
    """用户点赞一条朋友圈。body: { char_id, timestamp }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths()
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

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    likers = post.get("likers", [])
    if not likers and post.get("liker_ids"):
        for lid in post["liker_ids"]:
            likers.append({"liker_id": lid, "timestamp": post.get("timestamp", now)})
    already = any(l.get("liker_id") == "user" for l in likers)
    if not already:
        likers.append({"liker_id": "user", "timestamp": now})
        post["likers"] = likers
        raw[idx] = post
        safe_save_json(moments_path, raw)
    return jsonify({"status": "success", "liked": True})


@app.route("/api/moments/unlike", methods=["POST"])
def moments_unlike():
    """用户取消点赞一条朋友圈。body: { char_id, timestamp }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths()
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
    
    # 同时也处理旧版字段 liker_ids (如果有)
    if post.get("liker_ids"):
        post["liker_ids"] = [lid for lid in post["liker_ids"] if lid != "user"]

    if len(new_likers) != len(likers):
        post["likers"] = new_likers
        raw[idx] = post
        safe_save_json(moments_path, raw)
        
    return jsonify({"status": "success", "liked": False})


@app.route("/api/moments/comment", methods=["POST"])
def moments_comment():
    """用户评论一条朋友圈。body: { char_id, timestamp, content }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    content = (data.get("content") or "").strip()
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400
    if not content:
        return jsonify({"error": "评论内容不能为空"}), 400

    moments_path, _ = get_moments_paths()
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

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comments = post.get("comments", [])
    comments.append({"commenter_id": "user", "content": content, "timestamp": now})
    post["comments"] = comments
    raw[idx] = post
    safe_save_json(moments_path, raw)

    # 仅当评论的不是用户自己的朋友圈时，才由作者生成回复和记忆
    if char_id != "user":
        author_char_id = char_id
        post_content = post.get("content", "")
        reply_text = _generate_moment_reply_to_user(author_char_id, post_content, content)
        if reply_text:
            comments = post.get("comments", [])
            comments.append({"commenter_id": author_char_id, "content": reply_text, "timestamp": now, "reply_to": "user"})
            post["comments"] = comments
            raw[idx] = post
            safe_save_json(moments_path, raw)
            # 在此处记录角色回复的记忆：包含朋友圈原贴、用户评论、角色回复
            ctx = f"你在朋友圈发了内容：「{post_content}」。对于用户的评论「{content}」，你回复说：「{reply_text}」。"
            append_moment_event_to_short_memory(author_char_id, ctx)

    return jsonify({"status": "success", "comment": {"commenter_id": "user", "content": content, "timestamp": now}})


@app.route("/api/moments/comment/regenerate", methods=["POST"])
def moments_comment_regenerate():
    """
    重新生成某条评论内容（时间戳保持不变）。
    body: { char_id, timestamp, comment_index }
    """
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

    moments_path, _ = get_moments_paths()
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
    # 若是“作者回复用户评论”，优先使用专用回复函数
    if reply_to == "user":
        user_comment_text = ""
        for i in range(comment_index - 1, -1, -1):
            prev = comments[i]
            if prev.get("commenter_id") == "user":
                user_comment_text = prev.get("content", "")
                break
        new_text = _generate_moment_reply_to_user(commenter_id, post_content, user_comment_text or "谢谢你的评论")
    else:
        new_text = _generate_moment_comment(commenter_id, post_author_id, post_content)

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


@app.route("/api/moments/force_active", methods=["POST"])
def force_active_moment():
    """
    手动选择角色，催促其立即生成一条主动朋友圈。
    """
    data = request.get_json() or {}
    char_id = data.get("char_id")
    if not char_id:
        return jsonify({"error": "缺少角色参数"}), 400

    # 尝试在当前请求中跑触发逻辑
    success = trigger_active_moments(char_id)
    if success:
        return jsonify({"status": "success"})
    else:
        return jsonify({"error": "生成朋友圈失败或被过滤"}), 500


@app.route("/api/moments/post/ai_comment", methods=["POST"])
def moments_ai_comment_to_post():
    """
    手动指派某个角色直接对朋友圈本身生成评论（无 reply_to）。
    body: { char_id, timestamp, commenter_id }
    """
    data = request.get_json() or {}
    post_char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    commenter_id = data.get("commenter_id")
    
    if not all([post_char_id, timestamp_str, commenter_id]):
        return jsonify({"error": "缺少必要参数"}), 400

    moments_path, _ = get_moments_paths()
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

    post_content = post.get("content", "")

    # 调用现有函数生成角色对贴文的直接评论。该函数内部已包含系统提示的拼装和目标角色的关联
    new_text = _generate_moment_comment(commenter_id, post_char_id, post_content)
    if not new_text:
        return jsonify({"error": "AI生成的评论内容为空，请重试"}), 500
        
    comments = post.get("comments", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comments.append({
        "commenter_id": commenter_id,
        "content": new_text,
        "timestamp": now
        # 直接评论到朋友圈，不带 reply_to
    })
    post["comments"] = comments
    raw[idx] = post
    safe_save_json(moments_path, raw)
    
    def get_name(cid):
        if cid == "user": return get_current_username()
        return get_char_name(cid)

    post_author_name = get_name(post_char_id)
    ctx = f"在{post_author_name}的朋友圈：「{post_content}」下，你评论说：「{new_text}」。"
    append_moment_event_to_short_memory(commenter_id, ctx)

    return jsonify({"status": "success", "comment": comments[-1]})


@app.route("/api/moments/comment/ai_reply", methods=["POST"])
def moments_ai_reply_to_comment():
    """
    手动指派某个角色（replying_char_id）对指定的评论（comment_index）进行回复。
    body: { char_id, timestamp, comment_index, replying_char_id }
    """
    data = request.get_json() or {}
    post_char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")
    replying_char_id = data.get("replying_char_id")
    
    if not all([post_char_id, timestamp_str, replying_char_id]) or comment_index is None:
        return jsonify({"error": "缺少必要参数"}), 400

    try:
        comment_index = int(comment_index)
    except Exception:
        return jsonify({"error": "comment_index 无效"}), 400

    moments_path, _ = get_moments_paths()
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
    target_comment_content = target_comment.get("content", "")
    
    post_content = post.get("content", "")

    # 调用 AI 生成回复
    new_text = _generate_ai_reply_to_any_comment(replying_char_id, post_char_id, post_content, comments, comment_index)
    if not new_text:
        return jsonify({"error": "AI生成的回复内容为空，请重试"}), 500
        
    # 追加新的回复评论
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comments.append({
        "commenter_id": replying_char_id,
        "content": new_text,
        "timestamp": now,
        "reply_to": target_commenter_id
    })
    post["comments"] = comments
    raw[idx] = post
    safe_save_json(moments_path, raw)
    
    # 获取双方名字用于短期记忆构建
    def get_name(cid):
        if cid == "user": return get_current_username()
        return get_char_name(cid)

    target_name = get_name(target_commenter_id)
    post_author_name = get_name(post_char_id)
    ctx = f"在{post_author_name}的朋友圈：「{post_content}」下，你回复了{target_name}的评论「{target_comment_content}」，你说：「{new_text}」。"
    append_moment_event_to_short_memory(replying_char_id, ctx)

    return jsonify({"status": "success", "comment": comments[-1]})

@app.route("/api/moments/comment/user_reply", methods=["POST"])
def moments_user_reply_to_comment():
    """
    用户亲自回复某个角色的评论。
    系统会先保存用户评论，然后让被回复的角色自动回访一次。
    body: { char_id, timestamp, comment_index, content }
    """
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

    moments_path, _ = get_moments_paths()
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


def _generate_likes_comments_for_user_moment(post_ts_str, post_content):
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
    chars_cfg = _get_characters_config_file()
    if not os.path.exists(chars_cfg):
        return likers, comments
    try:
        with open(chars_cfg, "r", encoding="utf-8-sig") as f:
            chars_config = json.load(f)
    except Exception:
        return likers, comments

    # 解析 @ 提及的角色
    mentioned_ids = []
    # 获取备注名反查 ID 的映射
    remark_to_id = { (info.get("remark") or info.get("name") or cid): cid for cid, info in chars_config.items() }
    import re
    at_matches = re.findall(r"@([^\s@]+)", post_content)
    for name in at_matches:
        if name in remark_to_id:
            mentioned_ids.append(remark_to_id[name])
        elif name in chars_config:
            mentioned_ids.append(name)

    for char_id, info in chars_config.items():
        # 如果是被 @ 的角色，必须生成评论，且时间与朋友圈相同
        is_mentioned = char_id in mentioned_ids
        
        intimacy = max(0, min(100, int(info.get("intimacy", 60))))
        p_like = intimacy / 100.0
        p_comment = (intimacy / 100.0) * 0.6
        
        should_comment = is_mentioned or (random.random() < p_comment)
        should_like = is_mentioned or (random.random() < p_like)

        if should_like:
            ts = post_ts_str if is_mentioned else random_ts_in_24h()
            likers.append({"liker_id": char_id, "timestamp": ts})
            
        if should_comment:
            comment_text = _generate_moment_comment(char_id, "user", post_content)
            if comment_text:
                ts = post_ts_str if is_mentioned else random_ts_in_24h()
                comments.append({
                    "commenter_id": char_id,
                    "content": comment_text,
                    "timestamp": ts
                })
    return likers, comments


@app.route("/api/moments/post", methods=["POST"])
def moments_user_post():
    """用户发一条朋友圈。body: { content }。发完后按各角色亲密度随机生成点赞和评论。"""
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "内容不能为空"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    likers, comments = _generate_likes_comments_for_user_moment(now, content)
    new_post = {
        "char_id": "user",
        "content": content,
        "timestamp": now,
        "likers": likers,
        "comments": comments
    }

    moments_path, _ = get_moments_paths()
    raw = []
    if os.path.exists(moments_path):
        try:
            with open(moments_path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except Exception:
            raw = []
    raw.append(new_post)
    safe_save_json(moments_path, raw)

    # 统一处理角色的短期记忆：在朋友圈保存后再依次记录，确保包含朋友圈内容
    for c in new_post.get("comments", []):
        cid = c.get("commenter_id")
        if cid and cid != "user":
            # 格式：包含用户朋友圈内容 + 角色的评论内容
            ctx = f"看到用户的朋友圈：「{content}」。你评论说：「{c.get('content', '')}」。"
            append_moment_event_to_short_memory(cid, ctx)
            
    return jsonify({"status": "success", "timestamp": now})


@app.route("/api/moments/regenerate", methods=["POST"])
def moments_regenerate():
    """重新生成一条朋友圈：角色帖重生成正文，用户帖重生成点赞/评论。body: { char_id, timestamp }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths()
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

    if char_id == "user":
        post_content = post.get("content", "")
        likers, comments = _generate_likes_comments_for_user_moment(timestamp_str, post_content)
        post["likers"] = likers
        post["comments"] = comments
    else:
        # 重新生成角色帖前先同步该角色的单聊与群聊短期记忆
        try:
            ok, err = sync_memory_before_moments(char_id)
            if not ok:
                print(f"   ⚠️ [Moments] 重新生成前记忆同步失败: {err}")
        except Exception as e:
            print(f"   ⚠️ [Moments] 重新生成前记忆同步异常: {e}")

        base_system_prompt = build_system_prompt(char_id, include_global_format=False, recent_messages=None, include_long_memory=False)
        lang = get_ai_language()
        if lang == "zh":
            trigger_msg = (
                "请结合你最近的经历（如短期记忆里的事）发一条朋友圈，内容简短自然。可以包含：\n"
                "- 纯文字；或\n"
                "- 照片：用 [写真（说明）] 表示，可多条（0-9枚）；\n"
                "- 视频：用 [动画] 表示。\n"
                "只输出这一条朋友圈的内容，不要加引号、不要加「朋友圈：」等前缀。"
            )
        else:
            trigger_msg = (
                "最近の出来事（短期記憶など）を踏まえて、朋友圈を1本投稿してください。短く自然な内容にし、"
                "写真[写真（説明）]・動画[動画]等形式を使えます。"
                "引用符や接頭辞は付けず、本文だけを出力してください。"
            )
        messages = [
            {"role": "system", "content": base_system_prompt},
            {"role": "user", "content": trigger_msg}
        ]
        try:
            route, current_model = get_model_config("moments")
            if route == "relay":
                content = call_openrouter(messages, char_id=char_id, model_name=current_model)
            else:
                content = call_gemini(messages, char_id=char_id, model_name=current_model)
            if content:
                content = content.strip().strip('"\'')
                if content:
                    post["content"] = content
        except Exception as e:
            print(f"📷 [Moments] 重新生成内容失败: {e}")
            return jsonify({"error": "生成失败"}), 500

        raw[idx] = post
        safe_save_json(moments_path, raw)
    return jsonify({"status": "success"})


@app.route("/api/moments/delete", methods=["POST"])
def moments_delete():
    """彻底删除某条朋友圈。body: { char_id, timestamp }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    if not char_id or not timestamp_str:
        return jsonify({"error": "缺少 char_id 或 timestamp"}), 400

    moments_path, _ = get_moments_paths()
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

    # 执行物理删除
    del raw[idx]
    safe_save_json(moments_path, raw)
    return jsonify({"status": "success"})


@app.route("/api/moments/edit", methods=["POST"])
def moments_edit():
    """编辑朋友圈正文。body: { char_id, timestamp, new_content }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    new_content = (data.get("new_content") or "").strip()
    if not char_id or not timestamp_str or not new_content:
        return jsonify({"error": "缺少必要参数"}), 400

    moments_path, _ = get_moments_paths()
    if not os.path.exists(moments_path):
        return jsonify({"error": "暂无数据"}), 404
    try:
        with open(moments_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    idx, post = _find_moment_post(raw, char_id, timestamp_str)
    if idx is None:
        return jsonify({"error": "未找到该条朋友圈"}), 404

    post["content"] = new_content
    raw[idx] = post
    safe_save_json(moments_path, raw)
    return jsonify({"status": "success"})


@app.route("/api/moments/comment/delete", methods=["POST"])
def moments_comment_delete():
    """彻底删除某条评论。body: { char_id, timestamp, comment_index }。"""
    data = request.get_json() or {}
    char_id = data.get("char_id")
    timestamp_str = data.get("timestamp")
    comment_index = data.get("comment_index")
    try:
        comment_index = int(comment_index)
    except:
        return jsonify({"error": "无效的 comment_index"}), 400

    moments_path, _ = get_moments_paths()
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


@app.route("/api/moments/comment/edit", methods=["POST"])
def moments_comment_edit():
    """编辑某条评论。body: { char_id, timestamp, comment_index, new_content }。"""
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

    moments_path, _ = get_moments_paths()
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
        comments[comment_index]["content"] = new_content
        post["comments"] = comments
        raw[idx] = post
        safe_save_json(moments_path, raw)
        return jsonify({"status": "success"})
    return jsonify({"error": "评论未找到"}), 404


# 2. 【新增】个人主页
@app.route("/profile")
def profile_view():
    return render_template("profile.html")

# 聊天页面改为带 ID 的路由
# 注意：原来的 / 路由废弃或重定向
@app.route("/chat/<char_id>")
def chat_view(char_id):
    # 这里只是返回 HTML，前端会根据 URL 里的 ID 去加载数据
    # 实际项目中，您可能需要把 char_id 传给模板，或者让前端自己解析 URL
    return send_from_directory("templates", "chat.html")

# --- 【新增】群聊页面路由 ---
@app.route("/chat/group/<group_id>")
def group_chat_view(group_id):
    # 复用 chat.html，但在前端根据 URL 区分逻辑
    return send_from_directory("templates", "chat.html")

# --- 【修正版】获取通讯录 (角色 + 群聊 混合列表) ---
@app.route("/api/contacts")
def get_contacts():
    # 1. 读取当前用户的角色配置 (users/<user_id>/configs/characters.json)
    cfg_file = _get_characters_config_file()
    if not os.path.exists(cfg_file):
        return jsonify([])

    with open(cfg_file, "r", encoding="utf-8") as f:
        chars_config = json.load(f)

    contact_list = []

    # 当前登录用户，用于过滤只显示“自己拥有的角色”
    user_id = get_current_user_id()
    user_char_root = os.path.join(USERS_ROOT, str(user_id), "characters") if user_id else None

    # 1. 读取当前用户的已读状态文件（per-user）
    read_status = {}
    status_file = _get_read_status_file()
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    read_status = json.loads(content)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️ [Warning] 读取已读状态冲突 (可忽略): {e}")
            read_status = {}

    # --- A. 处理单人角色 ---
    for char_id, info in chars_config.items():
        # 登录状态下：如果该用户没有这个角色目录，则不在通讯录中显示
        if user_char_root:
            char_dir = os.path.join(user_char_root, char_id)
            if not os.path.exists(char_dir):
                continue

        db_path, _ = get_paths(char_id)

        last_msg = ""
        last_time = ""
        timestamp_val = 0

        # --- 【新增】计算未读数 ---
        unread_count = 0
        if os.path.exists(db_path):
            try:
                # 获取上次已读时间，如果没有则默认为很久以前
                last_read = read_status.get(char_id, "2000-01-01 00:00:00")

                # 查询：时间 > last_read 且 role != 'user' (不是我发的) 的消息数量
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ? AND role != 'user'", (last_read,))
                unread_count = cursor.fetchone()[0]
                conn.close()
            except: pass

        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT content, timestamp FROM messages ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                conn.close()
                if row:
                    last_msg = row[0]
                    timestamp_val = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S').timestamp()
                    # 简单的时间格式化
                    dt = datetime.fromtimestamp(timestamp_val)
                    if dt.date() == datetime.now().date():
                        last_time = dt.strftime('%H:%M')
                    else:
                        last_time = dt.strftime('%m-%d')
            except: pass

        contact_list.append({
            "type": "char", # 标记类型
            "id": char_id,
            "avatar": info.get("avatar", "/static/default_avatar.png"),
            "name": info.get("name"),
            "remark": info.get("remark") or info["name"],
            "last_msg": last_msg,
            "last_time": last_time,
            "timestamp": timestamp_val,
            "pinned": info.get("pinned", False),
            "unread": unread_count # <--- 加上这个
        })

    # --- B. 处理群聊（per-user: users/<user_id>/configs/groups.json）---
    groups_cfg = _get_groups_config_file()
    if os.path.exists(groups_cfg):
        try:
            with open(groups_cfg, "r", encoding="utf-8") as f:
                groups_config = json.load(f)

            for group_id, info in groups_config.items():
                group_dir = get_group_dir(group_id)
                db_path = os.path.join(group_dir, "chat.db")

                last_msg = ""
                last_time = ""
                timestamp_val = 0
                unread_count = 0  # <--- 初始化为 0

                if os.path.exists(db_path):
                    try:
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()

                        # 1. 获取最后一条消息
                        cursor.execute("SELECT content, timestamp FROM messages ORDER BY id DESC LIMIT 1")
                        row = cursor.fetchone()

                        if row:
                            last_msg = row[0]
                            timestamp_val = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S').timestamp()
                            dt = datetime.fromtimestamp(timestamp_val)
                            if dt.date() == datetime.now().date():
                                last_time = dt.strftime('%H:%M')
                            else:
                                last_time = dt.strftime('%m-%d')

                        # 2. 【新增】计算未读数
                        # 获取该群的最后阅读时间
                        last_read = read_status.get(group_id, "2000-01-01 00:00:00")

                        # 统计：时间 > last_read 且 发言人不是 user 的消息
                        cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ? AND role != 'user'", (last_read,))
                        unread_count = cursor.fetchone()[0]

                        conn.close()
                    except: pass

                contact_list.append({
                    "type": "group",
                    "id": group_id,
                    "avatar": info.get("avatar", "/static/default_group.png"),
                    "name": info.get("name"),
                    "remark": info.get("name"),
                    "last_msg": last_msg,
                    "last_time": last_time,
                    "timestamp": timestamp_val,
                    "pinned": info.get("pinned", False),
                    "members": info.get("members", []),
                    "unread": unread_count  # <--- 【关键】把计算结果放进去
                })
        except Exception as e:
            print(f"Error loading groups: {e}")

    # 4. 统一排序
    contact_list.sort(key=lambda x: (1 if x['pinned'] else 0, x['timestamp']), reverse=True)

    return jsonify(contact_list)

# --- 【修正版】单聊历史记录 (精准定位版) ---
@app.route("/api/<char_id>/history", methods=["GET"])
def get_history(char_id):
    limit = request.args.get('limit', 20, type=int)
    target_id = request.args.get('target_id', type=int)
    before_id = request.args.get('before_id', type=int)

    db_path, _ = get_paths(char_id)
    if not os.path.exists(db_path): init_char_db(char_id)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    messages = []

    # A. 向上滚动 (锚点模式)
    if before_id:
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    # B. 跳转定位 (精准覆盖模式)
    elif target_id:
        # 1. 找到目标消息的时间戳
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']
            # 2. 计算比它新的消息有多少条
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]

            # 3. 【关键】设定 Limit = 比它新的数量 + 1 (它自己) + 5 (缓冲，防止毫秒级误差)
            # 这样一次性加载从最新到它（包含它）的所有数据
            dynamic_limit = count_newer + 6

            cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ?", (dynamic_limit,))
            messages = [dict(row) for row in cursor.fetchall()][::-1]

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
    if get_ai_language() == "ja":
        for m in messages:
            m["content"] = _add_furigana_to_japanese(m["content"])

    return jsonify({
        "messages": messages,
        "total": total_messages
    })

# --- 【修正版】群聊历史记录 (精准定位版) ---
@app.route("/api/group/<group_id>/history", methods=["GET"])
def get_group_history(group_id):
    limit = request.args.get('limit', 20, type=int)
    target_id = request.args.get('target_id', type=int)
    before_id = request.args.get('before_id', type=int)

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

    # B. 跳转定位
    elif target_id:
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]

            # 动态 Limit
            dynamic_limit = count_newer + 6

            cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ?", (dynamic_limit,))
            messages = [dict(row) for row in cursor.fetchall()][::-1]

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
    if get_ai_language() == "ja":
        for m in messages:
            m["content"] = _add_furigana_to_japanese(m["content"])

    return jsonify({"messages": messages, "total": total})

# 这是在 app.py 文件中

# ---------------------- 核心聊天接口 (时间感知注入版) ----------------------
# --- 核心聊天接口 (多角色适配 + 返回ID修正版) ---
@app.route("/api/<char_id>/chat", methods=["POST"])
def chat(char_id):
    # 1. 动态获取路径
    db_path, prompts_dir = get_paths(char_id)

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
        ok, err = sync_memory_before_single_chat(char_id)
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
    messages = build_messages_for_chat_v2(char_id, user_msg_raw, recent_messages=[r["content"] for r in history_rows])
        
        # 添加系统提示时间信息
    now = datetime.now()
    lang = get_ai_language()
    hour = now.hour
        
    if 5 <= hour < 11: period = "早上" if lang == "zh" else "朝"
    elif 11 <= hour < 13: period = "中午" if lang == "zh" else "昼"
    elif 13 <= hour < 18: period = "下午" if lang == "zh" else "午後"
    elif 18 <= hour < 23: period = "晚上" if lang == "zh" else "夜"
    else: period = "深夜" if lang == "zh" else "深夜"
        
    if lang == "zh":
        system_hint = (
            f"（系统提示：现在是{period} {now.strftime('%H:%M')}。）\n"
            f"（用户发来了一条消息。请根据时间线中的上下文，回复用户的消息。）\n"
            f"（要求：自然、简短，不要重复上一句话。）\n"
            f"（无特殊说明时用斜线表示换行和句号。）"
        )
    else:
        system_hint = (
            f"（システム通知：現在は{period} {now.strftime('%H:%M')}です。）\n"
            f"（ユーザーからメッセージが来ました。タイムライン内容を踏まえて回信してください。）\n"
            f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）\n"
            f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
        )
        
    messages.append({"role": "system", "content": system_hint})
    
    # 获取当前配置
    route, current_model = get_model_config("chat") # 任务类型是 chat

    print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

    try:
        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 清理时间戳
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()
        # 移除 AI 连续重复的拍一拍
        cleaned_reply_text = _strip_consecutive_tickle(cleaned_reply_text)
        # 把 AI 回复里的 [表情]name 转成 [表情]path 再入库
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

        if get_ai_language() == "ja":
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        # 【重点】把 ID 返回给前端；记忆同步失败时附带提示
        resp = {
            "replies": reply_bubbles,
            "id": ai_msg_id,
            "user_id": user_msg_id
        }
        if memory_sync_warning:
            resp["memory_sync_warning"] = memory_sync_warning
        return jsonify(resp)

    except Exception as e:
        print(f"Chat Error: {e}")
        return jsonify({"error": str(e)}), 500


# ===================== 【新增】System Prompt v2 测试路由 =====================

@app.route("/api/<char_id>/chat_v2", methods=["POST"])
def chat_v2(char_id):
    """【测试版】使用新的时间线聚合System Prompt v2版本的聊天接口。"""
    # 1. 路径准备
    db_path, prompts_dir = get_paths(char_id)
    if not os.path.exists(db_path):
        init_char_db(char_id)

    # 2. 获取用户输入
    data = request.json or {}
    user_msg_raw = data.get("message", "").strip()
    if not user_msg_raw:
        return jsonify({"error": "empty message"}), 400

    # 3. 检查深睡眠状态
    is_deep_sleep = False
    cfg_file = _get_characters_config_file()
    try:
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            char_info = all_config.get(char_id, {})
            is_deep_sleep = char_info.get("deep_sleep", False)
    except:
        pass

    # 4. 存入用户消息
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))
    user_msg_id = cursor.lastrowid
    conn.commit()
    conn.close()

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
        ok, err = sync_memory_before_single_chat(char_id)
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
    messages = build_messages_for_chat_v2(char_id, user_msg_raw, recent_messages=recent_texts)

    # 添加时间提示
    lang = get_ai_language()
    hour = now.hour
    time_str = now.strftime('%H:%M')

    if 5 <= hour < 11: period = "早上" if lang == "zh" else "朝"
    elif 11 <= hour < 13: period = "中午" if lang == "zh" else "昼"
    elif 13 <= hour < 18: period = "下午" if lang == "zh" else "午後"
    elif 18 <= hour < 23: period = "晚上" if lang == "zh" else "夜"
    else: period = "深夜" if lang == "zh" else "深夜"

    if lang == "zh":
        system_hint = (
            f"（系统提示：现在是{period} {time_str}。）\n"
            f"（用户发来了一条消息。请根据时间线中的上下文，自然地回复用户。）\n"
            f"（要求：简短、自然，不要重复上一句话。）\n"
            f"（无特殊说明时用斜线表示换行和句号。）"
        )
    else:
        system_hint = (
            f"（システム通知：現在は{period} {time_str}です。）\n"
            f"（ユーザーからメッセージが来ました。タイムラインを踏まえて回信してください。）\n"
            f"（要件：簡潔で自然。直前の発言を繰り返さないこと。）\n"
            f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
        )

    messages.append({"role": "system", "content": system_hint})

    # 7. 调用AI
    route, current_model = get_model_config("chat")
    print(f"--- [Chat v2] char_id: {char_id}, route: {route}, model: {current_model} ---")

    try:
        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 清理回复
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply = re.sub(timestamp_pattern, '', reply_text_raw).strip()
        cleaned_reply = _strip_consecutive_tickle(cleaned_reply)
        cleaned_reply = _sticker_content_from_ai(cleaned_reply)

        # 存入AI回复
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        ai_ts = (now + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", cleaned_reply, ai_ts))
        ai_msg_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # 尝试记录到短期记忆
        try:
            today = now.strftime("%Y-%m-%d")
            time_label = now.strftime("%H:%M")
            ctx = f"(user) {user_msg_raw[:100]} (ai) {cleaned_reply[:100]}"
            append_short_memory_event(char_id, ctx, today, time_label)
        except Exception as e:
            print(f"   ⚠️ 无法记录短期记忆: {e}")

        resp = {
            "replies": [{"content": cleaned_reply, "id": ai_msg_id}],
            "id": ai_msg_id,
            "user_id": user_msg_id,
            "model": current_model
        }
        if memory_sync_warning:
            resp["memory_sync_warning"] = memory_sync_warning

        return jsonify(resp)

    except Exception as e:
        print(f"Chat v2 Error: {e}")
        return jsonify({"error": str(e)}), 500


# --- 【修正版】重新生成接口 (自动补全 User 引导) ---
@app.route("/api/<char_id>/regenerate", methods=["POST"])
def regenerate_message(char_id):
    # 1. 获取路径
    db_path, prompts_dir = get_paths(char_id)
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
        system_prompt = build_system_prompt_v2(char_id, recent_messages=recent_texts, user_latest_input=user_latest)
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

        # ================= 【核心新增】智能补位逻辑 =================
        # 检查发给 AI 的最后一条消息是谁说的
        if len(messages) > 1: # 排除掉只有 System Prompt 的情况
            last_msg_role = messages[-1]['role']
            lang = get_ai_language()
            hour = now.hour
            time_str = now.strftime('%H:%M')

            # 计算时间段
            if 5 <= hour < 11: period = "早上" if lang == "zh" else "朝"
            elif 11 <= hour < 13: period = "中午" if lang == "zh" else "昼"
            elif 13 <= hour < 18: period = "下午" if lang == "zh" else "午後"
            elif 18 <= hour < 23: period = "晚上" if lang == "zh" else "夜"
            else: period = "深夜" if lang == "zh" else "深夜"

            # 情况1: 最后一条是用户消息（正常重新生成）
            if last_msg_role == 'user':
                # 【全局采用 v2】v2已包含完整时间线，保持简洁的提示
                if lang == "zh":
                    system_hint = (
                        f"（系统提示：现在是{period} {time_str}。）\n"
                        f"（请根据系统时间线，回复用户的消息。）\n"
                        f"（要求：自然、简短。）"
                    )
                else:
                    system_hint = (
                        f"（システム通知：現在は{period} {time_str}です。）\n"
                        f"（タイムラインを踏まえて、ユーザーに返信してください。）\n"
                        f"（要件：自然で簡潔に。）"
                    )
                messages.append({"role": "system", "content": system_hint})
                print(f"--- [Regenerate] 最后一条是用户消息，添加简洁系统提示 ---")

            # 情况2: 最后一条是AI消息（连续回复，用主动消息风格触发）
            elif last_msg_role == 'assistant' or last_msg_role == 'model':
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

        # 7. 调用 AI
        route, current_model = get_model_config("chat")
        print(f"--- [Regenerate] Route: {route}, Model: {current_model} ---")

        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 8. 清理 & 存入
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()
        cleaned_reply_text = _strip_consecutive_tickle(cleaned_reply_text)
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

        if get_ai_language() == "ja":
            reply_bubbles = [_add_furigana_to_japanese(b) for b in reply_bubbles]

        return jsonify({
            "status": "success",
            "replies": reply_bubbles,
            "id": new_id
        })

    except Exception as e:
        print(f"Regenerate Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【修正版】群聊核心接口 (完整逻辑：@解析 + 串行 + 变量修复) ---
@app.route("/api/group/<group_id>/chat", methods=["POST"])
def group_chat(group_id):
    import random
    import re

    # 1. 基础准备
    data = request.json
    user_msg = data.get("message", "").strip()
    if not user_msg: return jsonify({"error": "empty"}), 400
    # 获取 user_id 用于后续 relay
    user_id = get_current_user_id()

    # --- 群聊前自动同步：总结群内各角色单聊 + 本群群聊短期记忆 ---
    memory_sync_warning = None
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

                # 2. 检查是否在线 (Deep Sleep False)
                # 只有在群成员列表里 且 没有深睡眠 的才算在线
                if cid in ai_members_all:
                    is_sleeping = cinfo.get("deep_sleep", False)
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
        sys_prompt = build_system_prompt_v2(speaker_id, include_global_format=True, recent_messages=recent_texts, user_latest_input=user_latest)
        
        other_members = [m for m in all_members if m != speaker_id]
        rel_prompt = build_group_relationship_prompt(speaker_id, other_members)

        full_sys_prompt = sys_prompt + "\n\n" + rel_prompt + "\n【Current Situation】\n当前是在群聊中。请注意上下文，与其他成员自然互动。"

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

        # 2. 循环处理每一条历史消息
        for row in history_rows:
            # a. 处理时间戳格式
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                if show_full_date:
                    # 跨天：显示 [12-25 14:30]
                    ts_str = dt_obj.strftime('[%m-%d %H:%M]')
                else:
                    # 同天：只显示 [14:30]
                    ts_str = dt_obj.strftime('[%H:%M]')
            except:
                ts_str = ""

            # b. 处理名字 (群聊必须带名字，否则AI分不清谁是谁)
            # id_to_name 是函数开头建立的映射表
            r_id = row['role']
            d_name = "User" if r_id == "user" else id_to_name.get(r_id, r_id)

            # c. 组合 Content (格式: [12:30] [洁世一]: 咱们去踢球吧)
            # 注意：历史记录里的所有人对当前AI来说都是 external input (user)
            msg_role = "user"
            content_for_ai = _sticker_content_for_ai(row['content'])
            content_with_tag = f"{ts_str} [{d_name}]: {content_for_ai}"

            messages.append({"role": msg_role, "content": content_with_tag})

        # 1. 获取当前配置
        route, current_model = get_model_config("chat") # 任务类型是 chat

        print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

        try:
            if route == "relay":
                reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model, user_id=user_id)
            else:
                reply_text = call_gemini(messages, char_id=speaker_id, model_name=current_model)

            timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
            cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()
            cleaned_reply = _strip_consecutive_tickle(cleaned_reply)

            # 去除 AI 自带的名字前缀
            name_pattern = f"^\\[{speaker_name}\\][:：]\\s*"
            cleaned_reply = re.sub(name_pattern, '', cleaned_reply).strip()
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
    if get_ai_language() == "ja":
        for rep in replies_for_frontend:
            # group chat 返回的是单个 string 还是分段？其实分段在前端分，这里 content 是 string
            rep["content"] = _add_furigana_to_japanese(rep["content"])

    # 7. 最终返回；记忆同步失败时附带提示
    resp = {"replies": replies_for_frontend, "user_id": user_msg_id}
    if memory_sync_warning:
        resp["memory_sync_warning"] = memory_sync_warning
    return jsonify(resp)

# --- 辅助：写入个人群聊日志 ---
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

# 3. 【新增】在 app.py 末尾添加这两个新接口
# --- 【修正版】删除消息 (带结果检查) ---
@app.route("/api/<char_id>/messages/<int:msg_id>", methods=["DELETE"])
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

# --- 【新增】群聊消息删除接口 ---
@app.route("/api/group/<group_id>/messages/<int:msg_id>", methods=["DELETE"])
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

# --- 【修正】编辑消息接口 (必须接收 char_id) ---
@app.route("/api/<char_id>/messages/<int:msg_id>", methods=["PUT"])
def edit_message(char_id, msg_id):  # <--- 1. 必须加上 char_id 参数
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

# --- 【新增】群聊消息编辑接口 ---
@app.route("/api/group/<group_id>/messages/<int:msg_id>", methods=["PUT"])
def edit_group_message(group_id, msg_id):
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

# --- 【新增】API 设置接口（多用户：每人一份 api_settings.json） ---
API_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "api_settings.json")  # 仅用于未登录或早期兼容

def _ensure_selected_models_in_options(config: dict, default_config: dict = None) -> dict:
    """
    保证 routes[*].models 中已配置的模型一定出现在 model_options[*] 里，
    避免前端下拉框找不到已保存值后回退到其他模型。
    """
    if not isinstance(config, dict):
        return config

    routes = config.get("routes") or {}
    if not isinstance(routes, dict):
        routes = {}
        config["routes"] = routes

    model_options = config.get("model_options")
    if not isinstance(model_options, dict):
        model_options = {}
        config["model_options"] = model_options

    default_options = (default_config or {}).get("model_options", {}) if isinstance(default_config, dict) else {}
    model_keys = ("chat", "moments", "gen_persona", "summary", "vision", "translation")

    for route_key, route_data in routes.items():
        existing = model_options.get(route_key)
        if isinstance(existing, list):
            options = existing
        else:
            options = list(default_options.get(route_key, []))
            model_options[route_key] = options

        models = (route_data or {}).get("models", {}) if isinstance(route_data, dict) else {}
        if not isinstance(models, dict):
            continue

        for mk in model_keys:
            mv = models.get(mk)
            if not isinstance(mv, str):
                continue
            mv = mv.strip()
            if mv and mv not in options:
                options.append(mv)

    return config

@app.route("/api/system_config", methods=["GET", "POST"])
def handle_system_config():
    # 在 handle_system_config 函数里

    # 初始化默认配置 (增加了 model_options 字段)
    default_config = {
        "active_route": "relay",
        "enable_system_prompt_v2": True,  # 【新增】默认启用 v2
        "routes": {
            "gemini": {
                "name": "线路一：Gemini 直连",
                "models": {"chat": "gemini-2.5-pro", "moments": "gemini-2.5-pro", "gen_persona": "gemini-3.1-pro-preview", "summary": "gemini-2.5-flash", "vision": "gemini-2.5-pro", "translation": "gemini-2.5-flash-lite"}
            },
            "relay": {
                "name": "线路二：国内中转",
                "relay_provider": "new",
                "models": {"chat": "gemini-2.5-flash", "moments": "gemini-2.5-flash", "gen_persona": "gemini-3.1-pro", "summary": "gemini-2.0-flash", "vision": "gpt-4o", "translation": "gpt-4o-mini"}
            }
        },
        # 【新增】可用的模型列表 (把以前前端写死的搬到这里)
        "model_options": {
            'gemini': [
                'gemini-3-pro-preview',
                'gemini-3-flash-preview',
                'gemini-2.5-pro',
                'gemini-2.5-flash-lite',
                'gemini-2.5-flash',
                'gemini-3.1-pro-preview',
                'gemini-1.5-flash-8b'
            ],
            'relay': [
                'gemini-3.1-pro',
                'gemini-2.5-pro',
                'gemini-2.5-flash',
                'gpt-4o',
                'gpt-3.5-turbo-0125',
                'gemini-2.0-flash',
                'gpt-4o-mini'
            ]
        }
    }

    user_id = get_current_user_id()
    # 登录用户的专属配置文件
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(user_cfg_dir, exist_ok=True)
        user_api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        user_api_cfg_file = API_CONFIG_FILE

    if request.method == "GET":
        cfg_file = user_api_cfg_file if user_api_cfg_file else API_CONFIG_FILE
        if not os.path.exists(cfg_file):
            return jsonify(default_config)
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            # 兼容旧配置补齐默认值
            for route_key, route_data in config.get("routes", {}).items():
                models = route_data.get("models", {})
                
                # 动态获取当前线路的基础 chat 模型，如果是缺失则默认为相应线路的兜底
                base_chat = models.get("chat", "gemini-2.5-pro" if route_key == "gemini" else "gpt-3.5-turbo")
                
                if "moments" not in models:
                    models["moments"] = base_chat
                if "vision" not in models:
                    models["vision"] = "gemini-2.5-pro" if route_key == "gemini" else "gpt-4o"
                if "translation" not in models:
                    models["translation"] = "gemini-1.5-flash-8b" if route_key == "gemini" else "gpt-3.5-turbo-0125"
                if "summary" not in models:
                    models["summary"] = base_chat
                if "gen_persona" not in models:
                    models["gen_persona"] = "gemini-3-pro-preview" if route_key == "gemini" else "gpt-3.5-turbo"
                
                if route_key == "relay" and "relay_provider" not in route_data:
                    route_data["relay_provider"] = "new" # relay 线路缺失 provider 时默认用新中转商
            
            if "model_options" not in config:
                config["model_options"] = default_config["model_options"]
            # 如果之前保存的数据里 active_route 不存在，也要退回 relay
            if "active_route" not in config:
                config["active_route"] = "relay"

            config = _ensure_selected_models_in_options(config, default_config)
            return jsonify(config)
        except:
            return jsonify(default_config)

    if request.method == "POST":
        new_config = request.json or {}
        new_config = _ensure_selected_models_in_options(new_config, default_config)
        cfg_file = user_api_cfg_file if user_api_cfg_file else API_CONFIG_FILE
        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(new_config, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# --- 【新增】一键切换 System Prompt v1/v2 ---
@app.route("/api/toggle_prompt_v2", methods=["POST"])
def toggle_prompt_v2():
    """
    一键切换 System Prompt v1/v2 版本。
    请求体：{"enable_v2": true/false} 或 {} （为空则自动切换）
    """
    user_id = get_current_user_id()
    
    # 获取配置文件路径
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(user_cfg_dir, exist_ok=True)
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE
    
    try:
        # 读取现有配置
        if os.path.exists(api_cfg_file):
            with open(api_cfg_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}
        
        # 获取请求数据
        data = request.get_json() or {}
        
        # 决定新状态
        if "enable_v2" in data:
            # 显式指定
            new_status = bool(data["enable_v2"])
        else:
            # 自动切换
            current = config.get("enable_system_prompt_v2", False)
            new_status = not current
        
        # 更新配置
        config["enable_system_prompt_v2"] = new_status
        
        # 保存
        os.makedirs(os.path.dirname(api_cfg_file), exist_ok=True)
        with open(api_cfg_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        
        return jsonify({
            "status": "success",
            "v2_enabled": new_status,
            "version": "v2 (时间线聚合版)" if new_status else "v1 (原始版本)",
            "message": f"已切换到 {'v2' if new_status else 'v1'} 版本"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【新增】查看当前 System Prompt 版本状态 ---
@app.route("/api/prompt_version_status", methods=["GET"])
def get_prompt_version_status():
    """
    查看当前使用的 System Prompt 版本。
    可选参数: char_id （查询特定角色的配置）
    """
    user_id = get_current_user_id()
    char_id = request.args.get("char_id")
    
    # 获取配置文件路径
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE
    
    try:
        # 读取全局配置
        if os.path.exists(api_cfg_file):
            with open(api_cfg_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}
        
        # 全局配置
        global_v2_enabled = config.get("enable_system_prompt_v2", False)
        
        # 检查是否有角色级别的配置（可选）
        char_v2_enabled = None
        if char_id:
            try:
                char_cfg_path = os.path.join(BASE_DIR, "configs", "characters.json")
                if os.path.exists(char_cfg_path):
                    with open(char_cfg_path, "r", encoding="utf-8") as f:
                        all_chars = json.load(f) or {}
                    char_info = all_chars.get(char_id, {})
                    if "use_prompt_v2" in char_info:
                        char_v2_enabled = char_info["use_prompt_v2"]
            except:
                pass
        
        # 最终状态：角色级 > 全局
        final_v2_enabled = char_v2_enabled if char_v2_enabled is not None else global_v2_enabled
        
        return jsonify({
            "status": "success",
            "global_v2_enabled": global_v2_enabled,
            "char_id": char_id,
            "char_v2_enabled": char_v2_enabled,
            "final_v2_enabled": final_v2_enabled,
            "version": "v2 (时间线聚合版)" if final_v2_enabled else "v1 (原始版本)"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== 【新增】主题风格设置 API =====================

# 预定义的主题颜色方案
THEME_PRESETS = {
    "pink": {
        "name": "粉红",
        "primary": "#ffb6b9",
        "primary-dark": "#f09598",
        "bg": "#f2f4f8",
        "card-bg": "#ffffff"
    },
    "blue": {
        "name": "蓝色",
        "primary": "#a8d8ea",
        "primary-dark": "#7dbfd3",
        "bg": "#f0f4f8",
        "card-bg": "#ffffff"
    },
    "purple": {
        "name": "紫色",
        "primary": "#c8a8d8",
        "primary-dark": "#b390d3",
        "bg": "#f5f0f8",
        "card-bg": "#ffffff"
    },
    "green": {
        "name": "绿色",
        "primary": "#a8d8a8",
        "primary-dark": "#90c890",
        "bg": "#f0f8f0",
        "card-bg": "#ffffff"
    },
    "orange": {
        "name": "橙色",
        "primary": "#ffb366",
        "primary-dark": "#ff9944",
        "bg": "#f8f4f0",
        "card-bg": "#ffffff"
    }
}

def _get_theme_config_file():
    """获取当前用户的主题配置文件路径"""
    user_id = get_current_user_id()
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        os.makedirs(user_cfg_dir, exist_ok=True)
        return os.path.join(user_cfg_dir, "theme_settings.json")
    else:
        cfg_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "theme_settings.json")

@app.route("/api/theme/settings", methods=["GET", "POST"])
def handle_theme_settings():
    """获取或保存主题设置"""
    theme_file = _get_theme_config_file()
    default_theme = {
        "preset": "pink",
        "custom_colors": {},
        "default_chat_bg": None,  # 默认聊天背景图片名称
    }

    if request.method == "GET":
        if os.path.exists(theme_file):
            try:
                with open(theme_file, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                # 移合并默认值
                for key in default_theme:
                    if key not in settings:
                        settings[key] = default_theme[key]
                return jsonify(settings)
            except:
                return jsonify(default_theme)
        return jsonify(default_theme)

    elif request.method == "POST":
        try:
            data = request.json or {}
            theme_file_dir = os.path.dirname(theme_file)
            os.makedirs(theme_file_dir, exist_ok=True)
            
            with open(theme_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route("/api/theme/presets", methods=["GET"])
def get_theme_presets():
    """获取预定义的主题方案"""
    return jsonify(THEME_PRESETS)

@app.route("/api/theme/upload_bg", methods=["POST"])
def upload_theme_background():
    """上传默认聊天背景图"""
    try:
        user_id = get_current_user_id()
        if user_id:
            bg_dir = os.path.join(USERS_ROOT, str(user_id), "configs", "theme_backgrounds")
        else:
            bg_dir = os.path.join(BASE_DIR, "configs", "theme_backgrounds")
        
        os.makedirs(bg_dir, exist_ok=True)
        
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
        
        # 验证文件类型
        allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in allowed_ext:
            return jsonify({"error": "Image type not allowed"}), 400
        
        # 生成唯一文件名
        import uuid
        new_filename = f"default_bg_{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(bg_dir, new_filename)
        file.save(file_path)
        
        return jsonify({
            "status": "success",
            "filename": new_filename,
            "path": f"/theme_backgrounds/{new_filename}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/theme_backgrounds/<filename>")
def serve_theme_background(filename):
    """提供主题背景图片"""
    user_id = get_current_user_id()
    if user_id:
        bg_dir = os.path.join(USERS_ROOT, str(user_id), "configs", "theme_backgrounds")
    else:
        bg_dir = os.path.join(BASE_DIR, "configs", "theme_backgrounds")
    
    file_path = os.path.join(bg_dir, filename)
    if os.path.exists(file_path):
        return send_file(file_path)
    return "", 404

# ===================== 【新增】聊天背景设置 API =====================

def _get_chat_bg_config_file(char_id):
    """获取聊天背景配置文件路径"""
    _, prompts_dir = get_paths(char_id)
    return os.path.join(prompts_dir, "chat_bg_config.json")

@app.route("/api/<char_id>/chat_background", methods=["GET"])
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

@app.route("/api/<char_id>/upload_chat_background", methods=["POST"])
def upload_chat_background(char_id):
    """上传聊天背景图"""
    try:
        _, prompts_dir = get_paths(char_id)
        bg_dir = os.path.join(prompts_dir, "backgrounds")
        os.makedirs(bg_dir, exist_ok=True)
        
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
        
        # 验证文件类型
        allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in allowed_ext:
            return jsonify({"error": "Image type not allowed"}), 400
        
        # 生成唯一文件名
        import uuid
        new_filename = f"bg_{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(bg_dir, new_filename)
        file.save(file_path)
        
        return jsonify({
            "status": "success",
            "filename": new_filename
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/<char_id>/save_chat_background", methods=["POST"])
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

@app.route("/char_backgrounds/<char_id>/<filename>")
def serve_char_background(char_id, filename):
    """提供聊天背景图"""
    _, prompts_dir = get_paths(char_id)
    bg_dir = os.path.join(prompts_dir, "backgrounds")
    file_path = os.path.join(bg_dir, filename)
    
    if os.path.exists(file_path):
        return send_file(file_path)
    return "", 404

# ===================== 【新增】群聊背景设置 API =====================

def _get_group_chat_bg_config_file(group_id):
    """获取群聊背景配置文件路径"""
    group_dir = get_group_dir(group_id)
    return os.path.join(group_dir, "chat_bg_config.json")

@app.route("/api/group/<group_id>/chat_background", methods=["GET"])
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

@app.route("/api/group/<group_id>/upload_chat_background", methods=["POST"])
def upload_group_chat_background(group_id):
    """上传群聊背景图"""
    try:
        group_dir = get_group_dir(group_id)
        bg_dir = os.path.join(group_dir, "backgrounds")
        os.makedirs(bg_dir, exist_ok=True)
        
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
        
        # 验证文件类型
        allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in allowed_ext:
            return jsonify({"error": "Image type not allowed"}), 400
        
        # 生成唯一文件名
        import uuid
        new_filename = f"bg_{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(bg_dir, new_filename)
        file.save(file_path)
        
        return jsonify({
            "status": "success",
            "filename": new_filename
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/group/<group_id>/save_chat_background", methods=["POST"])
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

@app.route("/group_backgrounds/<group_id>/<filename>")
def serve_group_background(group_id, filename):
    """提供群聊背景图"""
    group_dir = get_group_dir(group_id)
    bg_dir = os.path.join(group_dir, "backgrounds")
    file_path = os.path.join(bg_dir, filename)
    
    if os.path.exists(file_path):
        return send_file(file_path)
    return "", 404

# 这是在 app.py 文件中的 call_openrouter 函数

# ---------------------- OpenRouter / Compatible API ----------------------

# --- 【新增】记录 API 报错日志 ---
def log_api_error(service_name, status_code, response_text, messages=None):
    """当 API 返回非 200 或解析失败时调用，记录到 logs/api.log"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_content = [
        f"\n{'!'*20} API ERROR: {service_name} {'!'*20}",
        f"Time: {timestamp}",
        f"Status Code: {status_code}",
        f"Response: {response_text[:1000]}" # 记录前1000字，防止过大
    ]
    if messages:
        log_content.append("--- Last Prompt Sent ---")
        for i, m in enumerate(messages[-3:]): # 只记录最后3条上下文，节省日志空间
            log_content.append(f"[{m.get('role')}]: {m.get('content')[:200]}")
    
    final_log = "\n".join(log_content) + f"\n{'!'*50}\n"
    print(final_log) # 终端显示
    
    try:
        log_dir = os.path.join(BASE_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "api.log"), "a", encoding="utf-8") as f:
            f.write(final_log)
    except: pass

def get_relay_provider(user_id=None):
    """
    获取当前 relay 线路配置的中转商提供商。
    返回值：'new' (新的中转商) 或 'old' (旧的中转商) 或自定义URL
    如果配置中未找到，默认返回 'old'
    
    参数：
    - user_id: 可选，用于后台任务直接指定用户，避免 ContextVar 在多线程中无效的问题
    """
    if user_id is None:
        user_id = get_current_user_id()
    
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE

    if not os.path.exists(api_cfg_file):
        return "old"  # 默认使用旧的中转商

    try:
        with open(api_cfg_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        relay_config = config.get("routes", {}).get("relay", {})
        provider = relay_config.get("relay_provider", "old")
        
        # 如果选择了自定义网址，返回自定义URL
        if provider == "custom":
            custom_url = relay_config.get("relay_custom_url")
            if custom_url:
                return custom_url
        
        return provider
    except:
        return "old"  # 读取失败时默认使用旧的中转商

def call_openrouter(messages, char_id="unknown", model_name="gpt-3.5-turbo", user_id=None):
    import requests
    import random
    import os
    import traceback

    # 🛡️ 1. 准备顶级浏览器伪装 User-Agent 库 (破解 Cloudflare 403 的核心)
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0"
    ]

    # 【日志】打印发送前的 Prompt
    log_full_prompt(f"OpenRouter ({model_name})", messages)

    # 2. 根据配置选择中转商
    relay_provider = get_relay_provider(user_id)
    if relay_provider.startswith("http://") or relay_provider.startswith("https://"):
        # 自定义网址
        base_url = relay_provider
        print(f"--- [Debug] Using CUSTOM relay provider: {base_url}")
    elif relay_provider == "old":
        base_url = OPENROUTER_BASE_URL_OLD
        print(f"--- [Debug] Using OLD relay provider: {base_url}")
    else:
        base_url = OPENROUTER_BASE_URL
        print(f"--- [Debug] Using NEW relay provider: {base_url}")

    url = f"{base_url}/chat/completions"

    # 🛡️ 3. 组装极其逼真的伪装 Headers
    headers = {
        "Authorization": f"Bearer {get_effective_openrouter_key()}",
        "Content-Type": "application/json",
        "User-Agent": random.choice(user_agents),  # 随机切换浏览器标识
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://kunigami-project-api.online/",
        "Origin": "https://kunigami-project-api.online",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache"
    }

    # 4. 构造 Payload
    # 聚合所有 system 消息
    final_messages = []
    system_contents = []
    for m in messages:
        if m.get('role') == 'system':
            system_contents.append(m.get('content', ''))
        else:
            final_messages.append(m)
    
    if system_contents:
        # 将多个 system 消息合并为一个，放在最前面
        merged_system = {"role": "system", "content": "\n\n".join(system_contents)}
        final_messages.insert(0, merged_system)

    payload = {
        "model": model_name,
        "messages": final_messages,
        "temperature": 1,
        "max_tokens": 4096
    }

    print(f"--- [Debug] Calling Compatible API at: {url}")
    print(f"--- [Debug] Using model: {payload['model']}")

    try:
        # 发起请求
        r = requests.post(url, json=payload, headers=headers, timeout=100)

        # 🛡️ 5. 核心修改：如果是 403 或 525 拦截，绝对不能把 HTML 源码传回前端
        if r.status_code != 200:
            log_api_error(f"OpenRouter ({model_name})", r.status_code, r.text, messages=messages)
            
            common_suffix = "（API网站：新中转商：https://api2d.com | 旧中转商：https://vg.a3e.top/ | 自定义：自行配置）"
            
            if r.status_code == 401:
                return f"（系统提示：身份验证失败。请检查是否在【个人主页-账号与通知设置-openrouter】中正确填写了 API Key。\n{common_suffix}）"
            elif r.status_code == 402:
                return f"（系统提示：账户点数不足，请前往 API 网站充值。\n{common_suffix}）"
            elif r.status_code == 403:
                # 细化 403：部分中转商用 403 表示模型代码错误
                err_text = r.text.lower()
                if "model" in err_text or "not found" in err_text:
                    return f"（系统提示：请正确填写模型代码。\n{common_suffix}）"
                return "（系统提示：当前网络波动，AI 暂时被防火墙拦截，请换个话题或稍后再试。）"
            elif r.status_code == 524:
                return "（系统提示：请求超时，AI 思考时间过长。请尝试缩短当前聊天内容或精简人设设定。）"
            elif r.status_code == 525:
                return "（系统提示：中转服务器连接异常(SSL)，请稍后再试或联系管理员。）"
            elif r.status_code == 429:
                return "（系统提示：请求过于频繁，AI 累了，请休息一分钟再聊哦。）"
            elif r.status_code >= 500:
                return f"（系统提示：AI 服务商目前繁忙（{r.status_code}），请稍后再试。）"
            else:
                return f"（系统提示：服务连接异常，错误码: {r.status_code}）"

        # 🛡️ 6. 核心修改：解析 JSON 异常处理（防 HTML 脏数据透传）
        try:
            result = r.json()
        except Exception as parse_err:
            log_api_error(f"OpenRouter ({model_name})", "JSON_PARSE_ERROR", r.text, messages=messages)
            return "（系统提示：AI 返回了无法解析的异常信号，请重试。）"

        # 处理 API 返回的 ['error'] 字段
        if "error" in result:
            err_msg = result["error"].get("message", "Unknown error")
            log_api_error(f"OpenRouter ({model_name})", "API_INTERNAL_ERROR", str(result["error"]), messages=messages)
            return f"（系统提示：AI 服务返回内部错误: {err_msg}）"

        # 7. Token 计费记录
        if 'usage' in result:
            usage = result['usage']
            record_token_usage(
                char_id,
                model_name,
                usage.get('prompt_tokens', 0),
                usage.get('completion_tokens', 0),
                usage.get('total_tokens', 0)
            )

        # 8. 检查 choices 列表是否为空
        if "choices" not in result or len(result["choices"]) == 0:
            print(f"⚠️ [Empty Response] API 返回了空列表。")
            return "（系统提示：AI 暂时陷入了沉思，请换个话题试试。）"

        # 🛡️ 就在这里！把 API 返回的所有原始 JSON 打印出来
        import json
        print("🔍 [DEBUG] API 原始完整响应:")
        print(json.dumps(result, indent=2, ensure_ascii=False)) 

        # 9. 一切正常，提取内容
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            print(f"⚠️ [Parse Error] 无法解析响应结构: {e}")
            return "（系统提示：数据结构解析失败，请重试。）"

        # 记录成功日志
        log_full_prompt(f"OpenRouter ({model_name})", messages, response_text=content)

        return content

    except requests.exceptions.Timeout:
        return "（系统提示：连接 AI 服务器超时，对方思考得太久了，请稍后重试。）"
    except Exception as e:
        print(f"[ERROR] API 调用异常: {e}\n{traceback.format_exc()}")
        return "（系统提示：网络链路不稳定，请稍后再试。）"
    
def call_gemini(messages, char_id="unknown", model_name="gemini-2.0-flash"):
    """
    Google 官方直连 (配合 Cloudflare Worker) - 深度加固版
    """
    import requests
    import json
    import random
    import os

    # 1. 动态获取地址与密钥
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
    api_key = get_effective_gemini_key()
    url = f"{base_url}/v1beta/models/{model_name}:generateContent?key={api_key}"

    # 2. 转换消息格式
    gemini_contents = []
    system_parts = []
    
    for msg in messages:
        if msg['role'] == 'system':
            system_parts.append(msg['content'])
        else:
            role = 'model' if msg['role'] == 'assistant' else 'user'
            gemini_contents.append({"role": role, "parts": [{"text": msg['content']}]})

    # 聚合所有 system 消息为一个 systemInstruction
    system_instruction = None
    if system_parts:
        system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]}

    # 3. 构造请求头 (加入浏览器伪装，防止被代理层拦截)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    }

    # 4. 构造 Payload (关掉安全审查)
    payload = {
        "contents": gemini_contents,
        "generationConfig": {
            "temperature": 1,
            "maxOutputTokens": 4096 # 建议从 10000 调低到 4096，更稳定
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    try:
        # 发送请求
        r = requests.post(url, json=payload, headers=headers, timeout=100)

        # 🛡️ 核心修改：拦截非 200 状态码
        if r.status_code != 200:
            log_api_error(f"Gemini {model_name}", r.status_code, r.text, messages=messages)
            
            if r.status_code == 400:
                return "（系统提示：请求参数异常（400），请联系管理员检查配置。）"
            elif r.status_code == 403:
                return "（系统提示：访问被拒绝（403）。请检查是否在【个人主页-账号与通知设置-gemini】中正确填写了 API Key。）"
            elif r.status_code == 429:
                return "（系统提示：请求过于频繁（429），谷歌端限制了访问频率，请发慢一点哦。）"
            elif r.status_code in [500, 504]:
                return f"（系统提示：服务器响应超时或内部错误（{r.status_code}），请尝试精简聊天内容或缩减人设设定。）"
            elif r.status_code == 503:
                return "（系统提示：谷歌服务端当前过载（503），请稍后再试。）"
            else:
                return f"（系统提示：AI 暂时无法连接，错误码: {r.status_code}）"

        # 🛡️ 核心修改：解析 JSON 失败
        try:
            result = r.json()
        except Exception as parse_err:
            log_api_error(f"Gemini {model_name}", "JSON_PARSE_ERROR", r.text, messages=messages)
            return "（系统提示：接收到了异常信号，请重试。）"

        # 处理错误响应字段
        if "error" in result:
            err_info = str(result["error"])
            log_api_error(f"Gemini {model_name}", "API_INTERNAL_ERROR", err_info, messages=messages)
            return f"（系统提示：API 内部错误: {result['error'].get('message', 'Unknown')}）"

        # 5. 提取 Token 数据
        token_usage = result.get('usageMetadata', {})
        if token_usage:
            record_token_usage(
                char_id,
                model_name,
                token_usage.get('promptTokenCount', 0),
                token_usage.get('candidatesTokenCount', 0),
                token_usage.get('totalTokenCount', 0)
            )

        # 6. 解析回复内容
        if 'candidates' not in result or not result['candidates']:
            return "（AI 陷入了沉默，没有给出回复。）"

        candidate = result['candidates'][0]
        text = ""
        
        # 尝试获取文本
        try:
            if 'content' in candidate and 'parts' in candidate['content']:
                text = candidate['content']['parts'][0]['text']
            else:
                # 获取结束原因（比如被安全策略拦截，虽然我们设了 BLOCK_NONE，但有时仍会触发）
                finish_reason = candidate.get('finishReason', 'UNKNOWN')
                text = f"（由于系统限制，AI 无法生成此段对话。原因: {finish_reason}）"
        except (KeyError, IndexError, TypeError) as e:
            print(f"⚠️ [Gemini 解析错误]: {e}")
            return "（系统提示：回复解析失败。）"

        # 7. 记录完整日志
        log_full_prompt(f"Gemini Interaction ({model_name})", messages, response_text=text, usage=token_usage)

        return text

    except requests.exceptions.Timeout:
        return "（系统提示：AI 思考太久啦，连接超时，请重试。）"
    except Exception as e:
        print(f"🔥 [Gemini 未知异常]: {e}")
        return "（系统提示：网络连接波动，请稍后再试。）"

def get_model_config(task_type="chat", user_id=None):
    """
    根据配置文件，获取当前应该用的 路由方式 和 模型名称
    task_type: 'chat' | 'moments' | 'gen_persona' | 'summary'
    """
    # 如果没有显式传入 user_id，才退回使用上下文获取
    if user_id is None:
        user_id = get_current_user_id()

    # 多用户：优先读取 users/<user_id>/configs/api_settings.json
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE

    if not os.path.exists(api_cfg_file):
        # 默认兜底
        return "relay", "gpt-3.5-turbo"

    try:
        with open(api_cfg_file, "r", encoding="utf-8") as f:
            config = json.load(f)

        route = config.get("active_route", "gemini")
        models = config.get("routes", {}).get(route, {}).get("models", {})
        # 朋友圈未配置时与 chat 相同
        if task_type == "moments" and "moments" not in models:
            model_name = models.get("chat", "gpt-3.5-turbo")
        else:
            model_name = models.get(task_type, "gpt-3.5-turbo")

        return route, model_name
    except:
        return "gemini", "gemini-2.5-pro"

@app.route("/api/user/profile_settings", methods=["GET", "POST"])
def user_profile_settings():
    # 读取逻辑：按当前登录用户的 user_settings.json（users/<user_id>/configs/user_settings.json）
    data = _load_user_settings()

    if request.method == "GET":
        # 注意：为了安全，GET请求不返回密码，或者返回空
        return jsonify({
            "name": data.get("current_user_name", "User"),
            "ai_language": data.get("ai_language", "zh"),
            "age": data.get("user_age"),
            "tickle_suffix": data.get("tickle_suffix", ""),
            "email": data.get("email", ""),
            "gemini_api_key": data.get("gemini_api_key", ""),
            "openrouter_api_key": data.get("openrouter_api_key", "")
            # 不返回 password
        })

    if request.method == "POST":
        data_in = request.json or {}

        # 更新字段
        if "name" in data_in: data["current_user_name"] = data_in["name"]
        if "ai_language" in data_in: data["ai_language"] = data_in["ai_language"]
        if "tickle_suffix" in data_in:
            # 去掉默认文案，允许为空
            data["tickle_suffix"] = str(data_in["tickle_suffix"]).strip()
        if "age" in data_in:
            val = data_in["age"]
            if val is None or val == "":
                data.pop("user_age", None)
                data.pop("user_age_last_incremented", None)
            else:
                try:
                    data["user_age"] = int(val)
                except (ValueError, TypeError):
                    pass

        # 新增：邮箱 & API Key
        if "email" in data_in:
            data["email"] = str(data_in["email"] or "").strip()
        if "gemini_api_key" in data_in:
            data["gemini_api_key"] = str(data_in["gemini_api_key"] or "").strip()
        if "openrouter_api_key" in data_in:
            data["openrouter_api_key"] = str(data_in["openrouter_api_key"] or "").strip()

        # 【新增】更新密码
        if "password" in data_in and data_in["password"]:
            data["password"] = data_in["password"]

        # 写回当前用户的设置文件
        path = _get_user_settings_file()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})


@app.route("/api/user/unlock_keys", methods=["POST"])
def unlock_keys():
    """
    校验当前登录账号的登录密码，正确后返回存储在 user_settings 中的 API Keys。
    仅用于个人主页短暂查看，不在会话中长期缓存。
    """
    uid = get_current_user_id()
    if not uid:
        return jsonify({"status": "error", "message": "未登录"}), 401

    data_in = request.get_json() or {}
    password = (data_in.get("password") or "").strip()
    if not password:
        return jsonify({"status": "error", "message": "密码不能为空"}), 400

    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE id = ?", (uid,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        print(f"[unlock_keys] users.db 查询失败: {e}")
        return jsonify({"status": "error", "message": "内部错误"}), 500

    if not row or not check_password_hash(row[0], password):
        return jsonify({"status": "error", "message": "密码不正确"}), 401

    # 校验通过后，从 user_settings 读取 Key
    settings = _load_user_settings()
    return jsonify({
        "status": "success",
        "gemini_api_key": settings.get("gemini_api_key", ""),
        "openrouter_api_key": settings.get("openrouter_api_key", "")
    })

# --- 【修正版】API：手动触发记忆整理 ---
@app.route("/api/<char_id>/memory/snapshot", methods=["POST"])
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

# --- 【新增】定向重新生成中期记忆 (Day Summary) ---
@app.route("/api/<char_id>/memory/regenerate_medium", methods=["POST"])
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


# --- 【新增】定向重新生成长期记忆 (Week Summary) ---
@app.route("/api/<char_id>/memory/regenerate_long", methods=["POST"])
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

# --- 【新增】群聊增量更新逻辑 ---
def update_group_short_memory(group_id, target_date_str):
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
    import re
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

# --- 【修正】群聊快照接口 (真实实现) ---
@app.route("/api/group/<group_id>/memory/snapshot", methods=["POST"])
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

# 加在 app.py 的路由区域
@app.route("/api/<char_id>/debug/force_maintenance")
def force_maintenance(char_id):
    scheduled_maintenance() # 手动调用上面那个定时函数
    return jsonify({"status": "triggered", "message": "已手动触发后台维护，请查看服务器控制台日志"})

# --- 【新增】记忆面板页面 ---
@app.route("/memory/<char_id>")
def memory_view(char_id):
    return send_from_directory("templates", "memory.html")

# --- 【修正版】获取 Prompts 数据 ---
@app.route("/api/<char_id>/prompts_data")
def get_prompts_data(char_id):
    # 每次加载时尝试迁移（若尚未迁移）
    migrate_persona_extract_age(char_id)

    data = {}
    files = {
        "base": "1_base_persona.md",
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
        path = os.path.join(prompts_dir, filename)
        content = "（文件不存在或为空）"

        # 在 get_prompts_data 函数里

        if os.path.exists(path):
            try:
                # 【修改点】把 utf-8 改为 utf-8-sig
                with open(path, "r", encoding="utf-8-sig") as f:
                    if filename.endswith(".json"):
                        try:
                            content = json.load(f)
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


# --- 【新增】关系图谱反向读取接口 ---
@app.route("/api/<char_id>/relationship_reverse")
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


# --- 【新增】保存反向关系接口 ---
@app.route("/api/<char_id>/save_relationship_reverse", methods=["POST"])
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


# --- 【新增】保存 Prompt 文件的接口 ---
@app.route("/api/<char_id>/save_prompt", methods=["POST"])
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

# ================= 群聊记忆页面专用接口 =================

# 1. 页面路由
@app.route("/memory/group/<group_id>")
def group_memory_view(group_id):
    return render_template("group_memory.html")

# 2. 获取群聊数据 (配置 + 记忆)
@app.route("/api/group/<group_id>/prompts_data")
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
@app.route("/api/group/<group_id>/save_memory", methods=["POST"])
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
@app.route("/api/group/<group_id>/update_meta", methods=["POST"])
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

        # 【新增】主动消息开关
        if "active_mode" in data:
            all_groups[group_id]["active_mode"] = bool(data["active_mode"])

        # 置顶开关（与单聊一致，通讯录中置顶显示）
        if "pinned" in data:
            all_groups[group_id]["pinned"] = bool(data["pinned"])

        with open(groups_cfg, "w", encoding="utf-8") as f:
            json.dump(all_groups, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【修正版】搜索接口 ---
@app.route("/api/<char_id>/search", methods=["POST"])
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

# --- 【新增】群聊搜索接口 ---
@app.route("/api/group/<group_id>/search", methods=["POST"])
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

# --- 【修正】常用语接口 (per-user: users/<user_id>/configs/quick_phrases.json) ---
@app.route("/api/quick_phrases", methods=["GET", "POST"])
def handle_quick_phrases():
    path = _get_quick_phrases_file()

    # GET: 读取列表
    if request.method == "GET":
        if not os.path.exists(path):
            return jsonify([])
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return jsonify(json.load(f))
        except:
            return jsonify([])

    # POST: 保存列表
    if request.method == "POST":
        new_list = request.json
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(new_list, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

# --- 表情库 API ---
def _list_official_packs():
    """stickers 下的子目录名列表"""
    if not os.path.isdir(STICKERS_ROOT):
        return []
    return [d for d in os.listdir(STICKERS_ROOT) if os.path.isdir(os.path.join(STICKERS_ROOT, d)) and not d.startswith(".")]


COVER_BASENAME = "cover"
PACK_META_FILE = "meta.json"


def _get_pack_meta(pack_id):
    """读取表情包 meta.json：{ name, uploaded_by, uploaded_by_name }，无则返回 None"""
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return None
    meta_path = os.path.join(STICKERS_ROOT, pack_id, PACK_META_FILE)
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _get_pack_cover_url(pack_id):
    """某表情包目录下名为 cover 的封面图（cover.png / cover.jpg 等），返回 URL，无则返回 None"""
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return None
    pack_dir = os.path.join(STICKERS_ROOT, pack_id)
    if not os.path.isdir(pack_dir):
        return None
    for ext in STICKER_IMAGE_EXT:
        f = COVER_BASENAME + ext
        if os.path.isfile(os.path.join(pack_dir, f)):
            path = f"official:{pack_id}:{f}"
            return _stickers_relative_to_url(path)
    return None


def _list_pack_stickers(pack_id):
    """某表情包下的表情文件，返回 [{path, name, url}]（不含封面 cover）"""
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return []
    pack_dir = os.path.join(STICKERS_ROOT, pack_id)
    if not os.path.isdir(pack_dir):
        return []
    out = []
    for f in os.listdir(pack_dir):
        if f.startswith("."):
            continue
        low = f.lower()
        if any(low.endswith(ext) for ext in STICKER_IMAGE_EXT):
            name_no_ext = os.path.splitext(f)[0]
            if name_no_ext.lower() == COVER_BASENAME:
                continue
            path = f"official:{pack_id}:{f}"
            out.append({"path": path, "name": name_no_ext, "url": _stickers_relative_to_url(path)})
    return out


def _search_stickers(q):
    """按名称搜索：官方库 + 当前用户个人上传。返回 [{path, name, url, pack_name}]"""
    q = (q or "").strip().lower()
    out = []
    # 官方库
    for pack_id in _list_official_packs():
        for s in _list_pack_stickers(pack_id):
            if q in s["name"].lower():
                s = dict(s)
                s["pack_name"] = pack_id
                out.append(s)
    # 用户上传
    ud = _get_stickers_upload_dir()
    if ud and os.path.isdir(ud):
        for f in os.listdir(ud):
            if f.startswith("."):
                continue
            low = f.lower()
            if any(low.endswith(ext) for ext in STICKER_IMAGE_EXT):
                name = os.path.splitext(f)[0]
                if q in name.lower():
                    path = f"user:{f}"
                    out.append({"path": path, "name": name, "url": _stickers_relative_to_url(path), "pack_name": "个人上传"})
    return out


def _load_favorites():
    path = _get_stickers_favorites_file()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return []


def _save_favorites(arr):
    path = _get_stickers_favorites_file()
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


@app.route("/api/stickers/allowed_descriptions", methods=["GET"])
def api_stickers_allowed_descriptions():
    """返回 AI 与上传页统一使用的表情描述列表（来自 sticker_descriptions_sorted.txt），上传时用户从此列表挑选"""
    return jsonify(_get_sticker_allowed_descriptions())


@app.route("/api/stickers/packs", methods=["GET"])
def api_stickers_packs():
    """列表：全部官方表情包（id, name, cover 封面 URL），用于添加页浏览；name 优先取 meta.json"""
    os.makedirs(STICKERS_ROOT, exist_ok=True)
    packs = []
    for p in _list_official_packs():
        meta = _get_pack_meta(p)
        name = (meta.get("name") if meta else None) or p
        cover = _get_pack_cover_url(p)
        packs.append({"id": p, "name": name, "cover": cover})
    return jsonify(packs)


@app.route("/api/stickers/my_packs", methods=["GET"])
def api_stickers_my_packs():
    """当前用户已添加的表情包（用于聊天页底部 tab 显示）"""
    added = _load_added_sticker_packs()
    official = {p for p in _list_official_packs()}
    packs = []
    for pid in added:
        if pid in official:
            meta = _get_pack_meta(pid)
            name = (meta.get("name") if meta else None) or pid
            cover = _get_pack_cover_url(pid)
            packs.append({"id": pid, "name": name, "cover": cover})
    return jsonify(packs)


@app.route("/api/stickers/packs/add", methods=["POST"])
def api_stickers_packs_add():
    """将某官方表情包添加到当前用户的「我的表情包」"""
    uid = get_current_user_id()
    if not uid:
        return jsonify({"status": "error", "message": "login required"}), 401
    data = request.json or {}
    pack_id = (data.get("pack_id") or data.get("packId") or "").strip()
    if not pack_id:
        return jsonify({"status": "error", "message": "pack_id required"}), 400
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return jsonify({"status": "error", "message": "invalid pack_id"}), 400
    if pack_id not in _list_official_packs():
        return jsonify({"status": "error", "message": "pack not found"}), 404
    added = _load_added_sticker_packs()
    if pack_id in added:
        return jsonify({"status": "success"})
    added.append(pack_id)
    if not _save_added_sticker_packs(added):
        return jsonify({"status": "error", "message": "save failed"}), 500
    return jsonify({"status": "success"})


@app.route("/api/stickers/pack/<pack_id>", methods=["GET"])
def api_stickers_pack(pack_id):
    """某表情包详情：名称 + 表情列表；name 优先取 meta.json"""
    meta = _get_pack_meta(pack_id)
    name = (meta.get("name") if meta else None) or pack_id
    stickers = _list_pack_stickers(pack_id)
    return jsonify({"name": name, "stickers": stickers})


@app.route("/api/stickers/search", methods=["GET"])
def api_stickers_search():
    q = request.args.get("q", "").strip()
    items = _search_stickers(q)
    return jsonify(items)


@app.route("/api/stickers/favorites", methods=["GET"])
def api_stickers_favorites_get():
    paths = _load_favorites()
    out = []
    for path in paths:
        ab = _stickers_path_to_abs(path)
        if not ab or not os.path.isfile(ab):
            continue
        name = os.path.splitext(os.path.basename(ab))[0]
        out.append({"path": path, "name": name, "url": _stickers_relative_to_url(path)})
    return jsonify(out)


@app.route("/api/stickers/favorites", methods=["POST"])
def api_stickers_favorites_add():
    data = request.json or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"status": "error", "message": "path required"}), 400
    if not path.startswith("official:") and not path.startswith("user:"):
        resolved = _resolve_sticker_name_to_path_deterministic(path)
        if resolved:
            path = resolved
    ab = _stickers_path_to_abs(path)
    if not ab or not os.path.isfile(ab):
        return jsonify({"status": "error", "message": "invalid path or 未找到匹配表情"}), 400
    paths = _load_favorites()
    if path in paths:
        return jsonify({"status": "success"})
    paths.append(path)
    if not _save_favorites(paths):
        return jsonify({"status": "error", "message": "save failed"}), 500
    return jsonify({"status": "success"})


@app.route("/api/stickers/favorites", methods=["DELETE"])
def api_stickers_favorites_remove():
    data = request.json or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"status": "error", "message": "path required"}), 400
    paths = _load_favorites()
    if path in paths:
        paths.remove(path)
        _save_favorites(paths)
    return jsonify({"status": "success"})


def _sanitize_sticker_name(s: str) -> str:
    """将用户输入的描述转为安全文件名（保留扩展名由调用方拼接）"""
    s = (s or "").strip()
    s = re.sub(r'[<>:"/\\|?*\s]+', "_", s)
    s = s.strip("._") or "sticker"
    return s[:80]


@app.route("/api/stickers/upload", methods=["POST"])
def api_stickers_upload():
    ud = _get_stickers_upload_dir()
    if not ud:
        return jsonify({"status": "error", "message": "login required"}), 401
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"status": "error", "message": "no file"}), 400
    low = f.filename.lower()
    if not any(low.endswith(ext) for ext in STICKER_IMAGE_EXT):
        return jsonify({"status": "error", "message": "unsupported format"}), 400
    ext = ""
    for e in STICKER_IMAGE_EXT:
        if low.endswith(e):
            ext = e
            break
    custom_name = (request.form.get("name") or request.form.get("description") or "").strip()
    if custom_name:
        base = _sanitize_sticker_name(custom_name)
        safe_name = base + ext
    else:
        safe_name = os.path.basename(f.filename)
    if ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        return jsonify({"status": "error", "message": "invalid filename"}), 400
    dest = os.path.join(ud, safe_name)
    if os.path.exists(dest):
        base = os.path.splitext(safe_name)[0]
        for i in range(1, 1000):
            safe_name = f"{base}_{i}{ext}"
            dest = os.path.join(ud, safe_name)
            if not os.path.exists(dest):
                break
    try:
        f.save(dest)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    path = f"user:{safe_name}"
    name = os.path.splitext(safe_name)[0]
    return jsonify({"path": path, "name": name, "url": _stickers_relative_to_url(path)})


@app.route("/stickers/upload")
def sticker_upload_page():
    """上传表情包至公开库的页面（需登录）"""
    if not get_current_user_id():
        return redirect(url_for("login_view") + "?next=" + request.path)
    return render_template("sticker_upload.html")


@app.route("/api/stickers/packs/upload", methods=["POST"])
def api_stickers_packs_upload():
    """上传新表情包到官方库：名称、封面、多个表情及名称。需登录。"""
    uid = get_current_user_id()
    if not uid:
        return jsonify({"status": "error", "message": "login required"}), 401
    username = get_current_username() or str(uid)
    pack_name = (request.form.get("name") or request.form.get("pack_name") or "").strip()
    if not pack_name:
        return jsonify({"status": "error", "message": "name required"}), 400
    cover = request.files.get("cover")
    if not cover or not cover.filename:
        return jsonify({"status": "error", "message": "cover required"}), 400
    cover_low = cover.filename.lower()
    if not any(cover_low.endswith(ext) for ext in STICKER_IMAGE_EXT):
        return jsonify({"status": "error", "message": "cover must be image"}), 400
    slug = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", pack_name).strip().replace(" ", "_")[:40] or "pack"
    slug = slug.replace(" ", "_")
    pack_id = slug + "_" + str(int(time.time()))
    pack_dir = os.path.join(STICKERS_ROOT, pack_id)
    if os.path.exists(pack_dir):
        pack_id = slug + "_" + str(uuid.uuid4())[:8]
        pack_dir = os.path.join(STICKERS_ROOT, pack_id)
    os.makedirs(pack_dir, exist_ok=True)
    cover_ext = None
    for ext in STICKER_IMAGE_EXT:
        if cover_low.endswith(ext):
            cover_ext = ext
            break
    cover.save(os.path.join(pack_dir, COVER_BASENAME + cover_ext))
    sticker_count = 0
    i = 0
    while True:
        f = request.files.get(f"sticker_{i}")
        if not f or not f.filename:
            break
        name_key = f"sticker_name_{i}"
        custom_name = (request.form.get(name_key) or request.form.get(f"name_{i}") or "").strip()
        low = f.filename.lower()
        if not any(low.endswith(ext) for ext in STICKER_IMAGE_EXT):
            i += 1
            continue
        ext = ""
        for e in STICKER_IMAGE_EXT:
            if low.endswith(e):
                ext = e
                break
        base = _sanitize_sticker_name(custom_name) if custom_name else os.path.splitext(f.filename)[0]
        base = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", base).strip().replace(" ", "_")[:60] or f"sticker_{i}"
        safe_name = base + ext
        dest = os.path.join(pack_dir, safe_name)
        idx = 0
        while os.path.exists(dest):
            idx += 1
            safe_name = f"{base}_{idx}{ext}"
            dest = os.path.join(pack_dir, safe_name)
        f.save(dest)
        sticker_count += 1
        i += 1
    if sticker_count == 0:
        return jsonify({"status": "error", "message": "at least one sticker required"}), 400
    meta = {
        "name": pack_name,
        "uploaded_by": uid,
        "uploaded_by_name": username,
    }
    with open(os.path.join(pack_dir, PACK_META_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "success", "pack_id": pack_id, "name": pack_name})


@app.route("/stickers/pack/<pack_id>")
def sticker_pack_detail_page(pack_id):
    """表情包详情页（独立 HTML）；显示 meta 中的 name 与上传者"""
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return "Invalid pack", 404
    if pack_id not in _list_official_packs():
        return "Pack not found", 404
    meta = _get_pack_meta(pack_id)
    name = (meta.get("name") if meta else None) or pack_id
    author = (meta.get("uploaded_by_name") if meta else None) or "官方"
    stickers = _list_pack_stickers(pack_id)
    from_char = request.args.get("from", "")
    from_group = request.args.get("group", "0")
    added = pack_id in _load_added_sticker_packs()
    return render_template(
        "pack_detail.html",
        pack_id=pack_id,
        name=name,
        stickers=stickers,
        author=author,
        from_char=from_char,
        from_group=from_group,
        added=added,
    )


@app.route("/api/stickers/file", methods=["GET"])
def api_stickers_file():
    """根据 path 或 name 返回表情图片。path 可为存储标识（official:xxx、user:xxx）或描述名（如 开心），后者会按名称包含匹配解析为实际 path 再返回。"""
    path = request.args.get("path", "").strip()
    if not path:
        return "", 404
    # 非存储 path 时视为描述名，解析为实际 path（确定性取第一条，避免历史变脸）
    if not path.startswith("official:") and not path.startswith("user:"):
        resolved = _resolve_sticker_name_to_path_deterministic(path)
        if resolved:
            path = resolved
    ab = _stickers_path_to_abs(path)
    if not ab or not os.path.isfile(ab):
        return "", 404
    return send_from_directory(os.path.dirname(ab), os.path.basename(ab), as_attachment=False)


@app.route("/api/stickers/resolve", methods=["GET"])
def api_stickers_resolve():
    """按名称匹配返回一个表情的 URL/path（确定性取第一条，避免历史记录每次刷新变脸）。"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"url": None})
    path = _resolve_sticker_name_to_path_deterministic(name)
    if not path:
        return jsonify({"url": None})
    url = _stickers_relative_to_url(path)
    name_display = _sticker_path_to_name(path)
    return jsonify({"url": url, "path": path, "name": name_display})

# --- 【新增】获取单个角色配置 ---
@app.route("/api/<char_id>/config")
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
                "tickle_suffix": ""
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

# --- 【新增】获取群组详情 (包含成员信息，支持多用户命名空间) ---
@app.route("/api/group/<group_id>/config")
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
    if os.path.exists(chars_cfg):
        try:
            with open(chars_cfg, "r", encoding="utf-8") as f:
                chars_config = json.load(f)
            for member_id in group_info.get("members", []):
                if member_id in chars_config:
                    members_details[member_id] = chars_config[member_id]
        except Exception as e:
            print(f"[get_group_details] 加载成员信息失败: {e}")

    return jsonify({
        "group_info": group_info,
        "members": members_details
    })

# --- 【新增】更新角色元数据 (头像/备注) ---
@app.route("/api/<char_id>/update_meta", methods=["POST"])
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
        new_remark = data.get("remark")
        new_avatar = data.get("avatar")
        new_pinned = data.get("pinned") # <--- 【新增】获取置顶状态

        # 允许改为空字符串，所以用 is not None 判断
        if new_remark is not None:
            all_config[char_id]["remark"] = new_remark.strip()

        if new_avatar is not None:
            all_config[char_id]["avatar"] = new_avatar.strip()

        # 【新增】更新置顶状态 (必须判断是否为 None，因为 False 也是有效值)
        if new_pinned is not None:
            all_config[char_id]["pinned"] = bool(new_pinned)

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

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Update Meta Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】获取角色私有资源 (图片等) ---
# 这样前端就能通过 /char_assets/kunigami/avatar.png 访问图片了
@app.route('/char_assets/<char_id>/<filename>')
def get_char_asset(char_id, filename):
    """
    角色私有资源读取：
    - 已登录：优先从 users/<user_id>/characters/<char_id>/ 下读取
    - 未登录：退回全局 characters/<char_id>/（主要用于调试）
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    uid = get_current_user_id()

    # 1. 已登录用户：从自己的角色目录读取
    if uid:
        user_char_dir = os.path.join(USERS_ROOT, str(uid), "characters", char_id)
        candidate = os.path.join(user_char_dir, filename)
        if os.path.exists(candidate):
            return send_from_directory(user_char_dir, filename)

    # 2. 未登录或用户目录不存在时，退回全局模板
    global_dir = os.path.join(base_dir, "characters", char_id)
    return send_from_directory(global_dir, filename)

# --- 【新增】上传角色头像 ---
@app.route("/api/<char_id>/upload_avatar", methods=["POST"])
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

            # 3. 更新 characters.json 里的路径（per-user）
            cfg_file = _get_characters_config_file()
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)

            # 生成新的访问 URL
            # 加上时间戳 ?v=... 是为了强制浏览器刷新缓存，立刻看到新头像
            timestamp = int(time.time())
            new_url = f"/char_assets/{char_id}/avatar.png?v={timestamp}"

            all_config[char_id]["avatar"] = new_url

            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(all_config, f, ensure_ascii=False, indent=2)

            return jsonify({"status": "success", "url": new_url})

        except Exception as e:
            print(f"Upload Error: {e}")
            return jsonify({"error": str(e)}), 500

# --- 【新增】获取群聊资源 (图片等) ---
@app.route('/group_assets/<group_id>/<filename>')
def get_group_asset(group_id, filename):
    # 指向 groups/<group_id> 文件夹
    directory = get_group_dir(group_id)
    return send_from_directory(directory, filename)

# --- 【新增】上传群聊头像 ---
@app.route("/api/group/<group_id>/upload_avatar", methods=["POST"])
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

            # 3. 更新 groups.json 配置
            if os.path.exists(GROUPS_CONFIG_FILE):
                with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                    groups_config = json.load(f)

                if group_id in groups_config:
                    # 生成新的访问 URL (带时间戳防缓存)
                    timestamp = int(time.time())
                    new_url = f"/group_assets/{group_id}/{filename}?v={timestamp}"

                    groups_config[group_id]["avatar"] = new_url

                    with open(GROUPS_CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(groups_config, f, ensure_ascii=False, indent=2)

                    return jsonify({"status": "success", "url": new_url})
                else:
                    return jsonify({"error": "Group config not found"}), 404
            else:
                return jsonify({"error": "Groups config file missing"}), 500

        except Exception as e:
            print(f"Group Upload Error: {e}")
            return jsonify({"error": str(e)}), 500

# --- 【新增】上传用户全局头像 ---
@app.route("/api/upload_user_avatar", methods=["POST"])
def upload_user_avatar():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            # 保存到当前用户目录：users/<user_id>/avatar.png
            user_dir = os.path.join(USERS_ROOT, str(user_id))
            os.makedirs(user_dir, exist_ok=True)
            
            # 删除旧的头像文件（所有格式）
            for old_avatar in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp", "avatar.gif"):
                old_path = os.path.join(user_dir, old_avatar)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception as e:
                        print(f"[UserAvatar] 删除旧头像失败: {e}")
            
            # 读取上传的图片，转换为PNG格式并保存
            save_path = os.path.join(user_dir, "avatar.png")
            
            # 使用PIL打开图片，统一转换为PNG（确保格式一致）
            try:
                img = Image.open(file.stream)
                # 如果是RGBA模式（带透明度），保留透明度；否则转换为RGB
                if img.mode in ('RGBA', 'LA', 'P'):
                    img_converted = img.convert('RGBA')
                else:
                    img_converted = img.convert('RGB')
                # 保存为PNG
                img_converted.save(save_path, 'PNG')
            except Exception as e:
                print(f"[UserAvatar] PIL转换失败，直接保存: {e}")
                # 如果PIL转换失败，直接保存原始文件
                file.seek(0)
                file.save(save_path)

            # 添加时间戳参数，防止浏览器缓存旧图片
            timestamp = int(time.time())
            new_url = f"/user_avatar?v={timestamp}"

            # 顺便把头像 URL 写回当前用户的 user_settings.json，供其它地方复用
            try:
                settings = _load_user_settings()
                settings["avatar"] = new_url
                safe_save_json(_get_user_settings_file(), settings)
            except Exception as e:
                print(f"[UserAvatar] 写入用户设置失败: {e}")

            return jsonify({"status": "success", "url": new_url})
        except Exception as e:
            print(f"User Avatar Upload Error: {e}")
            return jsonify({"error": str(e)}), 500


@app.route("/user_avatar")
def user_avatar():
    """
    按当前登录用户返回头像图片：
    - 优先读取 users/<user_id>/avatar.png 文件（统一PNG格式）
    - 如果不存在，则退回默认的 static/default_avatar.png
    """
    user_id = get_current_user_id()
    base_dir = os.path.dirname(os.path.abspath(__file__))

    if user_id:
        user_dir = os.path.join(USERS_ROOT, str(user_id))
        # 直接查找avatar.png（统一格式）
        candidate = os.path.join(user_dir, "avatar.png")
        if os.path.exists(candidate):
            return send_from_directory(user_dir, "avatar.png")

    # 未登录或用户没有自定义头像时，返回全局默认头像
    return send_from_directory(os.path.join(base_dir, "static"), "default_avatar.png")
# --- 【新增】获取全局配置 (用户人设 & 格式) ---
@app.route("/api/global_config", methods=["GET"])
def get_global_config():
    """
    获取“全局人设 & 格式”配置。
    已登录时优先读取 users/<user_id>/configs 下的覆盖文件，
    若不存在则退回 configs/ 目录下的共享模板。
    """
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = os.path.join(BASE_DIR, "configs")
    user_id = get_current_user_id()
    data = {}
    files = {
        "user_persona": "global_user_persona.md",
        "system_format": "global_format.md"
    }

    for key, filename in files.items():
        val = ""
        try:
            if user_id:
                # 已登录：只认自己 users/<user_id>/configs 下的文件，不再回落到全局
                user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
                user_path = os.path.join(user_cfg_dir, filename)
                if os.path.exists(user_path):
                    with open(user_path, "r", encoding="utf-8") as f:
                        val = f.read()
            else:
                # 未登录时才读取全局模板（主要是兼容调试场景）
                path = os.path.join(CONFIG_DIR, filename)
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        val = f.read()
        except Exception as e:
            print(f"[global_config] 读取 {filename} 失败: {e}")
        data[key] = val

    return jsonify(data)

# --- 【新增】保存全局配置 ---
@app.route("/api/save_global_config", methods=["POST"])
def save_global_config():
    key = request.json.get("key") # 'user_persona' or 'system_format'
    content = request.json.get("content")

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    user_id = get_current_user_id()

    filename_map = {
        "user_persona": "global_user_persona.md",
        "system_format": "global_format.md"
    }

    filename = filename_map.get(key)
    if not filename:
        return jsonify({"error": "Invalid key"}), 400

    try:
        # 保存到当前用户的 configs 目录；未登录则退回全局 configs
        if user_id:
            cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        else:
            cfg_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, filename), "w", encoding="utf-8") as f:
            f.write(content or "")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【新增】创建新角色接口 ---
@app.route("/api/characters/add", methods=["POST"])
def add_character():
    try:
        data = request.json
        new_id = data.get("id", "").strip()
        new_name = data.get("name", "").strip()

        # 1. 基础校验
        if not new_id or not new_name:
            return jsonify({"error": "ID和名称不能为空"}), 400

        # ID 只能是英文、数字、下划线 (作为文件夹名)
        import re
        if not re.match(r'^[a-zA-Z0-9_]+$', new_id):
            return jsonify({"error": "ID 只能包含字母、数字或下划线"}), 400

        # 2. 使用 per-user 路径（已登录写入 users/<uid>/configs/characters.json）
        cfg_file = _get_characters_config_file()
        uid = get_current_user_id()
        if uid:
            char_root = os.path.join(USERS_ROOT, str(uid), "characters")
        else:
            char_root = CHARACTERS_DIR

        # 3. 读取现有配置，检查 ID 是否重复
        all_config = {}
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)

        if new_id in all_config:
            return jsonify({"error": "该 ID 已存在"}), 400

        # 4. 创建文件夹结构
        target_char_dir = os.path.join(char_root, new_id)
        target_prompts_dir = os.path.join(target_char_dir, "prompts")

        if not os.path.exists(target_prompts_dir):
            os.makedirs(target_prompts_dir)

        # 5. 初始化数据库 (chat.db)
        # 直接调用我们要有的 init_char_db 函数
        init_char_db(new_id)

        # 6. 创建默认的空 Prompt 文件 (防止进入记忆页面报错)
        # 这些文件是必须存在的
        default_files = [
            "1_base_persona.md",
            "2_relationship.json",
            "3_user_persona.md", # 虽然有全局的，但局部文件最好也占个位
            "4_memory_long.json",
            "5_memory_medium.json",
            "6_memory_short.json",
            "7_schedule.json"
        ]

        for filename in default_files:
            file_path = os.path.join(target_prompts_dir, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                if filename.endswith(".json"):
                    f.write("{}") # JSON 写空对象
                else:
                    f.write("")   # MD 写空字符串

        # 6. 更新配置文件 (characters.json)
        all_config[new_id] = {
            "name": new_name,
            "remark": new_name, # 默认备注同名
            "avatar": "/static/default_avatar.png", # 默认头像
            "pinned": False,

            # --- 新增默认参数 ---
            "emotion": 1,
            "light_sleep": True,
            "deep_sleep": False,
            "ds_start": "23:00",
            "ds_end": "07:00"
        }

        # 6. 写入当前用户的 characters.json
        safe_save_json(cfg_file, all_config)

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Add Character Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】创建群聊接口 ---
@app.route("/api/groups/add", methods=["POST"])
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

        import re
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

# --- 【新增】复制他人日程接口 ---
@app.route("/api/<target_char_id>/copy_schedule", methods=["POST"])
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

# --- 【新增】将日程批量分发给其他角色 ---
@app.route("/api/distribute_schedule", methods=["POST"])
def distribute_schedule():
    data = request.json
    target_ids = data.get("target_ids", []) # 目标角色ID列表
    schedule_content = data.get("content", {}) # 日程内容 (JSON对象)

    if not target_ids or not schedule_content:
        return jsonify({"error": "没有选择目标或日程为空"}), 400

    success_list = []
    error_list = []

    for char_id in target_ids:
        try:
            # 获取该角色的路径
            _, prompts_dir = get_paths(char_id)
            target_file = os.path.join(prompts_dir, "7_schedule.json")

            # 确保目录存在
            if not os.path.exists(prompts_dir):
                os.makedirs(prompts_dir)

            # 覆盖写入 (使用 utf-8-sig 防止编码问题)
            with open(target_file, "w", encoding="utf-8-sig") as f:
                json.dump(schedule_content, f, ensure_ascii=False, indent=2)

            success_list.append(char_id)

        except Exception as e:
            error_list.append(f"{char_id}: {str(e)}")

    return jsonify({
        "status": "success",
        "updated": success_list,
        "errors": error_list
    })

# --- 【新增】AI 自动生成人设接口 ---
@app.route("/api/generate_persona", methods=["POST"])
def generate_persona():
    data = request.json
    char_name = data.get("char_name")
    source_ip = data.get("source_ip")

    lang = get_ai_language()

    # 日语 Prompt（不含姓名和年龄，由系统另行管理）
    prompt_ja = """
    あなたは熟練したキャラクター設定作家です。
    ユーザーから提供された「キャラクター名」と「作品名(IP)」に基づいて、以下の厳格なフォーマットに従ってキャラクター設定を作成してください。
    
    # 要件
    1. 言語：日本語
    2. 情報源：原作の公式設定やストーリーに基づき、正確かつ詳細に記述すること。
    3. 創作：もし情報が不足している部分は、キャラクターの性格に矛盾しない範囲で補完すること。
    4. フォーマット：以下の構造を厳守すること。
    5. 【重要】「名前」と「年齢」は絶対に含めないこと。これらはシステムで別に管理するため、出力から除外すること。
    
    # 出力フォーマット例（名前・年齢は含めない）
    # 役割
    (身長/誕生日 など)
    
    # 外見
    - 髪・瞳：(詳細な描写)
    - (その他の身体的特徴)
    
    # 経歴（年表）
    - (幼少期、学生時代、現在に至るまでの重要な出来事)
    
    # 生活状況
    - 拠点：(現在の住居や所属)
    - (寮や部屋割りなどの詳細があれば記述)
    - もしそのキャラクターがブルーロックの登場人物である場合：
    - 寮（ベッド順）：
        - ①潔世一(11)、千切豹馬(4)、御影玲王(14)、**國神錬介(50)**(現在のキャラクターをこのように示す)
        - ②烏旅人(6)、乙夜影汰(19)、雪宮剣優(5)、冰織羊(16)
        - ③黒名蘭世(96)、清羅刃(69)、雷市陣吾(22)、五十嵐栗夢(108)
        - ④糸師凛(9)、蜂楽廻(8)、七星虹郎(17)、（空）
        - ⑤我牙丸吟(1)、時光青志(20)、蟻生十兵衛(3)、（空）
        - ⑥オリーウェ・エゴ(2)、閃堂秋人(18)、士道龍聖(111)、（空）
        - ⑦馬狼照英(13)、凪誠士郎(7)、二子一揮(25)、剣城斬鉄(15)
    - 寮配置：①②③④/⑦⑥○⑤（①真正面は⑦）
    
    # 人間関係
    - (家族、友人、ライバル、敵対関係など)
    
    # 性格（キーワード）
    - 表面：(他人に見せる態度)
    - 内面：(隠された本音、デレ要素、執着など)
    - 特徴：
    - 弱点：
    
    # 好きなこと・詳細
    - 代表色：
    - 動物：
    - 好きな食べ物：
    - 苦手な食べ物：
    - 趣味：
    - 好きな季節/科目/座右の銘など：
    - 自認する長所/短所：
    - 嬉しいこと/悲しいこと：
    """

    # 中文 Prompt (结构一致，语言不同)
    prompt_zh = """
    你是一位资深的角色设定师。
    请根据用户提供的“角色名”和“作品名(IP)”，严格按照以下格式撰写角色设定。

    # 要求
    1. 语言：中文
    2. 信息源：基于原作官方设定，准确详细。
    3. 格式：严格遵守以下结构。
    4. 【重要】绝对不要包含「姓名」和「年龄」。这两项由系统单独管理，请从输出中完全排除。

    # 输出格式示例（不含姓名、年龄）
    # 角色
    (身高/生日 等)

    # 外貌
    - 发型瞳色：(详细描写)
    - (其他特征)

    # 经历 (年表)
    - (重要生平事件)

    # 生活状况
    - 据点：
    - (宿舍/房间等细节)
    - 如果是蓝色监狱的角色：
    - 寝室（床位顺序）：
        - ①洁世一(11)、千切豹马(4)、御影玲王(14)、**国神炼介(50)**(当前角色像这样标出)
        - ②乌旅人(6)、乙夜影汰(19)、雪宫剑优(5)、冰织羊(16)
        - ③黑名兰世(96)、清罗刃(69)、雷市阵吾(22)、五十岚栗梦(108)
        - ④糸师凛(9)、蜂乐廻(8)、七星虹郎(17)、（空）
        - ⑤我牙丸吟(1)、时光青志(20)、蚁生十兵卫(3)、（空）
        - ⑥奥利维·埃戈(2)、闪堂秋人(18)、士道龙圣(111)、（空）
        - ⑦马狼照英(13)、凪诚士郎(7)、二子一挥(25)、剑城斩铁(15)
    - 寝室配置：①②③④/⑦⑥○⑤（①正对面是⑦）

    # 人际关系
    - (家族、朋友、宿敌等)

    # 性格 (关键词)
    - 表面：
    - 内心：
    - 特征：
    - 弱点：

    # 喜好与细节
    - 代表色：
    - 喜欢的食物：
    - 讨厌的食物：
    - 兴趣：
    - 特长/弱项：
    - 座右铭：
    """

    system_prompt = prompt_zh if lang == "zh" else prompt_ja

    if not char_name or not source_ip:
        return jsonify({"error": "请输入角色名和作品名"}), 400

    # 构造请求
    user_content = f"キャラクター名: {char_name}\n作品名: {source_ip}"

    # 这里的 PERSONA_GENERATION_PROMPT 就是上面定义的那一大段字符串
    # 请务必把它定义在文件顶部或这个函数外面
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    try:
        print(f"--- [Gen Persona] Generating for {char_name} ({source_ip}) ---")

        # 定义一个特殊的记账 ID
        log_id = f"System:GenPersona({char_name})"

        # 1. 获取当前配置
        route, current_model = get_model_config("gen_persona") # 任务类型是 chat

        print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

        if route == "relay":
            generated_text = call_openrouter(messages, char_id=log_id, model_name=current_model)
        else:
            generated_text = call_gemini(messages, char_id=log_id, model_name=current_model)

        return jsonify({"status": "success", "content": generated_text})

    except Exception as e:
        print(f"Gen Persona Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】删除角色接口（支持 per-user）---
@app.route("/api/character/<char_id>/delete", methods=["DELETE"])
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


# --- 【新增】删除群聊接口（支持 per-user）---
@app.route("/api/group/<group_id>/delete", methods=["DELETE"])
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

# --- 【新增】指定日期重新生成短期记忆 ---
@app.route("/api/<char_id>/memory/regenerate_short", methods=["POST"])
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

# 1. 保存订阅接口
@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    subscription = request.json
    if not subscription: return jsonify({"error": "No data"}), 400

    subs = []
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            try: subs = json.load(f)
            except: pass

    # 避免重复添加
    if subscription not in subs:
        subs.append(subscription)
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(subs, f)

    return jsonify({"status": "success"})

# 2. 获取公钥接口 (前端需要用)
@app.route("/api/vapid_public_key")
def get_vapid_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})

# 3. 发送通知工具函数 (供 trigger_active_chat 调用)
def send_push_notification(title, body, url="/"):
    if not os.path.exists(SUBSCRIPTIONS_FILE): return

    with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
        subs = json.load(f)

    print(f"🔔 [Push] 正在向 {len(subs)} 个设备发送通知...")

    cleanup_needed = False
    valid_subs = []

    for sub_info in subs:
        try:
            webpush(
                subscription_info=sub_info,
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS
            )
            valid_subs.append(sub_info)
        except WebPushException as ex:
            # 如果返回 410 Gone，说明用户取消了订阅，需要清理
            if ex.response and ex.response.status_code == 410:
                print("   - 设备已取消订阅，移除")
                cleanup_needed = True
            else:
                print(f"   - 推送失败: {ex}")
                valid_subs.append(sub_info) # 暂时保留，可能是网络问题

    # 清理失效的订阅
    if cleanup_needed:
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(valid_subs, f)

# --- 【修正版】邮件发送功能 (符合 RFC 标准) ---
def _send_email_thread(subject, content, user_id=None):
    """实际发送邮件的线程函数。user_id 用于后台任务时指定读取哪个用户的邮箱配置。"""
    sender = os.getenv("MAIL_SENDER")
    password = os.getenv("MAIL_PASSWORD")

    # 后台任务在新线程中运行，contextvars 可能未继承，需显式设置用户以读取对应用户的 user_settings
    if user_id is not None:
        set_background_user(user_id)
    try:
        user_cfg = _load_user_settings()
        receiver = (user_cfg.get("email") or "").strip() or os.getenv("MAIL_RECEIVER")
    except Exception:
        receiver = os.getenv("MAIL_RECEIVER") or ""
    finally:
        if user_id is not None:
            clear_background_user()
    smtp_server = os.getenv("MAIL_SERVER", "smtp.qq.com")
    # 注意：QQ邮箱 SSL 端口通常是 465
    smtp_port = int(os.getenv("MAIL_PORT", 465))

    # 如果用户没有配置收件人邮箱，则静默跳过（不视为错误）
    if not receiver:
        print("[Email] 未配置收件人邮箱，跳过发送。")
        return

    # 发件人或密码缺失仍视为配置错误
    if not sender or not password:
        print("❌ [Email] 发件人或密码配置缺失，无法发送")
        return

    try:
        # 构造邮件对象
        message = MIMEText(content, 'plain', 'utf-8')

        # 【关键修改】使用 formataddr 生成标准发件人格式
        # 格式会自动处理为: "Kunigami AI" <xxxx@qq.com>
        message['From'] = formataddr(["Kunigami AI", sender])

        # 收件人同理 (也可以直接传字符串，但这样更稳)
        message['To'] = formataddr(["User", receiver])

        message['Subject'] = Header(subject, 'utf-8')

        # 连接服务器 (使用 SSL)
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        server.login(sender, password)
        server.sendmail(sender, [receiver], message.as_string())
        server.quit()

        print(f"📧 [Email] 邮件发送成功: {subject}")
    except Exception as e:
        print(f"❌ [Email] 发送失败: {e}")

def send_email_notification(title, body, user_id=None):
    """
    外部调用的异步接口。
    user_id: 后台任务（如主动消息）调用时传入，确保读取对应用户的邮箱配置；HTTP 请求时可不传，用 session。
    """
    thread = threading.Thread(target=_send_email_thread, args=(title, body, user_id))
    thread.start()

# --- 定时任务配置 ---
def scheduled_maintenance():
    """
    每天凌晨 04:00 运行一次
    顺序：群聊总结(分发) -> 个人总结(日记) -> 周结(若周一)
    """
    print("\n⏰ 正在执行每日后台维护...")

    # 【修改】在函数内部导入，避免循环引用
    import memory_jobs

    # 1. 【新增】先执行群聊日结 (把记忆分发给个人)
    memory_jobs.run_all_group_daily_rollovers()

    # 1. 执行全员日结
    # memory_jobs.process_daily_rollover()  <-- 旧的删掉
    memory_jobs.run_all_daily_rollovers()   # <-- 换成新的循环函数

    # 2. 如果今天是周一，执行全员周结
    if datetime.now().weekday() == 0:
        memory_jobs.run_all_weekly_rollovers() # <-- 换成新的循环函数

    print("✅ 后台维护结束\n")

# --- 【新增】调试工具：打印完整的 Prompt ---
def log_full_prompt(service_name, messages):
    print("\n" + "▼"*20 + f" 🟢 [DEBUG] 发送给 {service_name} 的完整内容 " + "▼"*20)

    for i, msg in enumerate(messages):
        role = msg.get('role', 'unknown').upper()
        content = msg.get('content', '')
        # 如果内容太长（比如几千字的记忆），也完整显示，方便您检查
        print(f"【{i}】<{role}>:")
        print(f"{content}")
        print("-" * 50)

    print("▲"*20 + " [DEBUG] END " + "▲"*20 + "\n")

# --- 【终极版】日志记录：内容 + Token 账单 ---
def log_full_prompt(service_name, messages, response_text=None, usage=None):
    # 获取当前时间
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构造日志内容
    log_content = []
    log_content.append(f"\n{'='*20} [{timestamp}] {service_name} {'='*20}")

    # 1. 打印上下文
    for i, msg in enumerate(messages):
        role = msg.get('role', 'unknown').upper()
        content = msg.get('content', '')
        log_content.append(f"【{i}】<{role}>:\n{content}\n{'-'*30}")

    # 2. 打印 AI 回复
    if response_text:
        log_content.append(f"\n【🤖 AI REPLY】:\n{response_text}")

    # 3. 【新增】打印 Token 消耗 (这就是您要的！)
    if usage:
        # Gemini 的格式通常是: {'promptTokenCount': 100, 'candidatesTokenCount': 20, 'totalTokenCount': 120}
        input_tokens = usage.get('promptTokenCount', 0)      # 提问消耗 (便宜)
        output_tokens = usage.get('candidatesTokenCount', 0) # 回复消耗 (贵)
        total_tokens = usage.get('totalTokenCount', 0)       # 总计

        log_content.append(f"\n【💰 TOKEN BILL】:")
        log_content.append(f"   📥 输入(Prompt): {input_tokens}")
        log_content.append(f"   📤 输出(Reply):  {output_tokens}")
        log_content.append(f"   💎 总计(Total):  {total_tokens}")

    log_content.append(f"{'='*50}\n")

    final_log = "\n".join(log_content)

    # 打印到黑框框
    print(final_log)

    # 保存到文件（强制创建 logs 目录以便保存调试日志）
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True) # 【修正】强制创建 logs 目录，否则主动消息日志不保存
        log_file = os.path.join(log_dir, "api.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(final_log)
    except Exception as e:
        print(f"FAILED TO WRITE API LOG: {e}")
        pass

def _get_usage_log_file() -> str:
    """多用户 Token 账单：每个 user 一份 usage_history.json。"""
    user_id = get_current_user_id()
    if user_id:
        base = os.path.join(USERS_ROOT, str(user_id), "logs")
        os.makedirs(base, exist_ok=True)
    else:
        # 未登录情况下不在根目录自动创建 logs 文件夹，仅在已存在时才使用
        base = os.path.join(BASE_DIR, "logs")
    return os.path.join(base, "usage_history.json")

# --- 【新增】Token 账单记录系统（多用户版） ---
def record_token_usage(char_id, model, input_tokens, output_tokens, total_tokens):
    """记录一次 API 调用的消耗"""
    try:
        usage_file = _get_usage_log_file()

        # 1. 读取现有日志
        logs = []
        if os.path.exists(usage_file):
            with open(usage_file, "r", encoding="utf-8") as f:
                try: logs = json.load(f)
                except: logs = []

        # 2. 追加新记录
        new_entry = {
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "char_id": char_id,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens
        }
        logs.append(new_entry)

        # 3. 只保留最近 50 条 (防止文件无限膨胀)
        if len(logs) > 50:
            logs = logs[-50:]

        # 4. 保存
        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"Log Usage Error: {e}")

# --- 【新增】获取账单接口（多用户版） ---
@app.route("/api/usage_logs")
def get_usage_logs():
    usage_file = _get_usage_log_file()
    if not os.path.exists(usage_file):
        return jsonify([])
    try:
        with open(usage_file, "r", encoding="utf-8") as f:
            # 倒序返回，最新的在前面
            logs = json.load(f)
            return jsonify(logs[::-1])
    except:
        return jsonify([])

# --- 【朋友圈】关系图谱候选（除用户外，用于点赞/评论抽样）---
def _get_moments_relationship_candidates(char_id):
    """从角色的 2_relationship.json 中取出除用户外的 (char_id, score) 列表。关系 key 为名字，需映射到 char_id。"""
    _, prompts_dir = get_paths(char_id)
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
    cfg_file = _get_characters_config_file()
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


def _generate_moment_comment(commenter_id, post_author_id, post_content):
    """让 commenter_id 角色对 post_content 生成一条简短评论。
    流程：总结短期记忆 -> 生成评论 -> 记录到短期记忆
    """
    # 生成前：同步该角色的短期记忆
    try:
        sync_memory_before_moments(commenter_id)
    except Exception as e:
        print(f"   ⚠️ [Moment Comment] 记忆同步失败 {commenter_id}: {e}，继续生成")
    
    recent_messages = [post_content]
    sys_prompt = build_system_prompt(commenter_id, include_global_format=False, recent_messages=recent_messages, target_char_id=post_author_id)

    # 从当前用户的 characters.json 中读取双方名字，便于在 Prompt 中明确说明评论对象与关系
    commenter_name = commenter_id
    author_name = post_author_id
    try:
        cfg_file = _get_characters_config_file()
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

    lang = get_ai_language()
    if lang == "zh":
        user_msg = (
            "【评论任务说明】\n"
            "你现在要为一条朋友圈写一条简短评论（仅一句话）。只输出评论内容，不要加引号，也不要加「评论：」之类的前缀。\n\n"
            "【评论对象与关系（请重点理解）】\n"
            f"- 被评论者：{author_name}（ID: {post_author_id}）\n"
            "你（当前说话的角色）与 TA 之间的具体关系（例如：队友、学长学弟、朋友、恋人、家人等）已经在系统角色设定与关系图谱中给出。\n"
            "写评论时要严格按照那种关系来称呼和说话，自然体现出这种亲疏远近和情感氛围。\n\n"
            "【朋友圈原文】\n"
            f"{post_content}"
        )
    else:
        user_msg = (
            "【コメントタスク】\n"
            "これから一件の「朋友圈（タイムライン投稿）」に対して、一言だけ短いコメントを書いてください。出力はコメント文のみで、引用符や「コメント：」などの接頭辞は付けないでください。\n\n"
            "【コメント対象と関係性】\n"
            f"- 投稿者：{author_name}（ID: {post_author_id}）\n"
            "あなた（現在発話しているキャラクター）と投稿者との具体的な関係（チームメイト、友人、恋人、家族など）は、システムプロンプトおよび関係図譜の中に定義されています。\n"
            "コメントを書くときは、その関係性に合った呼び方と言葉遣いを選び、その距離感や感情が自然に伝わるようにしてください。\n\n"
            "【朋友圈（投稿）内容】\n"
            f"{post_content}"
        )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg}
    ]
    try:
        route, current_model = get_model_config("moments")
        if route == "relay":
            text = call_openrouter(messages, char_id=commenter_id, model_name=current_model)
        else:
            text = call_gemini(messages, char_id=commenter_id, model_name=current_model)
        if text:
            text = text.strip().strip('"\'')
            if len(text) > 100:
                text = text[:100]
            
            # (底层不再自动写入记忆，由调用方统一处理)
            return text
    except Exception as e:
        print(f"   [Moments] 评论生成失败 {commenter_id}: {e}")
    return None


def _generate_moment_reply_to_user(author_char_id, post_content, user_comment):
    """让朋友圈作者（角色）对用户的评论生成一条简短回复。
    流程：总结短期记忆 -> 生成回复 -> 记录到短期记忆
    """
    # 生成前：同步该角色的短期记忆
    try:
        sync_memory_before_moments(author_char_id)
    except Exception as e:
        print(f"   ⚠️ [Moment Reply] 记忆同步失败 {author_char_id}: {e}，继续生成")
    
    recent_messages = [post_content, user_comment]
    sys_prompt = build_system_prompt(author_char_id, include_global_format=False, recent_messages=recent_messages)
    lang = get_ai_language()
    if lang == "zh":
        user_msg = (
            f"你在朋友圈发了这条内容：\n{post_content}\n\n"
            f"用户评论说：「{user_comment}」\n\n"
            f"请以你的身份回复一条简短评论（一句话）。只输出回复内容，不要引号或前缀。"
        )
    else:
        user_msg = (
            f"あなたの朋友圈投稿：\n{post_content}\n\n"
            f"ユーザーのコメント：「{user_comment}」\n\n"
            f"あなたの立場で短い返信を一言で書いてください。返信の内容だけを出力し、引用符や接頭辞は不要です。"
        )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg}
    ]
    try:
        route, current_model = get_model_config("moments")
        if route == "relay":
            text = call_openrouter(messages, char_id=author_char_id, model_name=current_model)
        else:
            text = call_gemini(messages, char_id=author_char_id, model_name=current_model)
        if text:
            text = text.strip().strip('"\'')
            if len(text) > 100:
                text = text[:100]
            
            # (底层不再自动写入记忆，由调用方统一处理)
            return text
    except Exception as e:
        print(f"   [Moments] 角色回复评论失败 {author_char_id}: {e}")
    return None

def _generate_ai_reply_to_any_comment(replying_char_id, post_author_id, post_content, comments_list, target_comment_index):
    """
    让 replying_char_id 对朋友圈的某条特定评论进行回复。
    """
    # 生成前：同步该角色的短期记忆
    try:
        sync_memory_before_moments(replying_char_id)
    except Exception as e:
        print(f"   ⚠️ [Moment Any Reply] 记忆同步失败 {replying_char_id}: {e}，继续生成")

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
    sys_prompt = build_system_prompt(replying_char_id, include_global_format=False, recent_messages=[post_content, target_comment_content], target_char_id=target_comment_author_id)

    lang = get_ai_language()
    if lang == "zh":
        user_msg = (
            "【评论互动任务】\n"
            "你正在浏览社交软件的朋友圈，现在你需要对其中的一条评论进行「回复」。只输出回复内容，不要加引号或「回复：」等前缀。\n\n"
            f"【朋友圈原文】\n"
            f"发布者：{post_author_name}\n"
            f"内容：{post_content}\n\n"
            f"【当前评论区的所有评论】\n"
            f"{comments_context}\n"
            f"【你要回复的目标评论（⚠️重点）】\n"
            f"评论者：{target_author_name}（你与他/她的关系已包含在人设中）\n"
            f"TA的评论内容：「{target_comment_content}」\n\n"
            "请结合整体语境，特别是针对你要回复的这条评论，以你的身份进行真实简短的回复（一两句话即可）。"
        )
    else:
        user_msg = (
            "【コメント返信タスク】\n"
            "あなたはSNSのタイムラインを見ています。以下の特定のコメントに対して「返信」を書いてください。出力は返信コメントのみとし、引用符や接頭辞は不要です。\n\n"
            f"【元の投稿】\n"
            f"投稿者：{post_author_name}\n"
            f"内容：{post_content}\n\n"
            f"【現在の全コメント】\n"
            f"{comments_context}\n"
            f"【あなたが返信する対象のコメント（⚠️重要）】\n"
            f"コメント者：{target_author_name}（あなたと相手との関係性はシステムプロンプトに記載されています）\n"
            f"コメント内容：「{target_comment_content}」\n\n"
            "全体の文脈を踏まえつつ、特にこの対象コメントに対して、あなたのキャラクターらしい自然で短い返信（1〜2文程度）を書いてください。"
        )

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg}
    ]
    
    try:
        route, current_model = get_model_config("moments")
        if route == "relay":
            text = call_openrouter(messages, char_id=replying_char_id, model_name=current_model)
        else:
            text = call_gemini(messages, char_id=replying_char_id, model_name=current_model)
            
        if text:
            text = text.strip().strip('"\'')
            if len(text) > 100:
                text = text[:100]
            return text
    except Exception as e:
        print(f"   [Moments] 任意回复评论生成失败 {replying_char_id}: {e}")
    return None

# --- 【朋友圈】角色主动发朋友圈（含点赞、评论）---
def trigger_active_moments(char_id):
    """生成一条该角色的朋友圈内容，并按关系图谱生成点赞与评论（排除用户）。"""
    print(f"📷 [Moments] 尝试触发 {char_id} 的主动朋友圈...")

    # 发朋友圈前先同步该角色的单聊与所有群聊短期记忆，便于 AI 结合最近经历
    try:
        ok, err = sync_memory_before_moments(char_id)
        if not ok:
            print(f"   ⚠️ [Moments] 记忆同步失败: {err}，继续生成")
    except Exception as e:
        print(f"   ⚠️ [Moments] 记忆同步异常: {e}，继续生成")

    base_system_prompt = build_system_prompt(char_id, include_global_format=False, recent_messages=None, include_long_memory=False)
    now = datetime.now()
    post_ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
    lang = get_ai_language()

    if lang == "zh":
        trigger_msg = (
            "请结合你最近的经历（如短期记忆里的事）发一条朋友圈，内容简短自然。可以包含：\n"
            "- 纯文字；或\n"
            "- 照片：用 [写真（说明）] 表示，可多条（0-9枚），如 [写真（训练后的夕阳）][写真（更衣室）]；\n"
            "- 视频：用 [动画] 表示，可带说明如 [动画] 或 [动画（比赛集锦）]。\n"
            "只输出这一条朋友圈的内容，不要加引号、不要加「朋友圈：」等前缀。"
        )
    else:
        trigger_msg = (
            "最近の出来事（短期記憶など）を踏まえて、朋友圈を1本投稿してください。短く自然な内容にし、次の形式を使えます：\n"
            "- テキストのみ；または\n"
            "- 写真：[写真（説明）]…（0-9枚）、例 [写真（練習後の夕焼け）][写真（ロッカー室）]；\n"
            "- 動画：[動画] または [動画（説明）]。\n"
            "引用符や「朋友圈：」などの接頭辞は付けず、本文だけを出力してください。"
        )

    messages = [
        {"role": "system", "content": base_system_prompt},
        {"role": "user", "content": trigger_msg}
    ]

    try:
        route, current_model = get_model_config("moments")
        if route == "relay":
            content = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            content = call_gemini(messages, char_id=char_id, model_name=current_model)
        if not content:
            return False
        content = content.strip().strip('"\'')
        if not content:
            return False
    except Exception as e:
        print(f"📷 [Moments] 生成内容失败: {e}")
        return False

    candidates = _get_moments_relationship_candidates(char_id)
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

    likers_data = []
    comments_data = []

    # 解析 @ 提及
    # 使用 _get_moments_id_display 获取角色备注与 ID 的映射
    _, remarks = _get_moments_id_display()
    remark_to_id = { name: cid for cid, name in remarks.items() }
    import re
    mentioned_ids = []
    at_matches = re.findall(r"@([^\s@]+)", content)
    for name in at_matches:
        if name in remark_to_id:
            mentioned_ids.append(remark_to_id[name])
        elif name in remarks: # ID 直接匹配
            mentioned_ids.append(name)

    if candidates:
        # 处理被 @ 的角色（强制点赞和评论）
        for mid in mentioned_ids:
            if mid == "user": continue # 此时是 AI 发的朋友圈，不去 @ 用户（用户无法自动回评论）
            # 被 @ 的角色强制点赞和评论，时间与朋友圈一致
            likers_data.append({"liker_id": mid, "timestamp": post_ts_str})
            comment_text = _generate_moment_comment(mid, char_id, content)
            if comment_text:
                comments_data.append({
                    "commenter_id": mid,
                    "content": comment_text,
                    "timestamp": post_ts_str
                })
        
        # 处理其他随机分配的角色
        remaining_candidates = [c for c in candidates if c not in mentioned_ids]
        
        n_like = random.randint(0, min(5, len(remaining_candidates)))
        like_cids = _weighted_sample_no_replacement(remaining_candidates, n_like)
        for cid in like_cids:
            likers_data.append({"liker_id": cid, "timestamp": random_ts_in_24h()})

        n_comment = random.randint(0, min(3, len(remaining_candidates)))
        comment_cids = _weighted_sample_no_replacement(remaining_candidates, n_comment)
        for cid in comment_cids:
            comment_text = _generate_moment_comment(cid, char_id, content)
            if comment_text:
                comments_data.append({
                    "commenter_id": cid,
                    "content": comment_text,
                    "timestamp": random_ts_in_24h()
                })

    new_post = {
        "char_id": char_id,
        "content": content,
        "timestamp": post_ts_str,
        "likers": likers_data,
        "comments": comments_data
    }

    # 追加到当前用户的 moments_data.json（per-user）
    moments_path, last_post_path = get_moments_paths()
    raw = []
    if os.path.exists(moments_path):
        try:
            with open(moments_path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except Exception:
            raw = []
    raw.append(new_post)
    safe_save_json(moments_path, raw)
    ctx = f"你发了一条朋友圈，内容：「{(content or '')[:300]}」。"
    append_moment_event_to_short_memory(char_id, ctx)

    # 更新上次发朋友圈时间（per-user）
    last_post = {}
    if os.path.exists(last_post_path):
        try:
            with open(last_post_path, "r", encoding="utf-8-sig") as f:
                last_post = json.load(f)
        except Exception:
            pass
    last_post[char_id] = post_ts_str
    safe_save_json(last_post_path, last_post)

    print(f"📷 [Moments] 发送成功: {content[:50]}...")
    return True


# --- 【修正版】单人主动消息 (伪装成 User 消息触发) ---
def trigger_active_chat(char_id, user_id=None):
    print(f"💓 [Active] 尝试触发 {char_id} 的主动消息...")
    print(f"   后台用户ID: {user_id}, 当前用户ID: {get_current_user_id()}")

    db_path, _ = get_paths(char_id)
    if not os.path.exists(db_path): return False

    # 0. 单聊前同步群聊记忆
    try:
        ok, err = sync_memory_before_single_chat(char_id)
        if not ok:
            print(f"   ⚠️ [Active] 记忆同步失败: {err}，继续生成")
    except Exception as e:
        print(f"   ⚠️ [Active] 记忆同步异常: {e}，继续生成")

    # 1. 先读取历史记录，再构建 System Prompt（便于长期记忆 RAI）
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
    history_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    recent_texts = [r["content"] for r in history_rows] if history_rows else []
    # 【修正】提取用户最后一条消息，用于精准筛选长期记忆
    user_last = history_rows[-1]["content"] if history_rows and history_rows[-1]["role"] == "user" else None
    
    # 【全局采用 v2】直接使用v2系统提示
    base_system_prompt = build_system_prompt_v2(char_id, recent_messages=recent_texts, user_latest_input=user_last)
    messages = [{"role": "system", "content": base_system_prompt}]

    # 2. 填充历史 (带智能时间戳)
    now = datetime.now()
    show_full_date = False
    if history_rows:
        try:
            first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
            if first_ts.date() != now.date(): show_full_date = True
        except: pass

    for row in history_rows:
        try:
            dt_object = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
            ts_str = dt_object.strftime('[%m-%d %H:%M]') if show_full_date else dt_object.strftime('[%H:%M]')
            formatted_content = f"{ts_str} {row['content']}"
            messages.append({"role": row['role'], "content": formatted_content})
        except:
            messages.append({"role": row['role'], "content": row['content']})

    # --- 4. 【关键修改】构造“伪造的”用户指令消息 ---
    # 这条消息只发给 AI 看，不会存入数据库

    lang = get_ai_language()
    hour = now.hour
    time_str = now.strftime('%H:%M')

    # 计算时间段
    if 5 <= hour < 11: period = "早上" if lang == "zh" else "朝"
    elif 11 <= hour < 13: period = "中午" if lang == "zh" else "昼"
    elif 13 <= hour < 18: period = "下午" if lang == "zh" else "午後"
    elif 18 <= hour < 23: period = "晚上" if lang == "zh" else "夜"
    else: period = "深夜" if lang == "zh" else "深夜"

    if lang == "zh":
        trigger_msg = (
            f"（系统提示：现在是{period} {time_str}。）\n"
            f"（用户已经很久没说话了。请你根据当前时间、之前的聊天内容，**主动**向用户发起一个新的话题。）\n"
            f"（要求：自然、简短，不要重复上一句话。）\n"
            f"（无特殊说明时用斜线表示换行和句号。）"
        )
    else:
        trigger_msg = (
            f"（システム通知：現在は{period} {time_str}です。）\n"
            f"（ユーザーからの返信が途絶えています。現在の時間帯やこれまでの会話を踏まえて、**自発的に**新しい話題を振ってください。）\n"
            f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）\n"
            f"（特に指定がない場合、改行と句点はスラッシュで表します。）"
        )

    # 把它伪装成 User 发的消息
    messages.append({"role": "user", "content": trigger_msg})

    # 5. 调用 AI
    try:
        route, current_model = get_model_config("chat")
        print(f"   -> [Active] Calling AI ({route}/{current_model})...")

        if route == "relay":
            reply_text = call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
        else:
            reply_text = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 【修正】检查 API 是否返回错误
        if isinstance(reply_text, str) and (reply_text.startswith("[ERROR]") or reply_text.startswith("[Gemini Error")):
            print(f"💓 [Active] API 调用失败: {reply_text}")
            return False

        # 清理
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()

        if not cleaned_reply: return False

        # 写时随机：将 [表情]名称 替换为 [表情]path 再入库，避免历史变脸
        cleaned_reply = _sticker_content_from_ai(cleaned_reply)

        # 6. 存库
        ai_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("assistant", cleaned_reply, ai_ts))
        conn.commit()
        conn.close()

        print(f"💓 [Active] 发送成功: {cleaned_reply}")

        # --- 【新增】发送手机通知 ---
        # 这里的 title 可以是角色名
        # body 是回复内容（截取前50字）
        char_name_display = char_id # 或者去读配置获取 name
        try:
            # 获取名字逻辑略... 假设您已有 id_to_name
            pass
        except: pass

        send_push_notification(
            title=f"{char_id} 发来一条消息",
            body=cleaned_reply[:50],
            url=f"/chat/{char_id}" # 点击跳转到单聊
        )

        # ✅ 邮件通知：传入 user_id 以读取对应用户的邮箱（后台任务在新线程中 context 可能丢失）
        email_title = f"【Kunigami】{char_id} 发来了一条消息"
        email_body = f"请前去查收"
        send_email_notification(email_title, email_body, user_id=get_current_user_id())
        # --------------------------

        return True

    except Exception as e:
        import traceback
        error_msg = f"💓 [Active] 发送失败: {e}\n{traceback.format_exc()}"
        print(error_msg)
        return False

# --- 【修正版】群聊主动消息 (伪装成 User 指令) ---
def trigger_group_active_chat(group_id, user_id=None):
    print(f"💓 [GroupActive] 尝试触发群 {group_id} 的主动消息...")

    # 【强制保护】
    if user_id is not None:
        set_background_user(user_id)

    # 0. 群聊前同步各成员单聊 + 本群群聊记忆
    try:
        ok, err = sync_memory_before_group_chat(group_id)
        if not ok:
            print(f"   ⚠️ [GroupActive] 记忆同步失败: {err}，继续生成")
    except Exception as e:
        print(f"   ⚠️ [GroupActive] 记忆同步异常: {e}，继续生成")

    group_dir = get_group_dir(group_id)
    db_path = os.path.join(group_dir, "chat.db")

    # 1. 基础读取逻辑 (保持不变)
    if not os.path.exists(GROUPS_CONFIG_FILE): return False
    with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
        group_conf = json.load(f).get(group_id, {})

    group_name = group_conf.get("name", "Group")
    all_members = group_conf.get("members", [])
    ai_members_all = [m for m in all_members if m != "user"]
    if not ai_members_all: return False

    # 2. 筛选在线成员 (保持不变)
    online_members = []
    id_to_name = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for cid, cinfo in c_conf.items():
                id_to_name[cid] = cinfo.get("name", cid)
                if cid in ai_members_all:
                    if not cinfo.get("deep_sleep", False):
                        online_members.append(cid)

    if not online_members: return False

    # --- 3. 决定对话轮数 (随机 2~4 句，营造热闹感) ---
    # 如果只有1个人在线，就只能发1句
    max_rounds = len(online_members)
    if len(online_members) == 1:
        num_rounds = 1
    else:
        num_rounds = random.randint(2, max_rounds)

    print(f"   -> 计划生成 {num_rounds} 条消息连击")

    # 内存中的临时上下文缓存 (用于让后面的人看到前面的人说了啥)
    context_buffer = []

    # 记录是否发送了通知 (只发第一条的通知，防止手机炸了)
    notification_sent = False

    # --- 4. 开始循环生成 ---
    for i in range(num_rounds):
        # 随机选人 (尽量不选上一个人，除非只有一个人)
        candidates = [m for m in online_members]
        if i > 0 and len(candidates) > 1:
            last_speaker = context_buffer[-1]['role_id']
            if last_speaker in candidates:
                candidates.remove(last_speaker)

        speaker_id = random.choice(candidates)
        speaker_name = id_to_name.get(speaker_id, speaker_id)

        print(f"   -> Round {i+1}: [{speaker_name}] 准备发言")

        # --- A. 先读取群聊历史，再构建 Prompt（便于长期记忆 RAI）---
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 15")
        history_rows = [dict(row) for row in cursor.fetchall()][::-1]
        conn.close()

        recent_texts = [r["content"] for r in history_rows] if history_rows else []
        user_latest = next((r["content"] for r in reversed(history_rows) if r["role"] == "user"), None)
        sys_prompt = build_system_prompt(speaker_id, recent_messages=recent_texts, user_latest_input=user_latest)
        other_members = [m for m in all_members if m != speaker_id and m != "user"]
        rel_prompt = build_group_relationship_prompt(speaker_id, other_members)

        now = datetime.now()
        time_str = now.strftime('%H:%M')
        lang = get_ai_language()

        # 【关键】区分“发起者”和“跟风者”的指令
        if i == 0:
            # 第一条：发起话题
            if lang == "zh":
                instruction = (
                    f"\n\n【System Event / 系统事件】\n"
                    f"现在是 {time_str}。群里很久没人说话了。\n"
                    f"请根据当前时间、群聊氛围及人际关系，**主动发起**一个新话题。\n"
                    f"要求：自然、简短。"
                )
            else:
                instruction = (
                    f"\n\n【System Event】\n"
                    f"現在は {time_str} です。チャットが静かです。\n"
                    f"**自発的に**新しい話題を振ってください。自然で簡潔に。"
                )
        else:
            # 后续：自然接话
            if lang == "zh":
                instruction = (
                    f"\n\n【System Event / 系统事件】\n"
                    f"现在是 {time_str}。这是群聊的后续对话。\n"
                    f"请根据上文其他成员的发言，自然地接话、吐槽或附和。\n"
                    f"要求：简短，符合人设。"
                )
            else:
                instruction = (
                    f"\n\n【System Event】\n"
                    f"現在は {time_str} です。\n"
                    f"他のメンバーの発言を受けて、自然に会話を続けてください。"
                )

        full_sys_prompt = sys_prompt + "\n\n" + rel_prompt + instruction
        messages = [{"role": "system", "content": full_sys_prompt}]

        # --- B. 处理历史 (带智能时间戳，history_rows 已在上方读取) ---
        # 智能时间戳逻辑
        show_full_date = False
        if history_rows:
            try:
                first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if first_ts.date() != now.date(): show_full_date = True
            except: pass

        for row in history_rows:
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                ts_str = dt_obj.strftime('[%m-%d %H:%M]') if show_full_date else dt_obj.strftime('[%H:%M]')
            except: ts_str = ""

            r_id = row['role']
            d_name = "User" if r_id == "user" else id_to_name.get(r_id, r_id)
            messages.append({"role": "user", "content": f"{ts_str} [{d_name}]: {row['content']}"})

        # --- D. 调用 AI ---
        try:
            # 【关键修复】将 user_id 显式传给配置获取函数
            route, current_model = get_model_config("chat", user_id=user_id)
            print(f"   -> [Active] Calling AI ({route}/{current_model})...")

            if route == "relay":
                # 【关键修复】将 user_id 显式传给 call_openrouter 以便其判断中转商线路
                reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model, user_id=user_id)
            else:
                reply_text = call_gemini(messages, char_id=speaker_id, model_name=current_model)

            timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
            cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()
            name_pattern = f"^\\[{speaker_name}\\][:：]\\s*"
            cleaned_reply = re.sub(name_pattern, '', cleaned_reply).strip()

            if not cleaned_reply: continue

            # 写时随机：将 [表情]名称 替换为 [表情]path 再入库
            cleaned_reply = _sticker_content_from_ai(cleaned_reply)

            # --- E. 存档 ---
            ai_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           (speaker_id, cleaned_reply, ai_ts))
            conn.commit()
            conn.close()

            # 更新 Buffer (供下一轮看)
            context_buffer.append({
                "role_id": speaker_id,
                "display_name": speaker_name,
                "content": cleaned_reply
            })

            print(f"   -> 生成成功: {cleaned_reply}")

            # --- F. 发送通知 (仅第一条) ---
            if not notification_sent:
                send_push_notification(
                    title=f"群聊 {group_name} 有新消息",
                    body=f"{speaker_name}: {cleaned_reply}",
                    url=f"/chat/group/{group_id}"
                )

                # ✅ 邮件通知：传入 user_id 以读取对应用户的邮箱
                email_title = f"【群聊】{group_name} 有新动态"
                email_body = f"请前去查收"
                send_email_notification(email_title, email_body, user_id=user_id)

                notification_sent = True

            # 稍微停顿一下，防止并发请求过快
            time.sleep(2)

        except Exception as e:
            print(f"Active Chat Error: {e}")
            # 如果出错就不继续后面几轮了，直接结束
            break

    return True

# ========================================================
# 识图与上传接口 (Vision & Upload)
# ========================================================
def _compress_chat_image_to_jpg(src_path: str, dst_path: str, max_edge: int = 1024, max_bytes: int = 500 * 1024):
    """
    压缩聊天图片：
    1) 长边 <= max_edge
    2) 文件体积 <= max_bytes（优先调 JPEG 质量，不够再继续降分辨率）
    """
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            # 含透明通道时，先铺白底再转 RGB
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if "A" in img.getbands():
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        elif img.mode == "L":
            img = img.convert("RGB")

        # 首先限制长边
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        work_img = img
        # 最多缩放 8 轮，避免极端死循环
        for _ in range(8):
            for q in (88, 82, 76, 70, 64, 58, 52, 46, 40):
                buf = io.BytesIO()
                work_img.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
                size = buf.tell()
                if size <= max_bytes:
                    with open(dst_path, "wb") as f:
                        f.write(buf.getvalue())
                    return
            # 质量已经很低仍超限，进一步缩小分辨率再试
            w, h = work_img.size
            if max(w, h) <= 480:
                # 已经很小了，直接落盘最低质量版本
                with open(dst_path, "wb") as f:
                    f.write(buf.getvalue())
                return
            work_img = work_img.resize((int(w * 0.85), int(h * 0.85)), Image.Resampling.LANCZOS)

        # 理论兜底
        with open(dst_path, "wb") as f:
            f.write(buf.getvalue())


@app.route("/api/vision/upload", methods=["POST"])
def vision_upload():
    """接收用户发送的图片，保存并调用配置的识图模型获取描述，返回给前端"""
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    # 聊天图片专用目录：users/<user_id>/chat_images/
    img_dir = os.path.join(USERS_ROOT, str(user_id), "chat_images")
    os.makedirs(img_dir, exist_ok=True)

    ext = ".jpg"
    base_name = uuid.uuid4().hex
    filename = base_name + ext
    filepath = os.path.join(img_dir, filename)
    # 名称重复时自动重命名
    n = 0
    while os.path.exists(filepath):
        n += 1
        filename = f"{base_name}_{n}{ext}"
        filepath = os.path.join(img_dir, filename)
    # 先保存原图临时文件，再压缩为 jpg
    tmp_raw_path = os.path.join(img_dir, f"{base_name}_raw_upload")
    file.save(tmp_raw_path)
    try:
        _compress_chat_image_to_jpg(tmp_raw_path, filepath, max_edge=1024, max_bytes=500 * 1024)
    finally:
        try:
            if os.path.exists(tmp_raw_path):
                os.remove(tmp_raw_path)
        except Exception:
            pass

    # 同步保存一份到 static/uploads，供模型通过公网 URL 拉取（不再传 base64）
    static_upload_dir = os.path.join(BASE_DIR, "static", "uploads")
    os.makedirs(static_upload_dir, exist_ok=True)
    public_filename = filename
    public_file_path = os.path.join(static_upload_dir, public_filename)
    m = 0
    while os.path.exists(public_file_path):
        m += 1
        public_filename = f"{base_name}_{m}.jpg"
        public_file_path = os.path.join(static_upload_dir, public_filename)
    shutil.copy2(filepath, public_file_path)

    def _public_base_url() -> str:
        # 可通过环境变量显式指定公网域名（允许填完整页面 URL，如 https://xxx/profile）
        configured = (os.getenv("PUBLIC_BASE_URL", "") or os.getenv("SITE_URL", "")).strip()
        if configured:
            parsed = urlparse(configured)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
            return configured.rstrip("/")

        # 反向代理场景优先使用转发头
        forwarded_proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
        forwarded_host = (request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "").split(",")[0].strip()
        if forwarded_host:
            return f"{forwarded_proto}://{forwarded_host}"
        return request.host_url.rstrip("/")

    public_image_url = f"{_public_base_url()}/static/uploads/{public_filename}"

    # 调用识图模型
    route, current_model = get_model_config("vision")
    print(f"--- [Vision] Route: {route}, Model: {current_model} ---")
    
    prompt = "请用中文简要描述这张图片的内容，直接描述你看到了什么，不用过多主观判断。"
    description = ""
    try:
        if route == "relay":
            # OpenRouter 格式 (OpenAI compatible vision)：直接传公网 URL
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": public_image_url}}
                ]
            }]
            description = call_openrouter(messages, char_id=None, model_name=current_model)
        else:
            # Gemini：通过 generateContent 传 file_data.file_uri（公网 URL），不传 base64
            import requests
            base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
            url = f"{base_url}/v1beta/models/{current_model}:generateContent?key={get_effective_gemini_key()}"
            payload = {
                "contents": [{
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"file_data": {"mime_type": "image/jpeg", "file_uri": public_image_url}}
                    ]
                }],
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024},
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                ]
            }
            r = requests.post(url, json=payload, timeout=100)
            if r.status_code != 200:
                raise RuntimeError(f"[Gemini Vision Error {r.status_code}] {r.text}")
            result = r.json()
            parts = (((result.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            description = ""
            for p in parts:
                t = p.get("text")
                if t:
                    description += t
            description = description.strip()

    except Exception as e:
        print(f"   [Vision] Error: {e}")
        description = "图片解析失败"

    # 地址仅用文件名，前端/DB 存为 [图片](filename)(描述)
    url = f"/api/user/image/{filename}"
    return jsonify({
        "status": "success",
        "url": url,
        "path": filename,
        "description": (description or "").strip()
    })

@app.route("/api/user/image/<filename>", methods=["GET"])
def get_user_chat_image(filename):
    """访问用户上传在聊天中的图片"""
    if ".." in filename or "/" in filename or "\\" in filename:
        return "Forbidden", 403
    user_id = get_current_user_id()
    if not user_id:
        return "Unauthorized", 401
    img_dir = os.path.join(USERS_ROOT, str(user_id), "chat_images")
    return send_from_directory(img_dir, filename)

@app.route("/api/translate", methods=["POST"])
def translate_text():
    data = request.json or {}
    text = data.get("text", "")
    context = data.get("context", "")
    direction = data.get("direction", "ja_to_zh")
    message_id = data.get("message_id")
    if message_id is not None:
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            message_id = None
    char_id = data.get("char_id")
    group_id = data.get("group_id")
    scene_hint = ""
    user_name = _load_user_settings().get("current_user_name", "用户")

    def _load_chars_cfg_local() -> dict:
        cfg = _get_characters_config_file()
        if not os.path.exists(cfg):
            return {}
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    chars_cfg_local = _load_chars_cfg_local()

    def _char_name_only(cid: str) -> str:
        if not cid:
            return "对方"
        info = chars_cfg_local.get(cid, {}) if isinstance(chars_cfg_local, dict) else {}
        # 按用户要求：使用姓名（name），不使用 remark
        return info.get("name") or cid

    def _relation_with_user_from_graph(cid: str, uname: str) -> str:
        if not cid:
            return ""
        try:
            _, prompts_dir = get_paths(cid)
            rel_file = os.path.join(prompts_dir, "2_relationship.json")
            if not os.path.exists(rel_file):
                return ""
            with open(rel_file, "r", encoding="utf-8-sig") as f:
                rel_data = json.load(f)
            if not isinstance(rel_data, dict):
                return ""
            user_rel = rel_data.get(uname, {})
            if isinstance(user_rel, dict):
                return str(user_rel.get("role", "") or "").strip()
            return ""
        except Exception:
            return ""

    # 若提供 message_id + char_id 或 group_id，则从数据库读取当前条与上文 20 条作为上下文
    if message_id is not None and (char_id or group_id):
        try:
            if group_id:
                group_dir = get_group_dir(group_id)
                db_path = os.path.join(group_dir, "chat.db")
            else:
                db_path, _ = get_paths(char_id)
            if not os.path.exists(db_path):
                return jsonify({"error": "数据库不存在"}), 400
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, role, content FROM messages WHERE id = ?", (message_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "消息不存在"}), 404
            text = (row["content"] or "").strip()
            # 上文 5 条（不包含当前条）：按 id 升序
            cursor.execute(
                "SELECT id, role, content FROM messages WHERE id < ? ORDER BY id DESC LIMIT 5",
                (message_id,),
            )
            prev_rows = cursor.fetchall()
            conn.close()
            prev_rows = list(reversed(prev_rows))
            context_parts = []
            for r in prev_rows:
                if r["role"] == "user":
                    role_label = f"用户:{user_name}"
                else:
                    assistant_name = _char_name_only(r["role"])
                    role_label = f"助手:{assistant_name}"
                context_parts.append(f"[{role_label}] {r['content']}")
            context = "\n".join(context_parts)

            # 组装简单背景：双方名字 + 关系
            counterpart_name = "对方"
            relationship = "聊天对象"
            try:
                if group_id:
                    speaker_role = row["role"]
                    if speaker_role != "user":
                        counterpart_name = _char_name_only(speaker_role)
                        relationship = _relation_with_user_from_graph(speaker_role, user_name) or "未知"
                    else:
                        counterpart_name = user_name
                        relationship = "用户本人"
                else:
                    counterpart_name = _char_name_only(char_id)
                    relationship = _relation_with_user_from_graph(char_id, user_name) or "未知"
            except Exception:
                pass

            scene_hint = f"【背景】双方姓名：{user_name} 与 {counterpart_name}。双方关系：{relationship}。"
        except Exception as e:
            print(f"Translation DB read Error: {e}")
            return jsonify({"error": str(e)}), 500

    # 无 message_id 场景也补充简单背景（例如输入框中译日）
    if not scene_hint and (char_id or group_id):
        try:
            counterpart_name = "对方"
            relationship = "聊天对象"
            if group_id:
                groups_cfg = {}
                groups_cfg_file = _get_groups_config_file()
                if os.path.exists(groups_cfg_file):
                    with open(groups_cfg_file, "r", encoding="utf-8") as f:
                        groups_cfg = json.load(f)
                g_info = groups_cfg.get(group_id, {}) if isinstance(groups_cfg, dict) else {}
                group_name = g_info.get("name", group_id)
                counterpart_name = group_name
                relationship = "群聊场景"
            else:
                counterpart_name = _char_name_only(char_id)
                relationship = _relation_with_user_from_graph(char_id, user_name) or "未知"
            scene_hint = f"【背景】双方姓名：{user_name} 与 {counterpart_name}。双方关系：{relationship}。"
        except Exception:
            pass

    if not text:
        return jsonify({"error": "No text provided"}), 400

    bg_prefix = (scene_hint + "\n") if scene_hint else ""
    if direction == "zh_to_ja":
        prompt = f"{bg_prefix}请将以下中文翻译成日语。仅输出翻译后的日语，不要带有任何解释或多余符号。\n\n[上下文参考]\n{context}\n\n[需要翻译的原句]\n{text}"
    else:
        prompt = f"{bg_prefix}请将以下日语翻译成中文。仅输出翻译后的中文，不要带有任何解释或多余符号。\n\n[上下文参考]\n{context}\n\n[需要翻译的原句]\n{text}"

    messages = [{"role": "user", "content": prompt}]

    try:
        route, current_model = get_model_config("translation")
        if route == "relay":
            result = call_openrouter(messages, char_id="system", model_name=current_model)
        else:
            result = call_gemini(messages, char_id="system", model_name=current_model)

        return jsonify({"result": result.strip()})
    except Exception as e:
        print(f"Translation Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/furigana", methods=["POST"])
def furigana_text():
    data = request.json or {}
    text = str(data.get("text", "") or "")
    if not text:
        return jsonify({"result": ""})
    try:
        return jsonify({"result": _add_furigana_to_japanese(text)})
    except Exception as e:
        print(f"Furigana Error: {e}")
        return jsonify({"result": text})

# ---------------------- 启动 ----------------------

if __name__ == "__main__":
    # 【关键修改】加上 use_reloader=False
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
