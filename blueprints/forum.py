# -*- coding: utf-8 -*-
import os
import re
import json
import sqlite3
import threading
import uuid
import requests
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context

from core.config import BASE_DIR, OPENROUTER_BASE_URL, OPENROUTER_BASE_URL_OLD
from core.context import get_current_user_id
from core.utils import get_effective_gemini_key, get_effective_openrouter_key
from services.ai_client import call_gemini, call_openrouter, get_model_config, get_relay_provider

forum_bp = Blueprint('forum', __name__)

FORUMS_DB = os.path.join(BASE_DIR, "configs", "forums.db")

# 后台任务存储 { task_id -> { status, result, error } }
_task_store = {}
_task_lock = threading.Lock()


def init_forums_db():
    os.makedirs(os.path.dirname(FORUMS_DB), exist_ok=True)
    conn = sqlite3.connect(FORUMS_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS forums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            topic TEXT NOT NULL,
            forum_type TEXT NOT NULL,
            custom_type_description TEXT DEFAULT '',
            supplementary_info TEXT DEFAULT '',
            posts_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_forums_db()


def call_active_model(messages, user_id=None, task_type="chat", max_tokens=8192):
    route, model_name = get_model_config(task_type, user_id)
    if route == "gemini":
        return call_gemini(messages, char_id="forum_generator", model_name=model_name, user_id=user_id)
    else:
        return call_openrouter(messages, char_id="forum_generator", model_name=model_name, user_id=user_id, max_tokens=max_tokens)


def extract_json_from_response(text):
    if not text:
        return None
    text = text.strip()

    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    return None


def validate_forum_json(data):
    if not isinstance(data, dict):
        return False, "根元素必须是 JSON 对象"
    if "posts" not in data:
        return False, "缺少 posts 字段"
    posts = data.get("posts", [])
    if not isinstance(posts, list) or len(posts) == 0:
        return False, "posts 必须是非空数组"
    for i, post in enumerate(posts):
        if not isinstance(post, dict):
            return False, f"posts[{i}] 必须是对象"
        if "name" not in post:
            return False, f"posts[{i}] 缺少 name"
        if "content" not in post:
            return False, f"posts[{i}] 缺少 content"
    return True, ""


def _build_chat_evidence_text(chat_segments):
    lines = []
    for seg in chat_segments:
        contact_name = seg.get("contact_name", "未知联系人")
        messages = seg.get("messages", [])
        lines.append(f"### 【{contact_name}】的聊天记录：")
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role == "assistant":
                label = "对方"
            elif role == "user":
                label = "用户"
            else:
                label = role
            lines.append(f"[{label}] {content}")
        lines.append("")
    return "\n".join(lines)


def build_forum_generation_prompt(chat_segments, forum_type, custom_type_description,
                                   floor_limit, supplementary_info, topic,
                                   existing_posts=None):
    chat_evidence = _build_chat_evidence_text(chat_segments)

    lines = [
        "你是一个高仿真互联网论坛内容生成器。根据提供的聊天记录作为\"流出证据\"，生成一个具有极高真实感的论坛讨论帖。",
        "",
        "## 输出格式（严格 JSON）",
        "请只输出一个 JSON 对象，不要包含任何 markdown 标记或解释文字：",
        "{",
        '  "title": "论坛帖子标题",',
        '  "posts": [',
        '    {"name": "吃瓜网友A", "role": "npc", "floor": 0, "replyTo": null, "content": "楼主的长帖内容..."},',
        '    {"name": "路人甲", "role": "npc", "floor": 1, "replyTo": 0, "content": "回复内容..."}',
        "  ]",
        "}",
        "- name: 论坛昵称（真人网名风格，多样化）",
        "- role: 始终为 \"npc\"",
        "- floor: 楼层号",
        "- replyTo: 回复的楼层号（整数）或 null",
        "- content: 帖子正文",
        "",
    ]

    if existing_posts:
        total_existing = len(existing_posts)
        lines += [
            "## 已有楼层（续写模式）",
            f"以下是该帖子已有的 {total_existing} 个楼层（楼层号 0 到 {total_existing - 1}）：",
            "```json",
            json.dumps(existing_posts, ensure_ascii=False, indent=2),
            "```",
            f"请生成 {floor_limit} 个新楼层，楼层号从 {total_existing} 到 {total_existing + floor_limit - 1}。",
            "保持已有讨论方向和人物设定，不要重复已有内容。",
            "",
        ]

    if forum_type == "novel_readers":
        type_desc = (
            "你是\"小说/动漫/影视作品读者论坛\"的网友。\n"
            "人设特点：\n"
            "- 喜欢长篇大论分析剧情、人物心理和隐藏细节\n"
            "- 擅长从只言片语中推理、脑补完整剧情\n"
            "- 热衷嗑CP、分析角色关系走向\n"
            "- 会引用原作台词或行为模式来佐证观点\n"
            "- 激动时使用感叹号、颜文字(≧▽≦)\n"
            "- 形成不同派系互相辩论（CP粉vs唯粉、A党vsB党）\n"
            "- 语气：热情、细腻、带二次元风格"
        )
    elif forum_type == "world_bystanders":
        type_desc = (
            "你是\"现实世界路人吃瓜论坛\"的网友。\n"
            "人设特点：\n"
            "- 用现实世界的逻辑和道德观分析、评论角色们的行为\n"
            "- 语气多样化：震惊的、怀疑的、起哄的、理性分析的\n"
            "- 喜欢脑补现实世界的戏剧性八卦展开\n"
            "- 讨论\"配不配\"\"渣不渣\"\"甜不甜\"等接地气话题\n"
            "- 常用论坛习惯语：前排/马克/蹲后续/火钳刘明\n"
            "- 有人搬运\"据说\"\"听说\"的八卦\n"
            "- 争论站队、互撕、磕糖，情绪丰富\n"
            "- 语气：接地气、网络化、真实感强"
        )
    else:
        type_desc = f"论坛类型：{custom_type_description}\n请根据这个论坛的网民特征生成内容。"

    lines += [
        "## 论坛网民人设",
        type_desc,
        "",
        "## 聊天记录（网友讨论的素材）",
        chat_evidence,
        "",
        "## 帖子主题",
        topic,
        "",
        "## 补充背景信息",
        supplementary_info if supplementary_info else "无",
        "",
        "## 生成要求",
        f"- 总共生成 {floor_limit} 个楼层",
        "- 楼主（floor=0）写出完整长帖（至少 50 字），清晰表达发帖目的、贴出聊天核心内容、提出问题或观点",
        "- 后续楼层（15-120 字）言之有物，不能空洞",
        "- 讨论要有层次感：支持、反对、质疑、歪楼、拉回正题等",
        "- 昵称多样化（至少 5 种不同风格），像真人网名",
        "- 大多数楼层回复主楼(floor=0)，少数楼层互相回复形成对话链",
        "- 适当使用网络用语、颜文字、emoji",
        "- 内容具体、有细节，体现不同人的性格和立场",
        "- 形成完整的\"网友吃瓜讨论\"氛围",
    ]

    system_prompt = "\n".join(lines)
    user_prompt = "请直接输出 JSON。"

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


# ==================== Streaming Helpers ====================

def stream_gemini_chunks(messages, model_name, user_id=None):
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
    api_key = get_effective_gemini_key(user_id=user_id)
    url = f"{base_url}/v1beta/models/{model_name}:streamGenerateContent?key={api_key}&alt=sse"

    gemini_contents = []
    system_parts = []
    for msg in messages:
        if msg['role'] == 'system':
            system_parts.append(msg['content'])
        else:
            role = 'model' if msg['role'] == 'assistant' else 'user'
            gemini_contents.append({"role": role, "parts": [{"text": msg['content']}]})

    system_instruction = None
    if system_parts:
        system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]}

    payload = {
        "contents": gemini_contents,
        "generationConfig": {"temperature": 1, "maxOutputTokens": 8192},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    r = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
    r.raise_for_status()

    for line in r.iter_lines():
        if not line:
            continue
        line = line.decode('utf-8', errors='replace')
        if line.startswith('data: '):
            data_str = line[6:]
            if data_str == '[DONE]':
                break
            try:
                data = json.loads(data_str)
                candidates = data.get('candidates', [])
                if candidates:
                    parts = candidates[0].get('content', {}).get('parts', [])
                    for part in parts:
                        text = part.get('text', '')
                        if text:
                            yield text
            except json.JSONDecodeError:
                pass


def stream_openrouter_chunks(messages, model_name, user_id=None, max_tokens=8192):
    relay_provider = get_relay_provider(user_id)
    if relay_provider.startswith("http://") or relay_provider.startswith("https://"):
        base_url = relay_provider
    elif relay_provider == "old":
        base_url = OPENROUTER_BASE_URL_OLD
    else:
        base_url = OPENROUTER_BASE_URL

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {get_effective_openrouter_key(user_id=user_id)}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/event-stream",
    }

    final_messages = []
    system_contents = []
    for m in messages:
        if m.get('role') == 'system':
            system_contents.append(m.get('content', ''))
        else:
            final_messages.append(m)
    if system_contents:
        final_messages.insert(0, {"role": "system", "content": "\n\n".join(system_contents)})

    payload = {
        "model": model_name,
        "messages": final_messages,
        "temperature": 1,
        "max_tokens": max_tokens,
        "stream": True
    }

    r = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
    r.raise_for_status()

    for line in r.iter_lines():
        if not line:
            continue
        line = line.decode('utf-8', errors='replace')
        if line.startswith('data: '):
            data_str = line[6:]
            if data_str.strip() == '[DONE]':
                break
            try:
                data = json.loads(data_str)
                choices = data.get('choices', [])
                if choices:
                    delta = choices[0].get('delta', {})
                    content = delta.get('content', '')
                    if content:
                        yield content
            except json.JSONDecodeError:
                pass


def stream_ai_call(messages, user_id=None, task_type="forum", max_tokens=8192):
    route, model_name = get_model_config(task_type, user_id)
    if route == "gemini":
        yield from stream_gemini_chunks(messages, model_name, user_id)
    else:
        yield from stream_openrouter_chunks(messages, model_name, user_id, max_tokens=max_tokens)


def build_forum_streaming_prompt(chat_segments, forum_type, custom_type_description,
                                  floor_limit, supplementary_info, topic,
                                  existing_posts=None):
    chat_evidence = _build_chat_evidence_text(chat_segments)

    lines = [
        "你是一个高仿真互联网论坛内容生成器。根据提供的聊天记录，生成一个真实感的论坛讨论帖。",
        "",
        "## 输出格式（逐楼输出论坛块）",
        "严格使用以下格式，不要输出任何其他解释文字：",
        "",
        "[FORUM:帖子标题]",
        "[POST:吃瓜网友A:npc:0]这里是楼主的长帖内容，至少50字，清晰表达发帖目的和观点。（内容不要引号包裹）[/POST]",
        "[POST:路人甲:npc:1:0]这里是回复主楼的内容，15-120字，言之有物。（内容不要引号包裹）[/POST]",
        "[POST:路人乙:npc:2]这是另一个直接回复主楼的回帖。[/POST]",
        "[POST:路人丙:npc:3:1]这是回复1楼的内容。[/POST]",
        "[/FORUM]",
        "",
        "格式说明：",
        "- [POST:昵称:角色:楼层号[:回复楼层号]] 开始一栋楼",
        "- 楼层号从0开始，0为楼主",
        "- 回复楼层号可选，不写=回复主楼，写了=回复指定楼",
        "- [/POST] 结束当前楼层，[/FORUM] 标记帖子结束",
        "- 所有帖子内容直接写在 [POST]...[/POST] 之间",
        "- **禁止**在内容外面加引号或代码块",
    ]

    if existing_posts:
        total_existing = len(existing_posts)
        lines += [
            "",
            f"## 已有楼层（续写，共 {total_existing} 楼）",
            "请从下一楼开始续写，保持已有讨论方向。",
        ]

    if forum_type == "novel_readers":
        type_desc = (
            "小说/动漫读者论坛网友。喜欢分析剧情、嗑CP、长篇大论。"
            "使用感叹号、颜文字(≧▽≦)。会有CP粉vs唯粉的辩论。语气热情细腻。"
        )
    elif forum_type == "world_bystanders":
        type_desc = (
            "现实世界路人吃瓜论坛网友。用现实逻辑讨论八卦。"
            "语气多样：震惊、怀疑、起哄、理性分析都有。"
            "常用论坛语：前排/马克/蹲后续。接地气、网络化。"
        )
    else:
        type_desc = f"论坛类型：{custom_type_description}"

    lines += [
        "",
        f"## 论坛人设：{type_desc}",
        "",
        "## 聊天记录",
        chat_evidence,
        "",
        f"## 帖子主题：{topic}",
        f"## 补充信息：{supplementary_info if supplementary_info else '无'}",
        "",
        f"## 要求：生成 {floor_limit} 个楼层，讨论有层次感，昵称多样化。直接输出论坛块。",
    ]

    system_prompt = "\n".join(lines)
    user_prompt = "请直接输出论坛块格式。"

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def parse_forum_block_output(text):
    title_match = re.search(r'\[FORUM:(.*?)\]', text)
    title = title_match.group(1).strip() if title_match else ''

    posts = []
    post_pattern = r'\[POST:([^:]+):(self|npc):(\d+)(?::(\d+))?\]([\s\S]*?)\[\/POST\]'
    for m in re.finditer(post_pattern, text):
        posts.append({
            'name': m.group(1).strip(),
            'role': m.group(2),
            'floor': int(m.group(3)),
            'replyTo': int(m.group(4)) if m.group(4) else None,
            'content': m.group(5).strip()
        })

    return {'title': title, 'posts': posts} if posts else None


# === Routes ===

@forum_bp.route("/forum")
def forum_view():
    return render_template("forum.html")


@forum_bp.route("/api/forums/list", methods=["GET"])
def list_forums():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify([]), 401

    conn = sqlite3.connect(FORUMS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, topic, forum_type, custom_type_description, supplementary_info, created_at "
        "FROM forums WHERE user_id = ? ORDER BY id DESC LIMIT 50",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@forum_bp.route("/api/forums/<int:forum_id>", methods=["GET"])
def get_forum(forum_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "未登录"}), 401

    conn = sqlite3.connect(FORUMS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM forums WHERE id = ? AND user_id = ?", (forum_id, user_id))
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "论坛不存在"}), 404

    data = dict(row)
    data["posts"] = json.loads(data.get("posts_json", "[]"))
    del data["posts_json"]
    return jsonify(data)


@forum_bp.route("/api/forums/<int:forum_id>", methods=["DELETE"])
def delete_forum(forum_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "未登录"}), 401

    conn = sqlite3.connect(FORUMS_DB)
    cur = conn.cursor()
    cur.execute("SELECT id FROM forums WHERE id = ? AND user_id = ?", (forum_id, user_id))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "论坛不存在"}), 404

    cur.execute("DELETE FROM forums WHERE id = ? AND user_id = ?", (forum_id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@forum_bp.route("/api/forums/generate", methods=["POST"])
def generate_forum():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    data = request.get_json(silent=True) or {}
    chat_segments = data.get("chat_segments", [])
    historical_forum_id = data.get("historical_forum_id")
    forum_type = data.get("forum_type", "novel_readers")
    custom_type_description = data.get("custom_type_description", "")
    floor_limit = data.get("floor_limit", 20)
    supplementary_info = data.get("supplementary_info", "")
    topic = data.get("topic", "")

    if not topic.strip():
        return jsonify({"error": "请输入论坛主题"}), 400
    if not chat_segments:
        return jsonify({"error": "请至少载入一段聊天记录"}), 400
    if forum_type not in ("novel_readers", "world_bystanders", "custom"):
        return jsonify({"error": "无效的论坛类型"}), 400
    if not isinstance(floor_limit, int) or floor_limit < 1 or floor_limit > 50:
        return jsonify({"error": "楼层数必须在 1-50 之间"}), 400

    task_id = str(uuid.uuid4())[:8]
    with _task_lock:
        _task_store[task_id] = {"status": "working"}

    thread = threading.Thread(
        target=_do_generate,
        args=(task_id, user_id, topic, chat_segments, forum_type,
              custom_type_description, floor_limit, supplementary_info,
              historical_forum_id),
        daemon=True
    )
    thread.start()

    return jsonify({"task_id": task_id})


@forum_bp.route("/api/forums/task/<task_id>", methods=["GET"])
def poll_task(task_id):
    with _task_lock:
        task = _task_store.get(task_id)
    if not task:
        return jsonify({"status": "not_found"}), 404
    return jsonify(task)

def _do_generate(task_id, user_id, topic, chat_segments, forum_type,
                 custom_type_description, floor_limit, supplementary_info,
                 historical_forum_id):
    """后台线程：执行 AI 生成并写库"""
    try:
        existing_posts = None
        existing_title = None
        existing_forum_type = forum_type
        existing_topic = topic

        if historical_forum_id:
            conn = sqlite3.connect(FORUMS_DB)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM forums WHERE id = ? AND user_id = ?", (historical_forum_id, user_id))
            row = cur.fetchone()
            conn.close()
            if row:
                row = dict(row)
                existing_posts = json.loads(row["posts_json"])
                existing_title = row["title"]
                existing_forum_type = row["forum_type"]
                existing_topic = row["topic"]
                custom_type_description = row.get("custom_type_description", "")
            else:
                with _task_lock:
                    _task_store[task_id] = {"status": "error", "error": "历史论坛不存在"}
                return

        messages = build_forum_generation_prompt(
            chat_segments=chat_segments,
            forum_type=existing_forum_type,
            custom_type_description=custom_type_description,
            floor_limit=floor_limit,
            supplementary_info=supplementary_info,
            topic=existing_topic,
            existing_posts=existing_posts,
        )

        response_text = call_active_model(messages, user_id=user_id, task_type="forum", max_tokens=16384)

        result = extract_json_from_response(response_text)
        if not result:
            retry_messages = messages + [
                {"role": "assistant", "content": response_text[:500]},
                {"role": "user", "content": "你的回复格式不正确！请只输出一个纯 JSON 对象，以 { 开头、} 结尾，不要包含代码块标记或解释。JSON 格式：{\"title\":\"...\", \"posts\":[...]}"},
            ]
            try:
                response_text = call_active_model(retry_messages, user_id=user_id, task_type="chat", max_tokens=16384)
                result = extract_json_from_response(response_text)
            except Exception:
                pass

        if not result:
            with _task_lock:
                _task_store[task_id] = {"status": "error", "error": "AI 返回格式无法解析，请重试"}
            return

        valid, err_msg = validate_forum_json(result)
        if not valid:
            with _task_lock:
                _task_store[task_id] = {"status": "error", "error": f"JSON 验证失败: {err_msg}"}
            return

        new_posts = result.get("posts", [])
        title = result.get("title", topic)

        if existing_posts is not None:
            all_posts = existing_posts + new_posts
            conn = sqlite3.connect(FORUMS_DB)
            cur = conn.cursor()
            cur.execute(
                "UPDATE forums SET title = ?, posts_json = ?, supplementary_info = ?, custom_type_description = ? WHERE id = ? AND user_id = ?",
                (title, json.dumps(all_posts, ensure_ascii=False), supplementary_info, custom_type_description, historical_forum_id, user_id)
            )
            conn.commit()
            forum_id = historical_forum_id
            conn.close()
        else:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(FORUMS_DB)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO forums (user_id, title, topic, forum_type, custom_type_description, supplementary_info, posts_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, title, topic, forum_type, custom_type_description, supplementary_info, json.dumps(new_posts, ensure_ascii=False), created_at)
            )
            conn.commit()
            forum_id = cur.lastrowid
            conn.close()

        final_posts = existing_posts + new_posts if existing_posts is not None else new_posts
        done_data = {
            "id": forum_id,
            "title": title,
            "topic": topic,
            "forum_type": existing_forum_type,
            "posts": final_posts,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with _task_lock:
            _task_store[task_id] = {"status": "done", "result": done_data}

    except Exception as e:
        with _task_lock:
            _task_store[task_id] = {"status": "error", "error": f"AI 调用失败: {str(e)}"}


# ==================== Streaming Generate ====================

def _save_forum_to_db(user_id, result, topic, forum_type, custom_type_description,
                       supplementary_info, existing_posts, historical_forum_id):
    new_posts = result.get("posts", [])
    title = result.get("title", topic)

    if existing_posts is not None:
        all_posts = existing_posts + new_posts
        conn = sqlite3.connect(FORUMS_DB)
        cur = conn.cursor()
        cur.execute(
            "UPDATE forums SET title = ?, posts_json = ?, supplementary_info = ?, custom_type_description = ? WHERE id = ? AND user_id = ?",
            (title, json.dumps(all_posts, ensure_ascii=False), supplementary_info, custom_type_description, historical_forum_id, user_id)
        )
        conn.commit()
        forum_id = historical_forum_id
        conn.close()
    else:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(FORUMS_DB)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO forums (user_id, title, topic, forum_type, custom_type_description, supplementary_info, posts_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, title, topic, forum_type, custom_type_description, supplementary_info, json.dumps(new_posts, ensure_ascii=False), created_at)
        )
        conn.commit()
        forum_id = cur.lastrowid
        conn.close()

    final_posts = existing_posts + new_posts if existing_posts is not None else new_posts
    return {
        "id": forum_id,
        "title": title,
        "topic": topic,
        "forum_type": forum_type,
        "posts": final_posts,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@forum_bp.route("/api/forums/generate-stream", methods=["POST"])
def generate_forum_stream():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    data = request.get_json(silent=True) or {}
    chat_segments = data.get("chat_segments", [])
    historical_forum_id = data.get("historical_forum_id")
    forum_type = data.get("forum_type", "novel_readers")
    custom_type_description = data.get("custom_type_description", "")
    floor_limit = data.get("floor_limit", 20)
    supplementary_info = data.get("supplementary_info", "")
    topic = data.get("topic", "")

    if not topic.strip():
        return jsonify({"error": "请输入论坛主题"}), 400
    if not chat_segments:
        return jsonify({"error": "请至少载入一段聊天记录"}), 400
    if forum_type not in ("novel_readers", "world_bystanders", "custom"):
        return jsonify({"error": "无效的论坛类型"}), 400
    if not isinstance(floor_limit, int) or floor_limit < 1 or floor_limit > 50:
        return jsonify({"error": "楼层数必须在 1-50 之间"}), 400

    existing_posts = None
    existing_forum_type = forum_type
    existing_topic = topic

    if historical_forum_id:
        conn = sqlite3.connect(FORUMS_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM forums WHERE id = ? AND user_id = ?", (historical_forum_id, user_id))
        row = cur.fetchone()
        conn.close()
        if row:
            row = dict(row)
            existing_posts = json.loads(row["posts_json"])
            existing_forum_type = row["forum_type"]
            existing_topic = row["topic"]
            custom_type_description = row.get("custom_type_description", "")
        else:
            return jsonify({"error": "历史论坛不存在"}), 404

    messages = build_forum_streaming_prompt(
        chat_segments=chat_segments,
        forum_type=existing_forum_type,
        custom_type_description=custom_type_description,
        floor_limit=floor_limit,
        supplementary_info=supplementary_info,
        topic=existing_topic,
        existing_posts=existing_posts,
    )

    def generate():
        buffer = ""
        saved_data = None
        try:
            for chunk in stream_ai_call(messages, user_id=user_id, task_type="forum", max_tokens=16384):
                buffer += chunk
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"

            result = parse_forum_block_output(buffer)
            if result:
                saved_data = _save_forum_to_db(
                    user_id, result, topic, existing_forum_type,
                    custom_type_description, supplementary_info,
                    existing_posts, historical_forum_id
                )
                yield f"data: {json.dumps({'done': True, 'forum': saved_data}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'AI 输出格式解析失败，请重试', 'raw': buffer[:500]}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )
