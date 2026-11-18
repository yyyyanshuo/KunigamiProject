import re
import secrets
import shutil
import sqlite3
import string
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, request, session, jsonify, redirect, render_template
from werkzeug.security import generate_password_hash
import core.config
from core.session_security import establish_authenticated_session, get_auth_version
from core.admin_permissions import admin_required, is_admin
from core.localization import tr
from core.user_workspace import initialize_user_workspace

admin_bp = Blueprint("admin", __name__)

IMPERSONATION_TTL_SECONDS = 15 * 60


def _restore_admin_session():
    """结束模拟登录并恢复原管理员身份。"""
    admin_id = session.pop("impersonator_user_id", None)
    if admin_id is None:
        return False
    session.pop("impersonated_user_id", None)
    session.pop("impersonation_expires_at", None)
    session.pop("impersonation_expired", None)
    original_version = session.pop("impersonator_auth_version", None)
    current_version = get_auth_version(admin_id) if is_admin(admin_id) else None
    if current_version is not None and (original_version is None or original_version == current_version):
        establish_authenticated_session(admin_id)
        return True
    session.clear()
    return False


@admin_bp.before_app_request
def expire_admin_impersonation():
    """模拟登录超过 15 分钟后，在下一次请求时自动恢复管理员。"""
    original_id = session.get("impersonator_user_id")
    if original_id is not None:
        current_version = get_auth_version(original_id) if is_admin(original_id) else None
        original_version = session.get("impersonator_auth_version")
        if current_version is None or (original_version is not None and original_version != current_version):
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(status="error", message=tr("管理员权限已失效，请重新登录"), redirect="/login"), 401
            return redirect("/login")
    if session.get("impersonation_expired"):
        if request.endpoint == "admin.admin_dashboard":
            session.pop("impersonation_expired", None)
            return None
        if request.path.startswith("/api/"):
            return jsonify({
                "status": "error",
                "message": tr("管理员模拟登录已到期"),
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
                "Admin impersonation expired: admin=%s target=%s ip=%s",
                original_id, target_id,
                request.remote_addr,
            )
            if request.path.startswith("/api/"):
                return jsonify({
                    "status": "error",
                    "message": tr("管理员模拟登录已到期"),
                    "redirect": "/admin/dashboard",
                }), 440
            return redirect("/admin/dashboard")
    return None


@admin_bp.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template("admin/dashboard.html")


@admin_bp.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    """Create an account without changing the administrator's login session."""
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify(status="error", message=tr("请求来源校验失败")), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(status="error", message=tr("请求无效，请检查输入后重试。")), 400
    email = data.get("email")
    display_name = data.get("display_name", "")
    if not isinstance(email, str) or not isinstance(display_name, str):
        return jsonify(status="error", message=tr("请求无效，请检查输入后重试。")), 400
    email = email.strip().lower()
    display_name = display_name.strip()
    email_pattern = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
    if len(email) > 254 or not re.fullmatch(email_pattern, email):
        return jsonify(status="error", message=tr("请输入有效的邮箱地址")), 400
    if len(display_name) > 100:
        return jsonify(status="error", message=tr("昵称不能超过 100 个字符")), 400
    display_name = display_name or email

    # Each password has 24 random characters and all four character classes.
    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%_-+")
    alphabet = "".join(groups)
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        if all(any(char in group for char in password) for group in groups):
            break
    password_hash = generate_password_hash(password)
    conn = None
    workspace = None
    try:
        conn = sqlite3.connect(core.config.USERS_DB)
        # Serialize duplicate checks and workspace allocation across requests.
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT id FROM users WHERE lower(email) = ?", (email,)).fetchone():
            return jsonify(status="error", message=tr("该邮箱已有账号")), 409
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
            (email, password_hash, display_name, datetime.now().isoformat()),
        )
        user_id = cursor.lastrowid
        new_workspace = Path(core.config.USERS_ROOT) / str(user_id)
        # Never reuse an orphaned workspace that could contain someone else's data.
        new_workspace.mkdir(parents=True, exist_ok=False)
        workspace = new_workspace
        initialize_user_workspace(core.config.USERS_ROOT, user_id)
        # The user must acknowledge legal documents themselves after signing in.
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        current_app.logger.exception("Admin account creation failed: admin=%s", session["user_id"])
        return jsonify(status="error", message=tr("创建账号失败，请稍后重试")), 500
    finally:
        if conn is not None:
            conn.close()

    current_app.logger.info("Admin created account: admin=%s user=%s", session["user_id"], user_id)
    response = jsonify(
        status="success", user_id=user_id, email=email,
        display_name=display_name, initial_password=password,
    )
    # The plaintext password is returned only by this creation response.
    response.headers["Cache-Control"] = "no-store"
    return response, 201


@admin_bp.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    try:
        from admin_stats import generate_admin_stats
        return jsonify(generate_admin_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/refresh_stickers", methods=["GET"])
@admin_required
def api_admin_refresh_stickers():
    try:
        core.config.CACHED_OFFICIAL_PACKS = None
        return jsonify({"status": "success", "message": tr("表情包缓存已清除")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/circuit_status")
@admin_required
def api_admin_circuit_status():
    try:
        from core.circuit_breaker import get_all_circuit_status
        return jsonify(get_all_circuit_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/user/<int:user_id>/unfreeze", methods=["POST"])
@admin_required
def api_admin_unfreeze_user(user_id):
    try:
        from core.circuit_breaker import unfreeze_user
        ok = unfreeze_user(user_id)
        if ok:
            return jsonify({"status": "success", "message": tr("用户 {user_id} 的熔断限制已清除", user_id=user_id)})
        else:
            return jsonify({"error": "Failed to unfreeze"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/impersonate", methods=["POST"])
@admin_required
def api_admin_impersonate():
    """临时排查普通用户账号，结束后恢复实际发起的管理员。"""
    admin_id = int(session["user_id"])
    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": tr("请输入有效的用户 ID")}), 400
    if target_id <= 0 or is_admin(target_id):
        return jsonify({"status": "error", "message": tr("不能模拟该用户")}), 400
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
        return jsonify({"status": "error", "message": tr("查询用户失败")}), 500
    if not row:
        return jsonify({"status": "error", "message": tr("用户不存在")}), 404
    expires_at = int(time.time()) + IMPERSONATION_TTL_SECONDS
    session["impersonator_user_id"] = admin_id
    session["impersonator_auth_version"] = get_auth_version(admin_id)
    session["impersonated_user_id"] = target_id
    session["impersonation_expires_at"] = expires_at
    establish_authenticated_session(target_id)
    current_app.logger.warning(
        "Admin impersonation started: admin=%s target=%s ip=%s",
        admin_id, target_id,
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
    active = is_admin(session.get("impersonator_user_id")) and target_id is not None and session.get("user_id") == target_id
    return jsonify({
        "active": active, "user_id": target_id if active else None,
        "expires_at": expires_at if active else None,
    })


@admin_bp.route("/api/admin/impersonation/exit", methods=["POST"])
def api_admin_impersonation_exit():
    admin_id = session.get("impersonator_user_id")
    target_id = session.get("impersonated_user_id") or session.get("user_id")
    if not _restore_admin_session():
        return jsonify({"status": "error", "message": tr("当前不在模拟登录状态")}), 403
    current_app.logger.warning(
        "Admin impersonation ended: admin=%s target=%s ip=%s",
        admin_id, target_id,
        request.remote_addr,
    )
    return jsonify({"status": "success", "redirect": "/admin/dashboard"})
