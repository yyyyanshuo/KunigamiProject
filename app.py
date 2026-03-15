import os
import time
import re
import json
import sqlite3 # 导入 sqlite3 库
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, render_template, session, redirect, url_for # <--- 加上这个
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
from urllib.parse import quote as url_quote
from werkzeug.security import generate_password_hash, check_password_hash
import uuid

# 这是在 app.py 文件的开头部分

load_dotenv()  # 从 .env 读取环境变量

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
# 新增下面这行，来读取我们配置的 API 地址
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

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
    if not os.path.exists(CONFIG_FILE):
        return char_id
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get(char_id, {}).get("name", char_id)
    except:
        return char_id

def get_char_age(char_id):
    """从 characters.json 获取角色年龄，默认 None 表示未设置"""
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
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


def build_system_prompt(char_id, include_global_format=True, recent_messages=None, user_latest_input=None, include_long_memory=True):
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

    # --- 1. 静态 Markdown 文件 (人设、用户、格式) ---
    # 姓名、年龄来自 characters.json，人设文件中不包含
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

    static_files = [
        ("1_base_persona.md", "【Role / キャラクター設定】"),
        ("3_user_persona.md", "【User / ユーザー情報】"),
        ("8_format.md", "【System Rules / 出力ルール】")
    ]
    for filename, title in static_files:
        try:
            path = os.path.join(prompts_dir, filename) # <--- 使用动态目录
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()
                    if content:
                        # 仅对人设文件注入姓名和年龄前缀
                        if filename == "1_base_persona.md" and name_age_prefix:
                            content = name_age_prefix + content
                        prompt_parts.append(f"{title}\n{content}")
        except Exception: pass

    # --- 2. 关系设定 (JSON) ---
    try:
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                rel_data = json.load(f)
                user_rel = rel_data.get(current_user_name)
                if user_rel:
                    # 【修改】拼装文本改成日语
                    rel_str = (f"対話相手：{current_user_name}\n"
                           f"関係性：{user_rel.get('role', '不明')}\n"
                           f"関係度：{user_rel.get('score', 1)}\n"
                           f"詳細：{user_rel.get('description', '')}")
                prompt_parts.append(f"【Relationship / 関係設定】\n{rel_str}")
    except Exception: pass

    # --- 4. 长期记忆 (JSON - 按周/月，RAI 筛选) ---
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

    # 3. 【全局通用】读取用户档案 (从 configs 读)，姓名和年龄单独注入
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

    # 4. 【全局通用】读取格式规则 (从 configs 读)，仅当 include_global_format 为 True 时加入
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

    # --- 5. 中期记忆 (JSON - 按天，最近7天) ---
    try:
        path = os.path.join(prompts_dir, "5_memory_medium.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                med_mem = json.load(f)
                recent_list = []
                # 倒推7天
                for i in range(7, 0, -1):
                    day_key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                    if day_key in med_mem:
                        recent_list.append(f"- {day_key}: {med_mem[day_key]}")
                if recent_list:
                    prompt_parts.append(f"【Medium-term Memory / 最近一週間の出来事】\n" + "\n".join(recent_list))
    except Exception: pass

    # --- 6. 短期记忆 (JSON - 当天事件) ---
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

    # --- 7. 近期安排 (JSON - 仅限未来7天) ---
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

    # --- 8. 实时时间注入 ---
    # 格式示例: 2025-11-29 Saturday

    # 简单的星期几映射
    week_map = ["月", "火", "水", "木", "金", "土", "日"]
    week_str = week_map[now.weekday()]

    current_date_str = now.strftime('%Y-%m-%d %A')

    # 【修改】说明文字改成日语
    prompt_parts.append(f"【Current Date / 現在の日付】\n今日は: {current_date_str}\n(以下の会話履歴には時間 [HH:MM] のみが含まれています。現在の日付に基づいて理解してください)")

    # --- 【新增】语言控制 ---
    lang = get_ai_language()
    if lang == "zh":
        # 强力指令：即使人设是日文，也要用中文回复
        lang_instruction = (
            "\n\n【Language Control / 语言控制】\n"
            "请注意：无论上述设定使用何种语言，你**必须使用中文**进行回复。\n"
            "在保留角色语气、口癖和性格特征的前提下，自然地转化为中文表达。"
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
            # 尝试直接匹配名字
            rel_info = rels_data.get(target_name)

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
        'static', 'manifest', 'service_worker', 'app_logo' # 静态资源 & PWA
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
    return render_template("moments.html")


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
        comments_ok = []
        for c in post.get("comments", []):
            try:
                ts = datetime.strptime(c["timestamp"], "%Y-%m-%d %H:%M:%S")
                if ts <= now:
                    reply_to_id = c.get("reply_to")
                    reply_to_remark = remarks.get(reply_to_id, reply_to_id or "") if reply_to_id else ""
                    comments_ok.append({
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

    # 用户评论后，由该条朋友圈的作者（角色）生成一条回复
    author_char_id = char_id
    post_content = post.get("content", "")
    reply_text = _generate_moment_reply_to_user(author_char_id, post_content, content)
    if reply_text:
        comments = post.get("comments", [])
        comments.append({"commenter_id": author_char_id, "content": reply_text, "timestamp": now, "reply_to": "user"})
        post["comments"] = comments
        raw[idx] = post
        safe_save_json(moments_path, raw)
        ctx = f"用户评论了你的朋友圈：「{content}」。你的回复：「{reply_text}」。"
        append_moment_event_to_short_memory(author_char_id, ctx)

    return jsonify({"status": "success", "comment": {"commenter_id": "user", "content": content, "timestamp": now}})


def _generate_likes_comments_for_user_moment(post_ts_str, post_content):
    """用户发朋友圈后，根据各角色亲密度随机生成点赞和评论。返回 (likers, comments)。"""
    now = datetime.now()
    try:
        post_dt = datetime.strptime(post_ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        post_dt = now
    end_dt = post_dt + timedelta(hours=24)

    def random_ts_in_24h():
        delta_sec = random.randint(0, 24 * 3600)
        t = post_dt + timedelta(seconds=delta_sec)
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

    for char_id, info in chars_config.items():
        if info.get("deep_sleep", False):
            continue
        intimacy = max(0, min(100, int(info.get("intimacy", 60))))
        p_like = intimacy / 100.0
        p_comment = (intimacy / 100.0) * 0.6
        if random.random() < p_like:
            likers.append({"liker_id": char_id, "timestamp": random_ts_in_24h()})
        if random.random() < p_comment:
            comment_text = _generate_moment_comment(char_id, "user", post_content)
            if comment_text:
                comments.append({
                    "commenter_id": char_id,
                    "content": comment_text,
                    "timestamp": random_ts_in_24h()
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
    for c in new_post.get("comments", []):
        cid = c.get("commenter_id")
        if cid and cid != "user":
            ctx = f"用户发了一条朋友圈：「{(content or '')[:200]}」。你的评论：「{c.get('content', '')}」。"
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

    recent_texts = [r["content"] for r in history_rows] if history_rows else []
    user_latest = history_rows[-1]["content"] if history_rows and history_rows[-1]["role"] == "user" else None
    system_prompt = build_system_prompt(char_id, recent_messages=recent_texts, user_latest_input=user_latest)

    # 7. 构建消息历史
    messages = [{"role": "system", "content": system_prompt}]

    # --- 【关键修改】判断时间跨度 ---
    now = datetime.now()
    show_full_date = False # 默认不显示日期，只显示时间

    if history_rows:
        try:
            # 1. 获取第一条历史记录的时间 (最早的一条)
            first_msg_ts_str = history_rows[0]['timestamp']
            first_dt = datetime.strptime(first_msg_ts_str, '%Y-%m-%d %H:%M:%S')

            # 2. 比较：最早一条的日期 vs 现在(最新一条)的日期
            # 如果日期不同 (比如昨天聊的 vs 今天聊的)，则开启“日期显示模式”
            if first_dt.date() != now.date():
                show_full_date = True
        except:
            # 如果解析出错，为了保险起见，保持默认或者开启
            pass

    # --- 循环处理历史消息 ---
    for row in history_rows:
        try:
            dt_object = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')

            if show_full_date:
                # 跨天模式：显示 [12-25 14:30]
                formatted_timestamp = dt_object.strftime('[%m-%d %H:%M]')
            else:
                # 同天模式：只显示 [14:30]
                formatted_timestamp = dt_object.strftime('[%H:%M]')

            content_for_ai = _sticker_content_for_ai(row['content'])
            formatted_content = f"{formatted_timestamp} {content_for_ai}"
            messages.append({"role": row['role'], "content": formatted_content})
        except:
            # 容错：原样添加
            messages.append({"role": row['role'], "content": row['content']})

    # 1. 获取当前配置
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
        system_prompt = build_system_prompt(char_id, recent_messages=recent_texts, user_latest_input=user_latest)
        messages = [{"role": "system", "content": system_prompt}]

        # 5. 构建上下文（history_rows 已在上方读取）

        # 6. 构建上下文
        show_full_date = False
        now = datetime.now()
        if history_rows:
            try:
                first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if first_ts.date() != now.date(): show_full_date = True
            except: pass

        for row in history_rows:
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                ts_str = dt_obj.strftime('[%m-%d %H:%M]') if show_full_date else dt_obj.strftime('[%H:%M]')
                content_for_ai = _sticker_content_for_ai(row['content'])
                formatted_content = f"{ts_str} {content_for_ai}"
                messages.append({"role": row['role'], "content": formatted_content})
            except:
                messages.append({"role": row['role'], "content": row['content']})

        # ================= 【核心新增】智能补位逻辑 =================
        # 检查发给 AI 的最后一条消息是谁说的
        if len(messages) > 1: # 排除掉只有 System Prompt 的情况
            last_msg_role = messages[-1]['role']

            # 如果上一条依然是 assistant (说明是连续回复)，补一条假的 User 消息
            if last_msg_role == 'assistant' or last_msg_role == 'model':
                lang = get_ai_language()

                # 构造引导词 (不存数据库，仅用于诱导 AI)
                if lang == "zh":
                    fake_prompt = "(继续说)"
                else:
                    fake_prompt = "(続き)"

                print(f"--- [Regenerate] 检测到连续对话，插入隐形引导: {fake_prompt} ---")
                messages.append({"role": "user", "content": fake_prompt})
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
        sys_prompt = build_system_prompt(speaker_id, recent_messages=recent_texts, user_latest_input=user_latest)
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
                reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model)
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

@app.route("/api/system_config", methods=["GET", "POST"])
def handle_system_config():
    # 在 handle_system_config 函数里

    # 初始化默认配置 (增加了 model_options 字段)
    default_config = {
        "active_route": "gemini",
        "routes": {
            "gemini": {
                "name": "线路一：Gemini 直连",
                "models": {"chat": "gemini-2.5-pro", "moments": "gemini-2.5-pro", "gen_persona": "gemini-3-pro-preview", "summary": "gemini-2.5-pro"}
            },
            "relay": {
                "name": "线路二：国内中转",
                "models": {"chat": "gpt-3.5-turbo", "moments": "gpt-3.5-turbo", "gen_persona": "gpt-3.5-turbo", "summary": "gpt-3.5-turbo"}
            }
        },
        # 【新增】可用的模型列表 (把以前前端写死的搬到这里)
        "model_options": {
            'gemini': [
                'gemini-3-pro-preview',
                'gemini-3-flash-preview',
                'gemini-2.5-pro',
                'gemini-2.5-flash-lite'
            ],
            'relay': [
                'gpt-3.5-turbo',
                'deepseek-ai/DeepSeek-R1',
                'gpt-3.5-turbo-0125',
                'deepseek-ai/DeepSeek-V3',
                'gpt-4o'
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
            # 兼容旧配置：若某线路未配置 moments，则与 chat 相同
            for route_key, route_data in config.get("routes", {}).items():
                models = route_data.get("models", {})
                if "moments" not in models:
                    models["moments"] = models.get("chat", "gemini-2.5-pro")
            return jsonify(config)
        except:
            return jsonify(default_config)

    if request.method == "POST":
        new_config = request.json
        cfg_file = user_api_cfg_file if user_api_cfg_file else API_CONFIG_FILE
        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(new_config, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# 这是在 app.py 文件中的 call_openrouter 函数

# ---------------------- OpenRouter / Compatible API ----------------------

def call_openrouter(messages, char_id="unknown", model_name="google/gemini-2.5-pro"):
    import requests

    # 强制不走系统代理
    no_proxy = {"http": None, "https": None}

    # 【新增】打印日志
    log_full_prompt(f"OpenRouter ({model_name})", messages)

    # 构造请求地址，我们现在用的是 .env 里配置的新地址
    # 它会自动拼接成 "https://vg.v1api.cc/v1/chat/completions"
    url = f"{OPENROUTER_BASE_URL}/chat/completions"

    headers = {
        "Authorization": f"Bearer {get_effective_openrouter_key()}",  # 优先使用用户配置的 Key
        "Content-Type": "application/json"
    }

    # 重要：这里的 'model' 名称需要根据你的 API 服务商文档来填写
    # 他们支持哪些模型，你就填哪个。例如 "gpt-3.5-turbo", "gpt-4", "claude-3-opus" 等
    # 如果不确定，"gpt-3.5-turbo" 通常是最安全的选择。
    payload = {
        "model": model_name, # 【修改】这里用传入的 model_name
        "messages": messages,
        "temperature": 1,
        "max_tokens": 10240
    }

    print(f"--- [Debug] Calling Compatible API at: {url}")  # 增加一个调试日志
    print(f"--- [Debug] Using model: {payload['model']}")  # 增加一个调试日志

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=100)
        # 打印出服务端的原始报错信息，方便调试
        if r.status_code != 200:
            return f"[ERROR] API call failed with status {r.status_code}: {r.text}"

        result = r.json()

        # --- 【新增】Token 计费记录 (DeepSeek/OpenAI 格式) ---
        if 'usage' in result:
            usage = result['usage']
            record_token_usage(
                char_id,
                model_name,
                usage.get('prompt_tokens', 0),
                usage.get('completion_tokens', 0),
                usage.get('total_tokens', 0)
            )

        # --- 【关键修复】检查 choices 列表是否为空 ---
        if "choices" not in result or len(result["choices"]) == 0:
            print(f"⚠️ [Empty Response] API 返回了空列表。完整响应如下：")
            print(result) # 打印出来看看是为什么
            return "[API无回复] 可能是内容被过滤或服务繁忙。"

        # 一切正常，提取内容
        content = result["choices"][0]["message"]["content"]

        # 记录日志
        log_full_prompt(f"OpenRouter ({model_name})", messages, response_text=content)

        return content
    except Exception as e:
        return f"[ERROR] API request failed: {e}"

# ---------------------- Gemini ----------------------
# 修改 call_gemini 定义
def call_gemini(messages, char_id="unknown", model_name="gemini-2.5-pro"):
    """
    Google 官方直连 (配合 Cloudflare Worker) - 增强版
    """
    import requests
    import json

    # 1. 动态获取 Cloudflare 地址
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
    url = f"{base_url}/v1beta/models/{model_name}:generateContent?key={get_effective_gemini_key()}"

    # 2. 转换消息格式
    gemini_contents = []
    system_instruction = None
    for msg in messages:
        if msg['role'] == 'system':
            system_instruction = {"parts": [{"text": msg['content']}]}
        else:
            role = 'model' if msg['role'] == 'assistant' else 'user'
            gemini_contents.append({"role": role, "parts": [{"text": msg['content']}]})

    # 3. 构造 Payload (加入关键的安全设置！)
    payload = {
        "contents": gemini_contents,
        "generationConfig": {
            "temperature": 1,
            "maxOutputTokens": 10000
        },
        # 【关键修改】把 4 个维度的审查全部关掉 (BLOCK_NONE)
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
        r = requests.post(url, json=payload, timeout=100)

        if r.status_code != 200:
            return f"[Gemini Error {r.status_code}] {r.text}"

        result = r.json()

        # --- 【新增】提取 Token 数据 ---
        # Google 的格式通常叫 usageMetadata
        # --- 【新增】记录 Token ---
        token_usage = result.get('usageMetadata', {})
        if token_usage:
            record_token_usage(
                char_id,
                model_name,
                token_usage.get('promptTokenCount', 0),
                token_usage.get('candidatesTokenCount', 0),
                # 【新增】直接提取 totalTokenCount
                token_usage.get('totalTokenCount', 0)
            )
        # ------------------------

        # 解析回复
        if 'candidates' not in result or not result['candidates']:
            return "[Error] No candidates returned."

        candidate = result['candidates'][0]

        # 尝试获取文本
        text = ""
        if 'content' in candidate and 'parts' in candidate['content']:
            text = candidate['content']['parts'][0]['text']
        else:
            finish_reason = candidate.get('finishReason', 'UNKNOWN')
            text = f"[未生成文本] 原因: {finish_reason}"

        # --- 【修改】调用日志时，把 token_usage 传进去 ---
        log_full_prompt(f"Gemini Interaction ({model_name})", messages, response_text=text, usage=token_usage)

        return text

    except Exception as e:
        log_full_prompt(f"Gemini ERROR ({model_name})", messages, response_text=str(e))
        raise e

def get_model_config(task_type="chat"):
    """
    根据配置文件，获取当前应该用的 路由方式 和 模型名称
    task_type: 'chat' | 'moments' | 'gen_persona' | 'summary'
    朋友圈(moments) 默认与 chat 使用相同线路与模型。
    """
    # 多用户：优先读取 users/<user_id>/configs/api_settings.json
    user_id = get_current_user_id()
    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE

    if not os.path.exists(api_cfg_file):
        # 默认兜底
        return "gemini", "gemini-2.5-pro"

    try:
        with open(api_cfg_file, "r", encoding="utf-8") as f:
            config = json.load(f)

        route = config.get("active_route", "gemini")
        models = config.get("routes", {}).get(route, {}).get("models", {})
        # 朋友圈未配置时与 chat 相同
        if task_type == "moments" and "moments" not in models:
            model_name = models.get("chat", "gemini-2.5-pro")
        else:
            model_name = models.get(task_type, "gemini-2.5-pro")

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

            # 2. 统一重命名为 avatar.png (或者保留原扩展名)
            ext = os.path.splitext(file.filename)[1]
            if not ext: ext = ".png"
            filename = f"avatar{ext}"
            file_path = os.path.join(char_dir, filename)

            # 保存文件 (覆盖旧的)
            file.save(file_path)

            # 3. 更新 characters.json 里的路径（per-user）
            cfg_file = _get_characters_config_file()
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)

            # 生成新的访问 URL
            # 加上时间戳 ?v=... 是为了强制浏览器刷新缓存，立刻看到新头像
            timestamp = int(time.time())
            new_url = f"/char_assets/{char_id}/{filename}?v={timestamp}"

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

            # 2. 统一重命名为 avatar.png
            filename = "avatar.png"
            file_path = os.path.join(target_group_dir, filename)

            # 保存文件
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
            ext = os.path.splitext(file.filename)[1].lower() or ".png"
            save_path = os.path.join(user_dir, f"avatar{ext}")

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
    - 优先读取 users/<user_id>/avatar.* 文件
    - 如果不存在，则退回默认的 static/avatar_user.png
    """
    user_id = get_current_user_id()
    base_dir = os.path.dirname(os.path.abspath(__file__))

    if user_id:
        user_dir = os.path.join(USERS_ROOT, str(user_id))
        # 支持常见扩展名
        for name in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp"):
            candidate = os.path.join(user_dir, name)
            if os.path.exists(candidate):
                return send_from_directory(user_dir, name)

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

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    # 1. 获取源路径 和 目标路径
    source_path = os.path.join(BASE_DIR, "characters", source_char_id, "prompts", "7_schedule.json")
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

    # 保存到文件（仅当已有 logs 目录时，不自动创建根目录 logs）
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        if os.path.exists(log_dir):
            log_file = os.path.join(log_dir, "api.log")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(final_log)
    except Exception:
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
    """让 commenter_id 角色对 post_content 生成一条简短评论。"""
    recent_messages = [post_content]
    sys_prompt = build_system_prompt(commenter_id, recent_messages=recent_messages)

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
            return text
    except Exception as e:
        print(f"   [Moments] 评论生成失败 {commenter_id}: {e}")
    return None


def _generate_moment_reply_to_user(author_char_id, post_content, user_comment):
    """让朋友圈作者（角色）对用户的评论生成一条简短回复。"""
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
            return text
    except Exception as e:
        print(f"   [Moments] 角色回复评论失败 {author_char_id}: {e}")
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
        delta_sec = random.randint(0, 24 * 3600)
        t = post_dt + timedelta(seconds=delta_sec)
        return t.strftime("%Y-%m-%d %H:%M:%S")

    likers_data = []
    if candidates:
        n_like = random.randint(0, min(5, len(candidates)))
        like_cids = _weighted_sample_no_replacement(candidates, n_like)
        for cid in like_cids:
            likers_data.append({"liker_id": cid, "timestamp": random_ts_in_24h()})

    comments_data = []
    if candidates:
        n_comment = random.randint(0, min(3, len(candidates)))
        comment_cids = _weighted_sample_no_replacement(candidates, n_comment)
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
def trigger_active_chat(char_id):
    print(f"💓 [Active] 尝试触发 {char_id} 的主动消息...")

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
    base_system_prompt = build_system_prompt(char_id, recent_messages=recent_texts)
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
            f"（要求：自然、简短，不要重复上一句话。）"
        )
    else:
        trigger_msg = (
            f"（システム通知：現在は{period} {time_str}です。）\n"
            f"（ユーザーからの返信が途絶えています。現在の時間帯やこれまでの会話を踏まえて、**自発的に**新しい話題を振ってください。）\n"
            f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）"
        )

    # 把它伪装成 User 发的消息
    messages.append({"role": "user", "content": trigger_msg})

    # 5. 调用 AI
    try:
        route, current_model = get_model_config("chat")
        print(f"   -> [Active] Calling AI ({route}/{current_model})...")

        if route == "relay":
            reply_text = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text = call_gemini(messages, char_id=char_id, model_name=current_model)

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
        print(f"💓 [Active] 发送失败: {e}")
        return False

# --- 【修正版】群聊主动消息 (伪装成 User 指令) ---
def trigger_group_active_chat(group_id):
    print(f"💓 [GroupActive] 尝试触发群 {group_id} 的主动消息...")

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
            route, current_model = get_model_config("chat")

            if route == "relay":
                reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model)
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
                send_email_notification(email_title, email_body, user_id=get_current_user_id())

                notification_sent = True

            # 稍微停顿一下，防止并发请求过快
            time.sleep(2)

        except Exception as e:
            print(f"Active Chat Error: {e}")
            # 如果出错就不继续后面几轮了，直接结束
            break

    return True

# ---------------------- 启动 ----------------------

if __name__ == "__main__":
    # 【关键修改】加上 use_reloader=False
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
