from flask import Blueprint, render_template, make_response, send_from_directory, request, jsonify

views_bp = Blueprint('views', __name__)


@views_bp.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')


@views_bp.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@views_bp.route("/")
def contact_list_view():
    return render_template("contacts.html")


@views_bp.route("/profile")
def profile_view():
    return render_template("profile.html")


@views_bp.route("/guide")
def guide_view():
    """Public getting-started guide. This page intentionally requires no login."""
    return render_template("guide.html")


@views_bp.route("/chat/<char_id>")
def chat_view(char_id):
    return render_template("chat.html", char_id=char_id)


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
