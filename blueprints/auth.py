import os
import json
import time
import sqlite3
import uuid
import random
import threading
import shutil
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

from flask import Blueprint, request, jsonify, session, redirect, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from pywebpush import webpush, WebPushException

from core.config import BASE_DIR, USERS_DB, USERS_ROOT, DEVICE_ACCOUNTS_FILE, USER_SETTINGS_FILE
from core.utils import safe_save_json
from core.context import get_current_user_id


SUBSCRIPTIONS_FILE = os.path.join(BASE_DIR, "configs", "subscriptions.json")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_CLAIMS = {"sub": "mailto:yyyyanshuo@foxmail.com"}

auth_bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------------
# 设备账号辅助函数
# ---------------------------------------------------------------------------

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
    仅按 device_id 区分"同一设备"。
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

        chars_root = os.path.join(user_root, "characters")
        groups_root = os.path.join(user_root, "groups")
        configs_root = os.path.join(user_root, "configs")
        logs_root = os.path.join(user_root, "logs")
        for d in (chars_root, groups_root, configs_root, logs_root):
            os.makedirs(d, exist_ok=True)

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


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

@auth_bp.route("/login")
def login_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("login.html")


@auth_bp.route("/register")
def register_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("register.html")


@auth_bp.route("/forgot_password")
def forgot_password_page():
    if 'user_id' in session or 'logged_in' in session:
        return redirect('/')
    return render_template("forgot_password.html")


# ---------------------------------------------------------------------------
# 注册 / 登录 API
# ---------------------------------------------------------------------------

@auth_bp.route("/api/register", methods=["POST"])
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

        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"status": "error", "message": "该邮箱已被注册"}), 400

        cur.execute(
            "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
            (email, generate_password_hash(password), display_name, datetime.now().isoformat())
        )
        user_id = cur.lastrowid
        conn.commit()
        conn.close()

        init_user_workspace(user_id)

        session['user_id'] = user_id
        session['logged_in'] = True
        session.permanent = True

        device_id = _track_device_login(user_id, email=email, display_name=display_name)
        resp = jsonify({"status": "success"})
        resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
        return resp
    except Exception as e:
        print(f"[Register] 注册失败: {e}")
        return jsonify({"status": "error", "message": "服务器错误"}), 500


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
                saved_pass = user_data.get("password", "123456")
        except Exception:
            pass

    if input_user == saved_user and input_pass == saved_pass:
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

            session['user_id'] = user_id
            session['logged_in'] = True
            session.permanent = True

            device_id = _track_device_login(user_id, email=email, display_name=saved_user)
            resp = jsonify({"status": "success"})
            resp.set_cookie("device_id", device_id, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
            return resp
        except Exception as e:
            print(f"[Login] 旧账号迁移失败: {e}")
            session['logged_in'] = True
            session.permanent = True
            return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "用户名或密码错误"}), 401


# ---------------------------------------------------------------------------
# 忘记密码
# ---------------------------------------------------------------------------

reset_codes = {}


@auth_bp.route("/api/forgot_password/send_code", methods=["POST"])
def forgot_password_send_code():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"status": "error", "message": "请输入有效的邮箱"}), 400

    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        conn.close()

        if not user:
            return jsonify({"status": "error", "message": "该邮箱未注册", "error_type": "email"}), 404

        code = str(random.randint(100000, 999999))

        reset_codes[email] = {
            "code": code,
            "expire": time.time() + 600
        }

        subject = "Kunigami AI - 重置密码验证码"
        content = f"您好！您正尝试为 Kunigami AI 账号重置密码。\n\n您的验证码是：{code}\n\n该验证码 10 分钟内有效。如果不是您本人操作，请忽略此邮件。"

        print(f"DEBUG: Attempting to send reset code {code} to {email}")

        def _send_reset_email(addr, sub, body):
            sender = os.getenv("MAIL_SENDER")
            pwd = os.getenv("MAIL_PASSWORD")
            host = os.getenv("MAIL_SERVER", "smtp.qq.com")
            port = int(os.getenv("MAIL_PORT", 465))

            if not sender or not pwd:
                print("❌ [Reset] 配置缺失: MAIL_SENDER 或 MAIL_PASSWORD")
                return

            try:
                msg = MIMEText(body, 'plain', 'utf-8')
                msg['From'] = formataddr(["Kunigami AI", sender])
                msg['To'] = formataddr(["User", addr])
                msg['Subject'] = Header(sub, 'utf-8')

                if port == 465:
                    server = smtplib.SMTP_SSL(host, port, timeout=10)
                else:
                    server = smtplib.SMTP(host, port, timeout=10)
                    if port == 587:
                        server.starttls()

                server.login(sender, pwd)
                server.sendmail(sender, [addr], msg.as_string())
                server.quit()
                print(f"📧 [Reset] 成功发送验证码到 {addr}")
            except Exception as e:
                print(f"❌ [Reset] 验证码发送失败: {e}")
                if "Connection unexpectedly closed" in str(e) and port == 465:
                    print("🔄 [Reset] 尝试使用 STARTTLS (587端口) 重试...")
                    try:
                        server = smtplib.SMTP(host, 587, timeout=10)
                        server.starttls()
                        server.login(sender, pwd)
                        server.sendmail(sender, [addr], msg.as_string())
                        server.quit()
                        print(f"📧 [Reset] 通过 587 端口重试成功！")
                    except Exception as e2:
                        print(f"❌ [Reset] 587 端口重试也失败: {e2}")

        threading.Thread(target=_send_reset_email, args=(email, subject, content)).start()

        return jsonify({"status": "success", "message": "验证码已发送至您的邮箱"})

    except Exception as e:
        print(f"[Reset] 数据库异常: {e}")
        return jsonify({"status": "error", "message": "系统繁忙，请重试"}), 500


@auth_bp.route("/api/forgot_password/reset", methods=["POST"])
def forgot_password_reset():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = data.get("password") or ""

    if not email or not code or not new_password:
        return jsonify({"status": "error", "message": "请填写完整信息"}), 400

    record = reset_codes.get(email)
    if not record:
        return jsonify({"status": "error", "message": "验证码错误或已失效", "error_type": "code"}), 400

    if time.time() > record["expire"]:
        del reset_codes[email]
        return jsonify({"status": "error", "message": "验证码已过期，请重新发送", "error_type": "code"}), 400

    target_code = str(record["code"]).strip()
    input_code = str(code).strip()

    if target_code != input_code:
        return jsonify({"status": "error", "message": "验证码输入错误", "error_type": "code"}), 400

    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()

        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"status": "error", "message": "找回失败：该邮箱账号不存在", "error_type": "email"}), 404

        password_hash = generate_password_hash(new_password)
        cur.execute("UPDATE users SET password_hash = ? WHERE email = ?", (password_hash, email))
        conn.commit()
        conn.close()

        del reset_codes[email]

        return jsonify({"status": "success", "message": "密码重置成功"})

    except Exception as e:
        print(f"[Reset] 更新密码失败: {e}")
        return jsonify({"status": "error", "message": "系统错误，请联系管理员"}), 500


# ---------------------------------------------------------------------------
# 登出 / 账号切换
# ---------------------------------------------------------------------------

@auth_bp.route("/logout")
def logout():
    session.pop('user_id', None)
    session.pop('logged_in', None)
    session.pop('impersonator_user_id', None)
    session.pop('impersonated_user_id', None)
    session.pop('impersonation_expires_at', None)
    session.pop('impersonation_expired', None)
    return redirect('/login')


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

    device_id = request.cookies.get("device_id")
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

    session['user_id'] = target_id
    session['logged_in'] = True
    session.permanent = True

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
                data=json.dumps({"title": title, "body": body, "url": url}),
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

    if cleanup_needed and user_id is not None:
        all_subs[user_id] = valid_subs
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_subs, f)
