import sqlite3
import time

from flask import Blueprint, current_app, request, session, jsonify, redirect, render_template
import core.config

admin_bp = Blueprint("admin", __name__)

IMPERSONATION_TTL_SECONDS = 15 * 60


def _restore_admin_session():
    """结束模拟登录并恢复原管理员身份。"""
    admin_id = session.pop("impersonator_user_id", None)
    session.pop("impersonated_user_id", None)
    session.pop("impersonation_expires_at", None)
    session.pop("impersonation_expired", None)
    if str(admin_id) == "1":
        session["user_id"] = 1
        session["logged_in"] = True
        session.permanent = True
        return True
    return False


@admin_bp.before_app_request
def expire_admin_impersonation():
    """模拟登录超过 15 分钟后，在下一次请求时自动恢复管理员。"""
    if session.get("impersonation_expired"):
        if request.endpoint == "admin.admin_dashboard":
            session.pop("impersonation_expired", None)
            return None
        if request.path.startswith("/api/"):
            return jsonify({
                "status": "error",
                "message": "管理员模拟登录已到期",
                "redirect": "/admin/dashboard",
            }), 440
        return redirect("/admin/dashboard")

    expires_at = session.get("impersonation_expires_at")
    if not expires_at:
        return None
    try:
        expired = time.time() >= float(expires_at)
    except (TypeError, ValueError):
        expired = True
    if expired:
        target_id = session.get("impersonated_user_id") or session.get("user_id")
        if _restore_admin_session():
            session["impersonation_expired"] = True
            current_app.logger.warning(
                "Admin impersonation expired: admin=1 target=%s ip=%s",
                target_id,
                request.remote_addr,
            )
            if request.path.startswith("/api/"):
                return jsonify({
                    "status": "error",
                    "message": "管理员模拟登录已到期",
                    "redirect": "/admin/dashboard",
                }), 440
            return redirect("/admin/dashboard")
    return None


@admin_bp.route("/admin/dashboard")
def admin_dashboard():
    if str(session.get("user_id")) != "1":
        return redirect("/")
    return render_template("admin/dashboard.html")


@admin_bp.route("/api/admin/stats")
def api_admin_stats():
    if str(session.get("user_id")) != "1":
        return jsonify({"error": "Forbidden"}), 403
    try:
        from admin_stats import generate_admin_stats
        return jsonify(generate_admin_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/refresh_stickers", methods=["GET"])
def api_admin_refresh_stickers():
    if str(session.get("user_id")) != "1":
        return jsonify({"status": "error", "message": "Admin only"}), 403
    try:
        core.config.CACHED_OFFICIAL_PACKS = None
        return jsonify({"status": "success", "message": "Sticker cache cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/circuit_status")
def api_admin_circuit_status():
    if str(session.get("user_id")) != "1":
        return jsonify({"error": "Forbidden"}), 403
    try:
        from core.circuit_breaker import get_all_circuit_status
        return jsonify(get_all_circuit_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/user/<int:user_id>/unfreeze", methods=["POST"])
def api_admin_unfreeze_user(user_id):
    if str(session.get("user_id")) != "1":
        return jsonify({"error": "Forbidden"}), 403
    try:
        from core.circuit_breaker import unfreeze_user
        ok = unfreeze_user(user_id)
        if ok:
            return jsonify({"status": "success", "message": f"用户 {user_id} 的熔断限制已清除"})
        else:
            return jsonify({"error": "Failed to unfreeze"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/impersonate", methods=["POST"])
def api_admin_impersonate():
    """让管理员 1 临时以目标用户身份排查问题，不修改目标用户密码。"""
    if str(session.get("user_id")) != "1" or session.get("impersonator_user_id"):
        return jsonify({"status": "error", "message": "Admin only"}), 403
    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "请输入有效的用户 ID"}), 400
    if target_id <= 0 or target_id == 1:
        return jsonify({"status": "error", "message": "不能模拟该用户"}), 400
    try:
        conn = sqlite3.connect(core.config.USERS_DB)
        try:
            row = conn.execute(
                "SELECT id, email, display_name FROM users WHERE id = ?", (target_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        current_app.logger.exception("Failed to query impersonation target %s", target_id)
        return jsonify({"status": "error", "message": "查询用户失败"}), 500
    if not row:
        return jsonify({"status": "error", "message": "用户不存在"}), 404
    expires_at = int(time.time()) + IMPERSONATION_TTL_SECONDS
    session["impersonator_user_id"] = 1
    session["impersonated_user_id"] = target_id
    session["impersonation_expires_at"] = expires_at
    session["user_id"] = target_id
    session["logged_in"] = True
    session.permanent = True
    current_app.logger.warning(
        "Admin impersonation started: admin=1 target=%s ip=%s",
        target_id,
        request.remote_addr,
    )
    return jsonify({
        "status": "success", "user_id": target_id, "email": row[1],
        "display_name": row[2] or row[1], "expires_at": expires_at,
    })


@admin_bp.route("/api/admin/impersonation/status", methods=["GET"])
def api_admin_impersonation_status():
    target_id = session.get("impersonated_user_id")
    expires_at = session.get("impersonation_expires_at")
    active = str(session.get("impersonator_user_id")) == "1" and target_id is not None
    return jsonify({
        "active": active, "user_id": target_id if active else None,
        "expires_at": expires_at if active else None,
    })


@admin_bp.route("/api/admin/impersonation/exit", methods=["POST"])
def api_admin_impersonation_exit():
    target_id = session.get("impersonated_user_id") or session.get("user_id")
    if not _restore_admin_session():
        return jsonify({"status": "error", "message": "当前不在模拟登录状态"}), 403
    current_app.logger.warning(
        "Admin impersonation ended: admin=1 target=%s ip=%s",
        target_id,
        request.remote_addr,
    )
    return jsonify({"status": "success", "redirect": "/admin/dashboard"})
