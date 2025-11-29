import os
import time
import re
import sqlite3 # 导入 sqlite3 库
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

# 这是在 app.py 文件的开头部分

load_dotenv()  # 从 .env 读取环境变量

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
# 新增下面这行，来读取我们配置的 API 地址
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

app = Flask(__name__, static_folder='static', template_folder='.')

# 配置项
PROMPT_FILE = "prompt.md"
MAX_CONTEXT_LINES = 10
MODEL_NAME = "gemini-3-pro"

# ---------------------- 工具函数 ----------------------

def get_timestamp():
    """生成时间戳"""
    return time.strftime("[%Y-%m-%d %A %H:%M:%S]", time.localtime())

DATABASE_FILE = "chat_history.db"

def init_db():
    """初始化数据库，创建 messages 表"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    # 创建一个表来存储消息，有 id、角色、内容和时间戳
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

# ---------------------- 主页面 ----------------------

@app.route("/")
def index():
    return send_from_directory(".", "templates/chat.html")

# ---------------------- API：获取历史记录 (修复版) ----------------------
@app.route("/api/history", methods=["GET"])
def get_history():
    """提供给前端，用于加载所有历史聊天记录"""
    # 1. 从 URL 参数获取页码和每页数量，设置默认值
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)  # 比如每次加载20条
    offset = (page - 1) * limit

    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 2. 【核心】使用 LIMIT 和 OFFSET 来实现分页查询
    # 我们按时间倒序查，这样拿到的就是最新的数据
    cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset))

    # 3. 把结果反转，这样前端收到的就是按时间正序的了
    messages = [dict(row) for row in cursor.fetchall()][::-1]

    # 4. （可选但推荐）同时告诉前端总共有多少条消息，方便它判断是否已加载完
    cursor.execute("SELECT COUNT(id) FROM messages")
    total_messages = cursor.fetchone()[0]

    conn.close()

    # 5. 返回一个包含数据和总数的对象
    return jsonify({
        "messages": messages,
        "total": total_messages
    })

# 这是在 app.py 文件中

# ---------------------- 核心聊天接口 (时间感知注入版) ----------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    # --- Part 1: 数据准备 ---
    data = request.json or {}
    user_msg_raw = data.get("message", "").strip()
    if not user_msg_raw:
        return jsonify({"error": "empty message"}), 400
    try:
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            system_prompt = f.read()
    except FileNotFoundError:
        system_prompt = "You are a friendly assistant."

    # --- Part 2: 构建带时间戳的 Prompt ---
    messages = [{"role": "system", "content": system_prompt}]

    # 2a. 【核心修正】从数据库读取历史记录，这次同时包含 role, content, 和 timestamp
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # 我们依然读取最近20条记录
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
    history_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    # 2b. 【核心修正】循环历史记录，动态拼接成带时间戳的格式
    for row in history_rows:
        # 这里的 timestamp 是从数据库读出来的，格式是 'YYYY-MM-DD HH:MM:SS'
        # 为了更人性化，我们把它转成我们之前用过的 [YYYY-MM-DD Day HH:MM:SS] 格式
        try:
            dt_object = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
            formatted_timestamp = dt_object.strftime('[%Y-%m-%d %A %H:%M:%S]')

            # 构造新的 content，并添加到 messages 列表
            formatted_content = f"{formatted_timestamp} {row['content']}"
            messages.append({"role": row['role'], "content": formatted_content})
        except (ValueError, TypeError):
            # 如果时间戳格式有问题，就用原始 content，防止程序崩溃
            messages.append({"role": row['role'], "content": row['content']})

    # 2c. 【核心修正】为当前用户输入也加上实时时间戳
    current_timestamp_str = get_timestamp()  # 使用我们已有的工具函数
    user_entry_for_ai = {"role": "user", "content": f"{current_timestamp_str} {user_msg_raw}"}
    messages.append(user_entry_for_ai)

    # --- [调试] 新增的打印代码 ---
    # 在这里，我们将完整的 messages 列表打印到控制台
    print("\n" + "="*50)
    print("--- [调试] 正在发送给 AI 的完整 Prompt ---")
    print("="*50)
    for i, message in enumerate(messages):
        role = message.get("role", "unknown")
        content = message.get("content", "").replace('\n', '\\n') # 将换行符可视化
        print(f"  [{i}] ({role}): {content[:100]}..." if len(content) > 100 else f"  [{i}] ({role}): {content}")
    print("="*50)
    print("--- [调试] Prompt 结束 ---")
    # --- 调试代码结束 ---

    # --- Part 3: 核心交互 ---
    try:
        if USE_OPENROUTER and OPENROUTER_KEY:
            reply_text_raw = call_openrouter(messages)
        else:
            reply_text_raw = call_gemini(messages)

        # --- 【核心修复】AI 回复的“安检门” ---
        # 我们用正则表达式，查找并移除回复开头可能存在的 [时间戳] 格式
        timestamp_pattern_in_reply = r'^\[\d{4}-\d{2}-\d{2}\s[A-Za-z]+\s\d{2}:\d{2}:\d{2}\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern_in_reply, '', reply_text_raw).strip()
        # --- 安检结束 ---

        now = datetime.now()
        user_timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        assistant_timestamp = (now + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        # 存入数据库的是纯净的用户消息
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("user", user_msg_raw, user_timestamp))
        # 【重要】存入数据库的是经过我们“安检”后的、纯净的AI回复
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("assistant", cleaned_reply_text, assistant_timestamp))
        conn.commit()
        cursor.execute("SELECT last_insert_rowid()")
        ai_msg_id = cursor.fetchone()[0]
        conn.close()

        reply_bubbles = list(filter(None, [part.strip() for part in cleaned_reply_text.split('/')]))
        return jsonify({"replies": reply_bubbles})

    except Exception as e:
        # 4. 如果 Part 2 的任何一步失败了（AI调用 或 数据库写入）
        #    我们就在后台打印一个非常明确的错误日志
        print("\n" + "!"*50)
        print(f"--- [CRITICAL ERROR] 在核心交互中失败 ---")
        print(f"--- 错误详情: {e}")
        print("!"*50 + "\n")
        
        # 5. 并给前端返回一个具体的错误信息
        #    注意：我们没有写入数据库，因为交互没有完成！
        return jsonify({"error": "AI call or DB write failed", "details": str(e)}), 500

# 3. 【新增】在 app.py 末尾添加这两个新接口
@app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
def delete_message(msg_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/messages/<int:msg_id>", methods=["PUT"])
def edit_message(msg_id):
    new_content = request.json.get("content", "")
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "content": new_content})

# 这是在 app.py 文件中的 call_openrouter 函数

# ---------------------- OpenRouter / Compatible API ----------------------

def call_openrouter(messages):
    import requests

    # 构造请求地址，我们现在用的是 .env 里配置的新地址
    # 它会自动拼接成 "https://vg.v1api.cc/v1/chat/completions"
    url = f"{OPENROUTER_BASE_URL}/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",  # 使用 .env 里配置的新 Key
        "Content-Type": "application/json"
    }

    # 重要：这里的 'model' 名称需要根据你的 API 服务商文档来填写
    # 他们支持哪些模型，你就填哪个。例如 "gpt-3.5-turbo", "gpt-4", "claude-3-opus" 等
    # 如果不确定，"gpt-3.5-turbo" 通常是最安全的选择。
    payload = {
        "model": "gemini-3-pro",
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 1024
    }

    print(f"--- [Debug] Calling Compatible API at: {url}")  # 增加一个调试日志
    print(f"--- [Debug] Using model: {payload['model']}")  # 增加一个调试日志

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        # 打印出服务端的原始报错信息，方便调试
        if r.status_code != 200:
            return f"[ERROR] API call failed with status {r.status_code}: {r.text}"

        r.raise_for_status()
        jr = r.json()
        return jr["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] API request failed: {e}"

# ---------------------- Gemini ----------------------

def call_gemini(messages):
    try:
        import google.generativeai as genai
    except ImportError as e:
        return f"[ERROR] google.generativeai not installed or import failed: {e}. Try 'pip install -U google-generativeai'"

    if not GEMINI_KEY:
        return "[ERROR] No GEMINI_API_KEY found in environment."

    genai.configure(api_key=GEMINI_KEY)

    # 1. 提取 system prompt 和历史记录
    system_prompt = ""
    if messages and messages[0]['role'] == 'system':
        system_prompt = messages[0]['content']
        history = messages[1:]
    else:
        history = messages

    # 2. 转换消息格式以适配 Gemini API
    gemini_messages = []
    for msg in history:
        role = 'model' if msg['role'] == 'assistant' else 'user'
        gemini_messages.append({'role': role, 'parts': [msg['content']]})

    # 3. 设置生成参数
    generation_config = {
        "temperature": 0.6,
        "max_output_tokens": 800,
    }

    try:
        model = genai.GenerativeModel(
            model_name="gemini-2.5-pro",  # 遵照您的要求，保留此模型
            generation_config=generation_config,
            system_instruction=system_prompt
        )

        print("--- [4] [Gemini] 配置完成，准备调用 generate_content ---")  # <-- 添加的日志

        # 4. 调用新的 generate_content API
        response = model.generate_content(gemini_messages)

        print("--- [5] [Gemini] generate_content 调用成功，已收到回复 ---")  # <-- 添加的日志

        return response.text
    except Exception as e:
        # 如果遇到关于模型的错误，例如 "model not found"，可以尝试换成 "gemini-1.5-pro-latest"
        return f"[ERROR] Gemini call failed: {e}"

# ---------------------- 启动 ----------------------

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
