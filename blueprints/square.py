import os
import io
import time
import re
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse
from flask import Blueprint, request, jsonify, session, render_template
from PIL import Image
from cos_utils import download_from_cos, upload_to_cos
from core.config import COS_BASE_URL, SQUARE_DB, SQUARE_AVATARS_DIR, USERS_DB, USERS_ROOT
from core.context import get_current_user_id
from core.credentials import CredentialError, get_user_credential
from core.utils import (
    safe_save_json,
    _get_characters_config_file,
    get_current_username,
    get_paths,
)
from services.ai_client import call_gemini
from services.persona_locks import PersonaLockError, validate_persona_locks

square_bp = Blueprint('square', __name__)

TRIAL_ADMIN_USER_ID = 1
TRIAL_MAX_TURNS = 10
TRIAL_MAX_MESSAGE_CHARS = 2000
TRIAL_RATE_LIMIT_PER_MINUTE = max(
    1, int(os.getenv("SQUARE_TRIAL_RATE_LIMIT_PER_MINUTE", "6"))
)
TRIAL_PENDING_TIMEOUT_SECONDS = max(
    30, int(os.getenv("SQUARE_TRIAL_PENDING_TIMEOUT_SECONDS", "90"))
)


def _utc_iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _trial_timestamp_for_chat(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _same_origin_json_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _ensure_trial_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS square_trial_sessions (
            user_id INTEGER NOT NULL,
            character_id TEXT NOT NULL,
            relationship TEXT NOT NULL DEFAULT '{}',
            completed_turns INTEGER NOT NULL DEFAULT 0,
            pending_request TEXT,
            imported_local_id TEXT,
            imported_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, character_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS square_trial_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            character_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_square_trial_message_request_role
        ON square_trial_messages(user_id, character_id, request_id, role)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS square_trial_requests (
            request_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            character_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_square_trial_requests_user_created
        ON square_trial_requests(user_id, created_at)
    """)


def _load_trial_character(conn, char_id):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, name, avatar, age, no_age_increase, base_persona
        FROM characters WHERE id = ?
        """,
        (char_id,),
    ).fetchone()


def _normalize_trial_relationship(raw):
    from blueprints.chat import normalize_relationship_graph

    value = normalize_relationship_graph({"user": raw or {}}).get("user") or {}
    role = str(value.get("role") or "").strip()
    description = str(value.get("description") or "").strip()
    if not role:
        raise ValueError("请填写关系定位")
    if len(role) > 50:
        raise ValueError("关系定位不能超过50个字")
    if len(description) > 1000:
        raise ValueError("关系描述不能超过1000个字")
    value["role"] = role
    value["description"] = description
    return value


def _trial_relationship_from_row(row):
    if not row:
        return None
    try:
        value = json.loads(row["relationship"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("role") else None


def _build_trial_messages(character, relationship, history, user_message):
    user_name = get_current_username()
    age_line = f"\n年龄：{character['age']}" if character["age"] not in (None, "") else ""
    system_prompt = f"""【角色人设】
姓名：{character['name']}{age_line}
{character['base_persona'] or ''}

【当前对话对象】
用户姓名：{user_name}

【角色与当前用户的关系】
关系定位：{relationship.get('role', '')}
关系指数：{relationship.get('score', 1)}
关系描述：{relationship.get('description', '')}

【角色广场试聊规则】
- 始终保持上述角色人设和说话方式，只与当前用户进行普通文字聊天。
- 不得创建、修改或讨论系统记忆、计划、好感度、关系数值、情绪、睡眠状态或聊天模式。
- 不得发起群聊转向、语音、图片、表情、拍一拍、撤回、转账、通话或任何工具操作。
- 不得输出方括号形式的系统动作标签或隐藏指令。
- 回复应自然、简短，直接给出角色要说的话，不解释系统规则。"""
    messages = [{"role": "system", "content": system_prompt}]
    for item in history:
        if item["role"] in {"user", "assistant"}:
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": user_message})
    return messages


_TRIAL_ACTION_PATTERNS = [
    r"\[(?:ADD_PLAN|UPDATE_AFFINITY|SET_RELATION|SET_CHAT_MODE|SET_EMOTION|SET_PERSONALITY|SET_SLEEP_TIME|DIRECT_TO_GROUP|DIRECT_TO_USER|DIRECT_END|MOOD|SAFETY_ALERT|CALL_USER|GENERATE_IMAGE|SEARCH_IMG)(?::[^\]]*)?\]",
    r"\[(?:tickle(?:_user)?|recall|NONE)\]",
    r"\[voice\]\([^)]*\)\([^)]*\)",
    r"\[(?:表情|图片|音乐|文件|链接|视频)(?:\]|\([^\]]*\)|[^\s/]*)",
]


def _sanitize_trial_reply(value):
    text = str(value or "")
    for pattern in _TRIAL_ACTION_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", text).strip(" /\n\t")


def _trial_ai_error(reply):
    text = str(reply or "").strip()
    return (
        not text
        or text.startswith("（系统提示：")
        or text.startswith("（AI 陷入了沉默")
        or not bool(getattr(reply, "complete", True))
    )


def _ensure_square_character_columns(conn):
    """Backfill publish-source metadata on existing square databases."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(characters)").fetchall()}
    additions = {
        "author_user_id": "INTEGER",
        "source_character_id": "TEXT",
        "updated_at": "TEXT",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE characters ADD COLUMN {name} {sql_type}")


def _get_user_email(user_id):
    if not user_id:
        return ""
    try:
        with sqlite3.connect(USERS_DB) as conn:
            row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _is_square_author(row, user_id, author_email):
    """Prefer stable user id; retain email fallback for legacy publications."""
    if not row:
        return False
    keys = row.keys() if isinstance(row, sqlite3.Row) else ()
    owner_id = row["author_user_id"] if "author_user_id" in keys else None
    owner_email = row["author_email"] if "author_email" in keys else ""
    if owner_id is not None:
        return str(owner_id) == str(user_id)
    return bool(author_email and owner_email == author_email)


def _read_base_persona(prompts_dir):
    path = os.path.join(prompts_dir, "1_base_persona.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            value = json.load(f)
        if isinstance(value, dict) and isinstance(value.get("system_prompt"), str):
            return value["system_prompt"]
    return ""


def _load_local_publish_snapshot(user_id, local_character_id):
    """Read only the fields that are allowed to leave a private character."""
    local_character_id = str(local_character_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", local_character_id):
        raise ValueError("本地角色 ID 无效")

    config_path = _get_characters_config_file(user_id=user_id)
    if not os.path.isfile(config_path):
        raise FileNotFoundError("本地角色配置不存在")
    with open(config_path, "r", encoding="utf-8-sig") as f:
        characters = json.load(f) or {}
    info = characters.get(local_character_id)
    if not isinstance(info, dict):
        raise FileNotFoundError("本地角色不存在")

    prompts_dir = os.path.join(
        USERS_ROOT, str(user_id), "characters", local_character_id, "prompts"
    )
    relationship = {}
    relationship_path = os.path.join(prompts_dir, "2_relationship.json")
    if os.path.isfile(relationship_path):
        with open(relationship_path, "r", encoding="utf-8-sig") as f:
            relationship = json.load(f) or {}
    from blueprints.chat import normalize_relationship_graph
    relationship = normalize_relationship_graph(relationship)

    return {
        "id": local_character_id,
        "name": str(info.get("name") or info.get("remark") or local_character_id),
        "avatar": str(info.get("avatar") or "/static/default_avatar.png"),
        "age": info.get("age"),
        "no_age_increase": bool(info.get("no_age_increase", False)),
        "base_persona": _read_base_persona(prompts_dir),
        "relationship_graph": relationship,
        # IP/tags may be private-app metadata. The square form remains authoritative.
        "ip": str(info.get("ip") or ""),
        "tags": info.get("tags") or "",
    }


def _save_local_square_link(user_id, local_character_id, square_id):
    if not local_character_id:
        return
    config_path = _get_characters_config_file(user_id=user_id)
    if not os.path.isfile(config_path):
        return
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            characters = json.load(f) or {}
        info = characters.get(local_character_id)
        if not isinstance(info, dict):
            return
        info["square_published_id"] = square_id
        info["square_last_synced_at"] = datetime.now().isoformat()
        safe_save_json(config_path, characters)
    except Exception as e:
        print(f"Square local link save error: {e}")


class SquareAvatarError(ValueError):
    pass


def _private_avatar_cos_keys(user_id, avatar_url):
    """Resolve a private character avatar URL to current-user COS object keys."""
    if not user_id or not avatar_url:
        return []

    parsed = urlparse(avatar_url)
    path = unquote(parsed.path or "")
    prefix = "/char_assets/"
    if path.startswith(prefix):
        parts = path[len(prefix):].split("/", 1)
    else:
        cos_host = urlparse(COS_BASE_URL).netloc
        if not cos_host or parsed.netloc != cos_host:
            return []
        object_key = path.lstrip("/")
        private_prefix = f"users/{user_id}/characters/"
        if not object_key.startswith(private_prefix):
            return []
        parts = object_key[len(private_prefix):].split("/", 1)

    if len(parts) != 2:
        return []

    private_char_id, filename = parts
    if not re.fullmatch(r"[A-Za-z0-9_]+", private_char_id):
        return []
    if filename != os.path.basename(filename):
        return []

    filenames = [filename]
    for candidate in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp", "avatar.gif"):
        if candidate not in filenames:
            filenames.append(candidate)
    return [
        f"users/{user_id}/characters/{private_char_id}/{candidate}"
        for candidate in filenames
    ]


def _read_private_avatar_from_cos(user_id, avatar_url):
    for object_key in _private_avatar_cos_keys(user_id, avatar_url):
        payload = download_from_cos(object_key)
        if payload:
            return io.BytesIO(payload)
    return None


def _save_square_avatar_image(image_source, square_id, name_prefix="square"):
    os.makedirs(SQUARE_AVATARS_DIR, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_]+", "_", square_id or "char").strip("_") or "char"
    filename = f"{name_prefix}_{safe_id}_{int(time.time())}.png"
    local_path = os.path.join(SQUARE_AVATARS_DIR, filename)

    img = Image.open(image_source)
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
    else:
        img = img.convert('RGB')
    img.save(local_path, 'PNG')

    cos_url = upload_to_cos(local_path, f"square/avatars/{filename}")
    if cos_url:
        try:
            os.remove(local_path)
        except OSError:
            pass
        return cos_url
    try:
        os.remove(local_path)
    except OSError:
        pass
    raise SquareAvatarError("广场头像上传到 COS 失败，请稍后重试")


def _materialize_square_avatar(square_id, user_id, uploaded_file=None, avatar_url=None, current_avatar=None):
    """
    Store square avatars as public square assets.
    Private /char_assets/... and private COS URLs are read from COS, then
    republished under square/avatars/. The server's local character folder is
    never used as an avatar source.
    """
    if uploaded_file and getattr(uploaded_file, "filename", ""):
        try:
            return _save_square_avatar_image(uploaded_file.stream, square_id)
        except Exception as e:
            print(f"Square Avatar Upload Error: {e}")
            raise SquareAvatarError("头像处理失败，请重新选择图片") from e

    source_url = (avatar_url or current_avatar or "").strip()
    if not source_url:
        return "/static/default_avatar.png"

    private_cos_keys = _private_avatar_cos_keys(user_id, source_url)
    if private_cos_keys:
        source_file = _read_private_avatar_from_cos(user_id, source_url)
        if source_file is not None:
            try:
                return _save_square_avatar_image(source_file, square_id, "square_import")
            except Exception as e:
                print(f"Square Avatar Import Error: {e}")
                raise SquareAvatarError("COS 头像处理失败，请稍后重试") from e
        raise SquareAvatarError("无法从 COS 读取角色头像，请先重新上传角色头像")

    if source_url.startswith("http://") or source_url.startswith("https://") or source_url.startswith("/static/"):
        return source_url

    return current_avatar or "/static/default_avatar.png"


def init_square_db():
    """初始化角色广场数据库结构"""
    os.makedirs(os.path.dirname(SQUARE_DB), exist_ok=True)
    os.makedirs(SQUARE_AVATARS_DIR, exist_ok=True)
    conn = sqlite3.connect(SQUARE_DB)
    cur = conn.cursor()
    # 角色表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            avatar TEXT,
            age INTEGER,
            no_age_increase INTEGER DEFAULT 0,
            base_persona TEXT,
            relationship_graph TEXT,
            tags TEXT,
            ip TEXT,
            author_email TEXT,
            author_user_id INTEGER,
            source_character_id TEXT,
            likes_count INTEGER DEFAULT 0,
            favorites_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    _ensure_square_character_columns(conn)
    # IP表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ips (
            name TEXT PRIMARY KEY,
            heat INTEGER DEFAULT 0,
            character_count INTEGER DEFAULT 0
        )
    """)
    # 评论表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT,
            content TEXT,
            created_at TEXT
        )
    """)
    # 收藏夹表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER,
            character_id TEXT,
            PRIMARY KEY (user_id, character_id)
        )
    """)
    # 点赞表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS likes (
            user_id INTEGER,
            character_id TEXT,
            PRIMARY KEY (user_id, character_id)
        )
    """)
    _ensure_trial_tables(conn)
    conn.commit()
    conn.close()


# ---------------------- 角色广场 API ----------------------

@square_bp.route("/square")
def square_index_page():
    return render_template("square/index.html")

@square_bp.route("/square/upload")
def square_upload_page():
    return render_template("square/upload.html")

@square_bp.route("/square/character/<char_id>")
def square_character_page(char_id):
    return render_template("square/character.html", char_id=char_id)


@square_bp.route("/square/character/<char_id>/trial")
def square_character_trial_page(char_id):
    return render_template("square/trial_chat.html", char_id=char_id)

@square_bp.route("/api/square/ips")
def api_square_ips():
    try:
        conn = sqlite3.connect(SQUARE_DB)
        _ensure_square_character_columns(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT name, heat, character_count FROM ips "
            "WHERE character_count > 0 ORDER BY heat DESC"
        )
        rows = cur.fetchall()
        conn.close()
        return jsonify([{"name": r[0], "heat": r[1], "count": r[2]} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/search_ip")
def api_square_search_ip():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        if q:
            cur.execute("SELECT name, character_count FROM ips WHERE name LIKE ? LIMIT 10", (f"%{q}%",))
        else:
            cur.execute("SELECT name, character_count FROM ips ORDER BY heat DESC LIMIT 10")
        rows = cur.fetchall()
        conn.close()
        return jsonify([{"name": r[0], "count": r[1]} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/list")
def api_square_list():
    search = request.args.get("search", "").strip()
    ip_filter = request.args.get("ip", "").strip()
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        query = "SELECT id, name, avatar, ip, likes_count, tags FROM characters WHERE 1=1"
        params = []
        if ip_filter:
            query += " AND ip = ?"
            params.append(ip_filter)
        if search:
            query += " AND (name LIKE ? OR ip LIKE ? OR tags LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        query += " ORDER BY likes_count DESC"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "name": r[1], "avatar": r[2],
            "ip": r[3], "likes": r[4], "tags": r[5]
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _linked_square_id(conn, user_id, author_email, local_character_id, exclude_id=None):
    query = """
        SELECT id FROM characters
        WHERE (author_user_id = ? OR (author_user_id IS NULL AND author_email = ?))
          AND (source_character_id = ? OR (source_character_id IS NULL AND id = ?))
    """
    params = [user_id, author_email, local_character_id, local_character_id]
    if exclude_id:
        query += " AND id != ?"
        params.append(exclude_id)
    query += " ORDER BY CASE WHEN source_character_id = ? THEN 0 ELSE 1 END LIMIT 1"
    params.append(local_character_id)
    row = conn.execute(query, params).fetchone()
    return row[0] if row else None


@square_bp.route("/api/square/local_characters")
def api_square_local_characters():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    config_path = _get_characters_config_file(user_id=user_id)
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            characters = json.load(f) or {}
    except FileNotFoundError:
        characters = {}
    except Exception as e:
        return jsonify({"error": f"读取本地角色失败: {e}"}), 500

    author_email = _get_user_email(user_id)
    with sqlite3.connect(SQUARE_DB) as conn:
        _ensure_square_character_columns(conn)
        result = []
        for char_id, info in characters.items():
            if not isinstance(info, dict):
                continue
            result.append({
                "id": char_id,
                "name": info.get("name") or info.get("remark") or char_id,
                "avatar": info.get("avatar") or "/static/default_avatar.png",
                "linked_square_id": _linked_square_id(conn, user_id, author_email, char_id),
            })
    result.sort(key=lambda item: str(item["name"]).casefold())
    return jsonify(result)


@square_bp.route("/api/square/local_character/<local_character_id>/preview")
def api_square_local_character_preview(local_character_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    try:
        snapshot = _load_local_publish_snapshot(user_id, local_character_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"读取本地角色失败: {e}"}), 500

    author_email = _get_user_email(user_id)
    with sqlite3.connect(SQUARE_DB) as conn:
        _ensure_square_character_columns(conn)
        linked_id = _linked_square_id(conn, user_id, author_email, local_character_id)
    snapshot["linked_square_id"] = linked_id
    return jsonify({"status": "success", "character": snapshot})

@square_bp.route("/api/square/upload", methods=["POST"])
def api_square_upload():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    # 获取作者邮箱
    author_email = _get_user_email(user_id)

    # 处理表单数据
    # 因为涉及头像上传，可能需要 multipart/form-data
    data = request.form
    char_id_base = data.get("id", "").strip()
    name = data.get("name", "").strip()
    age = data.get("age", "").strip()
    no_age_increase = 1 if data.get("no_age_increase") == "true" else 0
    ip = data.get("ip", "").strip()
    tags_raw = data.get("tags", "").strip()
    # 规范化标签：支持中英文逗号和空格，统一转为英文逗号分隔
    import re
    tags_list = [t.strip() for t in re.split(r'[,，\s]+', tags_raw) if t.strip()]
    tags = ",".join(tags_list)

    relationship_graph = data.get("relationship_graph", "{}").strip()
    try:
        from blueprints.chat import normalize_relationship_graph
        relationship_graph = json.dumps(
            normalize_relationship_graph(relationship_graph),
            ensure_ascii=False,
            indent=2,
        )
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": f"关系图谱 JSON 无效: {e}"}), 400
    base_persona = data.get("base_persona", "").strip()
    try:
        validate_persona_locks(base_persona)
    except PersonaLockError as e:
        return jsonify({"error": f"人设 LOCK 标签格式错误：{e}", "code": e.code}), 400
    source_character_id = data.get("source_character_id", "").strip() or None

    if not char_id_base or not name:
        return jsonify({"error": "ID和名称不能为空"}), 400

    if source_character_id:
        try:
            _load_local_publish_snapshot(user_id, source_character_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404

        with sqlite3.connect(SQUARE_DB) as conn:
            _ensure_square_character_columns(conn)
            existing_id = _linked_square_id(conn, user_id, author_email, source_character_id)
        if existing_id:
            return jsonify({
                "status": "already_published",
                "square_id": existing_id,
                "message": "该本地角色已经发布，可更新原作品",
            }), 409

    # 生成唯一 ID
    final_id = generate_unique_square_id(char_id_base)

    # 头像处理：广场头像必须固化为公共资源，不能保存 /char_assets/... 私有路由
    uploaded_avatar = request.files.get('avatar')
    try:
        avatar_url = _materialize_square_avatar(
            final_id,
            user_id,
            uploaded_file=uploaded_avatar,
            avatar_url=data.get("avatar_url"),
        )
    except SquareAvatarError as e:
        return jsonify({"error": str(e), "code": "square_avatar_unavailable"}), 422

    # 写入数据库
    try:
        conn = sqlite3.connect(SQUARE_DB)
        _ensure_square_character_columns(conn)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO characters (
                id, name, avatar, age, no_age_increase, base_persona,
                relationship_graph, tags, ip, author_email, author_user_id,
                source_character_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            final_id, name, avatar_url, age, no_age_increase, base_persona,
            relationship_graph, tags, ip, author_email, user_id,
            source_character_id, datetime.now().isoformat(), datetime.now().isoformat(),
        ))

        # 更新 IP 表
        if ip:
            cur.execute("SELECT name FROM ips WHERE name = ?", (ip,))
            if cur.fetchone():
                cur.execute("UPDATE ips SET character_count = character_count + 1 WHERE name = ?", (ip,))
            else:
                cur.execute("INSERT INTO ips (name, heat, character_count) VALUES (?, 0, 1)", (ip,))

        conn.commit()
        conn.close()
        _save_local_square_link(user_id, source_character_id, final_id)
        return jsonify({"status": "success", "id": final_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/character/<char_id>")
def api_square_character_detail(char_id):
    try:
        conn = sqlite3.connect(SQUARE_DB)
        conn.row_factory = sqlite3.Row  # 使用 Row 模式，通过列名访问
        _ensure_square_character_columns(conn)
        _ensure_trial_tables(conn)
        cur = conn.cursor()

        cur.execute("SELECT * FROM characters WHERE id = ?", (char_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "角色不存在"}), 404

        # 转换为字典，这样列顺序不再影响结果
        char_data = dict(row)

        # 统一字段名以兼容前端
        char_data["likes"] = char_data.get("likes_count", 0)
        char_data["favorites"] = char_data.get("favorites_count", 0)
        char_data["comments"] = char_data.get("comment_count", 0)

        # 获取评论
        cur.execute("SELECT content, created_at FROM comments WHERE character_id = ? ORDER BY id DESC", (char_id,))
        comments = [{"content": r["content"], "created_at": r["created_at"]} for r in cur.fetchall()]

        # 获取该作者其他角色
        cur.execute("""
            SELECT id, name, avatar FROM characters
            WHERE (author_user_id = ? OR (author_user_id IS NULL AND author_email = ?))
              AND id != ?
        """, (char_data.get("author_user_id"), char_data.get("author_email"), char_id))
        other_chars = [{"id": r["id"], "name": r["name"], "avatar": r["avatar"]} for r in cur.fetchall()]

        # 检查点赞/收藏状态
        is_liked = False
        is_favorited = False
        is_author = False
        user_id = get_current_user_id()
        trial_summary = None

        if user_id:
            # 检查收藏
            cur.execute("SELECT 1 FROM favorites WHERE user_id = ? AND character_id = ?", (user_id, char_id))
            if cur.fetchone(): is_favorited = True

            # 检查点赞
            cur.execute("SELECT 1 FROM likes WHERE user_id = ? AND character_id = ?", (user_id, char_id))
            if cur.fetchone(): is_liked = True

            is_author = _is_square_author(row, user_id, _get_user_email(user_id))
            trial_row = cur.execute(
                """
                SELECT completed_turns FROM square_trial_sessions
                WHERE user_id = ? AND character_id = ?
                """,
                (user_id, char_id),
            ).fetchone()
            if trial_row:
                trial_summary = {
                    "completed_turns": int(trial_row["completed_turns"] or 0),
                    "message_count": cur.execute(
                        """
                        SELECT COUNT(*) FROM square_trial_messages
                        WHERE user_id = ? AND character_id = ? AND status = 'completed'
                        """,
                        (user_id, char_id),
                    ).fetchone()[0],
                }

        conn.close()
        return jsonify({
            "character": char_data,
            "comments": comments,
            "other_characters": other_chars,
            "is_favorited": is_favorited,
            "is_liked": is_liked,
            "is_author": is_author,
            "trial": trial_summary,
        })
    except Exception as e:
        print(f"Detail API Error: {e}")
        return jsonify({"error": str(e)}), 500


@square_bp.route("/api/square/character/<char_id>/trial/bootstrap")
def api_square_trial_bootstrap(char_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    with sqlite3.connect(SQUARE_DB) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_trial_tables(conn)
        character = _load_trial_character(conn, char_id)
        if not character:
            return jsonify({"error": "角色不存在"}), 404
        trial = conn.execute(
            "SELECT * FROM square_trial_sessions WHERE user_id = ? AND character_id = ?",
            (user_id, char_id),
        ).fetchone()
        messages = conn.execute(
            """
            SELECT id, turn_number, role, content, created_at
            FROM square_trial_messages
            WHERE user_id = ? AND character_id = ? AND status = 'completed'
            ORDER BY id ASC
            """,
            (user_id, char_id),
        ).fetchall()

    completed_turns = int(trial["completed_turns"] or 0) if trial else 0
    response = jsonify({
        "character": {
            "id": character["id"],
            "name": character["name"],
            "avatar": character["avatar"] or "/static/default_avatar.png",
            "age": character["age"],
        },
        "user": {"name": get_current_username()},
        "relationship": _trial_relationship_from_row(trial),
        "messages": [dict(row) for row in messages],
        "completed_turns": completed_turns,
        "remaining_turns": max(0, TRIAL_MAX_TURNS - completed_turns),
        "max_turns": TRIAL_MAX_TURNS,
        "trial_completed": completed_turns >= TRIAL_MAX_TURNS,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@square_bp.route("/api/square/character/<char_id>/trial/relationship", methods=["PUT"])
def api_square_trial_relationship(char_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    if not _same_origin_json_request():
        return jsonify({"error": "请求来源校验失败"}), 403
    try:
        relationship = _normalize_trial_relationship((request.get_json(silent=True) or {}).get("relationship"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    now = _utc_iso_now()
    with sqlite3.connect(SQUARE_DB) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_trial_tables(conn)
        if not _load_trial_character(conn, char_id):
            return jsonify({"error": "角色不存在"}), 404
        conn.execute(
            """
            INSERT INTO square_trial_sessions (
                user_id, character_id, relationship, completed_turns,
                created_at, updated_at
            ) VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(user_id, character_id) DO UPDATE SET
                relationship = excluded.relationship,
                updated_at = excluded.updated_at
            """,
            (user_id, char_id, json.dumps(relationship, ensure_ascii=False), now, now),
        )
        conn.commit()
    return jsonify({"status": "success", "relationship": relationship})


def _mark_trial_request_failed(user_id, char_id, request_id):
    now = _utc_iso_now()
    with sqlite3.connect(SQUARE_DB) as conn:
        _ensure_trial_tables(conn)
        conn.execute(
            """
            UPDATE square_trial_requests SET status = 'failed', updated_at = ?
            WHERE request_id = ? AND user_id = ? AND character_id = ?
            """,
            (now, request_id, user_id, char_id),
        )
        conn.execute(
            """
            UPDATE square_trial_sessions SET pending_request = NULL, updated_at = ?
            WHERE user_id = ? AND character_id = ? AND pending_request = ?
            """,
            (now, user_id, char_id, request_id),
        )
        conn.commit()


@square_bp.route("/api/square/character/<char_id>/trial/chat", methods=["POST"])
def api_square_trial_chat(char_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    if not _same_origin_json_request():
        return jsonify({"error": "请求来源校验失败"}), 403

    payload = request.get_json(silent=True) or {}
    user_message = str(payload.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "请输入消息"}), 400
    if len(user_message) > TRIAL_MAX_MESSAGE_CHARS:
        return jsonify({"error": f"消息不能超过{TRIAL_MAX_MESSAGE_CHARS}个字"}), 400
    request_id = str(payload.get("request_id") or uuid.uuid4().hex).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
        return jsonify({"error": "request_id 格式错误"}), 400

    try:
        admin_key = get_user_credential(TRIAL_ADMIN_USER_ID, "gemini", allow_legacy=True)
    except CredentialError:
        return jsonify({"error": "试聊服务暂时不可用，请联系管理员"}), 503
    if not admin_key:
        return jsonify({"error": "管理员尚未配置试聊 Gemini API Key"}), 503

    now = _utc_iso_now()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    character = None
    relationship = None
    history = []
    turn_number = None

    with sqlite3.connect(SQUARE_DB, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_trial_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        character = _load_trial_character(conn, char_id)
        if not character:
            conn.rollback()
            return jsonify({"error": "角色不存在"}), 404
        trial = conn.execute(
            "SELECT * FROM square_trial_sessions WHERE user_id = ? AND character_id = ?",
            (user_id, char_id),
        ).fetchone()
        relationship = _trial_relationship_from_row(trial)
        if not relationship:
            conn.rollback()
            return jsonify({"error": "relationship_required", "message": "请先填写与角色的关系"}), 409

        completed = conn.execute(
            """
            SELECT role, content, created_at, turn_number
            FROM square_trial_messages
            WHERE user_id = ? AND character_id = ? AND request_id = ?
              AND status = 'completed'
            ORDER BY id ASC
            """,
            (user_id, char_id, request_id),
        ).fetchall()
        if len(completed) == 2:
            conn.commit()
            completed_turns = int(trial["completed_turns"] or 0)
            return jsonify({
                "status": "success",
                "turn": completed[0]["turn_number"],
                "remaining_turns": max(0, TRIAL_MAX_TURNS - completed_turns),
                "user_message": dict(completed[0]),
                "assistant_message": dict(completed[1]),
                "trial_completed": completed_turns >= TRIAL_MAX_TURNS,
                "replayed": True,
            })

        completed_turns = int(trial["completed_turns"] or 0)
        if completed_turns >= TRIAL_MAX_TURNS:
            conn.rollback()
            return jsonify({
                "error": "trial_limit_reached",
                "message": "该角色的10轮试聊已经完成",
                "completed_turns": completed_turns,
                "remaining_turns": 0,
            }), 409

        pending = str(trial["pending_request"] or "")
        if pending:
            pending_row = conn.execute(
                "SELECT updated_at FROM square_trial_requests WHERE request_id = ?",
                (pending,),
            ).fetchone()
            pending_at = _parse_utc_iso(pending_row["updated_at"] if pending_row else None)
            is_stale = not pending_at or (
                datetime.now(timezone.utc).replace(tzinfo=None) - pending_at
            ).total_seconds() > TRIAL_PENDING_TIMEOUT_SECONDS
            if not is_stale:
                conn.rollback()
                return jsonify({"error": "trial_busy", "message": "上一条消息仍在生成中"}), 409
            conn.execute(
                "UPDATE square_trial_requests SET status = 'failed', updated_at = ? WHERE request_id = ?",
                (now, pending),
            )

        recent_count = conn.execute(
            """
            SELECT COUNT(*) FROM square_trial_requests
            WHERE user_id = ? AND created_at >= ?
            """,
            (user_id, cutoff),
        ).fetchone()[0]
        if recent_count >= TRIAL_RATE_LIMIT_PER_MINUTE:
            conn.rollback()
            return jsonify({"error": "rate_limited", "message": "试聊请求过于频繁，请稍后再试"}), 429

        history = conn.execute(
            """
            SELECT role, content FROM square_trial_messages
            WHERE user_id = ? AND character_id = ? AND status = 'completed'
            ORDER BY id ASC
            """,
            (user_id, char_id),
        ).fetchall()
        turn_number = completed_turns + 1
        conn.execute(
            """
            INSERT INTO square_trial_requests (
                request_id, user_id, character_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (request_id, user_id, char_id, now, now),
        )
        conn.execute(
            """
            UPDATE square_trial_sessions
            SET pending_request = ?, updated_at = ?
            WHERE user_id = ? AND character_id = ?
            """,
            (request_id, now, user_id, char_id),
        )
        conn.commit()

    model_name = os.getenv("SQUARE_TRIAL_GEMINI_MODEL", "gemini-2.5-flash")
    messages = _build_trial_messages(character, relationship, history, user_message)
    reply_raw = call_gemini(
        messages,
        char_id=f"square_trial:{char_id}",
        model_name=model_name,
        user_id=TRIAL_ADMIN_USER_ID,
        max_tokens=1200,
    )
    if _trial_ai_error(reply_raw):
        _mark_trial_request_failed(user_id, char_id, request_id)
        return jsonify({
            "error": "generation_failed",
            "message": str(reply_raw or "角色暂时没有回复，请稍后重试"),
        }), 503
    reply = _sanitize_trial_reply(reply_raw)
    if not reply:
        _mark_trial_request_failed(user_id, char_id, request_id)
        return jsonify({"error": "generation_failed", "message": "回复为空，请重试"}), 503

    user_created_at = _utc_iso_now()
    assistant_created_at = _utc_iso_now()
    with sqlite3.connect(SQUARE_DB, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_trial_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        trial = conn.execute(
            "SELECT * FROM square_trial_sessions WHERE user_id = ? AND character_id = ?",
            (user_id, char_id),
        ).fetchone()
        if not trial or trial["pending_request"] != request_id:
            conn.rollback()
            return jsonify({"error": "trial_state_changed", "message": "试聊状态已变化，请刷新页面"}), 409
        conn.executemany(
            """
            INSERT INTO square_trial_messages (
                user_id, character_id, turn_number, request_id,
                role, content, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?)
            """,
            [
                (user_id, char_id, turn_number, request_id, "user", user_message, user_created_at),
                (user_id, char_id, turn_number, request_id, "assistant", reply, assistant_created_at),
            ],
        )
        conn.execute(
            """
            UPDATE square_trial_sessions
            SET completed_turns = completed_turns + 1,
                pending_request = NULL,
                updated_at = ?
            WHERE user_id = ? AND character_id = ?
            """,
            (assistant_created_at, user_id, char_id),
        )
        conn.execute(
            "UPDATE square_trial_requests SET status = 'completed', updated_at = ? WHERE request_id = ?",
            (assistant_created_at, request_id),
        )
        completed_turns = int(trial["completed_turns"] or 0) + 1
        conn.commit()

    return jsonify({
        "status": "success",
        "turn": turn_number,
        "remaining_turns": max(0, TRIAL_MAX_TURNS - completed_turns),
        "user_message": {
            "turn_number": turn_number,
            "role": "user",
            "content": user_message,
            "created_at": user_created_at,
        },
        "assistant_message": {
            "turn_number": turn_number,
            "role": "assistant",
            "content": reply,
            "created_at": assistant_created_at,
        },
        "trial_completed": completed_turns >= TRIAL_MAX_TURNS,
    })

@square_bp.route("/api/square/like", methods=["POST"])
def api_square_like():
    user_id = get_current_user_id()
    if not user_id: return jsonify({"error": "请先登录"}), 401
    char_id = request.json.get("id")
    action = request.json.get("action", "toggle") # toggle, add, remove
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM likes WHERE user_id = ? AND character_id = ?", (user_id, char_id))
        exists = cur.fetchone()

        if exists:
            if action in ["toggle", "remove"]:
                cur.execute("DELETE FROM likes WHERE user_id = ? AND character_id = ?", (user_id, char_id))
                cur.execute("UPDATE characters SET likes_count = MAX(0, likes_count - 1) WHERE id = ?", (char_id,))
                cur.execute("SELECT ip FROM characters WHERE id = ?", (char_id,))
                row = cur.fetchone()
                if row and row[0]:
                    cur.execute("UPDATE ips SET heat = MAX(0, heat - 1) WHERE name = ?", (row[0],))
                status = "removed"
            else: status = "already_exists"
        else:
            if action in ["toggle", "add"]:
                cur.execute("INSERT INTO likes (user_id, character_id) VALUES (?, ?)", (user_id, char_id))
                cur.execute("UPDATE characters SET likes_count = likes_count + 1 WHERE id = ?", (char_id,))
                cur.execute("SELECT ip FROM characters WHERE id = ?", (char_id,))
                row = cur.fetchone()
                if row and row[0]:
                    cur.execute("UPDATE ips SET heat = heat + 1 WHERE name = ?", (row[0],))
                status = "added"
            else: status = "not_found"

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "like_status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/favorite", methods=["POST"])
def api_square_favorite():
    user_id = get_current_user_id()
    if not user_id: return jsonify({"error": "请先登录"}), 401
    char_id = request.json.get("id")
    action = request.json.get("action", "toggle") # toggle, add, remove
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM favorites WHERE user_id = ? AND character_id = ?", (user_id, char_id))
        exists = cur.fetchone()

        if exists:
            if action in ["toggle", "remove"]:
                cur.execute("DELETE FROM favorites WHERE user_id = ? AND character_id = ?", (user_id, char_id))
                cur.execute("UPDATE characters SET favorites_count = MAX(0, favorites_count - 1) WHERE id = ?", (char_id,))
                status = "removed"
            else: status = "already_exists"
        else:
            if action in ["toggle", "add"]:
                cur.execute("INSERT INTO favorites (user_id, character_id) VALUES (?, ?)", (user_id, char_id))
                cur.execute("UPDATE characters SET favorites_count = favorites_count + 1 WHERE id = ?", (char_id,))
                status = "added"
            else: status = "not_found"

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "favorite_status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/favorites/list")
def api_square_favorites_list():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify([])
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        # 关联 favorites 表和 characters 表
        query = """
            SELECT c.id, c.name, c.avatar, c.ip, c.likes_count, c.tags
            FROM favorites f
            JOIN characters c ON f.character_id = c.id
            WHERE f.user_id = ?
            ORDER BY c.likes_count DESC
        """
        cur.execute(query, (user_id,))
        rows = cur.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "name": r[1], "avatar": r[2],
            "ip": r[3], "likes": r[4], "tags": r[5]
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/my_posts")
def api_square_my_posts():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify([])
    try:
        # 获取用户邮箱
        author_email = ""
        conn_u = sqlite3.connect(USERS_DB)
        cur_u = conn_u.cursor()
        cur_u.execute("SELECT email FROM users WHERE id = ?", (user_id,))
        row = cur_u.fetchone()
        if row: author_email = row[0]
        conn_u.close()

        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        _ensure_square_character_columns(conn)
        query = """
            SELECT id, name, avatar, ip, likes_count, tags FROM characters
            WHERE author_user_id = ? OR (author_user_id IS NULL AND author_email = ?)
            ORDER BY created_at DESC
        """
        cur.execute(query, (user_id, author_email))
        rows = cur.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "name": r[1], "avatar": r[2],
            "ip": r[3], "likes": r[4], "tags": r[5]
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/delete", methods=["POST"])
def api_square_delete():
    user_id = get_current_user_id()
    if not user_id: return jsonify({"error": "请先登录"}), 401
    char_id = request.json.get("id")

    try:
        # 鉴权：检查是否是作者
        conn_u = sqlite3.connect(USERS_DB)
        cur_u = conn_u.cursor()
        cur_u.execute("SELECT email FROM users WHERE id = ?", (user_id,))
        u_row = cur_u.fetchone()
        author_email = u_row[0] if u_row else ""
        conn_u.close()

        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        conn.row_factory = sqlite3.Row
        _ensure_square_character_columns(conn)
        cur = conn.cursor()
        cur.execute("SELECT ip, author_email, author_user_id FROM characters WHERE id = ?", (char_id,))
        c_row = cur.fetchone()

        if not c_row:
            conn.close()
            return jsonify({"error": "角色不存在"}), 404

        if not _is_square_author(c_row, user_id, author_email):
            conn.close()
            return jsonify({"error": "无权删除他人作品"}), 403

        ip = c_row["ip"]
        # 执行删除
        cur.execute("DELETE FROM characters WHERE id = ?", (char_id,))
        cur.execute("DELETE FROM likes WHERE character_id = ?", (char_id,))
        cur.execute("DELETE FROM favorites WHERE character_id = ?", (char_id,))
        cur.execute("DELETE FROM comments WHERE character_id = ?", (char_id,))
        _ensure_trial_tables(conn)
        cur.execute("DELETE FROM square_trial_messages WHERE character_id = ?", (char_id,))
        cur.execute("DELETE FROM square_trial_requests WHERE character_id = ?", (char_id,))
        cur.execute("DELETE FROM square_trial_sessions WHERE character_id = ?", (char_id,))

        # 更新 IP 表计数
        if ip:
            cur.execute("UPDATE ips SET character_count = MAX(0, character_count - 1) WHERE name = ?", (ip,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/comment", methods=["POST"])
def api_square_comment():
    char_id = request.json.get("id")
    content = request.json.get("content", "").strip()
    if not content: return jsonify({"error": "内容不能为空"}), 400
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        cur.execute("INSERT INTO comments (character_id, content, created_at) VALUES (?, ?, ?)",
                    (char_id, content, datetime.now().isoformat()))
        cur.execute("UPDATE characters SET comment_count = comment_count + 1 WHERE id = ?", (char_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@square_bp.route("/api/square/update", methods=["POST"])
def api_square_update():
    user_id = get_current_user_id()
    if not user_id: return jsonify({"error": "请先登录"}), 401

    data = request.form
    char_id = data.get("id") # 这里的 ID 是不允许改的

    try:
        # 鉴权
        conn_u = sqlite3.connect(USERS_DB)
        cur_u = conn_u.cursor()
        cur_u.execute("SELECT email FROM users WHERE id = ?", (user_id,))
        u_row = cur_u.fetchone()
        author_email = u_row[0] if u_row else ""
        conn_u.close()

        conn = sqlite3.connect(SQUARE_DB)
        conn.row_factory = sqlite3.Row
        _ensure_square_character_columns(conn)
        cur = conn.cursor()
        cur.execute("""
            SELECT avatar, ip, author_email, author_user_id, source_character_id
            FROM characters WHERE id = ?
        """, (char_id,))
        c_row = cur.fetchone()

        if not c_row:
            conn.close()
            return jsonify({"error": "角色不存在"}), 404
        if not _is_square_author(c_row, user_id, author_email):
            conn.close()
            return jsonify({"error": "无权修改他人作品"}), 403

        old_avatar = c_row["avatar"]
        old_ip = c_row["ip"]

        # 准备更新的数据
        name = data.get("name")
        age = data.get("age")
        no_age_increase = 1 if data.get("no_age_increase") == "true" else 0
        new_ip = data.get("ip", "").strip()
        tags_raw = data.get("tags", "").strip()
        import re
        tags = ",".join([t.strip() for t in re.split(r'[,，\s]+', tags_raw) if t.strip()])
        relationship_graph = data.get("relationship_graph", "{}")
        try:
            from blueprints.chat import normalize_relationship_graph
            relationship_graph = json.dumps(
                normalize_relationship_graph(relationship_graph),
                ensure_ascii=False,
                indent=2,
            )
        except (ValueError, json.JSONDecodeError) as e:
            conn.close()
            return jsonify({"error": f"关系图谱 JSON 无效: {e}"}), 400
        base_persona = data.get("base_persona", "")
        try:
            validate_persona_locks(base_persona)
        except PersonaLockError as e:
            conn.close()
            return jsonify({"error": f"人设 LOCK 标签格式错误：{e}", "code": e.code}), 400
        requested_source_character_id = data.get("source_character_id", "").strip()
        source_character_id = (
            requested_source_character_id or c_row["source_character_id"] or None
        )
        if requested_source_character_id:
            try:
                _load_local_publish_snapshot(user_id, requested_source_character_id)
            except ValueError as e:
                conn.close()
                return jsonify({"error": str(e)}), 400
            except FileNotFoundError as e:
                conn.close()
                return jsonify({"error": str(e)}), 404
            linked_id = _linked_square_id(
                conn, user_id, author_email, requested_source_character_id, exclude_id=char_id
            )
            if linked_id:
                conn.close()
                return jsonify({
                    "error": f"该本地角色已经关联广场作品 {linked_id}",
                    "square_id": linked_id,
                }), 409

        uploaded_avatar = request.files.get('avatar')
        try:
            avatar_url = _materialize_square_avatar(
                char_id,
                user_id,
                uploaded_file=uploaded_avatar,
                avatar_url=data.get("avatar_url"),
                current_avatar=old_avatar,
            )
        except SquareAvatarError as e:
            conn.close()
            return jsonify({"error": str(e), "code": "square_avatar_unavailable"}), 422

        # 更新
        cur.execute("""
            UPDATE characters SET
            name=?, avatar=?, age=?, no_age_increase=?, base_persona=?,
            relationship_graph=?, tags=?, ip=?, author_user_id=?,
            source_character_id=?, updated_at=?
            WHERE id=?
        """, (
            name, avatar_url, age, no_age_increase, base_persona,
            relationship_graph, tags, new_ip, user_id,
            source_character_id, datetime.now().isoformat(), char_id,
        ))

        # 更新 IP 表（如果 IP 变了）
        if old_ip != new_ip:
            if old_ip: cur.execute("UPDATE ips SET character_count = MAX(0, character_count - 1) WHERE name = ?", (old_ip,))
            if new_ip:
                cur.execute("SELECT name FROM ips WHERE name = ?", (new_ip,))
                if cur.fetchone(): cur.execute("UPDATE ips SET character_count = character_count + 1 WHERE name = ?", (new_ip,))
                else: cur.execute("INSERT INTO ips (name, heat, character_count) VALUES (?, 0, 1)", (new_ip,))

        conn.commit()
        conn.close()
        _save_local_square_link(user_id, source_character_id, char_id)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/add_to_local", methods=["POST"])
def api_square_add_to_local():
    from app import init_char_db

    user_id = get_current_user_id()
    if not user_id: return jsonify({"error": "请先登录"}), 401
    payload = request.get_json(silent=True) or {}
    char_id = str(payload.get("id") or "").strip()
    import_trial_history = payload.get("import_trial_history", True) is not False
    if not char_id:
        return jsonify({"error": "角色ID不能为空"}), 400
    try:
        conn = sqlite3.connect(SQUARE_DB)
        conn.row_factory = sqlite3.Row
        _ensure_trial_tables(conn)
        cur = conn.cursor()
        columns = [
            "id", "name", "avatar", "age", "no_age_increase",
            "base_persona", "relationship_graph", "tags", "ip", "author_email"
        ]
        cur.execute(f"SELECT {', '.join(columns)} FROM characters WHERE id = ?", (char_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "角色不存在"}), 404

        # 使用字典映射
        s = dict(row)
        trial = conn.execute(
            "SELECT * FROM square_trial_sessions WHERE user_id = ? AND character_id = ?",
            (user_id, char_id),
        ).fetchone()
        trial_relationship = _trial_relationship_from_row(trial)
        trial_messages = conn.execute(
            """
            SELECT role, content, created_at
            FROM square_trial_messages
            WHERE user_id = ? AND character_id = ? AND status = 'completed'
            ORDER BY id ASC
            """,
            (user_id, char_id),
        ).fetchall()
        conn.close()

        cfg_file = _get_characters_config_file()
        all_config = {}
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)

        existing_local_id = next(
            (
                local_char_id for local_char_id, info in all_config.items()
                if isinstance(info, dict) and info.get("square_origin_id") == s["id"]
            ),
            None,
        )
        if existing_local_id:
            return jsonify({
                "status": "already_added",
                "local_id": existing_local_id,
                "trial_history_imported": bool(
                    all_config[existing_local_id].get("square_trial_history_imported")
                ),
            })

        local_id = s["id"]
        if local_id in all_config:
            local_id = f"{s['id']}_sq"
            counter = 1
            while local_id in all_config:
                local_id = f"{s['id']}_sq_{counter}"
                counter += 1

        char_root = os.path.join(USERS_ROOT, str(user_id), "characters")
        target_char_dir = os.path.join(char_root, local_id)
        target_prompts_dir = os.path.join(target_char_dir, "prompts")
        os.makedirs(target_prompts_dir, exist_ok=True)
        init_char_db(local_id)

        safe_save_json(
            os.path.join(target_prompts_dir, "1_base_persona.json"),
            {"system_prompt": s["base_persona"] or ""},
        )
        from blueprints.chat import normalize_relationship_graph
        try:
            relationship_graph = normalize_relationship_graph(s["relationship_graph"] or {})
        except (ValueError, json.JSONDecodeError):
            relationship_graph = {}
        if trial_relationship:
            relationship_graph[str(user_id)] = trial_relationship
        safe_save_json(
            os.path.join(target_prompts_dir, "2_relationship.json"),
            relationship_graph,
        )

        for fn in ["4_memory_long.json", "5_memory_medium.json", "6_memory_short.json", "7_schedule.json"]:
            with open(os.path.join(target_prompts_dir, fn), "w", encoding="utf-8") as f:
                if fn == "7_schedule.json":
                    json.dump({"_undated": []}, f, ensure_ascii=False, indent=2)
                else:
                    f.write("{}" if fn.endswith(".json") else "")

        imported_message_count = 0
        if import_trial_history and trial_messages:
            db_path, _ = get_paths(local_id, user_id=user_id)
            with sqlite3.connect(db_path) as chat_conn:
                chat_conn.execute("BEGIN IMMEDIATE")
                for message in trial_messages:
                    chat_conn.execute(
                        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                        (
                            message["role"],
                            message["content"],
                            _trial_timestamp_for_chat(message["created_at"]),
                        ),
                    )
                    imported_message_count += 1
                chat_conn.commit()

        all_config[local_id] = {
            "name": s["name"], "remark": s["name"], "avatar": s["avatar"], "pinned": False,
            "emotion": 1, "light_sleep": True, "deep_sleep": False,
            "ds_start": "23:00", "ds_end": "07:00", "square_origin_id": s["id"],
            "timezone": "Asia/Shanghai", "timezone_source": "system_default",
            "ds_time_basis": "character", "ds_set_by": "default",
            "ds_timezone_at_set": "Asia/Shanghai",
            "deep_sleep_source": "schedule", "sleep_manual_override": False,
            "age": s["age"], "no_age_increase": bool(s["no_age_increase"]),
            "square_trial_history_imported": imported_message_count > 0,
            "square_trial_history_imported_at": _utc_iso_now() if imported_message_count > 0 else None,
            "square_trial_completed_turns": int(trial["completed_turns"] or 0) if trial else 0,
        }
        safe_save_json(cfg_file, all_config)
        if imported_message_count > 0:
            with sqlite3.connect(SQUARE_DB) as conn:
                _ensure_trial_tables(conn)
                conn.execute(
                    """
                    UPDATE square_trial_sessions
                    SET imported_local_id = ?, imported_at = ?, updated_at = ?
                    WHERE user_id = ? AND character_id = ?
                    """,
                    (local_id, _utc_iso_now(), _utc_iso_now(), user_id, char_id),
                )
                conn.commit()
        return jsonify({
            "status": "success",
            "local_id": local_id,
            "imported_message_count": imported_message_count,
            "relationship_imported": bool(trial_relationship),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/ai_complete_graph", methods=["POST"])
def api_square_ai_complete_graph():
    # 复用 generate_persona 的逻辑，但返回关系图谱
    data = request.json
    name = data.get("name")
    ip = data.get("ip")
    tags = data.get("tags")
    current_graph = data.get("current_graph", "{}")

    # 构造 Prompt
    prompt = f"你是一个角色设定专家。请为角色「{name}」（来自作品「{ip}」，标签「{tags}」）补全或优化人际关系图谱。\n"
    prompt += f"角色当前的已有关系图谱如下：\n{current_graph}\n\n"
    prompt += "要求：\n1. 基于原作设定补全缺失的关键角色，或优化现有描述。\n"
    prompt += "2. 返回一个纯JSON对象，键是人名，值是一个包含以下字段的对象：\n"
    prompt += "- role: 关系定位 (如: 队友/劲敌/青梅竹马)\n"
    prompt += "- score: 关系指数 (0-5的数字，表示关系紧密度)\n"
    prompt += "- description: 详细的关系描述\n"
    prompt += "3. 请合并已有数据和新生成的数据，返回一个完整的最终结果。\n"
    prompt += "4. 只返回JSON，不要有任何解释文字。"

    try:
        return _call_llm_for_graph(prompt)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _call_llm_for_graph(prompt):
    from app import call_openrouter, call_gemini, get_model_config

    messages = [{"role": "user", "content": prompt}]
    try:
        # 使用项目统一的模型配置逻辑
        route, current_model = get_model_config("gen_persona")

        if route == "relay":
            response_text = call_openrouter(messages, model_name=current_model)
        else:
            response_text = call_gemini(messages, model_name=current_model)

        clean_json = response_text.strip()
        # 移除 Markdown 代码块包裹
        if clean_json.startswith("```"):
            clean_json = re.sub(r'^```(?:json)?\s*|\s*```$', '', clean_json, flags=re.MULTILINE).strip()

        # 尝试解析校验一下是否是合法 JSON
        try:
            parsed_graph = json.loads(clean_json)
            # 如果成功解析，确保它是对象格式直接返回
            return jsonify({"status": "success", "graph": parsed_graph})
        except:
            # 如果不是合法 JSON，尝试提取第一个 { 到最后一个 }
            start = clean_json.find('{')
            end = clean_json.rfind('}')
            if start != -1 and end != -1:
                clean_json_extracted = clean_json[start:end+1]
                try:
                    parsed_graph = json.loads(clean_json_extracted)
                    return jsonify({"status": "success", "graph": parsed_graph})
                except:
                    # 如果仍然失败，返回原始 clean_json 但放在 graph 字段供前端处理
                    pass

        return jsonify({"status": "success", "graph": clean_json})
    except Exception as e:
        print(f"Graph LLM Call Error: {e}")
        return jsonify({"error": str(e)}), 500

def generate_unique_square_id(base_id):
    import re
    if not re.match(r'^[a-zA-Z0-9_]+$', base_id):
        base_id = "char"
    conn = sqlite3.connect(SQUARE_DB)
    cur = conn.cursor()
    candidate = base_id
    counter = 1
    while True:
        cur.execute("SELECT id FROM characters WHERE id = ?", (candidate,))
        if not cur.fetchone():
            break
        candidate = f"{base_id}_{counter}"
        counter += 1
    conn.close()
    return candidate
