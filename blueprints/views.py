import os
from datetime import datetime

from flask import Blueprint, render_template, send_from_directory, jsonify

from core.runtime import SOURCE_ROOT
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
            "show_once": False,
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
    import json
    from core.branding import request_brand_name
    payload = json.loads((SOURCE_ROOT / 'static' / 'manifest.json').read_text())
    brand = request_brand_name()
    payload.update(name=brand, short_name=brand)
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie, Accept-Language'
    return response


@views_bp.route('/sw.js')
def service_worker():
    response = send_from_directory(str(SOURCE_ROOT / 'static'), 'sw.js')
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


@views_bp.route("/memory/<char_id>")
def memory_view(char_id):
    from services.memory_store import memory_timezone
    return render_template("memory.html", memory_timezone=memory_timezone())
