"""
音乐状态管理 + 本地歌单 CRUD
存储路径: users/<user_id>/music/
  - state.json   : 播放状态 (music_mode, is_playing, current_song含lyric全文, queue, volume)
  - playlists.json : 本地歌单数组 [{id, name, songs: [{net_ease_id, title, artist}], created_at}]
"""
import json
import os
import uuid
from datetime import datetime

MUSIC_DIR_NAME = "music"
STATE_FILE = "state.json"
PLAYLISTS_FILE = "playlists.json"


def _get_music_dir(user_id) -> str:
    """获取用户音乐数据目录"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    music_dir = os.path.join(base_dir, "users", str(user_id), MUSIC_DIR_NAME)
    os.makedirs(music_dir, exist_ok=True)
    return music_dir


def _read_json(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _write_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==================== 播放状态 ====================

def _default_state() -> dict:
    return {
        "music_mode": False,
        "is_playing": False,
        "current_song": None,
        "volume": 80,
        "queue": [],
        "position": 0
    }


def get_music_state(user_id) -> dict:
    """获取用户当前音乐状态"""
    if not user_id:
        return _default_state()
    filepath = os.path.join(_get_music_dir(user_id), STATE_FILE)
    state = _read_json(filepath)
    if not state:
        return _default_state()

    for k, v in _default_state().items():
        if k not in state:
            state[k] = v
    return state


def save_music_state(user_id, state: dict):
    """保存用户音乐状态"""
    if not user_id:
        return
    filepath = os.path.join(_get_music_dir(user_id), STATE_FILE)
    _write_json(filepath, state)


def set_music_mode(user_id, enabled: bool):
    """设置音乐模式开关"""
    state = get_music_state(user_id)
    state["music_mode"] = enabled
    save_music_state(user_id, state)


def is_music_mode(user_id) -> bool:
    """查询是否在音乐模式"""
    return get_music_state(user_id).get("music_mode", False)


def update_current_song(user_id, song_data: dict):
    """设置当前播放歌曲，写入完整信息（含歌词全文）"""
    state = get_music_state(user_id)
    state["current_song"] = song_data
    state["is_playing"] = True
    save_music_state(user_id, state)


def get_current_song(user_id) -> dict:
    """获取当前播放歌曲"""
    state = get_music_state(user_id)
    return state.get("current_song")


def is_playing(user_id) -> bool:
    """是否正在播放"""
    state = get_music_state(user_id)
    return state.get("is_playing", False) and state.get("current_song") is not None


def pause_playback(user_id):
    """暂停播放"""
    state = get_music_state(user_id)
    state["is_playing"] = False
    save_music_state(user_id, state)


def resume_playback(user_id):
    """恢复播放"""
    state = get_music_state(user_id)
    if state.get("current_song"):
        state["is_playing"] = True
        save_music_state(user_id, state)


def stop_playback(user_id):
    """停止播放，清除当前歌曲和歌词上下文"""
    state = get_music_state(user_id)
    state["is_playing"] = False
    state["current_song"] = None
    save_music_state(user_id, state)


def clear_playback(user_id):
    """完全清除播放状态（含音乐模式）"""
    state = get_music_state(user_id)
    state["is_playing"] = False
    state["current_song"] = None
    state["music_mode"] = False
    save_music_state(user_id, state)


def set_volume(user_id, volume: int):
    """设置音量 0-100"""
    state = get_music_state(user_id)
    state["volume"] = max(0, min(100, volume))
    save_music_state(user_id, state)


def get_queue(user_id) -> list:
    """获取播放队列"""
    state = get_music_state(user_id)
    return state.get("queue", [])


def add_to_queue(user_id, song_data: dict):
    """添加到播放队列"""
    state = get_music_state(user_id)
    if "queue" not in state:
        state["queue"] = []
    state["queue"].append(song_data)
    save_music_state(user_id, state)


def clear_queue(user_id):
    """清空播放队列"""
    state = get_music_state(user_id)
    state["queue"] = []
    save_music_state(user_id, state)


def play_next(user_id) -> dict:
    """播放下队列下一首"""
    state = get_music_state(user_id)
    queue = state.get("queue", [])
    if queue:
        next_song = queue.pop(0)
        state["current_song"] = next_song
        state["is_playing"] = True
        save_music_state(user_id, state)
        return next_song
    return None


# ==================== 本地歌单 ====================

def _load_playlists(user_id) -> dict:
    if not user_id:
        return {"playlists": []}
    filepath = os.path.join(_get_music_dir(user_id), PLAYLISTS_FILE)
    data = _read_json(filepath)
    if not data or "playlists" not in data:
        data = {"playlists": []}
    return data


def _save_playlists(user_id, data: dict):
    if not user_id:
        return
    filepath = os.path.join(_get_music_dir(user_id), PLAYLISTS_FILE)
    _write_json(filepath, data)


def list_playlists(user_id) -> list:
    """列出所有歌单 [{"id": "...", "name": "...", "song_count": N, "created_at": "..."}]"""
    data = _load_playlists(user_id)
    result = []
    for pl in data.get("playlists", []):
        result.append({
            "id": pl["id"],
            "name": pl["name"],
            "song_count": len(pl.get("songs", [])),
            "created_at": pl.get("created_at", "")
        })
    return result


def get_playlist(user_id, playlist_id: str) -> dict:
    """获取歌单详情"""
    data = _load_playlists(user_id)
    for pl in data.get("playlists", []):
        if pl["id"] == playlist_id:
            return pl
    return None


def create_playlist(user_id, name: str) -> dict:
    """创建歌单，返回新歌单"""
    data = _load_playlists(user_id)
    new_pl = {
        "id": f"pl_{uuid.uuid4().hex[:8]}",
        "name": name,
        "songs": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    data["playlists"].append(new_pl)
    _save_playlists(user_id, data)
    return new_pl


def add_to_playlist(user_id, playlist_id: str, song_data: dict) -> bool:
    """
    添加歌曲到歌单
    song_data: {net_ease_id, title, artist}
    返回是否成功
    """
    data = _load_playlists(user_id)
    for pl in data.get("playlists", []):
        if pl["id"] == playlist_id:
            dups = [s for s in pl["songs"] if str(s.get("net_ease_id")) == str(song_data.get("net_ease_id"))]
            if dups:
                return False  # 已存在，不重复添加
            pl["songs"].append({
                "net_ease_id": song_data.get("net_ease_id", ""),
                "title": song_data.get("title", ""),
                "artist": song_data.get("artist", "")
            })
            _save_playlists(user_id, data)
            return True
    return False


def remove_from_playlist(user_id, playlist_id: str, index: int) -> bool:
    """从歌单移除歌曲 (按索引)"""
    data = _load_playlists(user_id)
    for pl in data.get("playlists", []):
        if pl["id"] == playlist_id:
            if 0 <= index < len(pl.get("songs", [])):
                pl["songs"].pop(index)
                _save_playlists(user_id, data)
                return True
    return False


def delete_playlist(user_id, playlist_id: str) -> bool:
    """删除歌单"""
    data = _load_playlists(user_id)
    old_len = len(data["playlists"])
    data["playlists"] = [pl for pl in data["playlists"] if pl["id"] != playlist_id]
    if len(data["playlists"]) != old_len:
        _save_playlists(user_id, data)
        return True
    return False
