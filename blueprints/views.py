import os
from datetime import datetime

from flask import Blueprint, render_template, make_response, send_from_directory, request, jsonify

from core.time_utils import BEIJING_TZ, beijing_now
from core.legal import get_legal_document, get_legal_documents

views_bp = Blueprint('views', __name__)

VOICE_ANNOUNCEMENT_ID = "voice-service-change-2026-08"
VOICE_ANNOUNCEMENT_START = os.getenv(
    "VOICE_ANNOUNCEMENT_START",
    "2026-08-06T00:00:00+08:00",
)
VOICE_ANNOUNCEMENT_END = os.getenv(
    "VOICE_ANNOUNCEMENT_END",
    "2026-08-13T00:00:00+08:00",
)
VOICE_LEGACY_DELETE_AT = os.getenv(
    "VOICE_LEGACY_DELETE_AT",
    VOICE_ANNOUNCEMENT_END,
)
ELEVENLABS_REFERRAL_URL = "https://try.elevenlabs.io/8f8ks0unlqsw"
PERSONA_LOCK_ANNOUNCEMENT_ID = "persona-lock-2026-08"
PERSONA_LOCK_ANNOUNCEMENT_START = os.getenv(
    "PERSONA_LOCK_ANNOUNCEMENT_START",
    "2026-08-10T00:00:00+08:00",
)
PERSONA_LOCK_ANNOUNCEMENT_END = os.getenv(
    "PERSONA_LOCK_ANNOUNCEMENT_END",
    "2026-08-24T00:00:00+08:00",
)


def _parse_announcement_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def _voice_announcement_payload(now=None):
    current = beijing_now(now)
    start = _parse_announcement_time(VOICE_ANNOUNCEMENT_START)
    end = _parse_announcement_time(VOICE_ANNOUNCEMENT_END)
    delete_at = _parse_announcement_time(VOICE_LEGACY_DELETE_AT)
    payload = {
        "id": VOICE_ANNOUNCEMENT_ID,
        "active": start <= current < end,
        "server_date": current.strftime("%Y-%m-%d"),
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "legacy_voice_deletes_at": delete_at.isoformat(),
        "legacy_voice_deletes_on": delete_at.strftime("%Y年%m月%d日"),
        "title": "语音功能调整通知",
        "cta_url": ELEVENLABS_REFERRAL_URL,
    }
    persona_start = _parse_announcement_time(PERSONA_LOCK_ANNOUNCEMENT_START)
    persona_end = _parse_announcement_time(PERSONA_LOCK_ANNOUNCEMENT_END)
    announcements = []
    if payload["active"]:
        announcements.append({**payload, "kind": "voice", "show_once": False})
    if persona_start <= current < persona_end:
        announcements.append({
            "id": PERSONA_LOCK_ANNOUNCEMENT_ID,
            "kind": "persona_lock",
            "active": True,
            "show_once": True,
            "starts_at": persona_start.isoformat(),
            "ends_at": persona_end.isoformat(),
            "title": "人设 AI 编辑保护功能上线",
            "body": "使用 [[LOCK]] 和 [[/LOCK]] 标记核心设定。模型以后编辑人设时必须保留，用户本人仍可随时修改。",
            "cta_url": "/guide#persona-lock",
            "cta_label": "查看使用方法",
        })
    payload["announcements"] = announcements
    return payload


@views_bp.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')


@views_bp.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@views_bp.route('/api/app/announcement')
def app_announcement():
    return jsonify(_voice_announcement_payload())


@views_bp.route("/")
def contact_list_view():
    return render_template("contacts.html")


@views_bp.route("/profile")
def profile_view():
    return render_template(
        "profile.html",
        legal_documents=get_legal_documents(),
    )


@views_bp.route("/guide")
def guide_view():
    """Public getting-started guide. This page intentionally requires no login."""
    return render_template("guide.html")


@views_bp.route("/terms")
def terms_view():
    """Public user agreement; authentication is intentionally not required."""
    return render_template(
        "terms.html",
        document=get_legal_document("terms"),
        legal_documents=get_legal_documents(),
    )


@views_bp.route("/privacy")
def privacy_view():
    """Public privacy policy; authentication is intentionally not required."""
    return render_template(
        "privacy.html",
        document=get_legal_document("privacy"),
        legal_documents=get_legal_documents(),
    )


@views_bp.route("/chat/<char_id>")
def chat_view(char_id):
    from core.time_utils import get_user_timezone
    from core.utils import _load_user_settings
    return render_template(
        "chat.html",
        char_id=char_id,
        user_timezone=get_user_timezone(_load_user_settings()),
    )


@views_bp.route("/sakura")
def sakura_chat_view():
    return render_template("sakura_chat.html")


@views_bp.route("/api/sakura/chat", methods=["POST"])
def sakura_chat_api():
    from services.ai_client import call_gemini
    data = request.get_json(force=True) or {}
    user_message = str(data.get("message", "")).strip()
    history = data.get("history", [])

    if not user_message:
        return jsonify({"reply": "请输入消息内容", "error": True})

    if not isinstance(history, list):
        history = []

    history = history[-40:]

    system_msg = {
        "role": "system",
        "content": "你叫Sakura，是一个简洁、友好、乐于助人的AI聊天助手。用中文回复（除非用户用其他语言提问）。回答简洁明了。"
    }

    messages = [system_msg] + history + [{"role": "user", "content": user_message}]

    try:
        reply = call_gemini(messages, char_id="sakura", model_name="gemini-3.5-flash", user_id="1")
    except Exception as e:
        return jsonify({"reply": f"服务暂时不可用：{e}", "error": True})

    return jsonify({"reply": reply, "error": False})


@views_bp.route("/memory/<char_id>")
def memory_view(char_id):
    return send_from_directory("templates", "memory.html")
