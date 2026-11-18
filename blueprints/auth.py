from core.localization import user_language
from core.runtime import DATA_ROOT, LOCAL_TEST
import os
import json
from contextlib import closing
import sqlite3
import uuid
import re
import secrets
import ssl
import threading
import shutil
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

from flask import Blueprint, current_app, request, jsonify, session, redirect, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from pywebpush import webpush, WebPushException

from core.config import BASE_DIR, USERS_DB, USERS_ROOT, DEVICE_ACCOUNTS_FILE, USER_SETTINGS_FILE
from core.json_storage import update_user_settings
from core.utils import safe_save_json
from core.context import get_current_user_id
from core.session_security import establish_authenticated_session, get_auth_version
from core.password_recovery import COOLDOWN, Limited, RecoveryStore, Unavailable, source_address
from core.legal import (
    get_user_legal_status,
    init_legal_consents_table,
    record_current_legal_consents,
    validate_legal_acceptance,
)


SUBSCRIPTIONS_FILE = os.path.join(DATA_ROOT, "configs", "subscriptions.json")
SQUARE_DB = os.path.join(DATA_ROOT, "configs", "square.db")
FORUMS_DB = os.path.join(DATA_ROOT, "configs", "forums.db")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_CLAIMS = {"sub": "mailto:yyyyanshuo@foxmail.com"}

auth_bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------------
# 设备账号辅助函数
# ---------------------------------------------------------------------------

def _shared_config_lock(path_for_request):
    from functools import wraps
    from services.character_state import character_state_lock
    def decorate(func):
        @wraps(func)
        def locked(*args, **kwargs):
            with character_state_lock(path_for_request()):
                return func(*args, **kwargs)
        return locked
    return decorate


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


@_shared_config_lock(lambda: DEVICE_ACCOUNTS_FILE)
def _track_device_login(user_id: int, email: str, display_name: str) -> str:
    """
    在当前设备上记录一次登录：
    - 生成/读取 device_id（存入 cookie）
    - 在 DEVICE_ACCOUNTS_FILE 中记录该 device_id 下最近登录过的账号列表
    """
    device_id = request.cookies.get(("saki_global_device_id" if LOCAL_TEST else "device_id")) or uuid.uuid4().hex
    data = _load_device_accounts()
    entry = data.get(device_id, {})
    now_str = datetime.now().isoformat()
    entry[str(user_id)] = {
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
        "last_login": now_str,
        "auth_version": session["auth_version"],
    }
    data[device_id] = entry
    _save_device_accounts(data)
    return device_id


def _get_recent_device_accounts(max_age_days: int = 30) -> list[dict]:
    """
    返回当前设备在最近 max_age_days 天内登录过的账号列表。
    仅按 device_id 区分"同一设备"。
    """
    device_id = request.cookies.get(("saki_global_device_id" if LOCAL_TEST else "device_id"))
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
        try:
            info["user_id"] = int(uid_str)
        except Exception:
            pass
        results.append(info)

    results.sort(key=lambda x: x.get("last_login", ""), reverse=True)
    return results


# ---------------------------------------------------------------------------
# 用户工作区初始化
# ---------------------------------------------------------------------------

def init_user_workspace(user_id: int) -> None:
    """Generate new-account defaults without copying shared or personal data."""
    from core.user_workspace import initialize_user_workspace
    initialize_user_workspace(USERS_ROOT, user_id)


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

@auth_bp.route("/login")
def login_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("login.html")


@auth_bp.route("/forgot_password")
def forgot_password_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("forgot_password.html")


# ---------------------------------------------------------------------------
# 登录 / 协议 API
# ---------------------------------------------------------------------------

@auth_bp.route("/api/legal/status")
def legal_status_api():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    return jsonify({"status": "success", **get_user_legal_status(int(user_id))})


@auth_bp.route("/api/legal/consent", methods=["POST"])
def legal_consent_api():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "请先登录"}), 401

    data = request.get_json() or {}
    legal_valid, legal_message = validate_legal_acceptance(data)
    if not legal_valid:
        return jsonify({"status": "error", "message": legal_message}), 400

    conn = sqlite3.connect(USERS_DB)
    try:
        init_legal_consents_table(conn)
        record_current_legal_consents(conn, int(user_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", **get_user_legal_status(int(user_id))})


@auth_bp.route("/api/login", methods=["POST"])
def login_api():
    data = request.json or {}
    input_user = (data.get("username") or "").strip()
    input_pass = data.get("password") or ""

    # 1. 优先按邮箱在 users.db 里查找（新多用户逻辑）
    if input_user and input_pass:
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            cur.execute("SELECT id, email, password_hash, display_name, COALESCE(auth_version,1) FROM users WHERE email = ?", (input_user.lower(),))
            row = cur.fetchone()
            conn.close()
            if row and check_password_hash(row[2], input_pass):
                user_id = row[0]
                email = row[1]
                display_name = row[3] or email

                establish_authenticated_session(user_id)
                # A password reset between the credential read and session creation
                # must not turn an old password into a grant for the new version.
                if session['auth_version'] != row[4]:
                    session.clear()
                    return jsonify({"status": "error", "message": "用户名或密码错误"}), 401

                from core.account_lifecycle import pending
                if pending(user_id):
                    return jsonify(status="success", account_deleting=True)
                device_id = _track_device_login(user_id, email=email, display_name=display_name)
                resp = jsonify({"status": "success"})
                resp.set_cookie(
                    ("saki_global_device_id" if LOCAL_TEST else "device_id"), device_id, max_age=30 * 24 * 3600,
                    secure=bool(current_app.config.get("SESSION_COOKIE_SECURE")), httponly=True, samesite="Lax",
                )
                return resp
        except Exception as e:
            print(f"[Login] users.db 查询失败: {e}")

    # 2. 旧明文账号登录默认关闭，只用于一次性的受控迁移。
    if os.getenv("ALLOW_LEGACY_PLAINTEXT_LOGIN", "false").strip().lower() != "true":
        return jsonify({"status": "error", "message": "用户名或密码错误"}), 401

    saved_user = ""
    saved_pass = ""
    user_data = {}
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                saved_user = str(user_data.get("current_user_name") or "").strip()
                saved_pass = str(user_data.get("password") or "")
        except Exception:
            pass

    if saved_user and saved_pass and input_user == saved_user and input_pass == saved_pass:
        try:
            conn = sqlite3.connect(USERS_DB)
            cur = conn.cursor()
            email = (user_data.get("email") or f"{saved_user}@local").lower()
            cur.execute("SELECT id FROM users WHERE email = ?", (email,))
            row = cur.fetchone()
            if row:
                user_id = row[0]
            else:
                cur.execute(
                    "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                    (email, generate_password_hash(saved_pass), saved_user, datetime.now().isoformat())
                )
                user_id = cur.lastrowid
                conn.commit()
            conn.close()

            establish_authenticated_session(user_id)

            # 数据库账号已建立后立即清除旧配置中的明文登录密码。
            if "password" in user_data:
                update_user_settings(USER_SETTINGS_FILE, {}, remove=("password",))

            device_id = _track_device_login(user_id, email=email, display_name=saved_user)
            resp = jsonify({"status": "success"})
            resp.set_cookie(
                ("saki_global_device_id" if LOCAL_TEST else "device_id"), device_id, max_age=30 * 24 * 3600,
                secure=bool(current_app.config.get("SESSION_COOKIE_SECURE")), httponly=True, samesite="Lax",
            )
            return resp
        except Exception as e:
            print(f"[Login] 旧账号迁移失败: {e}")
            return jsonify({"status": "error", "message": "旧账号迁移失败，请联系管理员"}), 500

    return jsonify({"status": "error", "message": "用户名或密码错误"}), 401


# ---------------------------------------------------------------------------
# 忘记密码
# ---------------------------------------------------------------------------

def _reset_response(message, status=400, *, retry=None, field=None):
    from core.localization import tr
    data = {'status': 'accepted' if status == 202 else ('success' if status == 200 else 'error'),
            'message': tr(message)}
    if field:
        data['error_type'] = field
    response = jsonify(data)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    if retry is not None:
        response.headers['Retry-After'] = str(retry)
    return response


def _reset_input():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}, ''
    raw = data.get('email')
    email = raw.strip().lower() if isinstance(raw, str) else ''
    if (len(email) > 254 or email.count('@') != 1 or not all(email.split('@')) or
            any(c.isspace() or not c.isprintable() for c in email)):
        email = ''
    return data, email


def _recovery_store():
    return RecoveryStore(USERS_DB, current_app.secret_key)


def _reset_mail(email, uid, code):
    from core.branding import brand_name
    from core.localization import tr
    language = user_language(uid)
    brand = brand_name(language)
    sender, host = os.environ['MAIL_SENDER'], os.environ['MAIL_SERVER']
    msg = MIMEText(tr('reset_body', language, brand=brand, code=code), 'plain', 'utf-8')
    msg['From'] = formataddr([brand, sender]); msg['To'] = email
    msg['Subject'] = Header(tr('reset_subject', language, brand=brand), 'utf-8')
    port = int(os.getenv('MAIL_PORT', '465'))
    tls = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=8, context=tls)
    else:
        smtp = smtplib.SMTP(host, port, timeout=8)
    try:
        if port != 465:
            smtp.starttls(context=tls)
        smtp.login(sender, os.environ['MAIL_PASSWORD'])
        # Returning from sendmail means acceptance by SMTP, not inbox delivery.
        if smtp.sendmail(sender, [email], msg.as_bytes()):
            raise smtplib.SMTPRecipientsRefused({})
    finally:
        smtp.close()  # An error in a later QUIT must not invalidate accepted mail.


def _deliver_reset(app_obj, users_db, store, email, ticket):
    accepted = None
    transport_down = False
    from core.account_lifecycle import activity, DeletionPending
    lease = None
    with app_obj.app_context():
        try:
            with closing(sqlite3.connect(users_db)) as db:
                row = db.execute('SELECT id,COALESCE(auth_version,1) FROM users WHERE email=?', (email,)).fetchone()
            if row:
                lease = activity(row[0], os.path.dirname(os.path.dirname(users_db)))
                code = f'{secrets.randbelow(1_000_000):06d}'
                _reset_mail(email, row[0], code)
                accepted = (*row, code)
        except DeletionPending:
            pass
        except smtplib.SMTPRecipientsRefused:
            app_obj.logger.warning('Password recovery: recipient rejected')
        except (OSError, smtplib.SMTPException, KeyError, ValueError) as exc:
            transport_down = True
            app_obj.logger.warning('Password recovery: transport unavailable (%s)', type(exc).__name__)
        except Exception as exc:
            app_obj.logger.error('Password recovery: worker failed (%s)', type(exc).__name__)
        finally:
            try:
                store.finish(email, ticket, accepted=accepted, transport_down=transport_down)
            except (OSError, sqlite3.Error) as exc:
                app_obj.logger.error('Password recovery: delivery state unavailable (%s)', type(exc).__name__)
            finally:
                if lease is not None:
                    lease.close()


@auth_bp.route('/api/forgot_password/send_code', methods=['POST', 'GET'])
def forgot_password_send_code():
    if request.method in ('GET', 'HEAD'):
        try:
            retry = _recovery_store().transport_retry_after()
        except (OSError, sqlite3.Error):
            retry = 30
        if retry:
            return _reset_response('找回密码服务暂时不可用，请稍后重试。', 503, retry=retry)
        return '', 204, {'Cache-Control': 'no-store'}
    _, email = _reset_input()
    if not email:
        return _reset_response('请输入有效的邮箱地址', field='email')
    store = _recovery_store()
    try:
        ticket = store.reserve(email, source_address(request.remote_addr, request.headers.get('X-Real-IP')))
        try:
            threading.Thread(target=_deliver_reset,
                             args=(current_app._get_current_object(), USERS_DB, store, email, ticket),
                             daemon=True).start()
        except (OSError, RuntimeError):
            store.finish(email, ticket)
            raise Unavailable()
    except Limited as exc:
        return _reset_response('请求过于频繁，请稍后重试。', 429, retry=exc.retry_after)
    except (OSError, sqlite3.Error, Unavailable):
        return _reset_response('找回密码服务暂时不可用，请稍后重试。', 503, retry=30)
    # Lookup and delivery happen after admission; never reveal membership or say mail was sent.
    return _reset_response('已受理请求。如果该邮箱绑定了账号，请稍后查看收件箱和垃圾邮件；持续未收到请联系支持。', 202, retry=COOLDOWN)


@auth_bp.route('/api/forgot_password/reset', methods=['POST'])
def forgot_password_reset():
    data, email = _reset_input()
    password, code = data.get('password'), data.get('code')
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        return _reset_response('新密码长度应为 8 至 256 个字符。', field='password')
    if not email or not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        return _reset_response('验证码错误或已失效', field='code')
    try:
        grant = _recovery_store().claim(email, source_address(request.remote_addr, request.headers.get('X-Real-IP')), code)
        if grant is None:
            return _reset_response('验证码错误或已失效', field='code')
        # Consume first (fail closed). Even a second challenge racing a password change
        # cannot overwrite it: the user DB independently checks the issue-time version.
        uid, version = grant
        digest = generate_password_hash(password)
        with closing(sqlite3.connect(USERS_DB)) as db, db:
            changed = db.execute('''UPDATE users SET password_hash=?,auth_version=COALESCE(auth_version,1)+1
                WHERE id=? AND email=? AND COALESCE(auth_version,1)=?''', (digest, uid, email, version)).rowcount
        if not changed:
            return _reset_response('验证码错误或已失效', field='code')
        return _reset_response('密码重置成功，请重新登录', 200)
    except Limited as exc:
        return _reset_response('请求过于频繁，请稍后重试。', 429, retry=exc.retry_after)
    except (OSError, sqlite3.Error):
        return _reset_response('找回密码服务暂时不可用，请稍后重试。', 503, retry=30)


@auth_bp.route("/api/user/change_password", methods=["POST"])
def change_password():
    current_uid = get_current_user_id()
    if not current_uid:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    if session.get("impersonator_user_id"):
        return jsonify({"status": "error", "message": "管理员模拟登录期间不能修改密码"}), 403
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"status": "error", "message": "请求来源校验失败"}), 403

    data = request.get_json(silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""
    if not current_password or not new_password:
        return jsonify({"status": "error", "message": "请输入当前密码和新密码"}), 400
    if len(new_password) < 8:
        return jsonify({"status": "error", "message": "新密码至少需要 8 个字符"}), 400
    if current_password == new_password:
        return jsonify({"status": "error", "message": "新密码不能与当前密码相同"}), 400

    conn = sqlite3.connect(USERS_DB)
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (int(current_uid),)).fetchone()
        if not row or not row[0] or not check_password_hash(row[0], current_password):
            return jsonify({"status": "error", "message": "当前密码不正确"}), 403
        conn.execute(
            "UPDATE users SET password_hash = ?, auth_version = COALESCE(auth_version, 1) + 1 WHERE id = ?",
            (generate_password_hash(new_password), int(current_uid)),
        )
        conn.commit()
    finally:
        conn.close()

    settings_path = os.path.join(USERS_ROOT, str(int(current_uid)), "configs", "user_settings.json")
    if os.path.exists(settings_path):
        try:
            update_user_settings(settings_path, {}, remove=("password",))
        except Exception:
            current_app.logger.exception("Failed to remove legacy plaintext password for user %s", current_uid)

    _clear_auth_session()
    response = jsonify({"status": "success", "message": "密码已修改，请重新登录", "requires_login": True})
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# 登出 / 账号切换
# ---------------------------------------------------------------------------

def _clear_auth_session():
    session.pop('user_id', None)
    session.pop('logged_in', None)
    session.pop('auth_version', None)
    session.pop('impersonator_user_id', None)
    session.pop('impersonated_user_id', None)
    session.pop('impersonation_expires_at', None)
    session.pop('impersonation_expired', None)


@auth_bp.route("/logout")
def logout():
    _clear_auth_session()
    return redirect('/login')


@_shared_config_lock(lambda: DEVICE_ACCOUNTS_FILE)
def _remove_user_from_device_accounts(user_id: int) -> None:
    user_key = str(user_id)
    from services.memory_store import load_json_object
    all_devices = load_json_object(DEVICE_ACCOUNTS_FILE)
    changed = False

    for device_id in list(all_devices.keys()):
        accounts = all_devices.get(device_id)
        if not isinstance(accounts, dict):
            continue
        if user_key in accounts:
            accounts.pop(user_key, None)
            changed = True
        if not accounts:
            all_devices.pop(device_id, None)
            changed = True

    if changed:
        _save_device_accounts(all_devices)


@_shared_config_lock(lambda: SUBSCRIPTIONS_FILE)
def _remove_user_subscriptions(user_id: int) -> None:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return

    with open(SUBSCRIPTIONS_FILE, encoding="utf-8") as handle:
        all_subs = json.load(handle)
    if isinstance(all_subs, list):
        return  # Legacy unowned subscription list.
    if not isinstance(all_subs, dict):
        raise ValueError("Invalid subscription store")

    user_key = str(user_id)
    if user_key in all_subs:
        all_subs.pop(user_key, None)
        safe_save_json(SUBSCRIPTIONS_FILE, all_subs)


def _safe_remove_user_workspace(user_id: int) -> None:
    user_key = str(int(user_id))
    root = os.path.abspath(USERS_ROOT)
    target = os.path.abspath(os.path.join(root, user_key))
    if not target.startswith(root + os.sep):
        raise ValueError("Unsafe user workspace path")
    if os.path.isdir(target):
        shutil.rmtree(target)


def _delete_user_forums(user_id: int) -> None:
    if not os.path.exists(FORUMS_DB):
        return

    conn = sqlite3.connect(FORUMS_DB)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM forums WHERE user_id = ?", (int(user_id),))
        if cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_backgrounds'").fetchone():
            cur.execute("DELETE FROM forum_backgrounds WHERE user_id = ?", (int(user_id),))
        if cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_tasks'").fetchone():
            cur.execute("DELETE FROM forum_tasks WHERE user_id=?", (int(user_id),))
        conn.commit()
    finally:
        conn.close()


def _cleanup_square_user_data(user_id: int, email: str) -> None:
    if not os.path.exists(SQUARE_DB):
        return

    conn = sqlite3.connect(SQUARE_DB)
    try:
        from core.account_cleanup import clean_square
        clean_square(conn, user_id, email, users_root=USERS_ROOT)
        conn.commit()
    finally:
        conn.close()


def _delete_user_account_record(user_id: int) -> None:
    conn = sqlite3.connect(USERS_DB)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM circuit_breaker WHERE user_id = ?", (int(user_id),))
        from core.account_cleanup import columns
        if "user_id" in columns(conn, "legal_consents"):
            cur.execute("DELETE FROM legal_consents WHERE user_id = ?", (int(user_id),))
        cur.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        conn.commit()
    finally:
        conn.close()


@auth_bp.route("/api/account/delete", methods=["POST"])
def delete_account():
    current_uid = get_current_user_id()
    if not current_uid:
        return jsonify({"status": "error", "message": "请先登录"}), 401

    if session.get("impersonator_user_id"):
        return jsonify({"status": "error", "message": "请先退出管理员模拟登录"}), 403

    data = request.get_json() or {}
    password = data.get("password") or ""
    confirm_text = (data.get("confirm_text") or "").strip()

    if confirm_text not in {"注销账户", "アカウント削除", "DELETE ACCOUNT"}:
        return jsonify({"status": "error", "message": "请输入“注销账户”确认操作"}), 400
    if not password:
        return jsonify({"status": "error", "message": "请输入当前密码"}), 400

    conn = sqlite3.connect(USERS_DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT email, password_hash FROM users WHERE id = ?", (int(current_uid),))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        _clear_auth_session()
        return jsonify({"status": "error", "message": "账号不存在或已注销"}), 404

    email, password_hash = row
    if not password_hash or not check_password_hash(password_hash, password):
        return jsonify({"status": "error", "message": "当前密码不正确"}), 403

    from pathlib import Path
    from core.account_lifecycle import deletion, step, DeletionPending
    from core.account_cleanup import preserve_square_avatars, clean_image_usage, unlink_sticker_packs
    from cos_utils import delete_user_objects
    root = Path(USERS_ROOT).parent
    try:
        with deletion(current_uid, root) as marker:
            step(marker, 'public-avatars', preserve_square_avatars, SQUARE_DB, current_uid, email)
            step(marker, 'cloud', delete_user_objects, current_uid)
            step(marker, 'image-usage', clean_image_usage, Path(USERS_DB).with_name('image_usage.db'), current_uid)
            step(marker, 'stickers', unlink_sticker_packs, root / 'stickers', current_uid)
            step(marker, 'forums', _delete_user_forums, current_uid)
            step(marker, 'square', _cleanup_square_user_data, current_uid, email)
            step(marker, 'devices', _remove_user_from_device_accounts, current_uid)
            step(marker, 'subscriptions', _remove_user_subscriptions, current_uid)
            recovery = _recovery_store()
            if recovery.path.is_file():
                with sqlite3.connect(recovery.path) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='challenges'").fetchone():
                        db.execute("DELETE FROM challenges WHERE account=? OR uid=?", (recovery.tag('email', email), current_uid))
            maintenance = Path(USERS_DB).with_name('maintenance_recovery.db')
            if maintenance.exists():
                with sqlite3.connect(maintenance) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work'").fetchone():
                        db.execute("DELETE FROM work WHERE owner=?", (str(current_uid),))
            if (root / '.agent-runtime').is_dir():
                from core.web_agent import discard_account_jobs
                discard_account_jobs(root, current_uid)
            step(marker, 'workspace', _safe_remove_user_workspace, current_uid)
            _delete_user_account_record(current_uid)
            _clear_auth_session()
    except DeletionPending:
        return jsonify(status="pending", message="后台任务正在结束，请稍后重试注销。"), 409
    except Exception as exc:
        current_app.logger.error("Account deletion paused: user=%s type=%s", current_uid, type(exc).__name__)
        return jsonify(status="pending", message="注销尚未完成，进度已保存，请稍后重试。"), 503

    response = jsonify({"status": "success", "message": "账号已注销"})
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    return response


@auth_bp.route("/api/accounts/recent", methods=["GET"])
def get_recent_accounts():
    """
    返回当前设备最近 30 天内登录过的账号列表，用于前端「切换账号」下拉。
    只在当前已登录时可用。
    """
    current_uid = get_current_user_id()
    if not current_uid:
        return jsonify([]), 401

    accounts = _get_recent_device_accounts(max_age_days=30)

    seen_ids = {acc.get("user_id") for acc in accounts}
    if current_uid not in seen_ids:
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


@auth_bp.route("/api/accounts/switch", methods=["POST"])
@_shared_config_lock(lambda: DEVICE_ACCOUNTS_FILE)
def switch_account():
    """
    在同一设备上，在最近 30 天内登录过的账号之间进行切换，而无需重新输入密码。
    通过 device_id + DEVICE_ACCOUNTS_FILE 校验权限。
    """
    current_uid = get_current_user_id()
    if not current_uid:
        return jsonify({"status": "error", "message": "尚未登录"}), 401

    if session.get("impersonator_user_id"):
        return jsonify({"status": "error", "message": "请先退出管理员模拟登录"}), 403

    data = request.get_json() or {}
    target_id = data.get("user_id")
    try:
        target_id = int(target_id)
    except Exception:
        return jsonify({"status": "error", "message": "无效的目标账号"}), 400

    device_id = request.cookies.get(("saki_global_device_id" if LOCAL_TEST else "device_id"))
    if not device_id or not os.path.exists(DEVICE_ACCOUNTS_FILE):
        return jsonify({"status": "error", "message": "当前设备暂无可切换账号"}), 403

    all_devices = _load_device_accounts()
    entries = all_devices.get(device_id, {})
    info = entries.get(str(target_id))
    if not info:
        return jsonify({"status": "error", "message": "目标账号未在本设备登录过"}), 403

    try:
        ts = info.get("last_login")
        if not ts:
            raise ValueError("no ts")
        dt = datetime.fromisoformat(ts)
        if (datetime.now() - dt).days > 30:
            return jsonify({"status": "error", "message": "该账号登录已超过 30 天，请重新登录"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "无法确认登录时间，请重新登录该账号"}), 403

    version = get_auth_version(target_id)
    if version is None:
        return jsonify({"status": "error", "message": "用户不存在"}), 404

    # Legacy grants predate version binding: only accept the initial account epoch.
    if info.get("auth_version", 1) != version:
        return jsonify({"status": "error", "message": "账号凭据已更新，请重新登录"}), 403

    # Drop account-scoped memory context and pending UI preferences. A switch
    # must not carry the previous account's state into the destination.
    previous_session = dict(session)
    try:
        session.clear()
        establish_authenticated_session(target_id)
        if session['auth_version'] != version:
            session.clear()
            session.update(previous_session)
            return jsonify({"status": "error", "message": "账号凭据已更新，请重新登录"}), 403
    except Exception:
        session.clear()
        session.update(previous_session)
        raise

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


# ---------------------------------------------------------------------------
# 推送通知 (Web Push / VAPID)
# ---------------------------------------------------------------------------

@auth_bp.route("/api/subscribe", methods=["POST"])
@_shared_config_lock(lambda: SUBSCRIPTIONS_FILE)
def subscribe():
    subscription = request.json
    if not subscription:
        return jsonify({"error": "No data"}), 400

    current_user_id = get_current_user_id()
    if current_user_id is None:
        return jsonify({"error": "Not logged in"}), 401
    user_id = str(current_user_id)

    endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
    if not endpoint:
        return jsonify({"error": "Invalid subscription"}), 400

    all_subs = {}
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            try:
                all_subs = json.load(f)
            except Exception:
                pass

    # 旧版数组无法判断订阅属于哪个用户，绝不能继续作为广播列表使用。
    if not isinstance(all_subs, dict):
        all_subs = {}

    # 同一设备可以由同一个人显式订阅多个账号；发送时仍按 user_id 隔离。
    # 这里只在当前账号内按 endpoint 去重，并用最新密钥覆盖旧订阅。
    user_subs = all_subs.get(user_id, [])
    if not isinstance(user_subs, list):
        user_subs = []
    user_subs = [
        item for item in user_subs
        if not (isinstance(item, dict) and item.get("endpoint") == endpoint)
    ]
    user_subs.append(subscription)
    all_subs[user_id] = user_subs
    safe_save_json(SUBSCRIPTIONS_FILE, all_subs)

    return jsonify({"status": "success"})


@auth_bp.route("/api/vapid_public_key")
def get_vapid_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


def send_push_notification(title, body, url="/", user_id=None):
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return

    with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
        all_subs = json.load(f)

    # 推送必须明确指定用户。旧版数组和缺少 user_id 的调用一律拒绝，
    # 防止任意用户的消息被广播给所有订阅设备。
    if not isinstance(all_subs, dict) or user_id is None:
        print("⚠️ [Push] 已阻止不安全的广播推送")
        return

    user_id = str(user_id)
    target_subs = all_subs.get(user_id, [])

    if not target_subs:
        return

    print(f"🔔 [Push] 正在向用户 {user_id or '全部'} 的 {len(target_subs)} 个设备发送通知...")

    cleanup_needed = False
    valid_subs = []

    for sub_info in target_subs:
        try:
            claims = dict(VAPID_CLAIMS)
            webpush(
                subscription_info=sub_info,
                data=json.dumps({"title": title, "body": body, "url": url, "language": user_language(user_id)}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=claims,
            )
            valid_subs.append(sub_info)
        except WebPushException as ex:
            if ex.response and ex.response.status_code == 410:
                print("   - 设备已取消订阅，移除")
                cleanup_needed = True
            else:
                print(f"   - 推送失败: {ex}")
                valid_subs.append(sub_info)

    if cleanup_needed:
        from core.account_cleanup import prune_expired_subscriptions
        prune_expired_subscriptions(SUBSCRIPTIONS_FILE, user_id, target_subs, valid_subs)
