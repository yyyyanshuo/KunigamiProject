"""
网易云音乐开放平台 API 封装
RSA-SHA256 签名认证
搜索 + 批量可播放检测
"""
import base64
import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

NCM_APP_ID = os.getenv("NCM_APP_ID", "")
NCM_PRIVATE_KEY = os.getenv("NCM_PRIVATE_KEY", "")
NCM_API_BASE = "https://interface.music.163.com"


def _load_rsa_key():
    if not NCM_PRIVATE_KEY:
        raise RuntimeError("NCM_PRIVATE_KEY 未配置")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    key_data = NCM_PRIVATE_KEY
    if "BEGIN PRIVATE KEY" not in key_data:
        key_data = f"-----BEGIN PRIVATE KEY-----\n{key_data}\n-----END PRIVATE KEY-----"
    return serialization.load_pem_private_key(key_data.encode(), password=None, backend=default_backend())


def _rsa_sign(data: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return base64.b64encode(_load_rsa_key().sign(data.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()


def _opapi_get(endpoint: str, params: dict = None) -> dict:
    if params is None:
        params = {}
    params["appId"] = NCM_APP_ID
    params["timestamp"] = str(int(time.time() * 1000))
    keys = sorted(params.keys())
    params["sign"] = _rsa_sign("&".join(f"{k}={params[k]}" for k in keys))
    try:
        resp = requests.get(f"{NCM_API_BASE}{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[MusicAPI] GET {endpoint}: {e}")
        return {"code": -1, "message": str(e)}


def search_songs(keyword: str, limit: int = 10, offset: int = 0) -> list:
    """搜索歌曲"""
    result = _opapi_get("/api/search/get", {
        "s": keyword, "type": "1", "limit": str(limit), "offset": str(offset)
    })
    songs = []
    try:
        for s in result.get("result", {}).get("songs", []):
            songs.append({
                "net_ease_id": s.get("id", 0),
                "title": s.get("name", ""),
                "artist": _artist(s),
                "album": _album(s),
                "cover_url": _cover(s),
                "duration": s.get("duration", 0) // 1000 if s.get("duration") else 0,
                "songFee": s.get("fee", 0),
                "playable": False,
                "audio_url": "",
            })
    except Exception as e:
        print(f"[MusicAPI] 解析搜索失败: {e}")
    print(f"[MusicAPI] search '{keyword}' -> {len(songs)} songs")
    return songs


def batch_get_song_urls(song_ids: list) -> dict:
    """批量获取音频 URL -> {song_id: url}"""
    if not song_ids:
        return {}
    result = _opapi_get("/api/song/enhance/player/url", {
        "ids": json.dumps([int(s) for s in song_ids]), "br": "320000"
    })
    url_map = {}
    for item in result.get("data", []):
        url = item.get("url", "")
        if url:
            url_map[item.get("id", 0)] = url
    return url_map


def get_song_url(song_id) -> str:
    urls = batch_get_song_urls([song_id])
    return urls.get(int(song_id), "")


def get_lyric(song_id) -> str:
    result = _opapi_get("/api/song/lyric", {"id": str(song_id)})
    lrc = result.get("lrc", {})
    if isinstance(lrc, dict):
        return lrc.get("lyric", "") or ""
    return ""


def get_song_info(song_id) -> dict:
    result = _opapi_get("/api/song/detail", {"ids": json.dumps([int(song_id)])})
    songs = result.get("songs", [])
    if songs:
        s = songs[0]
        song = {
            "net_ease_id": s.get("id", 0),
            "title": s.get("name", ""),
            "artist": _artist(s),
            "album": _album(s),
            "cover_url": _cover(s),
            "duration": s.get("duration", 0) // 1000 if s.get("duration") else 0,
        }
    else:
        song = {"net_ease_id": song_id, "title": "未知歌曲", "artist": "未知歌手",
                "album": "", "cover_url": "", "duration": 0}
    song["audio_url"] = get_song_url(song_id)
    song["lyric"] = get_lyric(song_id)
    return song


def daily_recommend(limit: int = 20) -> list:
    result = _opapi_get("/api/recommend/songs", {"limit": str(limit)})
    songs = []
    for s in result.get("data", {}).get("dailySongs", []):
        songs.append({
            "net_ease_id": s.get("id", 0),
            "title": s.get("name", ""),
            "artist": _artist(s),
            "album": _album(s),
            "cover_url": _cover(s),
            "duration": s.get("duration", 0) // 1000 if s.get("duration") else 0,
            "playable": False,
        })
    return songs


def _artist(s: dict) -> str:
    ar = s.get("artists") or s.get("ar") or []
    names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in ar]
    return " / ".join(filter(None, names)) or "未知歌手"


def _album(s: dict) -> str:
    al = s.get("album") or s.get("al") or {}
    return al.get("name", "") if isinstance(al, dict) else (al if isinstance(al, str) else "")


def _cover(s: dict) -> str:
    al = s.get("album") or s.get("al") or {}
    if isinstance(al, dict):
        return al.get("picUrl", "") or ""
    return ""
