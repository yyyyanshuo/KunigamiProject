import os
import time
import re
import json
import random
import threading
import uuid
import io
import shutil
import sqlite3
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify, send_from_directory, send_file, render_template, session, redirect, url_for
from urllib.parse import quote as url_quote, urlparse
from PIL import Image, ImageOps

import core.config
from core.context import get_current_user_id, set_background_user
from core.utils import (
    _add_furigana_to_japanese, get_paths, get_current_username,
    _get_characters_config_file, get_effective_gemini_key,
)
from cos_utils import upload_to_cos, get_cos_list
from agent_utils import parse_music_tags
import music_api
import music_manager

media_bp = Blueprint('media', __name__)

# ==================== 表情库路径常量 ====================
STICKERS_ROOT = os.path.join(core.config.BASE_DIR, "stickers")
STICKER_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
STICKER_DESCRIPTIONS_FILE = os.path.join(core.config.BASE_DIR, "configs", "sticker_descriptions_sorted.txt")


# ==================== Sticker 辅助函数 ====================

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


def _get_stickers_upload_dir() -> str:
    uid = get_current_user_id()
    if not uid:
        return ""
    d = os.path.join(core.config.USERS_ROOT, str(uid), "sticker_uploads")
    os.makedirs(d, exist_ok=True)
    return d


def _get_stickers_favorites_file() -> str:
    uid = get_current_user_id()
    if not uid:
        return ""
    cfg_dir = os.path.join(core.config.USERS_ROOT, str(uid), "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "stickers_favorites.json")


def _get_added_sticker_packs_file() -> str:
    uid = get_current_user_id()
    if not uid:
        return ""
    cfg_dir = os.path.join(core.config.USERS_ROOT, str(uid), "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "added_sticker_packs.json")


def _load_added_sticker_packs() -> list:
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
    return path


def _stickers_relative_to_url(path: str) -> str:
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
    if not content or "[表情]" not in content:
        return content
    def repl(m):
        path = m.group(1).strip()
        name = _sticker_path_to_name(path)
        return f"[表情]{name}" if name else m.group(0)
    return re.sub(r"\[表情\]([^\]]+)", repl, content)


def _resolve_sticker_name_to_path(name: str) -> str:
    name = (name or "").strip()
    items = _search_stickers(name)
    if not items:
        return ""
    return random.choice(items)["path"]


def _resolve_sticker_name_to_path_deterministic(name: str) -> str:
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
    if not content:
        return content

    if "[表情]" not in content:
        return content
    pattern = r"\[表情\](.*?)(?=\s*/\s*|$)"
    def repl(m):
        name_or_path = (m.group(1) or "").strip()
        if name_or_path.startswith("official:") or name_or_path.startswith("user:"):
            return m.group(0)
        path = _resolve_sticker_name_to_path(name_or_path)
        return f"[表情]{path}" if path else m.group(0)
    return re.sub(pattern, repl, content)


# ==================== Sticker API 基础设施 ====================

def _list_official_packs():
    if core.config.CACHED_OFFICIAL_PACKS is not None:
        return core.config.CACHED_OFFICIAL_PACKS

    try:
        folders = get_cos_list("stickers/", get_folders=True)
        core.config.CACHED_OFFICIAL_PACKS = folders
        return core.config.CACHED_OFFICIAL_PACKS
    except Exception as e:
        print(f"   [COS Error] Failed to list official packs: {e}")
        return []


COVER_BASENAME = "cover"
PACK_META_FILE = "meta.json"


def _get_pack_meta(pack_id):
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
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return None

    try:
        files = get_cos_list(f"stickers/{pack_id}/")
        for s in files:
            name_no_ext = os.path.splitext(s["name"])[0].lower()
            if name_no_ext == COVER_BASENAME:
                return s["url"]
    except Exception:
        pass
    return None


def _list_pack_stickers(pack_id):
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        return []

    out = []
    try:
        files = get_cos_list(f"stickers/{pack_id}/")
        for s in files:
            filename = s["name"]
            name_no_ext, ext = os.path.splitext(filename)

            if COVER_BASENAME in name_no_ext.lower():
                continue

            if ext.lower() in STICKER_IMAGE_EXT:
                path = f"official:{pack_id}:{filename}"
                out.append({
                    "path": path,
                    "name": name_no_ext,
                    "url": s["url"]
                })
    except Exception as e:
        print(f"   [COS Error] Failed to list stickers for {pack_id}: {e}")

    return out


def _search_stickers(q):
    q = (q or "").strip().lower()
    out = []
    for pack_id in _list_official_packs():
        for s in _list_pack_stickers(pack_id):
            if q in s["name"].lower():
                s = dict(s)
                s["pack_name"] = pack_id
                out.append(s)
    uid = get_current_user_id()
    if uid:
        cos_prefix = f"users/{uid}/sticker_uploads/"
        user_stickers = get_cos_list(cos_prefix)
        for s in user_stickers:
            name = s["name"]
            if q in name.lower():
                path = f"user:{name}"
                out.append({
                    "path": path,
                    "name": os.path.splitext(name)[0],
                    "url": s["url"],
                    "pack_name": "个人上传"
                })
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


# ==================== Sticker 路由 ====================

@media_bp.route("/api/stickers/allowed_descriptions", methods=["GET"])
def api_stickers_allowed_descriptions():
    return jsonify(_get_sticker_allowed_descriptions())


@media_bp.route("/api/stickers/packs", methods=["GET"])
def api_stickers_packs():
    os.makedirs(STICKERS_ROOT, exist_ok=True)
    packs = []
    for p in _list_official_packs():
        meta = _get_pack_meta(p)
        name = (meta.get("name") if meta else None) or p
        cover = _get_pack_cover_url(p)
        packs.append({"id": p, "name": name, "cover": cover})
    return jsonify(packs)


@media_bp.route("/api/stickers/my_packs", methods=["GET"])
def api_stickers_my_packs():
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


@media_bp.route("/api/stickers/packs/add", methods=["POST"])
def api_stickers_packs_add():
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


@media_bp.route("/api/stickers/pack/<pack_id>", methods=["GET"])
def api_stickers_pack(pack_id):
    meta = _get_pack_meta(pack_id)
    name = (meta.get("name") if meta else None) or pack_id
    stickers = _list_pack_stickers(pack_id)
    return jsonify({"name": name, "stickers": stickers})


@media_bp.route("/api/stickers/search", methods=["GET"])
def api_stickers_search():
    q = request.args.get("q", "").strip()
    items = _search_stickers(q)
    return jsonify(items)


@media_bp.route("/api/stickers/favorites", methods=["GET"])
def api_stickers_favorites_get():
    paths = _load_favorites()
    out = []
    uid = get_current_user_id()
    for path in paths:
        if path.startswith("user:") and uid:
            filename = path[5:].lstrip(":")
            found = _search_stickers(filename)
            match = next((item for item in found if item["path"] == path), None)
            if match:
                out.append(match)
            continue

        ab = _stickers_path_to_abs(path)
        if not ab or not os.path.isfile(ab):
            continue
        name = os.path.splitext(os.path.basename(ab))[0]
        out.append({"path": path, "name": name, "url": _stickers_relative_to_url(path)})
    return jsonify(out)


@media_bp.route("/api/stickers/favorites", methods=["POST"])
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


@media_bp.route("/api/stickers/favorites", methods=["DELETE"])
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
    s = (s or "").strip()
    s = re.sub(r'[<>:"/\\|?*\s]+', "_", s)
    s = s.strip("._") or "sticker"
    return s[:80]


@media_bp.route("/api/stickers/upload", methods=["POST"])
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
        user_id = get_current_user_id()
        if user_id:
            cos_path = f"users/{user_id}/sticker_uploads/{safe_name}"
            upload_to_cos(dest, cos_path)
            if os.path.exists(dest):
                os.remove(dest)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    path = f"user:{safe_name}"
    name = os.path.splitext(safe_name)[0]
    return jsonify({"path": path, "name": name, "url": _stickers_relative_to_url(path)})


@media_bp.route("/stickers/upload")
def sticker_upload_page():
    if not get_current_user_id():
        return redirect(url_for("login_view") + "?next=" + request.path)
    return render_template("sticker_upload.html")


@media_bp.route("/api/stickers/packs/upload", methods=["POST"])
def api_stickers_packs_upload():
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

    cover_local_path = os.path.join(pack_dir, COVER_BASENAME + cover_ext)
    cover.save(cover_local_path)
    upload_to_cos(cover_local_path, f"stickers/{pack_id}/{COVER_BASENAME + cover_ext}")

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
        upload_to_cos(dest, f"stickers/{pack_id}/{safe_name}")

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


@media_bp.route("/stickers/pack/<pack_id>")
def sticker_pack_detail_page(pack_id):
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


@media_bp.route("/api/stickers/file", methods=["GET"])
def api_stickers_file():
    path = request.args.get("path", "").strip()
    if not path:
        return "", 404
    if not path.startswith("official:") and not path.startswith("user:"):
        resolved = _resolve_sticker_name_to_path_deterministic(path)
        if resolved:
            path = resolved

    if path.startswith("user:"):
        uid = get_current_user_id()
        if uid:
            filename = path[5:].lstrip(":")
            ab = _stickers_path_to_abs(path)
            if ab and os.path.isfile(ab):
                response = send_from_directory(os.path.dirname(ab), os.path.basename(ab), as_attachment=False)
                response.headers["Access-Control-Allow-Origin"] = "*"
                return response
            if core.config.COS_BASE_URL:
                cos_url = f"{core.config.COS_BASE_URL}/users/{uid}/sticker_uploads/{filename}"
                return redirect(cos_url)

    if path.startswith("official:"):
        parts = path.split(":", 2)
        if len(parts) >= 3:
            pack_id, filename = parts[1], parts[2]
            ab = _stickers_path_to_abs(path)
            if ab and os.path.isfile(ab):
                response = send_from_directory(os.path.dirname(ab), os.path.basename(ab), as_attachment=False)
                response.headers["Access-Control-Allow-Origin"] = "*"
                return response
            if core.config.COS_BASE_URL:
                cos_url = f"{core.config.COS_BASE_URL}/stickers/{pack_id}/{filename}"
                return redirect(cos_url)

    ab = _stickers_path_to_abs(path)
    if ab and os.path.isfile(ab):
        response = send_from_directory(os.path.dirname(ab), os.path.basename(ab), as_attachment=False)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    return "Not Found", 404


@media_bp.route("/api/stickers/resolve", methods=["GET"])
def api_stickers_resolve():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"url": None})
    path = _resolve_sticker_name_to_path_deterministic(name)
    if not path:
        return jsonify({"url": None})
    url = _stickers_relative_to_url(path)
    name_display = _sticker_path_to_name(path)
    return jsonify({"url": url, "path": path, "name": name_display})


# ==================== 网易云音乐 API ====================

MUSIC_MODULE_PROMPT = """[系统] 你已进入音乐模式，可以使用以下网易云音乐操作标签：
- [MUSIC_SEARCH:关键词:数量(可选)] 搜索歌曲
- [MUSIC_PLAY:歌曲ID] 播放指定歌曲
- [MUSIC_PAUSE] 暂停播放
- [MUSIC_RESUME] 恢复播放
- [MUSIC_STOP] 停止播放
- [MUSIC_NEXT] 下一首 / [MUSIC_PREV] 上一首
- [MUSIC_PLAYLIST_LIST] 查看用户所有歌单
- [MUSIC_PLAYLIST_VIEW:歌单ID] 查看歌单详细内容
- [MUSIC_PLAYLIST_CREATE:歌单名称] 创建新歌单
- [MUSIC_PLAYLIST_ADD:歌单ID:歌曲ID] 添加歌曲到歌单
- [MUSIC_PLAYLIST_DELETE:歌单ID] 删除歌单
- [MUSIC_MODE_EXIT] 退出音乐模式
当前播放: {playing_info}
请根据用户的需求选择合适的操作。搜索后你会收到 [MUSIC_RESULT:...] 消息，再决定播放哪首。"""


def _get_user_id_for_request():
    return get_current_user_id()


@media_bp.route("/api/music/search", methods=["POST"])
def api_music_search():
    data = request.json or {}
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400
    limit = int(data.get("limit", 10))

    songs = music_api.search_songs(keyword, max(limit * 3, 30))

    if songs:
        song_ids = [s["net_ease_id"] for s in songs]
        url_map = music_api.batch_get_song_urls(song_ids)
        for s in songs:
            s["playable"] = bool(url_map.get(s["net_ease_id"], ""))
            s["audio_url"] = url_map.get(s["net_ease_id"], "")

    playable = [s for s in songs if s["playable"]]
    unplayable = [s for s in songs if not s["playable"]]

    result = playable[:limit]
    remaining = limit - len(result)
    if remaining > 0:
        result.extend(unplayable[:remaining])

    print(f"[Music] search '{keyword}': playable={len(playable)}/{len(songs)}, returning {len(result)}")
    return jsonify({
        "songs": result,
        "total_playable": len(playable),
        "total_searched": len(songs)
    })


@media_bp.route("/api/music/play", methods=["POST"])
def api_music_play():
    user_id = _get_user_id_for_request()
    data = request.json or {}
    song_id = data.get("song_id")
    if not song_id:
        return jsonify({"error": "song_id required"}), 400

    pre_url = data.get("audio_url", "")
    pre_title = data.get("title", "")
    pre_artist = data.get("artist", "")
    pre_cover = data.get("cover_url", "")
    pre_duration = data.get("duration", 0)

    if pre_url:
        song_info = {
            "net_ease_id": song_id,
            "title": pre_title,
            "artist": pre_artist,
            "album": "",
            "cover_url": pre_cover,
            "duration": pre_duration,
            "audio_url": pre_url,
            "lyric": ""
        }
        lyric = music_api.get_lyric(song_id)
        song_info["lyric"] = lyric or ""
    else:
        song_info = music_api.get_song_info(song_id)

    if not song_info.get("audio_url"):
        return jsonify({"error": "无法获取播放地址", "song": song_info}), 500

    music_manager.update_current_song(user_id, song_info)
    return jsonify({"success": True, "song": song_info})


@media_bp.route("/api/music/pause", methods=["POST"])
def api_music_pause():
    user_id = _get_user_id_for_request()
    music_manager.pause_playback(user_id)
    return jsonify({"success": True})


@media_bp.route("/api/music/resume", methods=["POST"])
def api_music_resume():
    user_id = _get_user_id_for_request()
    music_manager.resume_playback(user_id)
    return jsonify({"success": True})


@media_bp.route("/api/music/stop", methods=["POST"])
def api_music_stop():
    user_id = _get_user_id_for_request()
    music_manager.stop_playback(user_id)
    return jsonify({"success": True})


@media_bp.route("/api/music/next", methods=["POST"])
def api_music_next():
    user_id = _get_user_id_for_request()
    next_song = music_manager.play_next(user_id)
    if next_song:
        song_info = music_api.get_song_info(next_song.get("net_ease_id"))
        if song_info:
            music_manager.update_current_song(user_id, song_info)
            return jsonify({"success": True, "song": song_info})
    music_manager.stop_playback(user_id)
    return jsonify({"success": True, "song": None})


@media_bp.route("/api/music/prev", methods=["POST"])
def api_music_prev():
    user_id = _get_user_id_for_request()
    return jsonify({"success": True, "message": "上一首功能需要队列历史支持"})


@media_bp.route("/api/music/volume", methods=["POST"])
def api_music_volume():
    user_id = _get_user_id_for_request()
    data = request.json or {}
    vol = data.get("volume", 80)
    music_manager.set_volume(user_id, int(vol))
    return jsonify({"success": True, "volume": int(vol)})


@media_bp.route("/api/music/state", methods=["GET"])
def api_music_state():
    user_id = _get_user_id_for_request()
    state = music_manager.get_music_state(user_id)
    return jsonify(state)


@media_bp.route("/api/music/playlist/list", methods=["GET"])
def api_music_playlist_list():
    user_id = _get_user_id_for_request()
    playlists = music_manager.list_playlists(user_id)
    return jsonify({"playlists": playlists})


@media_bp.route("/api/music/playlist/<playlist_id>", methods=["GET"])
def api_music_playlist_detail(playlist_id):
    user_id = _get_user_id_for_request()
    pl = music_manager.get_playlist(user_id, playlist_id)
    if not pl:
        return jsonify({"error": "歌单不存在"}), 404
    return jsonify(pl)


@media_bp.route("/api/music/playlist/<playlist_id>", methods=["DELETE"])
def api_music_playlist_delete(playlist_id):
    user_id = _get_user_id_for_request()
    ok = music_manager.delete_playlist(user_id, playlist_id)
    if not ok:
        return jsonify({"error": "歌单不存在"}), 404
    return jsonify({"success": True})


@media_bp.route("/api/music/playlist/create", methods=["POST"])
def api_music_playlist_create():
    user_id = _get_user_id_for_request()
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    pl = music_manager.create_playlist(user_id, name)
    return jsonify({"success": True, "playlist": pl})


@media_bp.route("/api/music/playlist/add", methods=["POST"])
def api_music_playlist_add():
    user_id = _get_user_id_for_request()
    data = request.json or {}
    playlist_id = data.get("playlist_id", "").strip()
    song_id = data.get("song_id")
    if not playlist_id or not song_id:
        return jsonify({"error": "playlist_id and song_id required"}), 400

    title = data.get("title", "").strip()
    artist = data.get("artist", "").strip()
    if title:
        song = {"net_ease_id": song_id, "title": title, "artist": artist}
    else:
        detail = music_api.get_song_detail([song_id])
        if detail and len(detail) > 0:
            song = detail[0]
        else:
            song = {"net_ease_id": song_id, "title": "未知歌曲", "artist": "未知歌手"}

    ok = music_manager.add_to_playlist(user_id, playlist_id, song)
    if not ok:
        return jsonify({"error": "歌单不存在或歌曲已存在"}), 400
    return jsonify({"success": True})


@media_bp.route("/api/music/playlist/<playlist_id>/song", methods=["DELETE"])
def api_music_playlist_remove_song(playlist_id):
    user_id = _get_user_id_for_request()
    data = request.json or {}
    index = data.get("index", -1)
    ok = music_manager.remove_from_playlist(user_id, playlist_id, int(index))
    if not ok:
        return jsonify({"error": "歌单不存在或索引无效"}), 400
    return jsonify({"success": True})


@media_bp.route("/api/music/recommend", methods=["POST"])
def api_music_recommend():
    user_id = _get_user_id_for_request()
    data = request.json or {}
    limit = data.get("limit", 20)
    songs = music_api.daily_recommend(limit)
    return jsonify({"songs": songs})


@media_bp.route("/api/music/lyric/<int:song_id>", methods=["GET"])
def api_music_lyric(song_id):
    lyric = music_api.get_lyric(song_id)
    return jsonify({"lyric": lyric})


# ==================== 音乐后台自动继续 ====================

def _start_music_auto_continue(char_id, user_id, result_content):
    def _run():
        set_background_user(user_id)
        try:
            from app import get_ai_language, build_system_prompt_v2, call_openrouter, call_gemini, get_model_config

            now = datetime.now()
            lang = get_ai_language(char_id)

            messages = []
            system_prompt = build_system_prompt_v2(char_id, user_latest_input="")
            messages.append({"role": "system", "content": system_prompt})

            playing_info = "无"
            if music_manager.is_playing(user_id):
                song = music_manager.get_current_song(user_id)
                if song:
                    playing_info = f"《{song.get('title', '')}》- {song.get('artist', '')}"
            music_prompt = MUSIC_MODULE_PROMPT.replace("{playing_info}", playing_info)
            messages.append({"role": "system", "content": music_prompt})

            messages.append({"role": "system", "content": result_content})

            if lang == "zh":
                hint = "(无需等待用户回复，请直接根据搜索结果选择一首歌曲播放，使用 [MUSIC_PLAY:歌曲ID] 标签。)"
            elif lang == "ja":
                hint = "(ユーザーからの返信を待たずに、検索結果から直接曲を選んで [MUSIC_PLAY:曲ID] タグで再生してください。)"
            else:
                hint = "(Do not wait for user reply. Pick a song from the results and play it using [MUSIC_PLAY:song_id] tag.)"
            messages.append({"role": "system", "content": hint})

            hour = now.hour
            time_str = now.strftime('%H:%M')
            if 5 <= hour < 11:
                period = "朝" if lang == "ja" else "早上"
            elif 11 <= hour < 13:
                period = "昼" if lang == "ja" else "中午"
            elif 13 <= hour < 18:
                period = "午後" if lang == "ja" else "下午"
            elif 18 <= hour < 23:
                period = "夜" if lang == "ja" else "晚上"
            else:
                period = "深夜" if lang == "ja" else "深夜"
            time_hint = f"(系统通知：现在时间 {period} {time_str})" if lang in ("zh", "ja") else f"(System: {period} {time_str})"
            messages.append({"role": "system", "content": time_hint})

            route, current_model = get_model_config("chat")
            print(f"[MusicAuto] auto-continue 调用AI, model={current_model}")
            if route == "relay":
                reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model)
            else:
                reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model)

            timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
            cleaned_reply = re.sub(timestamp_pattern, '', reply_text_raw).strip()

            cleaned_reply, music_tags = parse_music_tags(cleaned_reply)

            for tag in music_tags:
                if tag["type"] == "play":
                    song_info = music_api.get_song_info(tag["song_id"])
                    if song_info and song_info.get("audio_url"):
                        music_manager.update_current_song(user_id, song_info)
                        cleaned_reply += f"\n已开始播放《{song_info.get('title', '')}》"
                elif tag["type"] == "exit":
                    music_manager.set_music_mode(user_id, False)

            db_path, _ = get_paths(char_id)
            if db_path:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                cur.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           ("assistant", cleaned_reply, now_ts))
                conn.commit()
                conn.close()

            print(f"[MusicAuto] auto-continue 完成: char={char_id}")
        except Exception as e:
            print(f"[MusicAuto] auto-continue 失败: {e}")
            import traceback
            traceback.print_exc()

    threading.Thread(target=_run, daemon=True).start()


def _handle_music_tag_in_chat(char_id, user_id, tag):
    if tag["type"] == "enter":
        music_manager.set_music_mode(user_id, True)
        print(f"[Music] char={char_id} 进入音乐模式")

    elif tag["type"] == "exit":
        music_manager.set_music_mode(user_id, False)
        music_manager.stop_playback(user_id)
        print(f"[Music] char={char_id} 退出音乐模式")

    elif tag["type"] == "pause":
        music_manager.pause_playback(user_id)

    elif tag["type"] == "resume":
        music_manager.resume_playback(user_id)

    elif tag["type"] == "stop":
        music_manager.stop_playback(user_id)

    elif tag["type"] == "next":
        music_manager.play_next(user_id)

    elif tag["type"] == "prev":
        pass

    elif tag["type"] == "playlist_list":
        pass

    elif tag["type"] == "playlist_view":
        pl = music_manager.get_playlist(user_id, tag["playlist_id"])
        if pl:
            db_path, _ = get_paths(char_id)
            if db_path:
                songs_info = "\n".join(
                    f"  {i+1}. {s['title']} — {s['artist']} (ID:{s['net_ease_id']})"
                    for i, s in enumerate(pl.get("songs", []))
                )
                result = f"[MUSIC_RESULT:{json.dumps({'type': 'playlist_view', 'playlist': pl}, ensure_ascii=False)}]"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           ("system", result, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
                conn.close()

                _start_music_auto_continue(char_id, user_id, result)

    elif tag["type"] == "playlist_create":
        pl = music_manager.create_playlist(user_id, tag["name"])
        db_path, _ = get_paths(char_id)
        if db_path:
            result = f"[MUSIC_RESULT:{json.dumps({'type': 'playlist_created', 'playlist': pl}, ensure_ascii=False)}]"
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("system", result, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
            conn.close()

    elif tag["type"] == "playlist_add":
        details = music_api.search_songs(str(tag["song_id"]), 1)
        song = details[0] if details else {"net_ease_id": tag["song_id"], "title": "未知歌曲", "artist": "未知歌手"}
        music_manager.add_to_playlist(user_id, tag["playlist_id"], song)

    elif tag["type"] == "playlist_delete":
        music_manager.delete_playlist(user_id, tag["playlist_id"])


def _start_music_search_and_continue(char_id, user_id, music_tags):
    def _run():
        set_background_user(user_id)
        try:
            db_path, _ = get_paths(char_id)
            if not db_path:
                return

            now = datetime.now()

            for tag in music_tags:
                if tag["type"] != "search":
                    continue

                keyword = tag["keyword"]
                limit = tag.get("limit", 10)
                print(f"[MusicSearch] char={char_id} 搜索: {keyword}")

                songs = music_api.search_songs(keyword, max(limit * 2, 20))
                result_data = {
                    "type": "search",
                    "keyword": keyword,
                    "songs": songs
                }
                result_msg = f"[MUSIC_RESULT:{json.dumps(result_data, ensure_ascii=False)}]"

                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                result_ts = now.strftime('%Y-%m-%d %H:%M:%S')
                cur.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           ("system", result_msg, result_ts))
                conn.commit()
                conn.close()

                time.sleep(0.8)

                _start_music_auto_continue(char_id, user_id, result_msg)

                break

        except Exception as e:
            print(f"[MusicSearch] 搜索+auto-continue 失败: {e}")
            import traceback
            traceback.print_exc()

    threading.Thread(target=_run, daemon=True).start()


# ==================== TTS 语音合成 ====================

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
TTS_DAILY_LIMIT = 15


def _check_tts_quota(uid):
    if uid is None:
        return False, "无法识别用户"
    if str(uid) == "1":
        return True, None
    usage_file = os.path.join(core.config.USERS_ROOT, str(uid), "configs", "tts_usage.json")
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(usage_file), exist_ok=True)
    usage = {}
    if os.path.exists(usage_file):
        try:
            with open(usage_file, "r", encoding="utf-8") as f:
                usage = json.load(f)
        except Exception:
            usage = {}
    if usage.get("date") != today:
        usage = {"date": today, "count": 0}
    if usage["count"] >= TTS_DAILY_LIMIT:
        return False, f"今日TTS次数已用完（{TTS_DAILY_LIMIT}次/天）"
    usage["count"] += 1
    try:
        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(usage, f)
    except Exception as e:
        print(f"[TTS_QUOTA] 写入失败: {e}")
    return True, None


def _resolve_voice_config(char_id, key, default=""):
    cfg_file = _get_characters_config_file()
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            val = (all_config.get(char_id, {}).get(key) or "").strip()
            if val:
                return val
        except Exception:
            pass
    global_cfg = os.path.join(core.config.BASE_DIR, "configs", "characters.json")
    if os.path.exists(global_cfg):
        try:
            with open(global_cfg, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            val = (all_config.get(char_id, {}).get(key) or "").strip()
            if val:
                return val
        except Exception:
            pass
    return default


@media_bp.route("/api/<char_id>/tts", methods=["POST"])
def char_tts(char_id):
    uid = get_current_user_id()
    allowed, err_msg = _check_tts_quota(uid)
    if not allowed:
        return jsonify({"error": err_msg}), 429
    voice_id = _resolve_voice_config(char_id, "voice_id")
    voice_emotion = _resolve_voice_config(char_id, "voice_emotion")

    data = request.get_json(silent=True) or {}
    override_voice = (data.get("voice_id") or "").strip()
    if override_voice:
        voice_id = override_voice

    override_emotion = (data.get("voice_emotion") or "").strip()
    if override_emotion:
        voice_emotion = override_emotion

    if not voice_id:
        return jsonify({"error": "No voice_id configured for this character"}), 400

    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    text = re.sub(r'<ruby>([^<]*)<rt>[^<]*</rt></ruby>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('/', '。')
    text = text.strip()
    if not text:
        return jsonify({"error": "Text is empty after stripping tags"}), 400

    stability = 0.5
    similarity_boost = 0.75
    style = 0.0
    if voice_emotion:
        e = voice_emotion.strip()
        if e == "开心":
            stability = 0.2; similarity_boost = 0.7; style = 0.8
        elif e == "愤怒":
            stability = 0.15; similarity_boost = 0.6; style = 0.95
        elif e == "悲伤":
            stability = 0.3; similarity_boost = 0.8; style = 0.5
        elif e == "温柔":
            stability = 0.35; similarity_boost = 0.8; style = 0.35
        elif e == "害羞":
            stability = 0.3; similarity_boost = 0.75; style = 0.4
        elif e == "冷淡":
            stability = 0.6; similarity_boost = 0.6; style = 0.0
        elif e == "兴奋":
            stability = 0.15; similarity_boost = 0.65; style = 0.9
        elif e == "平静":
            stability = 0.5; similarity_boost = 0.75; style = 0.0
        elif "开心" in e or "笑" in e:
            stability = 0.2; similarity_boost = 0.7; style = 0.8
        elif "愤怒" in e or "怒" in e or "激动" in e:
            stability = 0.15; similarity_boost = 0.6; style = 0.95
        elif "悲伤" in e or "难过" in e or "泣" in e:
            stability = 0.3; similarity_boost = 0.8; style = 0.5
        elif "温柔" in e or "優" in e or "暖" in e:
            stability = 0.35; similarity_boost = 0.8; style = 0.35
        elif "害羞" in e or "紧张" in e or "照" in e:
            stability = 0.3; similarity_boost = 0.75; style = 0.4
        elif "冷淡" in e or "冷" in e or "酷" in e:
            stability = 0.6; similarity_boost = 0.6; style = 0.0
        elif "兴奋" in e:
            stability = 0.15; similarity_boost = 0.65; style = 0.9
        else:
            stability = 0.3; similarity_boost = 0.72; style = 0.5
    print(f"[TTS] char={char_id} emotion={voice_emotion!r} stability={stability} similarity={similarity_boost} style={style}")

    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {
                    "stability": stability,
                    "similarity_boost": similarity_boost,
                    "style": style
                }
            },
            timeout=30
        )
        if resp.status_code != 200:
            return jsonify({"error": f"ElevenLabs TTS failed: {resp.text}"}), resp.status_code

        return send_file(
            io.BytesIO(resp.content),
            mimetype="audio/mpeg",
            as_attachment=False
        )
    except Exception as e:
        print(f"TTS Error: {e}")
        return jsonify({"error": str(e)}), 500


@media_bp.route("/api/<char_id>/tts_voice", methods=["POST"])
def char_tts_voice(char_id):
    uid = get_current_user_id()
    allowed, err_msg = _check_tts_quota(uid)
    if not allowed:
        return jsonify({"error": err_msg}), 429
    voice_id = _resolve_voice_config(char_id, "voice_id")

    data = request.get_json(silent=True) or {}
    override_voice = (data.get("voice_id") or "").strip()
    if override_voice:
        voice_id = override_voice

    if not voice_id:
        return jsonify({"error": "No voice_id configured for this character"}), 400

    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    text = re.sub(r'<ruby>([^<]*)<rt>[^<]*</rt></ruby>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('/', '。')
    text = text.strip()
    if not text:
        return jsonify({"error": "Text is empty after stripping tags"}), 400

    print(f"[TTS_VOICE] char={char_id} text={text[:50]}...")

    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "model_id": "eleven_v3",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0
                }
            },
            timeout=30
        )
        if resp.status_code != 200:
            return jsonify({"error": f"ElevenLabs TTS failed: {resp.text}"}), resp.status_code

        return send_file(
            io.BytesIO(resp.content),
            mimetype="audio/mpeg",
            as_attachment=False
        )
    except Exception as e:
        print(f"TTS Voice Error: {e}")
        return jsonify({"error": str(e)}), 500


@media_bp.route("/api/voice_clone", methods=["POST"])
def voice_clone():
    if "file" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400
    try:
        resp = requests.post(
            "https://api.elevenlabs.io/v1/voices/add",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            files={"files": (file.filename, file.read(), file.content_type)},
            data={"name": request.form.get("name", "kunigami_voice")}
        )
        if resp.status_code != 200:
            return jsonify({"error": f"ElevenLabs clone failed: {resp.text}"}), resp.status_code
        result = resp.json()
        voice_id = result.get("voice_id", "")
        return jsonify({"status": "success", "voice_id": voice_id})
    except Exception as e:
        print(f"Voice Clone Error: {e}")
        return jsonify({"error": str(e)}), 500


# ==================== 视觉 / 图片 ====================

def _compress_chat_image_to_jpg(src_path: str, dst_path: str, max_edge: int = 1024, max_bytes: int = 500 * 1024):
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if "A" in img.getbands():
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        elif img.mode == "L":
            img = img.convert("RGB")

        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        work_img = img
        for _ in range(8):
            for q in (88, 82, 76, 70, 64, 58, 52, 46, 40):
                buf = io.BytesIO()
                work_img.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
                size = buf.tell()
                if size <= max_bytes:
                    with open(dst_path, "wb") as f:
                        f.write(buf.getvalue())
                    return
            w, h = work_img.size
            if max(w, h) <= 480:
                with open(dst_path, "wb") as f:
                    f.write(buf.getvalue())
                return
            work_img = work_img.resize((int(w * 0.85), int(h * 0.85)), Image.Resampling.LANCZOS)

        with open(dst_path, "wb") as f:
            f.write(buf.getvalue())


@media_bp.route("/api/vision/upload", methods=["POST"])
def vision_upload():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    img_dir = os.path.join(core.config.USERS_ROOT, str(user_id), "chat_images")
    os.makedirs(img_dir, exist_ok=True)

    ext = ".jpg"
    base_name = uuid.uuid4().hex
    filename = base_name + ext
    filepath = os.path.join(img_dir, filename)
    n = 0
    while os.path.exists(filepath):
        n += 1
        filename = f"{base_name}_{n}{ext}"
        filepath = os.path.join(img_dir, filename)
    tmp_raw_path = os.path.join(img_dir, f"{base_name}_raw_upload")
    file.save(tmp_raw_path)
    try:
        _compress_chat_image_to_jpg(tmp_raw_path, filepath, max_edge=1024, max_bytes=500 * 1024)
    except Exception as e:
        print(f"   [Vision] Image processing error: {e}")
        try:
            if os.path.exists(tmp_raw_path):
                os.remove(tmp_raw_path)
        except: pass
        return jsonify({"error": f"图片处理失败，请确保上传的是有效图片格式: {str(e)}"}), 400

    try:
        if os.path.exists(tmp_raw_path):
            os.remove(tmp_raw_path)
    except Exception:
        pass

    static_upload_dir = os.path.join(core.config.BASE_DIR, "static", "uploads")
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
        configured = (os.getenv("PUBLIC_BASE_URL", "") or os.getenv("SITE_URL", "")).strip()
        if configured:
            parsed = urlparse(configured)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
            return configured.rstrip("/")

        forwarded_proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
        forwarded_host = (request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "").split(",")[0].strip()
        if forwarded_host:
            return f"{forwarded_proto}://{forwarded_host}"
        return request.host_url.rstrip("/")

    public_image_url = f"{_public_base_url()}/static/uploads/{public_filename}"

    from app import get_model_config, call_openrouter

    route, current_model = get_model_config("vision")
    print(f"--- [Vision] Route: {route}, Model: {current_model} ---")

    prompt = "请用中文简要描述这张图片的内容，直接描述你看到了什么，不用过多主观判断。"
    description = ""
    try:
        if route == "relay":
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
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
                        {"text": prompt},
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
            r = requests.post(url, json=payload, timeout=100)
            if r.status_code != 200:
                raise RuntimeError(f"[Gemini Vision Error {r.status_code}] {r.text}")
            result = r.json()
            finish_reason = (result.get("candidates") or [{}])[0].get("finishReason")
            if finish_reason and finish_reason != "STOP":
                print(f"   [Vision] Gemini finishReason={finish_reason} (可能被截断)")
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

    try:
        cos_path = f"users/{user_id}/chat_images/{filename}"
        upload_to_cos(filepath, cos_path)
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        print(f"   [COS Upload Error] {e}")
    finally:
        if os.path.exists(public_file_path):
            os.remove(public_file_path)

    url = f"/api/user/image/{filename}"
    return jsonify({
        "status": "success",
        "url": url,
        "path": filename,
        "description": (description or "").strip()
    })


@media_bp.route("/api/user/image/<filename>", methods=["GET"])
def get_user_chat_image(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return "Forbidden", 403
    user_id = get_current_user_id()
    if not user_id:
        return "Unauthorized", 401

    img_dir = os.path.join(core.config.USERS_ROOT, str(user_id), "chat_images")
    local_path = os.path.join(img_dir, filename)

    if os.path.exists(local_path):
        response = send_from_directory(img_dir, filename)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    else:
        cos_path = f"users/{user_id}/chat_images/{filename}"
        if core.config.COS_BASE_URL:
            cos_url = f"{core.config.COS_BASE_URL}/{cos_path}"
            return redirect(cos_url)
        else:
            return "File not found locally and COS not configured", 404


# ==================== 振假名 ====================

@media_bp.route("/api/furigana", methods=["POST"])
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
