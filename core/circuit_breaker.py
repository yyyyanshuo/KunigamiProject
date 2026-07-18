import sqlite3
import os
import time as _time
import threading
from datetime import datetime, timedelta
from contextvars import ContextVar

from core.config import USERS_DB

_cb_info_var: ContextVar[dict | None] = ContextVar("circuit_breaker_info", default=None)

AUTO_RESET_MINUTES = 30
COOLDOWN_MINUTES = 10

# --- 全局 Cloudflare WAF 拦截暂停（服务器级别，非按用户） ---
_relay_pause_until: float = 0
_relay_pause_lock = threading.Lock()
CF_PAUSE_SECONDS = 180  # 默认暂停 3 分钟


def check_relay_global_pause() -> dict | None:
    """检查 relay 是否因 Cloudflare WAF 拦截被全局暂停。返回 None 表示放行。"""
    with _relay_pause_lock:
        now = _time.time()
        if now < _relay_pause_until:
            remaining = int(_relay_pause_until - now)
            mins = max(1, remaining // 60)
            return {
                "type": "cooldown",
                "message": f"中转服务器暂时不可用（安全拦截），请 {mins} 分钟后重试。",
                "remaining_seconds": remaining,
            }
    return None


def mark_relay_global_pause(duration_seconds: int = CF_PAUSE_SECONDS) -> None:
    """触发全局 relay 暂停（Cloudflare HTML 403 被检测到时调用）。"""
    with _relay_pause_lock:
        global _relay_pause_until
        _relay_pause_until = max(_relay_pause_until, _time.time() + duration_seconds)
        print(f"[circuit_breaker] relay global pause {duration_seconds}s, until {datetime.fromtimestamp(_relay_pause_until)}")


def _is_cloudflare_block(response_text: str) -> bool:
    """检测响应是否为 Cloudflare WAF 拦截 HTML 页面。"""
    if not response_text:
        return False
    upper = response_text.strip().upper()
    looks_like_html = upper.startswith("<!DOCTYPE") or upper.startswith("<HTML")
    return looks_like_html and "CLOUDFLARE" in upper


def _get_conn():
    return sqlite3.connect(USERS_DB)


def _ensure_route_disabled_column(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE circuit_breaker ADD COLUMN route_disabled INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def set_circuit_breaker_info(info: dict | None) -> None:
    _cb_info_var.set(info)


def get_circuit_breaker_info() -> dict | None:
    try:
        return _cb_info_var.get()
    except LookupError:
        return None


def clear_circuit_breaker_info() -> None:
    _cb_info_var.set(None)


def check_circuit_breaker(user_id: int, route: str) -> dict | None:
    """在发起 API 调用前检查熔断状态。返回 None 表示放行，否则返回弹窗信息。"""
    if not user_id:
        return None

    _auto_reset_expired(user_id)

    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        cur = conn.execute(
            "SELECT status_code, trigger_count, cooldown_until, route_disabled FROM circuit_breaker WHERE user_id = ? AND route = ?",
            (user_id, route),
        )
        now = datetime.now()
        for sc, count, cooldown_until, route_disabled in cur.fetchall():
            if route_disabled:
                return {
                    "type": "route_disabled",
                    "message": f"您的 {route} API 路由已因连续触发致命错误 {sc} {count} 次被禁用，请检查 API 配置或联系管理员解除。",
                    "route": route,
                    "status_code": sc,
                }
            if cooldown_until:
                cd = datetime.fromisoformat(cooldown_until)
                if now < cd:
                    mins = max(1, int((cd - now).total_seconds() / 60))
                    secs = int((cd - now).total_seconds())
                    return {
                        "type": "cooldown",
                        "message": f"您已连续触发致命错误 {sc} {count} 次，冷却中，请 {mins} 分钟后再试。请检查 API 配置。",
                        "remaining_seconds": secs,
                    }
    finally:
        conn.close()
    return None


def record_fatal_error(user_id: int, route: str, status_code: int) -> dict | None:
    """记录一次致命 API 错误。返回熔断弹窗信息。"""
    if not user_id:
        return None

    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        now = datetime.now()
        now_iso = now.isoformat()

        cur = conn.execute(
            "SELECT id, trigger_count FROM circuit_breaker WHERE user_id = ? AND route = ? AND status_code = ?",
            (user_id, route, status_code),
        )
        row = cur.fetchone()

        if row:
            record_id, count = row
            new_count = count + 1
            conn.execute(
                "UPDATE circuit_breaker SET trigger_count = ?, last_trigger_time = ? WHERE id = ?",
                (new_count, now_iso, record_id),
            )
        else:
            new_count = 1
            conn.execute(
                "INSERT INTO circuit_breaker (user_id, route, status_code, trigger_count, first_trigger_time, last_trigger_time) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, route, status_code, new_count, now_iso, now_iso),
            )

        if new_count == 2:
            cooldown_until = (now + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
            conn.execute(
                "UPDATE circuit_breaker SET cooldown_until = ? WHERE user_id = ? AND route = ? AND status_code = ?",
                (cooldown_until, user_id, route, status_code),
            )
            conn.commit()
            conn.close()
            return {
                "type": "cooldown",
                "message": f"您已连续触发致命错误 {status_code} 2 次，进入 {COOLDOWN_MINUTES} 分钟冷却模式。请检查 API 配置。",
                "remaining_seconds": COOLDOWN_MINUTES * 60,
            }
        elif new_count >= 3:
            conn.execute(
                "UPDATE circuit_breaker SET route_disabled = 1, cooldown_until = NULL WHERE user_id = ? AND route = ? AND status_code = ?",
                (user_id, route, status_code),
            )
            conn.commit()
            conn.close()
            return {
                "type": "route_disabled",
                "message": f"您已连续触发致命错误 {status_code} {new_count} 次，{route} API 路由已被禁用。请检查 API 配置或联系管理员解除。",
                "route": route,
                "status_code": status_code,
            }
        else:
            conn.commit()
            conn.close()
            return {
                "type": "warning",
                "message": f"检测到致命错误 {status_code}（{route}），如反复出现可能触发熔断机制。请检查 API 配置。",
            }
    except Exception as e:
        print(f"[circuit_breaker] record_fatal_error error: {e}")
        try:
            conn.close()
        except:
            pass
        return None


def _auto_reset_expired(user_id: int) -> None:
    """延迟清理：若某错误码距上次触发超过 30 分钟且不在冷却中，自动复位计数器。"""
    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        now = datetime.now()
        cur = conn.execute(
            "SELECT id, last_trigger_time, cooldown_until, route_disabled FROM circuit_breaker WHERE user_id = ?",
            (user_id,),
        )
        expired_ids = []
        for rid, last_time_str, cooldown_str, route_disabled in cur.fetchall():
            if route_disabled:
                continue
            if not last_time_str:
                continue
            last_time = datetime.fromisoformat(last_time_str)
            if (now - last_time) > timedelta(minutes=AUTO_RESET_MINUTES):
                if cooldown_str:
                    cd = datetime.fromisoformat(cooldown_str)
                    if now < cd:
                        continue
                expired_ids.append(rid)

        for rid in expired_ids:
            conn.execute("DELETE FROM circuit_breaker WHERE id = ?", (rid,))
        if expired_ids:
            conn.commit()
    except Exception as e:
        print(f"[circuit_breaker] _auto_reset_expired error: {e}")
    finally:
        conn.close()


def reset_route_success(user_id: int, route: str) -> None:
    """某路由成功调用后清理该路由的非禁用熔断计数。"""
    if not user_id:
        return
    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        conn.execute(
            "DELETE FROM circuit_breaker WHERE user_id = ? AND route = ? AND COALESCE(route_disabled, 0) = 0",
            (user_id, route),
        )
        conn.commit()
    except Exception as e:
        print(f"[circuit_breaker] reset_route_success error: {e}")
    finally:
        conn.close()


def get_user_circuit_status(user_id: int) -> dict:
    """获取单个用户的熔断状态。"""
    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        cur = conn.execute("SELECT is_frozen FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        is_frozen = row[0] if row else 0

        cur = conn.execute(
            "SELECT route, status_code, trigger_count, first_trigger_time, last_trigger_time, cooldown_until, route_disabled FROM circuit_breaker WHERE user_id = ?",
            (user_id,),
        )
        breakers = []
        for r in cur.fetchall():
            breakers.append({
                "route": r[0],
                "status_code": r[1],
                "count": r[2],
                "first_trigger_time": r[3],
                "last_trigger_time": r[4],
                "cooldown_until": r[5],
                "route_disabled": r[6] or 0,
            })
        return {"user_id": user_id, "is_frozen": is_frozen, "has_disabled_route": any(b["route_disabled"] for b in breakers), "breakers": breakers}
    finally:
        conn.close()


def get_all_circuit_status() -> dict:
    """获取所有用户的熔断状态（管理员用）。"""
    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        cur = conn.execute("SELECT id, email, is_frozen FROM users ORDER BY id")
        users = []
        for r in cur.fetchall():
            users.append({"user_id": r[0], "email": r[1], "is_frozen": r[2] or 0})

        cur = conn.execute(
            "SELECT user_id, route, status_code, trigger_count, first_trigger_time, last_trigger_time, cooldown_until, route_disabled FROM circuit_breaker ORDER BY user_id, last_trigger_time DESC"
        )
        breaker_map = {}
        for r in cur.fetchall():
            uid = r[0]
            if uid not in breaker_map:
                breaker_map[uid] = []
            breaker_map[uid].append({
                "route": r[1],
                "status_code": r[2],
                "count": r[3],
                "first_trigger_time": r[4],
                "last_trigger_time": r[5],
                "cooldown_until": r[6],
                "route_disabled": r[7] or 0,
            })

        for u in users:
            u["breakers"] = breaker_map.get(u["user_id"], [])
            u["has_disabled_route"] = any(b["route_disabled"] for b in u["breakers"])

        return {"users": users}
    finally:
        conn.close()


def unfreeze_user(user_id: int) -> bool:
    """管理员解除限制：清除冻结标记和所有熔断记录。"""
    conn = _get_conn()
    try:
        _ensure_route_disabled_column(conn)
        conn.execute("UPDATE users SET is_frozen = 0 WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM circuit_breaker WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[circuit_breaker] unfreeze_user error: {e}")
        return False
    finally:
        conn.close()


def is_user_frozen(user_id: int) -> bool:
    """兼容旧调用：仅检查旧的用户冻结标记，不再把路由禁用视为整用户冻结。"""
    if not user_id:
        return False
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT is_frozen FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return row is not None and row[0] == 1
    except:
        return False
    finally:
        conn.close()
