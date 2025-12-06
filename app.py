import os
import time
import re
import json
import sqlite3 # 导入 sqlite3 库
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler # 新增
import memory_jobs # 导入刚才那个模块

# 这是在 app.py 文件的开头部分

load_dotenv()  # 从 .env 读取环境变量

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
# 新增下面这行，来读取我们配置的 API 地址
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

app = Flask(__name__, static_folder='static', template_folder='.')

# 配置项
MAX_CONTEXT_LINES = 10
MODEL_NAME = "gemini-3-pro"

DATABASE_FILE = "chat_history.db"

# 当前对话的用户名字 (用于读取关系 JSON)
CURRENT_USER_NAME = "篠原桐奈"

# ---------------------- 核心：Prompt 构建系统 ----------------------

def build_system_prompt():
    """
    根据 prompts/ 文件夹下的文件，动态组装 System Prompt。
    包含：人设、关系、用户档案、格式要求、长/中/短期记忆、日程表、当前时间。
    """
    prompt_parts = []

    # 获取当前日期对象，用于筛选记忆
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # --- 1. 静态 Markdown 文件 (人设、用户、格式) ---
    # 文件名 -> 标题
    static_files = [
        ("1_base_persona.md", "【Role / キャラクター設定】"),
        ("3_user_persona.md", "【User / ユーザー情報】"),
        ("8_format.md", "【System Rules / 出力ルール】")
    ]
    for filename, title in static_files:
        try:
            path = os.path.join("prompts", filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        prompt_parts.append(f"{title}\n{content}")
        except Exception: pass

    # --- 2. 关系设定 (JSON) ---
    try:
        path = os.path.join("prompts", "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                rel_data = json.load(f)
                user_rel = rel_data.get(CURRENT_USER_NAME)
                if user_rel:
                    # 【修改】拼装文本改成日语
                    rel_str = (f"対話相手：{CURRENT_USER_NAME}\n"
                           f"関係性：{user_rel.get('role', '不明')}\n"
                           f"詳細：{user_rel.get('description', '')}")
                prompt_parts.append(f"【Relationship / 関係設定】\n{rel_str}")
    except Exception: pass

    # --- 4. 长期记忆 (JSON - 按月) ---
    # 这里简单处理：全部读取。如果记忆太长，可以根据 now.strftime("%Y-%m") 只读取当月和上个月
    try:
        path = os.path.join("prompts", "4_memory_long.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                long_mem = json.load(f)
                if long_mem:
                    # 格式化为： - 2025-10: xxxxx
                    mem_list = [f"- {k}: {v}" for k, v in long_mem.items()]
                    prompt_parts.append(f"【Long-term Memory / 長期記憶】\n" + "\n".join(mem_list))
    except Exception: pass

    # --- 5. 中期记忆 (JSON - 按天，最近7天) ---
    try:
        path = os.path.join("prompts", "5_memory_medium.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                med_mem = json.load(f)
                recent_list = []
                # 倒推7天
                for i in range(7, 0, -1):
                    day_key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                    if day_key in med_mem:
                        recent_list.append(f"- {day_key}: {med_mem[day_key]}")
                if recent_list:
                    prompt_parts.append(f"【Medium-term Memory / 最近一週間の出来事】\n" + "\n".join(recent_list))
    except Exception: pass

    # --- 6. 短期记忆 (JSON - 当天事件) ---
    try:
        path = os.path.join("prompts", "6_memory_short.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                short_mem = json.load(f)

                # 获取当天的数据
                day_data = short_mem.get(today_str)

                # 【兼容旧版本】如果它是列表，直接用；如果是字典(新版)，取 events 字段
                today_events = []
                if isinstance(day_data, list):
                    today_events = day_data
                elif isinstance(day_data, dict):
                    today_events = day_data.get("events", [])

                if today_events:
                    events_str = "\n".join([f"- [{e.get('time')}] {e.get('event')}" for e in today_events])
                    prompt_parts.append(f"【Short-term Memory / 今日の出来事】\n{events_str}")
    except Exception: pass

    # --- 7. 近期安排 (JSON - 日程表) ---
    # 筛选今天及以后的日程
    try:
        path = os.path.join("prompts", "7_schedule.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                schedule = json.load(f)
                future_plans = []
                # 简单的字符串比较日期 (YYYY-MM-DD 格式支持直接比较)
                sorted_dates = sorted(schedule.keys())
                for date_key in sorted_dates:
                    if date_key >= today_str:
                        future_plans.append(f"- {date_key}: {schedule[date_key]}")

                if future_plans:
                    prompt_parts.append(f"【Schedule / 今後の予定】\n" + "\n".join(future_plans))
    except Exception: pass

    # --- 8. 实时时间注入 ---
    # 格式示例: 2025-11-29 Saturday

    # 简单的星期几映射
    week_map = ["月", "火", "水", "木", "金", "土", "日"]
    week_str = week_map[now.weekday()]

    current_date_str = now.strftime('%Y-%m-%d %A')

    # 【修改】说明文字改成日语
    prompt_parts.append(f"【Current Date / 現在の日付】\n今日は: {current_date_str}\n(以下の会話履歴には時間 [HH:MM] のみが含まれています。現在の日付に基づいて理解してください)")

    return "\n\n".join(prompt_parts)

# --- 【新增】AI 总结专用函数 ---
def call_ai_to_summarize(text_content, prompt_type="short"):
    """
    调用 AI 对文本进行总结
    prompt_type: 'short' (生成今日事件), 'medium' (生成每日摘要), 'long' (生成月度回忆)
    """
    if not text_content:
        return None

    system_instruction = ""
    # --- 【修改】全部换成日语指令 ---
    if prompt_type == "short":
        # 即使是日语，格式标记 [HH:MM] 依然要保持，方便代码正则提取
        system_instruction = "あなたは記憶整理係です。以下の会話から重要な出来事を抽出し（具体的な時間を含む）、無関係な雑談は無視してください。出力フォーマット：\n- [HH:MM] 出来事の内容\n- [HH:MM] 出来事の内容"
    elif prompt_type == "medium":
        system_instruction = "あなたは日記記録係です。この一日のすべての断片的な出来事を、國神錬介（Kunigami Rensuke）の一人称視点で、300文字以内の一貫した日記にまとめてください。"
    elif prompt_type == "long":
        system_instruction = "あなたは伝記作家です。この一週間の日記に基づいて、今週の重要な転換点と二人の関係の進展をまとめ、長期記憶として保存してください。"

    # 构造请求消息
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"内容は以下の通りです：\n{text_content}"} # 提示语改成日语
    ]

    print(f"--- 正在进行记忆总结 ({prompt_type}) ---")

    # 复用现有的 API 调用逻辑 (优先 OpenRouter, 其次 Gemini)
    if USE_OPENROUTER:
        return call_openrouter(messages)
    else:
        return call_gemini(messages)

# --- 【新增】核心逻辑：对指定日期进行增量记忆更新 ---
def update_short_memory_for_date(target_date_str):
    """
    读取指定日期的新增消息(last_id之后)，调用AI总结，并追加到短期记忆中。
    返回: (added_count, new_events)
    """
    short_mem_path = os.path.join("prompts", "6_memory_short.json")

    # 1. 读取现有记忆，获取 last_id
    current_data = {}
    if os.path.exists(short_mem_path):
        with open(short_mem_path, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    day_data = current_data.get(target_date_str)

    existing_events = []
    last_id = 0

    # 兼容处理：如果是旧格式(list)，视为 last_id=0，并准备转换为新格式
    if isinstance(day_data, list):
        existing_events = day_data
        # 尝试去数据库找这些事件里最大的ID？太麻烦，直接设为0，
        # 可能会导致第一次重复总结一点点旧数据，但问题不大，之后就好了。
        last_id = 0
    elif isinstance(day_data, dict):
        existing_events = day_data.get("events", [])
        last_id = day_data.get("last_id", 0)

    # 2. 从数据库查询【大于 last_id】的新消息
    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    # 关键：只查 id > last_id 的
    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"[{target_date_str}] 没有新增消息需要总结。")
        return 0, []

    # 更新 max_id
    new_max_id = rows[-1][0]

    # 3. 拼接文本
    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        name = "ユーザー" if role == "user" else "私" # 改成日语，配合全日语Prompt
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 4. 调用 AI 总结 (Short模式)
    try:
        summary_text = call_ai_to_summarize(chat_log, "short")
        if not summary_text: return 0, []

        # 解析 AI 返回
        new_events = []
        import re
        for line in summary_text.split('\n'):
            line = line.strip()
            if line:
                match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
                event_time = match_time.group(1) if match_time else datetime.now().strftime("%H:%M")
                # 这里的正则去掉前面的 [HH:MM] 和 - 符号
                event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
                new_events.append({"time": event_time, "event": event_text})

        if not new_events: return 0, []

        # 5. 追加写入 (Append)
        final_events = existing_events + new_events

        # 保存为新结构： { "events": [...], "last_id": 123 }
        current_data[target_date_str] = {
            "events": final_events,
            "last_id": new_max_id
        }

        with open(short_mem_path, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)

        return len(new_events), new_events

    except Exception as e:
        print(f"增量总结出错: {e}")
        return 0, []

# ---------------------- 工具函数 ----------------------

def get_timestamp():
    """生成时间戳"""
    return time.strftime("[%Y-%m-%d %A %H:%M:%S]", time.localtime())

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

# --- 【修改】历史记录接口 (支持定位) ---
# 请找到原来的 get_history 函数，完全替换为下面这个：
@app.route("/api/history", methods=["GET"])
def get_history():
    limit = request.args.get('limit', 20, type=int)
    page = request.args.get('page', 1, type=int)
    # 新增参数：target_id (如果有这个，就忽略 page，自动计算该消息在哪一页)
    target_id = request.args.get('target_id', type=int)

    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    offset = (page - 1) * limit

    # --- 核心：如果指定了跳转 ID，自动计算它前面的消息数量，从而反推 offset ---
    if target_id:
        # 1. 获取目标消息的时间戳
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']
            # 2. 计算有多少条消息比它“新” (用于计算 offset)
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]
            # 3. 设定 offset，让这条消息刚好出现在这一批数据的开头
            offset = count_newer
            # 4. 反向更新 page (给前端用)
            page = (offset // limit) + 1

    # 常规查询
    cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset))
    messages = [dict(row) for row in cursor.fetchall()][::-1]

    cursor.execute("SELECT COUNT(id) FROM messages")
    total_messages = cursor.fetchone()[0]
    conn.close()

    return jsonify({
        "messages": messages,
        "total": total_messages,
        "page": page  # 返回当前真实的页码
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
    # 1. 动态构建 System Prompt
    system_prompt = build_system_prompt()

    # --- Part 2: 构建带时间戳的 Prompt ---
    messages = [{"role": "system", "content": system_prompt}]

    # --- Part 2: 读取并处理历史记录 (修改版) ---
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
    history_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    # 获取今天的日期字符串 (用于判断)
    today_str = datetime.now().strftime('%Y-%m-%d')

    for row in history_rows:
        try:
            # 1. 解析数据库里的完整时间
            dt_object = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')

            # 2. 【智能压缩逻辑】
            # 如果这条消息是“今天”发的，就只显示 [12:30]
            # 如果是“以前”发的，为了防止 AI 搞混，我们可以显示 [11-28 12:30] (月-日 时间)
            # 或者按照您的绝对要求，无论哪天都只显示 [12:30]

            #=== 方案 A: 极简模式 (您要求的，只显示时间) ===
            # formatted_timestamp = dt_object.strftime('[%H:%M]')

            #=== 方案 B: 智能模式 (推荐: 如果不是今天，还是带个日期吧，不然 AI 会以为那是今天发生的事) ===
            msg_day_str = dt_object.strftime('%Y-%m-%d')
            if msg_day_str == today_str:
                formatted_timestamp = dt_object.strftime('[%H:%M]')
            else:
                formatted_timestamp = dt_object.strftime('[%m-%d %H:%M]') # 以前的消息带个短日期

            # 3. 拼接
            formatted_content = f"{formatted_timestamp} {row['content']}"
            messages.append({"role": row['role'], "content": formatted_content})
        except:
            # 如果解析失败，就原样放进去
            messages.append({"role": row['role'], "content": row['content']})

    # --- Part 3: 添加当前用户消息 (修改版) ---
    # 获取当前短时间
    current_short_time = datetime.now().strftime('[%H:%M]')
    messages.append({"role": "user", "content": f"{current_short_time} {user_msg_raw}"})

    # --- Part 3: 核心交互 ---
    try:
        if USE_OPENROUTER and OPENROUTER_KEY:
            reply_text_raw = call_openrouter(messages)
        else:
            reply_text_raw = call_gemini(messages)

        # --- 【修改】清理 AI 回复中的时间戳 (安检门) ---
        # 这里的正则会同时匹配两种格式：
        # 1. [HH:MM]  (例如 [12:30])
        # 2. [MM-DD HH:MM] (例如 [12-04 12:30])
        # 同时也兼容一位数的小时 (如 [9:30])
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'

        # 使用 re.sub 将其替换为空字符串
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()

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

    # 【新增】打印日志
    log_full_prompt(f"OpenRouter ({MODEL_NAME})", messages)

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
    # 【新增】打印日志
    log_full_prompt(f"Gemini ({MODEL_NAME})", messages)

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
#--------------------------------
    # import requests
    # import json
    #
    # # 您的 Cloudflare 地址 (后面不需要加 v1beta...)
    # # 记得把下面这个换成您刚才申请到的地址！
    # BASE_URL = "https://gemini-proxy.lashongracelynyc623.workers.dev/"
    #
    # if not GEMINI_KEY:
    #     return "[ERROR] No GEMINI_API_KEY found."
    #
    # # 1. 构造请求 URL
    # # Gemini 1.5 Pro 的标准接口地址
    # url = f"{BASE_URL}/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_KEY}"
    #
    # # 2. 转换消息格式 (OpenAI 格式 -> Gemini 格式)
    # gemini_contents = []
    # system_instruction = None
    #
    # for msg in messages:
    #     if msg['role'] == 'system':
    #         system_instruction = {"parts": [{"text": msg['content']}]}
    #     else:
    #         role = 'model' if msg['role'] == 'assistant' else 'user'
    #         gemini_contents.append({
    #             "role": role,
    #             "parts": [{"text": msg['content']}]
    #         })
    #
    # payload = {
    #     "contents": gemini_contents,
    #     "generationConfig": {
    #         "temperature": 0.6,
    #         "maxOutputTokens": 800
    #     }
    # }
    #
    # if system_instruction:
    #     payload["systemInstruction"] = system_instruction
    #
    # try:
    #     # 直接发送 HTTP 请求，不走 SDK，不走代理
    #     response = requests.post(url, json=payload, timeout=60)
    #
    #     if response.status_code != 200:
    #         return f"[ERROR] Gemini API Error: {response.text}"
    #
    #     result = response.json()
    #     # 提取回复文本
    #     return result['candidates'][0]['content']['parts'][0]['text']
    #
    # except Exception as e:
    #     return f"[ERROR] Request failed: {e}"

# --- API：手动触发今日记忆整理 (增量版) ---
@app.route("/api/memory/snapshot", methods=["POST"])
def snapshot_memory():
    today_str = datetime.now().strftime('%Y-%m-%d')
    try:
        count, new_events = update_short_memory_for_date(today_str)
        if count > 0:
            return jsonify({"status": "success", "summary": new_events, "message": f"新增了 {count} 条记忆"})
        else:
            return jsonify({"status": "no_data", "message": "暂时没有新对话需要整理"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 加在 app.py 的路由区域
@app.route("/api/debug/force_maintenance")
def force_maintenance():
    scheduled_maintenance() # 手动调用上面那个定时函数
    return jsonify({"status": "triggered", "message": "已手动触发后台维护，请查看服务器控制台日志"})

# --- 【新增】记忆面板页面 ---
@app.route("/memory")
def memory_view():
    return send_from_directory("templates", "memory.html")

# --- 【新增】获取所有 Prompts 和记忆的 API ---
@app.route("/api/prompts_data")
def get_prompts_data():
    data = {}
    # 定义我们要读取的文件列表
    files = {
        "base": "1_base_persona.md",
        "relation": "2_relationship.json",
        "user": "3_user_persona.md",
        "long": "4_memory_long.json",
        "medium": "5_memory_medium.json",
        "short": "6_memory_short.json",
        "schedule": "7_schedule.json",
        "format": "8_format.md"
    }

    for key, filename in files.items():
        path = os.path.join("prompts", filename)
        content = "（文件不存在或为空）"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                # 如果是 JSON，解析一下以便前端格式化，否则直接读文本
                if filename.endswith(".json"):
                    try: content = json.load(f)
                    except: content = f.read() # 解析失败就读原文
                else:
                    content = f.read()
        data[key] = content

    return jsonify(data)

# --- 【新增】保存 Prompt 文件的接口 ---
@app.route("/api/save_prompt", methods=["POST"])
def save_prompt_file():
    key = request.json.get("key")
    new_content = request.json.get("content") # 可以是字符串(md)或对象(json)

    # 映射 Key 到 文件名
    files_map = {
        "base": "1_base_persona.md",
        "relation": "2_relationship.json",
        "user": "3_user_persona.md",
        "long": "4_memory_long.json",
        "medium": "5_memory_medium.json",
        "short": "6_memory_short.json",
        "schedule": "7_schedule.json",
        "format": "8_format.md"
    }

    filename = files_map.get(key)
    if not filename:
        return jsonify({"status": "error", "message": "Invalid key"}), 400

    path = os.path.join("prompts", filename)

    try:
        with open(path, "w", encoding="utf-8") as f:
            # 如果是 JSON 对象，要转成字符串再存，且保证格式美观
            if filename.endswith(".json") and isinstance(new_content, (dict, list)):
                json.dump(new_content, f, ensure_ascii=False, indent=2)
            else:
                # 如果是文本，直接写
                f.write(str(new_content))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 【新增】搜索接口 ---
@app.route("/api/search", methods=["POST"])
def search_messages():
    keyword = request.json.get("keyword", "").strip()
    if not keyword: return jsonify([])

    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # 模糊搜索，按时间倒序
    cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE content LIKE ? ORDER BY timestamp DESC", (f"%{keyword}%",))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(rows)

# --- 【新增】常用语接口 ---
@app.route("/api/quick_phrases", methods=["GET", "POST"])
def handle_quick_phrases():
    path = os.path.join("prompts", "quick_phrases.json")

    # GET: 读取列表
    if request.method == "GET":
        if not os.path.exists(path):
            return jsonify([]) # 文件不存在返回空列表
        try:
            with open(path, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except:
            return jsonify([])

    # POST: 保存列表
    if request.method == "POST":
        new_list = request.json
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(new_list, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

# --- 定时任务配置 ---
def scheduled_maintenance():
    """
    每天凌晨 04:00 运行一次
    """
    print("\n⏰ 正在执行每日后台维护...")

    # 1. 执行日结 (处理昨天的)
    memory_jobs.process_daily_rollover()

    # 2. 如果今天是周一，执行周结
    # weekday(): 0是周一, 6是周日
    if datetime.now().weekday() == 0:
        memory_jobs.process_weekly_rollover()

    print("✅ 后台维护结束\n")

# --- 【新增】调试工具：打印完整的 Prompt ---
def log_full_prompt(service_name, messages):
    print("\n" + "▼"*20 + f" 🟢 [DEBUG] 发送给 {service_name} 的完整内容 " + "▼"*20)

    for i, msg in enumerate(messages):
        role = msg.get('role', 'unknown').upper()
        content = msg.get('content', '')
        # 如果内容太长（比如几千字的记忆），也完整显示，方便您检查
        print(f"【{i}】<{role}>:")
        print(f"{content}")
        print("-" * 50)

    print("▲"*20 + " [DEBUG] END " + "▲"*20 + "\n")

# ---------------------- 启动 ----------------------

if __name__ == "__main__":
    init_db()
    # 确保 prompts 文件夹存在，防止报错
    if not os.path.exists("prompts"):
        os.makedirs("prompts")
        print("Created 'prompts' directory. Please add md/json files.")

    # --- 【新增】启动后台定时任务 ---
    scheduler = BackgroundScheduler()
    # 每天凌晨 4 点 0 分自动运行 (这个时候您肯定睡了，适合整理记忆)
    scheduler.add_job(func=scheduled_maintenance, trigger="cron", hour=4, minute=0)
    scheduler.start()
    print("--- [Scheduler] 后台记忆整理服务已启动 (每天 04:00) ---")

    app.run(host="0.0.0.0", port=5000, debug=True)
