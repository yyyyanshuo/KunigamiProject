import os
import time
import re
import json
import sqlite3
from datetime import datetime
from urllib.parse import unquote, urlparse
from flask import Blueprint, request, jsonify, session, render_template
from PIL import Image
from cos_utils import upload_to_cos
from core.config import SQUARE_DB, SQUARE_AVATARS_DIR, USERS_DB, USERS_ROOT
from core.context import get_current_user_id
from core.utils import get_paths, safe_save_json, _get_characters_config_file

square_bp = Blueprint('square', __name__)


def _find_private_avatar_file(user_id, avatar_url):
    """Resolve a /char_assets/... URL to the current user's local avatar file."""
    if not user_id or not avatar_url:
        return None

    parsed = urlparse(avatar_url)
    path = unquote(parsed.path or "")
    prefix = "/char_assets/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix):].split("/", 1)
    if len(parts) != 2:
        return None

    private_char_id, filename = parts
    char_dir = os.path.join(USERS_ROOT, str(user_id), "characters", private_char_id)
    exact_path = os.path.join(char_dir, filename)
    if os.path.isfile(exact_path):
        return exact_path

    for candidate in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp", "avatar.gif"):
        candidate_path = os.path.join(char_dir, candidate)
        if os.path.isfile(candidate_path):
            return candidate_path
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
    return f"/static/square_avatars/{filename}"


def _materialize_square_avatar(square_id, user_id, uploaded_file=None, avatar_url=None, current_avatar=None):
    """
    Store square avatars as public square assets.
    Private /char_assets/... URLs are copied from the author's local character folder.
    """
    if uploaded_file and getattr(uploaded_file, "filename", ""):
        try:
            return _save_square_avatar_image(uploaded_file.stream, square_id)
        except Exception as e:
            print(f"Square Avatar Upload Error: {e}")
            return current_avatar or "/static/default_avatar.png"

    source_url = (avatar_url or current_avatar or "").strip()
    if not source_url:
        return "/static/default_avatar.png"

    if urlparse(source_url).path.startswith("/char_assets/"):
        source_file = _find_private_avatar_file(user_id, source_url)
        if source_file:
            try:
                return _save_square_avatar_image(source_file, square_id, "square_import")
            except Exception as e:
                print(f"Square Avatar Import Error: {e}")
        return "/static/default_avatar.png"

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
            likes_count INTEGER DEFAULT 0,
            favorites_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
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

@square_bp.route("/api/square/ips")
def api_square_ips():
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        cur.execute("SELECT name, heat, character_count FROM ips ORDER BY heat DESC")
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

@square_bp.route("/api/square/upload", methods=["POST"])
def api_square_upload():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    # 获取作者邮箱
    author_email = ""
    try:
        conn_u = sqlite3.connect(USERS_DB)
        cur_u = conn_u.cursor()
        cur_u.execute("SELECT email FROM users WHERE id = ?", (user_id,))
        row = cur_u.fetchone()
        if row:
            author_email = row[0]
        conn_u.close()
    except:
        pass

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
    base_persona = data.get("base_persona", "").strip()

    if not char_id_base or not name:
        return jsonify({"error": "ID和名称不能为空"}), 400

    # 生成唯一 ID
    final_id = generate_unique_square_id(char_id_base)

    # 头像处理：广场头像必须固化为公共资源，不能保存 /char_assets/... 私有路由
    uploaded_avatar = request.files.get('avatar')
    avatar_url = _materialize_square_avatar(
        final_id,
        user_id,
        uploaded_file=uploaded_avatar,
        avatar_url=data.get("avatar_url"),
    )

    # 写入数据库
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO characters (id, name, avatar, age, no_age_increase, base_persona, relationship_graph, tags, ip, author_email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (final_id, name, avatar_url, age, no_age_increase, base_persona, relationship_graph, tags, ip, author_email, datetime.now().isoformat()))

        # 更新 IP 表
        if ip:
            cur.execute("SELECT name FROM ips WHERE name = ?", (ip,))
            if cur.fetchone():
                cur.execute("UPDATE ips SET character_count = character_count + 1 WHERE name = ?", (ip,))
            else:
                cur.execute("INSERT INTO ips (name, heat, character_count) VALUES (?, 0, 1)", (ip,))

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "id": final_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/character/<char_id>")
def api_square_character_detail(char_id):
    try:
        conn = sqlite3.connect(SQUARE_DB)
        conn.row_factory = sqlite3.Row  # 使用 Row 模式，通过列名访问
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
        cur.execute("SELECT id, name, avatar FROM characters WHERE author_email = ? AND id != ?", (char_data["author_email"], char_id))
        other_chars = [{"id": r["id"], "name": r["name"], "avatar": r["avatar"]} for r in cur.fetchall()]

        # 检查点赞/收藏状态
        is_liked = False
        is_favorited = False
        is_author = False
        user_id = get_current_user_id()

        if user_id:
            # 检查收藏
            cur.execute("SELECT 1 FROM favorites WHERE user_id = ? AND character_id = ?", (user_id, char_id))
            if cur.fetchone(): is_favorited = True

            # 检查点赞
            cur.execute("SELECT 1 FROM likes WHERE user_id = ? AND character_id = ?", (user_id, char_id))
            if cur.fetchone(): is_liked = True

            # 检查作者
            conn_u = sqlite3.connect(USERS_DB)
            cur_u = conn_u.cursor()
            cur_u.execute("SELECT email FROM users WHERE id = ?", (user_id,))
            u_row = cur_u.fetchone()
            if u_row and u_row[0] == char_data["author_email"]:
                is_author = True
            conn_u.close()

        conn.close()
        return jsonify({
            "character": char_data,
            "comments": comments,
            "other_characters": other_chars,
            "is_favorited": is_favorited,
            "is_liked": is_liked,
            "is_author": is_author
        })
    except Exception as e:
        print(f"Detail API Error: {e}")
        return jsonify({"error": str(e)}), 500

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

        if not author_email: return jsonify([])

        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        query = "SELECT id, name, avatar, ip, likes_count, tags FROM characters WHERE author_email = ? ORDER BY created_at DESC"
        cur.execute(query, (author_email,))
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
        cur.execute("SELECT ip, author_email FROM characters WHERE id = ?", (char_id,))
        c_row = cur.fetchone()

        if not c_row:
            conn.close()
            return jsonify({"error": "角色不存在"}), 404

        if c_row[1] != author_email:
            conn.close()
            return jsonify({"error": "无权删除他人作品"}), 403

        ip = c_row[0]
        # 执行删除
        cur.execute("DELETE FROM characters WHERE id = ?", (char_id,))
        cur.execute("DELETE FROM likes WHERE character_id = ?", (char_id,))
        cur.execute("DELETE FROM favorites WHERE character_id = ?", (char_id,))
        cur.execute("DELETE FROM comments WHERE character_id = ?", (char_id,))

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
        cur = conn.cursor()
        cur.execute("SELECT avatar, ip, author_email FROM characters WHERE id = ?", (char_id,))
        c_row = cur.fetchone()

        if not c_row:
            conn.close()
            return jsonify({"error": "角色不存在"}), 404
        if c_row[2] != author_email:
            conn.close()
            return jsonify({"error": "无权修改他人作品"}), 403

        old_avatar = c_row[0]
        old_ip = c_row[1]

        # 准备更新的数据
        name = data.get("name")
        age = data.get("age")
        no_age_increase = 1 if data.get("no_age_increase") == "true" else 0
        new_ip = data.get("ip", "").strip()
        tags_raw = data.get("tags", "").strip()
        import re
        tags = ",".join([t.strip() for t in re.split(r'[,，\s]+', tags_raw) if t.strip()])
        relationship_graph = data.get("relationship_graph", "{}")
        base_persona = data.get("base_persona", "")

        uploaded_avatar = request.files.get('avatar')
        avatar_url = _materialize_square_avatar(
            char_id,
            user_id,
            uploaded_file=uploaded_avatar,
            avatar_url=data.get("avatar_url"),
            current_avatar=old_avatar,
        )

        # 更新
        cur.execute("""
            UPDATE characters SET
            name=?, avatar=?, age=?, no_age_increase=?, base_persona=?,
            relationship_graph=?, tags=?, ip=?
            WHERE id=?
        """, (name, avatar_url, age, no_age_increase, base_persona, relationship_graph, tags, new_ip, char_id))

        # 更新 IP 表（如果 IP 变了）
        if old_ip != new_ip:
            if old_ip: cur.execute("UPDATE ips SET character_count = MAX(0, character_count - 1) WHERE name = ?", (old_ip,))
            if new_ip:
                cur.execute("SELECT name FROM ips WHERE name = ?", (new_ip,))
                if cur.fetchone(): cur.execute("UPDATE ips SET character_count = character_count + 1 WHERE name = ?", (new_ip,))
                else: cur.execute("INSERT INTO ips (name, heat, character_count) VALUES (?, 0, 1)", (new_ip,))

        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@square_bp.route("/api/square/add_to_local", methods=["POST"])
def api_square_add_to_local():
    from app import init_char_db

    user_id = get_current_user_id()
    if not user_id: return jsonify({"error": "请先登录"}), 401
    char_id = request.json.get("id")
    try:
        conn = sqlite3.connect(SQUARE_DB)
        cur = conn.cursor()
        columns = [
            "id", "name", "avatar", "age", "no_age_increase",
            "base_persona", "relationship_graph", "tags", "ip", "author_email"
        ]
        cur.execute(f"SELECT {', '.join(columns)} FROM characters WHERE id = ?", (char_id,))
        row = cur.fetchone()
        conn.close()
        if not row: return jsonify({"error": "角色不存在"}), 404

        # 使用字典映射
        s = dict(zip(columns, row))

        cfg_file = _get_characters_config_file()
        all_config = {}
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)

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

        with open(os.path.join(target_prompts_dir, "1_base_persona.md"), "w", encoding="utf-8") as f:
            f.write(s["base_persona"] or "")
        with open(os.path.join(target_prompts_dir, "2_relationship.json"), "w", encoding="utf-8") as f:
            f.write(s["relationship_graph"] or "{}")

        for fn in ["3_user_persona.md", "4_memory_long.json", "5_memory_medium.json", "6_memory_short.json", "7_schedule.json"]:
            with open(os.path.join(target_prompts_dir, fn), "w", encoding="utf-8") as f:
                f.write("{}" if fn.endswith(".json") else "")

        all_config[local_id] = {
            "name": s["name"], "remark": s["name"], "avatar": s["avatar"], "pinned": False,
            "emotion": 1, "light_sleep": True, "deep_sleep": False,
            "ds_start": "23:00", "ds_end": "07:00", "square_origin_id": s["id"],
            "age": s["age"], "no_age_increase": bool(s["no_age_increase"])
        }
        safe_save_json(cfg_file, all_config)
        return jsonify({"status": "success", "local_id": local_id})
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
