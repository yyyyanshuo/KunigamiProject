import os
import time
import re
import json
import sqlite3 # 导入 sqlite3 库
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, render_template, session, redirect, url_for # <--- 加上这个
from dotenv import load_dotenv
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler # 新增
import memory_jobs # 导入刚才那个模块
import shutil # 如果以后需要创建新角色用
import random

# 这是在 app.py 文件的开头部分

load_dotenv()  # 从 .env 读取环境变量

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
# 新增下面这行，来读取我们配置的 API 地址
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

app = Flask(__name__, static_folder='static', template_folder='templates')

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = "kunigami_secret_key_change_this" # 【新增】用于加密 Session，随便写
app.permanent_session_lifetime = timedelta(days=30) # 记住登录状态 30 天

# 配置项
MAX_CONTEXT_LINES = 10
DATABASE_FILE = "chat_history.db"

# 定义基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHARACTERS_DIR = os.path.join(BASE_DIR, "characters")
CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
# 【新增】群聊配置路径
GROUPS_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "groups.json")
GROUPS_DIR = os.path.join(BASE_DIR, "groups")

USER_SETTINGS_FILE = os.path.join(BASE_DIR, "configs", "user_settings.json")

# --- 【新增】已读状态管理 ---
READ_STATUS_FILE = os.path.join(BASE_DIR, "configs", "read_status.json")

def get_current_username():
    """获取当前设置的用户名"""
    default_name = "User"
    if not os.path.exists(USER_SETTINGS_FILE):
        return default_name
    try:
        with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("current_user_name", default_name)
    except:
        return default_name

def get_ai_language():
    """获取当前的 AI 回复语言设置 (默认日语 ja)"""
    default_lang = "ja"
    if not os.path.exists(USER_SETTINGS_FILE):
        return default_lang
    try:
        with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("ai_language", default_lang)
    except:
        return default_lang

# ... (之前的 imports 和 常用语接口 保持不变) ...

# --- 工具：获取路径 ---
def get_paths(char_id):
    """根据角色ID生成 数据库路径 和 Prompt文件夹路径"""
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    db_path = os.path.join(char_dir, "chat.db")
    prompts_dir = os.path.join(char_dir, "prompts")
    return db_path, prompts_dir

# --- 工具：初始化指定角色的数据库 ---
def init_char_db(char_id):
    db_path, _ = get_paths(char_id)
    # 确保文件夹存在
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
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

# --- 工具函数：获取指定角色的 DB 路径 ---
def get_char_db_path(char_id):
    return os.path.join(CHARACTERS_DIR, char_id, "chat.db")

def mark_char_as_read(char_id):
    """更新某个角色的最后阅读时间"""
    try:
        data = {}
        if os.path.exists(READ_STATUS_FILE):
            with open(READ_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

        # 记录当前时间
        data[char_id] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with open(READ_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except: pass

@app.route("/api/<char_id>/mark_read", methods=["POST"])
def mark_read_api(char_id):
    mark_char_as_read(char_id)
    return jsonify({"status": "success"})

# ---------------------- 核心：Prompt 构建系统 ----------------------

def build_system_prompt(char_id):  # <--- 增加参数
    """
    根据 prompts/ 文件夹下的文件，动态组装 System Prompt。
    包含：人设、关系、用户档案、格式要求、长/中/短期记忆、日程表、当前时间。
    """
    prompt_parts = []

    # 获取当前日期对象，用于筛选记忆
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 路径准备
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = os.path.join(BASE_DIR, "configs")
    # 获取该角色的 Prompt 目录
    _, prompts_dir = get_paths(char_id)

    # 【关键修改】获取当前动态用户名
    current_user_name = get_current_username()

    print(f"--- [Debug] 正在为 [{char_id}] 构建 Prompt，路径: {prompts_dir} ---") # <--- 加这行调试

    # --- 1. 静态 Markdown 文件 (人设、用户、格式) ---
    # 文件名 -> 标题
    static_files = [
        ("1_base_persona.md", "【Role / キャラクター設定】"),
        ("3_user_persona.md", "【User / ユーザー情報】"),
        ("8_format.md", "【System Rules / 出力ルール】")
    ]
    for filename, title in static_files:
        try:
            path = os.path.join(prompts_dir, filename) # <--- 使用动态目录
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()
                    if content:
                        prompt_parts.append(f"{title}\n{content}")
        except Exception: pass

    # --- 2. 关系设定 (JSON) ---
    try:
        path = os.path.join(prompts_dir, "2_relationship.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                rel_data = json.load(f)
                user_rel = rel_data.get(current_user_name)
                if user_rel:
                    # 【修改】拼装文本改成日语
                    rel_str = (f"対話相手：{current_user_name}\n"
                           f"関係性：{user_rel.get('role', '不明')}\n"
                           f"詳細：{user_rel.get('description', '')}")
                prompt_parts.append(f"【Relationship / 関係設定】\n{rel_str}")
    except Exception: pass

    # --- 4. 长期记忆 (JSON - 按月) ---
    # 这里简单处理：全部读取。如果记忆太长，可以根据 now.strftime("%Y-%m") 只读取当月和上个月
    try:
        path = os.path.join(prompts_dir, "4_memory_long.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                long_mem = json.load(f)
                if long_mem:
                    # 格式化为： - 2025-10: xxxxx
                    mem_list = [f"- {k}: {v}" for k, v in long_mem.items()]
                    prompt_parts.append(f"【Long-term Memory / 長期記憶】\n" + "\n".join(mem_list))
    except Exception: pass

    # 3. 【全局通用】读取用户档案 (从 configs 读)
    try:
        path = os.path.join(CONFIG_DIR, "global_user_persona.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                prompt_parts.append(f"【User / ユーザー情報】\n{f.read().strip()}")
    except: pass

    # 4. 【全局通用】读取格式规则 (从 configs 读)
    try:
        path = os.path.join(CONFIG_DIR, "global_format.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                prompt_parts.append(f"【System Rules / 出力ルール】\n{f.read().strip()}")
    except: pass

    # --- 5. 中期记忆 (JSON - 按天，最近7天) ---
    try:
        path = os.path.join(prompts_dir, "5_memory_medium.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
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
        path = os.path.join(prompts_dir, "6_memory_short.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                short_mem = json.load(f)

                # 获取当天的数据
                dates_to_load = [today_str]

                # 【关键逻辑】如果现在是凌晨 4 点之前，说明昨天还没日结，必须把昨天的也带上
                if now.hour < 4:
                    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    # 把昨天插在前面，按时间顺序读取
                    dates_to_load.insert(0, yesterday_str)

                # 2. 循环读取并拼接
                combined_events_str = ""
                for date_key in dates_to_load:
                    day_data = short_mem.get(date_key)
                    # 兼容格式
                    today_events = []
                    if isinstance(day_data, list): today_events = day_data
                    elif isinstance(day_data, dict): today_events = day_data.get("events", [])

                    if today_events:
                        # 加个日期头，让 AI 分得清
                        combined_events_str += f"\n--- {date_key} ---\n"
                        combined_events_str += "\n".join([f"- [{e.get('time')}] {e.get('event')}" for e in today_events])

                if combined_events_str:
                    prompt_parts.append(f"【Short-term Memory / 最近の出来事】{combined_events_str}")
    except Exception: pass

    # --- 7. 近期安排 (JSON - 仅限未来7天) ---
    try:
        path = os.path.join(prompts_dir, "7_schedule.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f: # 记得用 utf-8-sig
                schedule = json.load(f)
                future_plans = []

                # 计算日期范围: 今天 ~ 7天后
                limit_date = now + timedelta(days=7)
                limit_date_str = limit_date.strftime("%Y-%m-%d")

                sorted_dates = sorted(schedule.keys())
                for date_key in sorted_dates:
                    # 【修改】只选取：今天 <= 日期 <= 7天后
                    if today_str <= date_key <= limit_date_str:
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

    # --- 【新增】语言控制 ---
    lang = get_ai_language()
    if lang == "zh":
        # 强力指令：即使人设是日文，也要用中文回复
        lang_instruction = (
            "\n\n【Language Control / 语言控制】\n"
            "请注意：无论上述设定使用何种语言，你**必须使用中文**进行回复。\n"
            "在保留角色语气、口癖和性格特征的前提下，自然地转化为中文表达。"
        )
        prompt_parts.append(lang_instruction)

    return "\n\n".join(prompt_parts)

# --- 工具：构建群聊时的关系 Prompt (ID -> Name 映射版) ---
def build_group_relationship_prompt(current_char_id, other_member_ids):
    """
    当 current_char_id 说话时，注入他对群里其他人的看法。
    关键：需要把 other_member_ids (如 isagi) 转换为 关系JSON里的 Key (如 洁世一)
    """
    # 1. 读取全局角色配置，建立 ID -> Name 的映射表
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")

    id_to_name_map = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            for cid, cinfo in chars_config.items():
                id_to_name_map[cid] = cinfo.get("name", cid) # 没名字就用ID兜底
    except: pass

    # 2. 读取当前角色的关系文件
    _, prompts_dir = get_paths(current_char_id)
    rel_file = os.path.join(prompts_dir, "2_relationship.json")

    prompt_text = "【Group Relationships / 群聊关系认知】\n(你是群聊的一员，请参考以下你与其他成员的关系)\n"

    if not os.path.exists(rel_file):
        return ""

    try:
        with open(rel_file, "r", encoding="utf-8") as f:
            # 这里的 Key 是名字 (如 "洁世一")
            rels_data = json.load(f)

        found_any = False

        # 3. 遍历在场的其他人，查找关系
        for other_id in other_member_ids:
            if other_id == "user": continue

            # 获取对方的名字
            target_name = id_to_name_map.get(other_id, other_id)

            # 在关系表里查找
            # 尝试直接匹配名字
            rel_info = rels_data.get(target_name)

            if rel_info:
                role = rel_info.get('role', '未知')
                desc = rel_info.get('description', '特になし')
                prompt_text += f"- 対 {target_name}: {role} ({desc})\n"
                found_any = True
            else:
                # 如果没找到特定关系，也可以不写，或者写个默认
                pass

        if not found_any:
            return "" # 如果跟群里的人都没关系，就不加这段 prompt

        return prompt_text

    except Exception as e:
        print(f"Build Group Rel Error: {e}")
        return ""

# --- 【修正版】AI 总结专用函数 (双语支持) ---
def call_ai_to_summarize(text_content, prompt_type="short", char_id="kunigami"):
    if not text_content: return None

    # 获取角色名和语言
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
    char_name = "私"
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            if char_id in chars_config:
                char_name = chars_config[char_id]["name"]
    except: pass

    lang = get_ai_language()

    # --- Prompt 字典 ---
    prompts = {
        "ja": {
            "short": (
                f"あなたは{char_name}本人として、自身の記憶を整理しています。"
                "以下の会話ログから、重要な出来事を抽出してください。"
                "出力フォーマット：\n- [HH:MM] (自分または相手の行動・会話の要点、一言で)"
            ),
            "medium": (
                f"あなたは{char_name}本人です。この一日の出来事を振り返り、**一つの繋がった文章（段落形式）**で要約してください。"
                "**要件**：\n1. 時間表記は不可。\n2. 箇条書きは禁止。\n3. **一人称視点**で、事実のみを淡々と記述すること。\n4. 300字前後"
            ),
            "long": (
                f"あなたは{char_name}本人です。この一週間の記録を振り返り、全体的な流れを要約してください。"
                "**要件**：\n1. 時間表記は不可。\n2. 箇条書きは禁止。\n3. 事実ベースで記述すること。"
            ),
            "group_log": (
                "あなたはグループチャットの書記係（第三者）です。"
                "以下の会話ログから、重要なトピックや出来事を**客観的に**抽出してください。"
                "出力フォーマット：\n- [HH:MM] 出来事の内容"
            )
        },
        "zh": {
            "short": (
                f"你现在是{char_name}本人，正在整理自己的记忆。"
                "请从以下的对话记录中提取重要的事件。"
                "输出格式：\n- [HH:MM] (自己或对方的行动/对话要点，一句话)"
            ),
            "medium": (
                f"你现在是{char_name}本人。请回顾这一天发生的事情，将其总结为**一段连贯的文章（段落格式）**。"
                "**要求**：\n1. **不要**包含具体时间点（如[HH:MM]）。\n2. 禁止使用列表/条目格式。\n3. 使用**第一人称**（我），仅平实地记录发生的事实（不要过度抒情）。\n4. 300字左右"
            ),
            "long": (
                f"你现在是{char_name}本人。请回顾这一周的记录，总结整体的流程。"
                "**要求**：\n1. **不要**包含具体时间点。\n2. 禁止使用列表/条目格式。\n3. 基于事实，进行客观总结。"
            ),
            "group_log": (
                "你是群聊的书记员（第三方视角）。"
                "请从以下的对话记录中，**客观地**提取重要的话题或事件。"
                "要求：\n1. 不要使用第一人称。\n2. 明确主语（如“[名字]说了...”、“大家决定...”）。\n"
                "输出格式：\n- [HH:MM] 事件内容"
            )
        }
    }

    # 选择对应语言和类型的 Prompt
    system_instruction = prompts.get(lang, prompts["ja"]).get(prompt_type, "")

    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Log:\n{text_content}"}
    ]

    print(f"--- Memory Summary ({prompt_type}) [Lang:{lang}] ---")

    # 1. 获取当前配置
    route, current_model = get_model_config("summary") # 任务类型是 chat

    print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

    if route == "relay":
        return call_openrouter(messages, char_id=char_id, model_name=current_model)
    else:
        return call_gemini(messages, char_id=char_id, model_name=current_model)

# --- 【修正版】核心逻辑：增量更新 (支持强制重置) ---
def update_short_memory_for_date(char_id, target_date_str, force_reset=False): # <--- 增加参数
    # 1. 动态获取路径
    db_path, prompts_dir = get_paths(char_id)
    short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")

    # 2. 读取现有记忆
    current_data = {}
    if os.path.exists(short_mem_path):
        with open(short_mem_path, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    day_data = current_data.get(target_date_str)
    existing_events = []
    last_id = 0

    # 如果不是强制重置，才去读旧数据
    if not force_reset:
        if isinstance(day_data, list):
            existing_events = day_data
            last_id = 0
        elif isinstance(day_data, dict):
            existing_events = day_data.get("events", [])
            last_id = day_data.get("last_id", 0)
    else:
        print(f"   -> [Force Reset] 强制重置 {target_date_str}，从头开始扫描")

    # 3. 查询数据库 (私聊 DB)
    if not os.path.exists(db_path):
        return 0, []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    # 读取新消息
    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"[{target_date_str}] 没有新增私聊消息需要总结。")
        return 0, []

    new_max_id = rows[-1][0]

    # 4. 拼接文本
    # 加载名字映射
    id_to_name = {}
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        CHAR_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
        with open(CHAR_CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for k, v in c_conf.items(): id_to_name[k] = v["name"]
    except: pass

    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        name = "ユーザー" if role == "user" else id_to_name.get(role, role)
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 5. 调用 AI 总结
    try:
        # 传入 char_id 以匹配人设
        summary_text = call_ai_to_summarize(chat_log, "short", char_id)
        if not summary_text: return 0, []

        new_events_raw = []
        import re
        for line in summary_text.split('\n'):
            line = line.strip()
            if line:
                match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
                event_time = match_time.group(1) if match_time else datetime.now().strftime("%H:%M")
                event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
                new_events_raw.append({"time": event_time, "event": event_text})

        if not new_events_raw: return 0, []

        # --- 【关键修改】合并逻辑 (群聊记忆保护) ---

        all_events = []

        if last_id > 0:
            # A. 增量模式 (Append)：直接追加
            print(f"   -> [增量模式] 追加 {len(new_events_raw)} 条私聊记忆")
            all_events = existing_events + new_events_raw
        else:
            # B. 覆盖模式 (Reset/First Run)：
            # 以前是直接 all_events = new_events_raw (导致群聊丢失)
            # 现在我们要：保留旧数据里的【群聊】条目，只覆盖【私聊】条目

            print(f"   -> [覆盖模式] 正在保护群聊记忆...")

            # 筛选出旧数据里的群聊记忆 (特征：包含 "[群聊:")
            # 或者更严谨：我们假设 AI 总结的私聊不会自己加 [群聊:...] 前缀
            protected_group_events = [e for e in existing_events if "[群聊:" in e.get('event', '')]

            # 合并：旧的群聊 + 新总结的私聊
            all_events = protected_group_events + new_events_raw

        # 按时间重新排序 (让群聊和私聊按时间线穿插)
        all_events.sort(key=lambda x: x['time'])

        # 保存
        current_data[target_date_str] = {
            "events": all_events,
            "last_id": new_max_id
        }

        with open(short_mem_path, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)

        return len(new_events_raw), new_events_raw

    except Exception as e:
        print(f"增量总结出错: {e}")
        return 0, []

# --- 【修正版】分发群聊记忆给成员 ---
def distribute_group_memory(group_id, group_name, members, new_events, date_str):
    """
    将群聊新生成的事件，追加到每个成员的 6_memory_group_log.json 中
    """
    if not new_events:
        print("   [Distribute] 没有新事件需要分发")
        return

    print(f"   [Distribute] 正在分发 {len(new_events)} 条事件给成员: {members}")

    for char_id in members:
        if char_id == "user": continue # 跳过用户

        try:
            # 1. 找到该角色的文件路径
            _, prompts_dir = get_paths(char_id)
            # 【修改】目标文件改为 6_memory_short.json
            short_file = os.path.join(prompts_dir, "6_memory_short.json")

            # 2. 读取现有数据
            current_data = {}
            if os.path.exists(short_file):
                with open(short_file, "r", encoding="utf-8") as f:
                    try: current_data = json.load(f)
                    except: pass

            # 兼容新旧格式 (获取当天的 dict)
            day_data = current_data.get(date_str, {})
            # 如果是旧格式列表，转为字典结构
            if isinstance(day_data, list):
                existing_events = day_data
                last_id = 0
            else:
                existing_events = day_data.get("events", [])
                last_id = day_data.get("last_id", 0)

            # 3. 追加新事件 (格式化一下，标明来源)
            count_added = 0
            for event in new_events:
                # 格式化内容：[群聊:群名] 事件
                # 【修改】这里确保 event['event'] 是纯文本，不包含奇怪的 AI 生成头信息
                clean_event_text = event['event'].replace('AI生成信息发送的内容', '').strip()
                event_content = f"[群聊:{group_name}] {clean_event_text}"

                # 简单去重
                is_duplicate = False
                for old in existing_events:
                    if old['time'] == event['time'] and event_content in old['event']:
                        is_duplicate = True
                        break

                if not is_duplicate:
                    existing_events.append({
                        "time": event['time'],
                        "event": event_content
                    })
                    count_added += 1

            if count_added > 0:
                # 按时间重新排序 (保证群聊和私聊按时间穿插)
                existing_events.sort(key=lambda x: x['time'])

                # 保存回文件 (保持 last_id 不变，因为这些群聊消息不属于私聊数据库)
                current_data[date_str] = {
                    "events": existing_events,
                    "last_id": last_id
                }

                with open(short_file, "w", encoding="utf-8") as f:
                    json.dump(current_data, f, ensure_ascii=False, indent=2)

                print(f"     -> [{char_id}] 合并成功 (+{count_added}条)")

        except Exception as e:
            print(f"     ❌ 同步给 [{char_id}] 失败: {e}")

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

# --- 【新增】全局登录校验 ---
@app.before_request
def require_login():
    # 定义不需要登录就能访问的白名单
    allowed_routes = [
        'login_page', 'login_api', # 登录相关
        'static', 'manifest', 'service_worker', 'app_logo' # 静态资源 & PWA
    ]

    # 如果当前请求的 endpoint 不在白名单，且 session 里没有 logged_in 标记
    if request.endpoint and request.endpoint not in allowed_routes and 'logged_in' not in session:
        return redirect('/login')

# --- 【新增】登录页面 ---
@app.route("/login")
def login_page():
    if 'logged_in' in session:
        return redirect('/')
    return render_template("login.html")

# --- 【新增】登录 API ---
@app.route("/api/login", methods=["POST"])
def login_api():
    data = request.json
    input_user = data.get("username")
    input_pass = data.get("password")

    # 读取配置文件里的用户名和密码
    # 如果没有配置文件，默认账号: admin / 123456
    saved_user = "admin"
    saved_pass = "123456"

    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                saved_user = user_data.get("current_user_name", "admin")
                saved_pass = user_data.get("password", "123456") # 读取密码
        except: pass

    if input_user == saved_user and input_pass == saved_pass:
        session['logged_in'] = True
        session.permanent = True # 开启持久化
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "用户名或密码错误"}), 401

# --- 【新增】退出登录 (可选) ---
@app.route("/logout")
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

# --- 【新增】PWA 支持文件路由 ---
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    # 必须设置 Header 确保 Service Worker 权限正确
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route("/")
def contact_list_view():
    # 改用 render_template，这样 html 里的 {% include %} 才会生效
    return render_template("contacts.html")

# 2. 【新增】个人主页
@app.route("/profile")
def profile_view():
    return render_template("profile.html")

# 聊天页面改为带 ID 的路由
# 注意：原来的 / 路由废弃或重定向
@app.route("/chat/<char_id>")
def chat_view(char_id):
    # 这里只是返回 HTML，前端会根据 URL 里的 ID 去加载数据
    # 实际项目中，您可能需要把 char_id 传给模板，或者让前端自己解析 URL
    return send_from_directory("templates", "chat.html")

# --- 【新增】群聊页面路由 ---
@app.route("/chat/group/<group_id>")
def group_chat_view(group_id):
    # 复用 chat.html，但在前端根据 URL 区分逻辑
    return send_from_directory("templates", "chat.html")

# --- 【修正版】获取通讯录 (角色 + 群聊 混合列表) ---
@app.route("/api/contacts")
def get_contacts():
    # 1. 读取角色配置
    if not os.path.exists(CONFIG_FILE):
        return jsonify([])

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        chars_config = json.load(f)

    contact_list = []

    # 1. 先读取已读状态文件
    read_status = {}
    if os.path.exists(READ_STATUS_FILE):
        try:
            with open(READ_STATUS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip() # 先读成字符串
                if content: # 只有不为空才解析
                    read_status = json.loads(content)
        except (json.JSONDecodeError, ValueError) as e:
            # 如果正好撞上文件正在写入（为空），或者文件坏了
            # 这里的 print 可以帮您确认是不是这个问题，但不影响程序运行
            print(f"⚠️ [Warning] 读取已读状态冲突 (可忽略): {e}")
            read_status = {} # 降级处理：假装没有已读记录

    # --- A. 处理单人角色 ---
    for char_id, info in chars_config.items():
        db_path = os.path.join(BASE_DIR, "characters", char_id, "chat.db")

        last_msg = ""
        last_time = ""
        timestamp_val = 0

        # --- 【新增】计算未读数 ---
        unread_count = 0
        if os.path.exists(db_path):
            try:
                # 获取上次已读时间，如果没有则默认为很久以前
                last_read = read_status.get(char_id, "2000-01-01 00:00:00")

                # 查询：时间 > last_read 且 role != 'user' (不是我发的) 的消息数量
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ? AND role != 'user'", (last_read,))
                unread_count = cursor.fetchone()[0]
                conn.close()
            except: pass

        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT content, timestamp FROM messages ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                conn.close()
                if row:
                    last_msg = row[0]
                    timestamp_val = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S').timestamp()
                    # 简单的时间格式化
                    dt = datetime.fromtimestamp(timestamp_val)
                    if dt.date() == datetime.now().date():
                        last_time = dt.strftime('%H:%M')
                    else:
                        last_time = dt.strftime('%m-%d')
            except: pass

        contact_list.append({
            "type": "char", # 标记类型
            "id": char_id,
            "avatar": info.get("avatar", "/static/default_avatar.png"),
            "name": info.get("name"),
            "remark": info.get("remark") or info["name"],
            "last_msg": last_msg,
            "last_time": last_time,
            "timestamp": timestamp_val,
            "pinned": info.get("pinned", False),
            "unread": unread_count # <--- 加上这个
        })

    # --- B. 处理群聊 ---
    if os.path.exists(GROUPS_CONFIG_FILE):
        try:
            with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                groups_config = json.load(f)

            for group_id, info in groups_config.items():
                db_path = os.path.join(GROUPS_DIR, group_id, "chat.db")

                last_msg = ""
                last_time = ""
                timestamp_val = 0
                unread_count = 0  # <--- 初始化为 0

                if os.path.exists(db_path):
                    try:
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()

                        # 1. 获取最后一条消息
                        cursor.execute("SELECT content, timestamp FROM messages ORDER BY id DESC LIMIT 1")
                        row = cursor.fetchone()

                        if row:
                            last_msg = row[0]
                            timestamp_val = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S').timestamp()
                            dt = datetime.fromtimestamp(timestamp_val)
                            if dt.date() == datetime.now().date():
                                last_time = dt.strftime('%H:%M')
                            else:
                                last_time = dt.strftime('%m-%d')

                        # 2. 【新增】计算未读数
                        # 获取该群的最后阅读时间
                        last_read = read_status.get(group_id, "2000-01-01 00:00:00")

                        # 统计：时间 > last_read 且 发言人不是 user 的消息
                        cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ? AND role != 'user'", (last_read,))
                        unread_count = cursor.fetchone()[0]

                        conn.close()
                    except: pass

                contact_list.append({
                    "type": "group",
                    "id": group_id,
                    "avatar": info.get("avatar", "/static/default_group.png"),
                    "name": info.get("name"),
                    "remark": info.get("name"),
                    "last_msg": last_msg,
                    "last_time": last_time,
                    "timestamp": timestamp_val,
                    "pinned": info.get("pinned", False),
                    "members": info.get("members", []),
                    "unread": unread_count  # <--- 【关键】把计算结果放进去
                })
        except Exception as e:
            print(f"Error loading groups: {e}")

    # 4. 统一排序
    contact_list.sort(key=lambda x: (1 if x['pinned'] else 0, x['timestamp']), reverse=True)

    return jsonify(contact_list)

# --- 【修正版】单聊历史记录 (精准定位版) ---
@app.route("/api/<char_id>/history", methods=["GET"])
def get_history(char_id):
    limit = request.args.get('limit', 20, type=int)
    target_id = request.args.get('target_id', type=int)
    before_id = request.args.get('before_id', type=int)

    db_path, _ = get_paths(char_id)
    if not os.path.exists(db_path): init_char_db(char_id)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    messages = []

    # A. 向上滚动 (锚点模式)
    if before_id:
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    # B. 跳转定位 (精准覆盖模式)
    elif target_id:
        # 1. 找到目标消息的时间戳
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']
            # 2. 计算比它新的消息有多少条
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]

            # 3. 【关键】设定 Limit = 比它新的数量 + 1 (它自己) + 5 (缓冲，防止毫秒级误差)
            # 这样一次性加载从最新到它（包含它）的所有数据
            dynamic_limit = count_newer + 6

            cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ?", (dynamic_limit,))
            messages = [dict(row) for row in cursor.fetchall()][::-1]

    # C. 默认加载
    else:
        cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    cursor.execute("SELECT COUNT(id) FROM messages")
    total_messages = cursor.fetchone()[0]
    conn.close()

    return jsonify({
        "messages": messages,
        "total": total_messages
    })

# --- 【修正版】群聊历史记录 (精准定位版) ---
@app.route("/api/group/<group_id>/history", methods=["GET"])
def get_group_history(group_id):
    limit = request.args.get('limit', 20, type=int)
    target_id = request.args.get('target_id', type=int)
    before_id = request.args.get('before_id', type=int)

    db_path = os.path.join(GROUPS_DIR, group_id, "chat.db")
    if not os.path.exists(db_path):
        return jsonify({"messages": [], "total": 0})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    messages = []

    # A. 向上滚动
    if before_id:
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    # B. 跳转定位
    elif target_id:
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]

            # 动态 Limit
            dynamic_limit = count_newer + 6

            cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ?", (dynamic_limit,))
            messages = [dict(row) for row in cursor.fetchall()][::-1]

    # C. 默认加载
    else:
        cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        messages = [dict(row) for row in cursor.fetchall()][::-1]

    cursor.execute("SELECT COUNT(id) FROM messages")
    total = cursor.fetchone()[0]
    conn.close()

    return jsonify({"messages": messages, "total": total})

# 这是在 app.py 文件中

# ---------------------- 核心聊天接口 (时间感知注入版) ----------------------
# --- 核心聊天接口 (多角色适配 + 返回ID修正版) ---
@app.route("/api/<char_id>/chat", methods=["POST"])
def chat(char_id):
    # 1. 动态获取路径
    db_path, prompts_dir = get_paths(char_id)
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")

    # 2. 防御性初始化
    if not os.path.exists(db_path):
        init_char_db(char_id)

    # 数据准备
    data = request.json or {}
    user_msg_raw = data.get("message", "").strip()
    if not user_msg_raw:
        return jsonify({"error": "empty message"}), 400

    # --- 3. 检查深睡眠状态 ---
    is_deep_sleep = False
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            char_info = all_config.get(char_id, {})
            # 获取开关状态
            is_deep_sleep = char_info.get("deep_sleep", False)

            # (可选) 高级逻辑：如果想配合时间段自动判断，可以在这里加
            # 比如：虽然开关开了，但如果不在时间段内，视为醒着？
            # 或者：开关只作为总开关。这里暂时按您的要求：开关开=不回。
    except: pass

    # --- 4. 无论睡没睡，先存入用户消息 ---
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')

    # 存用户消息
    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))
    user_msg_id = cursor.lastrowid # 获取 ID

    conn.commit()
    conn.close()

    # --- 5. 如果在深睡眠，直接返回空回复，不调 AI ---
    if is_deep_sleep:
        print(f"--- [Deep Sleep] {char_id} 正在熟睡，不回复消息 ---")

        # 即使不回复，也把 user_id 传回去，这样用户发的气泡才有删除按钮
        return jsonify({
            "replies": [],
            "id": None,
            "user_id": user_msg_id
        })

    # ================= 醒着：正常调用 AI 逻辑 =================

    # 6. 动态构建 System Prompt
    system_prompt = build_system_prompt(char_id)

    # 7. 构建消息历史 (读取最近20条)
    messages = [{"role": "system", "content": system_prompt}]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 21")
    history_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    # --- 【关键修改】判断时间跨度 ---
    now = datetime.now()
    show_full_date = False # 默认不显示日期，只显示时间

    if history_rows:
        try:
            # 1. 获取第一条历史记录的时间 (最早的一条)
            first_msg_ts_str = history_rows[0]['timestamp']
            first_dt = datetime.strptime(first_msg_ts_str, '%Y-%m-%d %H:%M:%S')

            # 2. 比较：最早一条的日期 vs 现在(最新一条)的日期
            # 如果日期不同 (比如昨天聊的 vs 今天聊的)，则开启“日期显示模式”
            if first_dt.date() != now.date():
                show_full_date = True
        except:
            # 如果解析出错，为了保险起见，保持默认或者开启
            pass

    # --- 循环处理历史消息 ---
    for row in history_rows:
        try:
            dt_object = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')

            if show_full_date:
                # 跨天模式：显示 [12-25 14:30]
                formatted_timestamp = dt_object.strftime('[%m-%d %H:%M]')
            else:
                # 同天模式：只显示 [14:30]
                formatted_timestamp = dt_object.strftime('[%H:%M]')

            formatted_content = f"{formatted_timestamp} {row['content']}"
            messages.append({"role": row['role'], "content": formatted_content})
        except:
            # 容错：原样添加
            messages.append({"role": row['role'], "content": row['content']})

    # 1. 获取当前配置
    route, current_model = get_model_config("chat") # 任务类型是 chat

    print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

    try:
        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 清理时间戳
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()

        # 6. 存入数据库 (关键修改在这里！)
        now = datetime.now()
        user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        ai_ts = (now + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 存 AI 消息
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("assistant", cleaned_reply_text, ai_ts))

        # 【重点】获取刚插入的 AI 消息的 ID
        ai_msg_id = cursor.lastrowid

        conn.commit()
        conn.close()

        reply_bubbles = list(filter(None, [part.strip() for part in cleaned_reply_text.split('/')]))

        # 【重点】把 ID 返回给前端
        return jsonify({
            "replies": reply_bubbles,
            "id": ai_msg_id,  # <--- 这行是能够删除新消息的关键
            "user_id": user_msg_id # <--- 把这个带回去
        })

    except Exception as e:
        print(f"Chat Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【修正版】重新生成接口 (自动补全 User 引导) ---
@app.route("/api/<char_id>/regenerate", methods=["POST"])
def regenerate_message(char_id):
    # 1. 获取路径
    db_path, prompts_dir = get_paths(char_id)
    if not os.path.exists(db_path): return jsonify({"error": "DB not found"}), 404

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 2. 检查最后一条是否为 assistant (安全检查)
        cursor.execute("SELECT id, role, content FROM messages ORDER BY id DESC LIMIT 1")
        last_row = cursor.fetchone()

        if not last_row:
            conn.close()
            return jsonify({"error": "No messages"}), 400

        if last_row['role'] != 'assistant':
            conn.close()
            return jsonify({"error": "Last message is not from assistant"}), 400

        # 3. 删除这条消息
        cursor.execute("DELETE FROM messages WHERE id = ?", (last_row['id'],))
        conn.commit()

        # 4. 动态构建 System Prompt
        system_prompt = build_system_prompt(char_id)
        messages = [{"role": "system", "content": system_prompt}]

        # 5. 读取剩余的历史记录
        cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
        history_rows = [dict(row) for row in cursor.fetchall()][::-1]
        conn.close()

        # 6. 构建上下文
        show_full_date = False
        now = datetime.now()
        if history_rows:
            try:
                first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if first_ts.date() != now.date(): show_full_date = True
            except: pass

        for row in history_rows:
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                ts_str = dt_obj.strftime('[%m-%d %H:%M]') if show_full_date else dt_obj.strftime('[%H:%M]')
                formatted_content = f"{ts_str} {row['content']}"
                messages.append({"role": row['role'], "content": formatted_content})
            except:
                messages.append({"role": row['role'], "content": row['content']})

        # ================= 【核心新增】智能补位逻辑 =================
        # 检查发给 AI 的最后一条消息是谁说的
        if len(messages) > 1: # 排除掉只有 System Prompt 的情况
            last_msg_role = messages[-1]['role']

            # 如果上一条依然是 assistant (说明是连续回复)，补一条假的 User 消息
            if last_msg_role == 'assistant' or last_msg_role == 'model':
                lang = get_ai_language()

                # 构造引导词 (不存数据库，仅用于诱导 AI)
                if lang == "zh":
                    fake_prompt = "(继续说)"
                else:
                    fake_prompt = "(続き)"

                print(f"--- [Regenerate] 检测到连续对话，插入隐形引导: {fake_prompt} ---")
                messages.append({"role": "user", "content": fake_prompt})
        # ===========================================================

        # 7. 调用 AI
        route, current_model = get_model_config("chat")
        print(f"--- [Regenerate] Route: {route}, Model: {current_model} ---")

        if route == "relay":
            reply_text_raw = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 8. 清理 & 存入
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()

        ai_ts = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("assistant", cleaned_reply_text, ai_ts))
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()

        reply_bubbles = list(filter(None, [part.strip() for part in cleaned_reply_text.split('/')]))

        return jsonify({
            "status": "success",
            "replies": reply_bubbles,
            "id": new_id
        })

    except Exception as e:
        print(f"Regenerate Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【修正版】群聊核心接口 (完整逻辑：@解析 + 串行 + 变量修复) ---
@app.route("/api/group/<group_id>/chat", methods=["POST"])
def group_chat(group_id):
    import random
    import re

    # 1. 基础准备
    data = request.json
    user_msg = data.get("message", "").strip()
    if not user_msg: return jsonify({"error": "empty"}), 400

    group_dir = os.path.join(GROUPS_DIR, group_id)
    db_path = os.path.join(group_dir, "chat.db")

    # 2. 读取群成员 (所有成员)
    all_members = []
    if os.path.exists(GROUPS_CONFIG_FILE):
        with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
            group_conf = json.load(f)
            if group_id in group_conf:
                all_members = group_conf[group_id].get("members", [])

    # 排除用户
    ai_members_all = [m for m in all_members if m != "user"]
    if not ai_members_all: return jsonify({"error": "No AI members"}), 404

    # --- 【关键修正 1】提前初始化变量 ---
    replies_for_frontend = []

    # --- 【关键步骤】获取在线成员 (过滤掉深睡眠的) ---
    # 需要读取 characters.json 查看 deep_sleep 状态
    id_to_name = {}
    name_to_id = {}
    online_ai_members = [] # 最终的在线名单

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for cid, cinfo in c_conf.items():
                # ID -> Name (用于显示)
                name = cinfo.get("name", cid)
                id_to_name[cid] = name

                # Name -> ID (用于解析 @)
                # 映射 "国神" -> "kunigami"
                name_to_id[name] = cid
                # 映射 "英雄" (备注) -> "kunigami"
                if cinfo.get("remark"):
                    name_to_id[cinfo.get("remark")] = cid

                # 2. 检查是否在线 (Deep Sleep False)
                # 只有在群成员列表里 且 没有深睡眠 的才算在线
                if cid in ai_members_all:
                    is_sleeping = cinfo.get("deep_sleep", False)
                    if not is_sleeping:
                        online_ai_members.append(cid)
                    else:
                        print(f"   [GroupChat] 成员 {name}({cid}) 正在熟睡，跳过。")

    # 如果全员都在睡觉，直接返回空
    if not online_ai_members:
        print("--- [GroupChat] 全员睡眠中，无人回复 ---")
        # 依然要存用户消息
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        now = datetime.now()
        user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg, user_ts))
        conn.commit()
        conn.close()
        return jsonify({"replies": []})

    # 3. 存入用户消息
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                   ("user", user_msg, user_ts))
    user_msg_id = cursor.lastrowid # 【新增】获取刚存入的用户消息 ID
    conn.commit()
    conn.close()

    # 4. 决定回复顺序 (智能 @ 逻辑)
    responder_ids = []

    # A. 【最高优先级】检测 @所有人
    # 这里的判断很简单：只要字符串里包含这个词就行
    if "@所有人" in user_msg:
        print("--- [GroupChat] 模式: @所有人 (在线全员) ---")
        responder_ids = list(online_ai_members)
        # 打乱顺序，让每次“开会”的发言顺序都不一样，更真实
        random.shuffle(responder_ids)

    # B. 如果没有 @所有人，再检测具体名字
    else:
        # 解析用户消息里的 @
        mentioned_names = re.findall(r'@(.*?)(?:\s|$)', user_msg)

        if mentioned_names:
            print(f"--- [GroupChat] 检测到 @: {mentioned_names} ---")
            for name in mentioned_names:
                # 尝试匹配 ID
                if name in name_to_id:
                    target_id = name_to_id[name]
                    # 只有在线的才回
                    if target_id in online_ai_members:
                        responder_ids.append(target_id)
                    else:
                        print(f"   -> @{name} 在线状态不满足，不回复")
                else:
                    print(f"   -> 未找到名为 '{name}' 的群成员")

    # C. 如果没有有效 @，回退到随机逻辑
    if not responder_ids:
        # 获取当前群里 AI 的实际数量
        count_online = len(online_ai_members)

        # 逻辑：想要随机回复 1~2 人，但不能超过实际人数
        # 比如：如果只有 1 个 AI，那就只能回 1 次
        # 如果有 3 个 AI，可以随机回 1 或 2 次
        target_k = random.randint(1, count_online)

        responder_ids = random.sample(online_ai_members, k=target_k)
        print(f"--- [GroupChat] 模式: 随机抽取 {len(responder_ids)} 人 ---")
    else:
        print(f"--- [GroupChat] 指定模式: 顺序 {responder_ids} ---")

    # 5. 预加载历史记录 (Context Buffer)
    context_buffer = []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # 读取最近 15 条 (包含刚才用户的发言)
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 16")
    rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    for row in rows:
        role_id = row['role']
        # 这里用到了 id_to_name，前面已经定义好了，不会报错
        display_name = "User" if role_id == "user" else id_to_name.get(role_id, role_id)

        context_buffer.append({
            "role_id": role_id,
            "display_name": display_name,
            "content": row['content']
        })

    # 6. 串行循环生成
    # 【注意】这里遍历的是确定的 responder_ids 列表
    for i, speaker_id in enumerate(responder_ids):

        speaker_name = id_to_name.get(speaker_id, speaker_id)
        print(f"   -> 第 {i+1} 轮: 由 [{speaker_name}] 发言")

        # --- A. 构建 Prompt ---
        sys_prompt = build_system_prompt(speaker_id)
        other_members = [m for m in all_members if m != speaker_id]
        rel_prompt = build_group_relationship_prompt(speaker_id, other_members)

        full_sys_prompt = sys_prompt + "\n\n" + rel_prompt + "\n【Current Situation】\n当前是在群聊中。请注意上下文，与其他成员自然互动。"

        messages = [{"role": "system", "content": full_sys_prompt}]

        # ==================== 【开始替换】 ====================

        # --- B. 读取并处理历史记录 (智能时间戳 + 名字标签) ---
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # 读取最近 20 条
        cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
        history_rows = [dict(row) for row in cursor.fetchall()][::-1]
        conn.close()

        # 1. 判断时间跨度 (是否跨天)
        show_full_date = False
        now_dt = datetime.now() # 获取当前时间用于比较
        if history_rows:
            try:
                first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if first_ts.date() != now_dt.date():
                    show_full_date = True
            except: pass

        # 2. 循环处理每一条历史消息
        for row in history_rows:
            # a. 处理时间戳格式
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                if show_full_date:
                    # 跨天：显示 [12-25 14:30]
                    ts_str = dt_obj.strftime('[%m-%d %H:%M]')
                else:
                    # 同天：只显示 [14:30]
                    ts_str = dt_obj.strftime('[%H:%M]')
            except:
                ts_str = ""

            # b. 处理名字 (群聊必须带名字，否则AI分不清谁是谁)
            # id_to_name 是函数开头建立的映射表
            r_id = row['role']
            d_name = "User" if r_id == "user" else id_to_name.get(r_id, r_id)

            # c. 组合 Content (格式: [12:30] [洁世一]: 咱们去踢球吧)
            # 注意：历史记录里的所有人对当前AI来说都是 external input (user)
            msg_role = "user"
            content_with_tag = f"{ts_str} [{d_name}]: {row['content']}"

            messages.append({"role": msg_role, "content": content_with_tag})

        # --- C. 注入本轮已生成的 Context Buffer (也要带时间) ---
        # 这些是刚刚生成还没存库的，或者刚存库但逻辑上属于连贯对话
        # 其实上面的 SQL 查询已经包含了 user_msg，所以 buffer 里只存 AI 刚刚生成的
        for buf_msg in context_buffer:
            # 简单起见，Buffer 里的默认为当前时间
            cur_ts = now.strftime('[%H:%M]')
            buf_content = f"{cur_ts} [{buf_msg['display_name']}]: {buf_msg['content']}"
            messages.append({"role": "user", "content": buf_content})

        # 1. 获取当前配置
        route, current_model = get_model_config("chat") # 任务类型是 chat

        print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

        try:
            if route == "relay":
                reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model)
            else:
                reply_text = call_gemini(messages, char_id=speaker_id, model_name=current_model)

            timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
            cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()

            # 去除 AI 自带的名字前缀
            name_pattern = f"^\\[{speaker_name}\\][:：]\\s*"
            cleaned_reply = re.sub(name_pattern, '', cleaned_reply).strip()

            if not cleaned_reply: continue

            # --- D. 存档 ---
            ai_ts = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           (speaker_id, cleaned_reply, ai_ts))
            # 【关键修复】获取刚刚插入的这条消息的 ID
            new_msg_id = cursor.lastrowid
            conn.commit()
            conn.close()

            # 更新 Buffer (供下一个人看)
            context_buffer.append({
                "role_id": speaker_id,
                "display_name": speaker_name,
                "content": cleaned_reply
            })

            # 【关键修正 3】添加到返回列表
            replies_for_frontend.append({
                "id": new_msg_id,
                "char_id": speaker_id,
                "name": speaker_name,
                "content": cleaned_reply,
                "timestamp": ai_ts
            })

        except Exception as e:
            print(f"Group Chat Error ({speaker_id}): {e}")

    # 7. 最终返回
    return jsonify({
        "replies": replies_for_frontend,
        "user_id": user_msg_id # <--- 把这个带回去
    })

# --- 辅助：写入个人群聊日志 ---
def update_group_log(char_id, event_content, timestamp_str):
    _, prompts_dir = get_paths(char_id)
    log_file = os.path.join(prompts_dir, "6_memory_group_log.json")

    date_str = timestamp_str.split(' ')[0]
    time_str = timestamp_str.split(' ')[1][:5]

    current_data = {}
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    if date_str not in current_data:
        current_data[date_str] = []

    current_data[date_str].append({
        "time": time_str,
        "event": event_content
    })

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(current_data, f, ensure_ascii=False, indent=2)

# 3. 【新增】在 app.py 末尾添加这两个新接口
# --- 【修正版】删除消息 (带结果检查) ---
@app.route("/api/<char_id>/messages/<int:msg_id>", methods=["DELETE"])
def delete_message(char_id, msg_id):
    # 1. 统一使用工具函数获取路径，防止路径写错
    db_path, _ = get_paths(char_id)

    print(f"--- [Debug] 尝试删除消息 ID: {msg_id} (DB: {db_path}) ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Database not found"}), 404

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 执行删除
        cursor.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        rows_affected = cursor.rowcount # 获取受影响的行数

        conn.commit()
        conn.close()

        if rows_affected > 0:
            print(f"   ✅ 删除成功，影响行数: {rows_affected}")
            return jsonify({"status": "success"})
        else:
            print(f"   ⚠️ 删除失败: 数据库中找不到 ID={msg_id}")
            return jsonify({"error": "Message ID not found"}), 404

    except Exception as e:
        print(f"   ❌ 删除报错: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】群聊消息删除接口 ---
@app.route("/api/group/<group_id>/messages/<int:msg_id>", methods=["DELETE"])
def delete_group_message(group_id, msg_id):
    # 1. 获取群聊数据库路径
    # 确保 GROUPS_DIR 已定义 (在文件头部)
    group_dir = os.path.join(GROUPS_DIR, group_id)
    db_path = os.path.join(group_dir, "chat.db")

    print(f"--- [Debug] 删除群消息: Group={group_id}, MsgID={msg_id} ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Group DB not found"}), 404

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        rows_affected = cursor.rowcount

        conn.commit()
        conn.close()

        if rows_affected > 0:
            print(f"   ✅ 群消息删除成功")
            return jsonify({"status": "success"})
        else:
            return jsonify({"error": "Message ID not found"}), 404

    except Exception as e:
        print(f"   ❌ 群消息删除失败: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【修正】编辑消息接口 (必须接收 char_id) ---
@app.route("/api/<char_id>/messages/<int:msg_id>", methods=["PUT"])
def edit_message(char_id, msg_id):  # <--- 1. 必须加上 char_id 参数
    # 2. 动态获取该角色的数据库路径
    db_path, _ = get_paths(char_id)

    print(f"--- [Debug] 编辑消息: Char={char_id}, MsgID={msg_id} ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Database not found"}), 404

    new_content = request.json.get("content", "")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # 执行更新
        cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id))
        conn.commit()
        conn.close()

        print(f"   ✅ 编辑保存成功")
        return jsonify({"status": "success", "content": new_content})
    except Exception as e:
        print(f"   ❌ 编辑失败: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】群聊消息编辑接口 ---
@app.route("/api/group/<group_id>/messages/<int:msg_id>", methods=["PUT"])
def edit_group_message(group_id, msg_id):
    # 1. 获取群聊数据库路径
    group_dir = os.path.join(GROUPS_DIR, group_id)
    db_path = os.path.join(group_dir, "chat.db")

    print(f"--- [Debug] 编辑群消息: Group={group_id}, MsgID={msg_id} ---")

    if not os.path.exists(db_path):
        return jsonify({"error": "Group DB not found"}), 404

    new_content = request.json.get("content", "")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id))
        conn.commit()
        conn.close()

        print(f"   ✅ 群消息编辑成功")
        return jsonify({"status": "success", "content": new_content})

    except Exception as e:
        print(f"   ❌ 群消息编辑失败: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】API 设置接口 ---
API_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "api_settings.json")

@app.route("/api/system_config", methods=["GET", "POST"])
def handle_system_config():
    # 在 handle_system_config 函数里

    # 初始化默认配置 (增加了 model_options 字段)
    default_config = {
        "active_route": "gemini",
        "routes": {
            "gemini": {
                "name": "线路一：Gemini 直连",
                "models": {"chat": "gemini-2.5-pro", "gen_persona": "gemini-3-pro-preview", "summary": "gemini-2.5-pro"}
            },
            "relay": {
                "name": "线路二：国内中转",
                "models": {"chat": "gpt-3.5-turbo", "gen_persona": "gpt-3.5-turbo", "summary": "gpt-3.5-turbo"}
            }
        },
        # 【新增】可用的模型列表 (把以前前端写死的搬到这里)
        "model_options": {
            'gemini': [
                'gemini-3-pro-preview',
                'gemini-3-flash-preview',
                'gemini-2.5-pro',
                'gemini-2.5-flash-lite'
            ],
            'relay': [
                'gpt-3.5-turbo',
                'deepseek-ai/DeepSeek-R1',
                'gpt-3.5-turbo-0125',
                'deepseek-ai/DeepSeek-V3',
                'gpt-4o'
            ]
        }
    }

    if request.method == "GET":
        if not os.path.exists(API_CONFIG_FILE):
            return jsonify(default_config)
        try:
            with open(API_CONFIG_FILE, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except:
            return jsonify(default_config)

    if request.method == "POST":
        new_config = request.json
        try:
            with open(API_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(new_config, f, ensure_ascii=False, indent=2)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# 这是在 app.py 文件中的 call_openrouter 函数

# ---------------------- OpenRouter / Compatible API ----------------------

def call_openrouter(messages, char_id="unknown", model_name="google/gemini-2.5-pro"):
    import requests

    # 强制不走系统代理
    no_proxy = {"http": None, "https": None}

    # 【新增】打印日志
    log_full_prompt(f"OpenRouter ({model_name})", messages)

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
        "model": model_name, # 【修改】这里用传入的 model_name
        "messages": messages,
        "temperature": 1,
        "max_tokens": 10240
    }

    print(f"--- [Debug] Calling Compatible API at: {url}")  # 增加一个调试日志
    print(f"--- [Debug] Using model: {payload['model']}")  # 增加一个调试日志

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=100)
        # 打印出服务端的原始报错信息，方便调试
        if r.status_code != 200:
            return f"[ERROR] API call failed with status {r.status_code}: {r.text}"

        result = r.json()

        # --- 【新增】Token 计费记录 (DeepSeek/OpenAI 格式) ---
        if 'usage' in result:
            usage = result['usage']
            record_token_usage(
                char_id,
                model_name,
                usage.get('prompt_tokens', 0),
                usage.get('completion_tokens', 0),
                usage.get('total_tokens', 0)
            )

        # --- 【关键修复】检查 choices 列表是否为空 ---
        if "choices" not in result or len(result["choices"]) == 0:
            print(f"⚠️ [Empty Response] API 返回了空列表。完整响应如下：")
            print(result) # 打印出来看看是为什么
            return "[API无回复] 可能是内容被过滤或服务繁忙。"

        # 一切正常，提取内容
        content = result["choices"][0]["message"]["content"]

        # 记录日志
        log_full_prompt(f"OpenRouter ({model_name})", messages, response_text=content)

        return content
    except Exception as e:
        return f"[ERROR] API request failed: {e}"

# ---------------------- Gemini ----------------------
# 修改 call_gemini 定义
def call_gemini(messages, char_id="unknown", model_name="gemini-2.5-pro"):
    """
    Google 官方直连 (配合 Cloudflare Worker) - 增强版
    """
    import requests
    import json

    # 1. 动态获取 Cloudflare 地址
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
    url = f"{base_url}/v1beta/models/{model_name}:generateContent?key={GEMINI_KEY}"

    # 2. 转换消息格式
    gemini_contents = []
    system_instruction = None
    for msg in messages:
        if msg['role'] == 'system':
            system_instruction = {"parts": [{"text": msg['content']}]}
        else:
            role = 'model' if msg['role'] == 'assistant' else 'user'
            gemini_contents.append({"role": role, "parts": [{"text": msg['content']}]})

    # 3. 构造 Payload (加入关键的安全设置！)
    payload = {
        "contents": gemini_contents,
        "generationConfig": {
            "temperature": 1,
            "maxOutputTokens": 10000
        },
        # 【关键修改】把 4 个维度的审查全部关掉 (BLOCK_NONE)
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }

    if system_instruction:
        payload["systemInstruction"] = system_instruction

    try:
        # 发送请求
        r = requests.post(url, json=payload, timeout=100)

        if r.status_code != 200:
            return f"[Gemini Error {r.status_code}] {r.text}"

        result = r.json()

        # --- 【新增】提取 Token 数据 ---
        # Google 的格式通常叫 usageMetadata
        # --- 【新增】记录 Token ---
        token_usage = result.get('usageMetadata', {})
        if token_usage:
            record_token_usage(
                char_id,
                model_name,
                token_usage.get('promptTokenCount', 0),
                token_usage.get('candidatesTokenCount', 0),
                # 【新增】直接提取 totalTokenCount
                token_usage.get('totalTokenCount', 0)
            )
        # ------------------------

        # 解析回复
        if 'candidates' not in result or not result['candidates']:
            return "[Error] No candidates returned."

        candidate = result['candidates'][0]

        # 尝试获取文本
        text = ""
        if 'content' in candidate and 'parts' in candidate['content']:
            text = candidate['content']['parts'][0]['text']
        else:
            finish_reason = candidate.get('finishReason', 'UNKNOWN')
            text = f"[未生成文本] 原因: {finish_reason}"

        # --- 【修改】调用日志时，把 token_usage 传进去 ---
        log_full_prompt(f"Gemini Interaction ({model_name})", messages, response_text=text, usage=token_usage)

        return text

    except Exception as e:
        log_full_prompt(f"Gemini ERROR ({model_name})", messages, response_text=str(e))
        raise e

def get_model_config(task_type="chat"):
    """
    根据配置文件，获取当前应该用的 路由方式 和 模型名称
    task_type: 'chat' | 'gen_persona' | 'summary'
    """
    if not os.path.exists(API_CONFIG_FILE):
        # 默认兜底
        return "gemini", "gemini-2.5-pro"

    try:
        with open(API_CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        route = config.get("active_route", "gemini")
        models = config.get("routes", {}).get(route, {}).get("models", {})
        model_name = models.get(task_type, "gemini-2.5-pro")

        return route, model_name
    except:
        return "gemini", "gemini-2.5-pro"

@app.route("/api/user/profile_settings", methods=["GET", "POST"])
def user_profile_settings():
    # 读取逻辑
    data = {}
    if os.path.exists(USER_SETTINGS_FILE):
        with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            try: data = json.load(f)
            except: pass

    if request.method == "GET":
        # 注意：为了安全，GET请求不返回密码，或者返回空
        return jsonify({
            "name": data.get("current_user_name", "User"),
            "ai_language": data.get("ai_language", "ja")
            # 不返回 password
        })

    if request.method == "POST":
        data_in = request.json

        # 更新字段
        if "name" in data_in: data["current_user_name"] = data_in["name"]
        if "ai_language" in data_in: data["ai_language"] = data_in["ai_language"]

        # 【新增】更新密码
        if "password" in data_in and data_in["password"]:
            data["password"] = data_in["password"]

        with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})

# --- 【修正版】API：手动触发记忆整理 ---
@app.route("/api/<char_id>/memory/snapshot", methods=["POST"])
def snapshot_memory(char_id):  # <--- 1. 加上 char_id 参数
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    total_new_count = 0
    message_log = []

    try:
        # 凌晨检测逻辑
        if now.hour < 4:
            yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            # <--- 2. 传参给工具函数
            count_y, _ = update_short_memory_for_date(char_id, yesterday_str)
            if count_y > 0:
                total_new_count += count_y
                message_log.append(f"昨天新增 {count_y} 条")

        # 处理今天
        # <--- 3. 传参给工具函数
        count_t, _ = update_short_memory_for_date(char_id, today_str)
        if count_t > 0:
            total_new_count += count_t
            message_log.append(f"今天新增 {count_t} 条")

        if total_new_count > 0:
            return jsonify({
                "status": "success",
                "message": "记忆整理完成: " + "，".join(message_log),
                "count": total_new_count
            })
        else:
            return jsonify({"status": "no_data", "message": "暂时没有新对话需要整理"})

    except Exception as e:
        # 打印详细错误方便调试
        print(f"Snapshot Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】定向重新生成中期记忆 (Day Summary) ---
@app.route("/api/<char_id>/memory/regenerate_medium", methods=["POST"])
def regenerate_medium_memory(char_id):
    target_date = request.json.get("date")
    if not target_date: return jsonify({"error": "日期不能为空"}), 400

    _, prompts_dir = get_paths(char_id)
    short_file = os.path.join(prompts_dir, "6_memory_short.json")
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")

    try:
        # 1. 读取短期记忆作为素材
        if not os.path.exists(short_file): return jsonify({"error": "短期记忆文件不存在"}), 404
        with open(short_file, "r", encoding="utf-8") as f:
            short_data = json.load(f)

        # 兼容格式
        day_data = short_data.get(target_date)
        events = []
        if isinstance(day_data, list): events = day_data
        elif isinstance(day_data, dict): events = day_data.get("events", [])

        if not events:
            return jsonify({"error": f"{target_date} 没有短期记忆素材，无法总结"}), 400

        # 2. 拼接素材
        text_to_summarize = "\n".join([f"[{e['time']}] {e['event']}" for e in events])

        # 3. 调用 AI (使用 medium 模式)
        summary = call_ai_to_summarize(text_to_summarize, "medium", char_id)
        if not summary: return jsonify({"error": "AI 生成失败"}), 500

        # 4. 更新 Medium 文件
        medium_data = {}
        if os.path.exists(medium_file):
            with open(medium_file, "r", encoding="utf-8") as f:
                try: medium_data = json.load(f)
                except: pass

        medium_data[target_date] = summary

        with open(medium_file, "w", encoding="utf-8") as f:
            json.dump(medium_data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "content": summary})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- 【新增】定向重新生成长期记忆 (Week Summary) ---
@app.route("/api/<char_id>/memory/regenerate_long", methods=["POST"])
def regenerate_long_memory(char_id):
    week_key = request.json.get("week_key") # 例如 "2025-12-Week2"
    if not week_key: return jsonify({"error": "Week Key 不能为空"}), 400

    _, prompts_dir = get_paths(char_id)
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")
    long_file = os.path.join(prompts_dir, "4_memory_long.json")

    try:
        # 1. 解析周 Key 对应的日期范围
        # 假设格式: YYYY-MM-WeekN
        # 逻辑：Week1 = 1-7日, Week2 = 8-14日...
        try:
            parts = week_key.split('-Week')
            ym_str = parts[0] # 2025-12
            week_num = int(parts[1])

            year, month = map(int, ym_str.split('-'))

            start_day = (week_num - 1) * 7 + 1
            end_day = min(start_day + 6, 31) # 简单防溢出，实际会有 date 校验

            # 构造这一周的所有日期字符串
            target_dates = []
            for d in range(start_day, end_day + 1):
                try:
                    # 校验日期是否合法
                    current_dt = datetime(year, month, d)
                    target_dates.append(current_dt.strftime("%Y-%m-%d"))
                except ValueError:
                    break # 超出当月天数
        except:
            return jsonify({"error": "Week Key 格式无法解析"}), 400

        # 2. 读取中期记忆作为素材
        if not os.path.exists(medium_file): return jsonify({"error": "中期记忆文件不存在"}), 404
        with open(medium_file, "r", encoding="utf-8") as f:
            medium_data = json.load(f)

        summary_buffer = []
        for d_str in target_dates:
            if d_str in medium_data:
                summary_buffer.append(f"【{d_str}】: {medium_data[d_str]}")

        if not summary_buffer:
            return jsonify({"error": f"该周 ({target_dates[0]}~{target_dates[-1]}) 没有任何中期日记素材"}), 400

        full_text = "\n".join(summary_buffer)

        # 3. 调用 AI (使用 long 模式)
        long_summary = call_ai_to_summarize(full_text, "long", char_id)
        if not long_summary: return jsonify({"error": "AI 生成失败"}), 500

        # 4. 更新 Long 文件
        long_data = {}
        if os.path.exists(long_file):
            with open(long_file, "r", encoding="utf-8") as f:
                try: long_data = json.load(f)
                except: pass

        long_data[week_key] = long_summary

        with open(long_file, "w", encoding="utf-8") as f:
            json.dump(long_data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "content": long_summary})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【新增】群聊增量更新逻辑 ---
def update_group_short_memory(group_id, target_date_str):
    # 1. 路径准备
    group_dir = os.path.join(GROUPS_DIR, group_id)
    db_path = os.path.join(group_dir, "chat.db")
    memory_file = os.path.join(group_dir, "memory_short.json") # 群聊自己的记忆文件

    # 2. 读取群配置 (为了拿群名和成员列表)
    if not os.path.exists(GROUPS_CONFIG_FILE):
        return 0, []

    with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
        groups_config = json.load(f)
        group_info = groups_config.get(group_id, {})

    group_name = group_info.get("name", "Group")
    members = group_info.get("members", [])

    # 3. 读取现有群记忆 (获取 last_id)
    current_data = {}
    if os.path.exists(memory_file):
        with open(memory_file, "r", encoding="utf-8") as f:
            try: current_data = json.load(f)
            except: pass

    day_data = current_data.get(target_date_str, {})
    # 兼容处理：如果是列表转字典
    if isinstance(day_data, list):
        existing_events = day_data
        last_id = 0
    else:
        existing_events = day_data.get("events", [])
        last_id = day_data.get("last_id", 0)

    # 4. 查询群数据库
    if not os.path.exists(db_path): return 0, []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    # 只读取 ID > last_id 的新消息
    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows: return 0, []

    new_max_id = rows[-1][0]

    # 5. 拼接文本 (需要转换 role ID 为名字)
    # 加载名字映射
    id_to_name = {}
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        CHAR_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
        with open(CHAR_CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for k, v in c_conf.items(): id_to_name[k] = v["name"]
    except: pass

    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        # 如果是 user 显示用户，如果是 char_id 显示名字
        name = "ユーザー" if role == "user" else id_to_name.get(role, role)
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 6. 调用 AI 总结
    # 这里我们复用 call_ai_to_summarize，用 "short" 模式提取事件
    # 这里的 char_id 可以随便传一个群成员的，或者传 None，因为 short 模式主要是提取事实
    summary_text = call_ai_to_summarize(chat_log, "group_log", "system")

    if not summary_text: return 0, []

    # 7. 解析 AI 返回结果
    new_events = []
    import re
    for line in summary_text.split('\n'):
        line = line.strip()
        if line:
            match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
            event_time = match_time.group(1) if match_time else datetime.now().strftime("%H:%M")
            event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
            new_events.append({"time": event_time, "event": event_text})

    if not new_events: return 0, []

    # 8. 保存到群聊记忆 (追加模式)
    final_events = existing_events + new_events

    # 如果是重置模式(last_id=0)，且原本有数据，这里可以加去重逻辑(类似单人)，这里暂略，直接追加

    current_data[target_date_str] = {
        "events": final_events,
        "last_id": new_max_id
    }

    with open(memory_file, "w", encoding="utf-8") as f:
        json.dump(current_data, f, ensure_ascii=False, indent=2)

    # ================= 关键修复点 =================
    # 9. 【必须】调用分发函数，传给个人
    if new_events:
        print(f"--- [Sync] 开始同步群聊记忆到个人文件 ---")
        distribute_group_memory(group_id, group_name, members, new_events, target_date_str)
    # ============================================

    return len(new_events), new_events

# --- 【修正】群聊快照接口 (真实实现) ---
@app.route("/api/group/<group_id>/memory/snapshot", methods=["POST"])
def snapshot_group_memory(group_id):
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    total_new = 0
    msg_log = []

    try:
        # 1. 凌晨检测 (补录昨天)
        if now.hour < 4:
            yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            print(f"--- [Group Snapshot] 检查昨天 {yesterday} ---")
            c_y, _ = update_group_short_memory(group_id, yesterday)
            if c_y > 0:
                total_new += c_y
                msg_log.append(f"昨天 {c_y} 条")

        # 2. 处理今天
        print(f"--- [Group Snapshot] 检查今天 {today_str} ---")
        c_t, _ = update_group_short_memory(group_id, today_str)
        if c_t > 0:
            total_new += c_t
            msg_log.append(f"今天 {c_t} 条")

        if total_new > 0:
            return jsonify({
                "status": "success",
                "message": "群聊记忆整理完成 (并已同步给成员): " + "，".join(msg_log),
                "count": total_new
            })
        else:
            return jsonify({"status": "no_data", "message": "暂无新群聊消息"})

    except Exception as e:
        print(f"Group Snapshot Error: {e}")
        return jsonify({"error": str(e)}), 500

# 加在 app.py 的路由区域
@app.route("/api/<char_id>/debug/force_maintenance")
def force_maintenance(char_id):
    scheduled_maintenance() # 手动调用上面那个定时函数
    return jsonify({"status": "triggered", "message": "已手动触发后台维护，请查看服务器控制台日志"})

# --- 【新增】记忆面板页面 ---
@app.route("/memory/<char_id>")
def memory_view(char_id):
    return send_from_directory("templates", "memory.html")

# --- 【修正版】获取 Prompts 数据 ---
@app.route("/api/<char_id>/prompts_data")
def get_prompts_data(char_id):
    data = {}
    files = {
        "base": "1_base_persona.md",
        "relation": "2_relationship.json",
        "long": "4_memory_long.json",
        "medium": "5_memory_medium.json",
        "short": "6_memory_short.json",
        "schedule": "7_schedule.json"
    }

    # 1. 硬核拼接绝对路径，不依赖全局变量，防止出错
    current_dir = os.path.dirname(os.path.abspath(__file__))
    prompts_dir = os.path.join(current_dir, "characters", char_id, "prompts")

    print(f"\n--- [Debug] 正在读取记忆页面数据 ---")
    print(f"   -> 目标文件夹: {prompts_dir}")

    # 2. 检查文件夹是否存在
    if not os.path.exists(prompts_dir):
        print(f"   ❌ 文件夹不存在！请检查路径拼写或是否移动了文件")
        # 这种情况下返回错误信息给前端，方便您在页面上看到
        for key in files:
            data[key] = f"Error: 找不到文件夹 {prompts_dir}"
        return jsonify(data)

    # 3. 读取文件
    for key, filename in files.items():
        path = os.path.join(prompts_dir, filename)
        content = "（文件不存在或为空）"

        # 在 get_prompts_data 函数里

        if os.path.exists(path):
            try:
                # 【修改点】把 utf-8 改为 utf-8-sig
                with open(path, "r", encoding="utf-8-sig") as f:
                    if filename.endswith(".json"):
                        try:
                            content = json.load(f)
                        except Exception as e:
                            print(f"   ⚠️ JSON 解析失败 [{filename}]: {e} -> 读取原文")
                            f.seek(0)
                            content = f.read()
                    else:
                        content = f.read()
            except Exception as e:
                content = f"读取出错: {e}"
        else:
            print(f"   ⚠️ 文件缺失: {filename}")

        data[key] = content

    return jsonify(data)

# --- 【新增】保存 Prompt 文件的接口 ---
@app.route("/api/<char_id>/save_prompt", methods=["POST"])
def save_prompt_file(char_id):
    key = request.json.get("key")
    new_content = request.json.get("content") # 可以是字符串(md)或对象(json)

    # 获取该角色的 Prompt 目录
    _, prompts_dir = get_paths(char_id)

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

    path = os.path.join(prompts_dir, filename)

    try:
        # --- 【核心新增】如果是保存短期记忆，自动校准 last_id ---
        if key == "short" and isinstance(new_content, dict):
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()

            for date_str, day_data in new_content.items():
                # 1. 获取用户编辑后的事件列表
                events = []
                if isinstance(day_data, dict):
                    events = day_data.get("events", [])
                elif isinstance(day_data, list):
                    events = day_data # 兼容旧格式

                # 2. 如果列表被清空了，last_id 直接重置为 0 (全量重读)
                if not events:
                    if isinstance(day_data, dict): day_data['last_id'] = 0
                    else: new_content[date_str] = {"events": [], "last_id": 0}
                    print(f"[{date_str}] 事件被清空，进度重置为 0")
                    continue

                # 3. 如果还有事件，找到【最后一条事件】的时间
                last_event_time = events[-1].get('time', '00:00')

                # 4. 去数据库查这个时间点对应的最后一条消息 ID
                # 构造查询时间：精确到当天的这一分钟的最后一秒
                query_ts = f"{date_str} {last_event_time}:59"

                # 查找 <= 这个时间的最大 ID
                cursor.execute("SELECT MAX(id) FROM messages WHERE timestamp <= ?", (query_ts,))
                res = cursor.fetchone()

                if res and res[0]:
                    calibrated_id = res[0]
                    # 更新 last_id
                    if isinstance(day_data, dict):
                        day_data['last_id'] = calibrated_id
                    else:
                        new_content[date_str] = {"events": events, "last_id": calibrated_id}
                    print(f"[{date_str}] 智能回滚: 锚定时间 {last_event_time} -> 重置 ID 为 {calibrated_id}")
                else:
                    # 查不到 ID (可能时间填错了)，保险起见不改，或者设为0
                    pass

            conn.close()
        # ----------------------------------------------------

        with open(path, "w", encoding="utf-8") as f:
            if filename.endswith(".json") and isinstance(new_content, (dict, list)):
                json.dump(new_content, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(new_content))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= 群聊记忆页面专用接口 =================

# 1. 页面路由
@app.route("/memory/group/<group_id>")
def group_memory_view(group_id):
    return render_template("group_memory.html")

# 2. 获取群聊数据 (配置 + 记忆)
@app.route("/api/group/<group_id>/prompts_data")
def get_group_prompts_data(group_id):
    group_dir = os.path.join(GROUPS_DIR, group_id)
    memory_file = os.path.join(group_dir, "memory_short.json")

    data = {
        "meta": {},   # 群名、头像、成员
        "short": {}   # 群聊记录
    }

    # 读取配置
    if os.path.exists(GROUPS_CONFIG_FILE):
        with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
            all_groups = json.load(f)
            data["meta"] = all_groups.get(group_id, {})

    # 读取群聊记忆
    if os.path.exists(memory_file):
        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                data["short"] = json.load(f)
        except:
            data["short"] = {}

    return jsonify(data)

# --- 【确认/修正】保存群聊记忆接口 ---
@app.route("/api/group/<group_id>/save_memory", methods=["POST"])
def save_group_memory(group_id):
    # 1. 获取路径
    group_dir = os.path.join(GROUPS_DIR, group_id)
    memory_file = os.path.join(group_dir, "memory_short.json")

    # 2. 获取内容
    new_content = request.json.get("content")

    if not os.path.exists(group_dir):
        return jsonify({"error": "Group dir not found"}), 404

    try:
        # 3. 写入文件
        with open(memory_file, "w", encoding="utf-8") as f:
            json.dump(new_content, f, ensure_ascii=False, indent=2)
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Save Group Memory Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# 4. 更新群聊元数据 (头像/名称)
@app.route("/api/group/<group_id>/update_meta", methods=["POST"])
def update_group_meta(group_id):
    if not os.path.exists(GROUPS_CONFIG_FILE):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
            all_groups = json.load(f)

        if group_id not in all_groups:
            return jsonify({"error": "Group not found"}), 404

        data = request.json
        # 更新字段
        if "name" in data: all_groups[group_id]["name"] = data["name"].strip()
        if "avatar" in data: all_groups[group_id]["avatar"] = data["avatar"].strip()

        # 【新增】主动消息开关
        if "active_mode" in data:
            all_groups[group_id]["active_mode"] = bool(data["active_mode"])

        with open(GROUPS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_groups, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【修正版】搜索接口 ---
@app.route("/api/<char_id>/search", methods=["POST"])
def search_messages(char_id):
    keyword = request.json.get("keyword", "").strip()
    if not keyword: return jsonify([])

    # 1. 硬核拼接绝对路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(current_dir, "characters", char_id, "chat.db")

    print(f"\n--- [Debug] 正在搜索: {keyword} ---")
    print(f"   -> 目标数据库: {db_path}")

    if not os.path.exists(db_path):
        print(f"   ❌ 数据库文件不存在！")
        return jsonify([])

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 2. 模糊搜索
        cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE content LIKE ? ORDER BY timestamp DESC", (f"%{keyword}%",))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        print(f"   ✅ 搜索完成，找到 {len(rows)} 条结果")
        return jsonify(rows)

    except Exception as e:
        print(f"   ❌ 数据库查询报错: {e}")
        return jsonify([])

# --- 【新增】群聊搜索接口 ---
@app.route("/api/group/<group_id>/search", methods=["POST"])
def search_group_messages(group_id):
    keyword = request.json.get("keyword", "").strip()
    if not keyword: return jsonify([])

    db_path = os.path.join(GROUPS_DIR, group_id, "chat.db")
    if not os.path.exists(db_path): return jsonify([])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, role, content, timestamp FROM messages WHERE content LIKE ? ORDER BY timestamp DESC", (f"%{keyword}%",))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return jsonify(rows)

# --- 【修正】常用语接口 (改为读取全局配置) ---
@app.route("/api/quick_phrases", methods=["GET", "POST"])
def handle_quick_phrases():
    # 改存到 configs 文件夹，作为全局通用配置
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(BASE_DIR, "configs", "quick_phrases.json")

    # GET: 读取列表
    if request.method == "GET":
        if not os.path.exists(path):
            return jsonify([])
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
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

# --- 【新增】获取单个角色配置 ---
@app.route("/api/<char_id>/config")
def get_char_details(char_id):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")

    if not os.path.exists(CONFIG_FILE):
        return jsonify({})

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        char_info = all_config.get(char_id)
        if char_info:
            # 【新增】定义默认配置字典
            defaults = {
                "emotion": 1,
                "light_sleep": True,
                "deep_sleep": False,
                "ds_start": "23:00",
                "ds_end": "07:00"
            }
            # 将默认值合并进去 (如果 char_info 里没有该字段，就用默认的)
            # 这里的逻辑是：char_info 覆盖 defaults (已有的配置优先)
            final_info = defaults.copy()
            final_info.update(char_info)

            return jsonify(final_info)
        else:
            return jsonify({"error": "Character not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【新增】获取群组详情 (包含成员信息) ---
@app.route("/api/group/<group_id>/config")
def get_group_details(group_id):
    if not os.path.exists(GROUPS_CONFIG_FILE):
        return jsonify({"error": "Config not found"}), 404

    with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
        groups_config = json.load(f)

    group_info = groups_config.get(group_id)
    if not group_info:
        return jsonify({"error": "Group not found"}), 404

    # 还需要读取成员的详细信息(头像/名字)，前端好渲染
    members_details = {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f: # 读取 characters.json
        chars_config = json.load(f)

    for member_id in group_info.get("members", []):
        if member_id in chars_config:
            members_details[member_id] = chars_config[member_id]

    return jsonify({
        "group_info": group_info,
        "members": members_details
    })

# --- 【新增】更新角色元数据 (头像/备注) ---
@app.route("/api/<char_id>/update_meta", methods=["POST"])
def update_char_meta(char_id):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")

    if not os.path.exists(CONFIG_FILE):
        return jsonify({"error": "Config file not found"}), 404

    try:
        # 1. 读取现有配置
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        if char_id not in all_config:
            return jsonify({"error": "Character ID not found"}), 404

        # 2. 更新字段 (只更新前端传过来的字段)
        data = request.json
        new_remark = data.get("remark")
        new_avatar = data.get("avatar")
        new_pinned = data.get("pinned") # <--- 【新增】获取置顶状态

        # 允许改为空字符串，所以用 is not None 判断
        if new_remark is not None:
            all_config[char_id]["remark"] = new_remark.strip()

        if new_avatar is not None:
            all_config[char_id]["avatar"] = new_avatar.strip()

        # 【新增】更新置顶状态 (必须判断是否为 None，因为 False 也是有效值)
        if new_pinned is not None:
            all_config[char_id]["pinned"] = bool(new_pinned)

        # --- 【新增】生理节律状态 ---
        # 情绪 (0-100)
        if data.get("emotion") is not None:
            all_config[char_id]["emotion"] = float(data["emotion"])

        # 浅睡眠 (Bool)
        if data.get("light_sleep") is not None:
            all_config[char_id]["light_sleep"] = bool(data["light_sleep"])

        # 深睡眠 (Bool)
        if data.get("deep_sleep") is not None:
            all_config[char_id]["deep_sleep"] = bool(data["deep_sleep"])

        # 深睡眠自动时间段 (Start, End)
        if data.get("ds_start") is not None:
            all_config[char_id]["ds_start"] = data["ds_start"]
        if data.get("ds_end") is not None:
            all_config[char_id]["ds_end"] = data["ds_end"]

        # 3. 写回文件
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_config, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Update Meta Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】获取角色私有资源 (图片等) ---
# 这样前端就能通过 /char_assets/kunigami/avatar.png 访问图片了
@app.route('/char_assets/<char_id>/<filename>')
def get_char_asset(char_id, filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # 指向 characters/<char_id> 文件夹
    directory = os.path.join(base_dir, "characters", char_id)
    return send_from_directory(directory, filename)

# --- 【新增】上传角色头像 ---
@app.route("/api/<char_id>/upload_avatar", methods=["POST"])
def upload_char_avatar(char_id):
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            # 1. 确定保存路径: characters/<char_id>/
            char_dir = os.path.join(BASE_DIR, "characters", char_id)
            if not os.path.exists(char_dir):
                os.makedirs(char_dir)

            # 2. 统一重命名为 avatar.png (或者保留原扩展名)
            # 为了简单和防止缓存问题，我们建议统一叫 avatar.png，
            # 也可以保留原后缀，这里为了稳妥保留原后缀
            ext = os.path.splitext(file.filename)[1]
            if not ext: ext = ".png"
            filename = f"avatar{ext}"
            file_path = os.path.join(char_dir, filename)

            # 保存文件 (覆盖旧的)
            file.save(file_path)

            # 3. 更新 characters.json 里的路径
            CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                all_config = json.load(f)

            # 生成新的访问 URL
            # 加上时间戳 ?v=... 是为了强制浏览器刷新缓存，立刻看到新头像
            timestamp = int(time.time())
            new_url = f"/char_assets/{char_id}/{filename}?v={timestamp}"

            all_config[char_id]["avatar"] = new_url

            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(all_config, f, ensure_ascii=False, indent=2)

            return jsonify({"status": "success", "url": new_url})

        except Exception as e:
            print(f"Upload Error: {e}")
            return jsonify({"error": str(e)}), 500

# --- 【新增】获取群聊资源 (图片等) ---
@app.route('/group_assets/<group_id>/<filename>')
def get_group_asset(group_id, filename):
    # 指向 groups/<group_id> 文件夹
    directory = os.path.join(GROUPS_DIR, group_id)
    return send_from_directory(directory, filename)

# --- 【新增】上传群聊头像 ---
@app.route("/api/group/<group_id>/upload_avatar", methods=["POST"])
def upload_group_avatar(group_id):
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            # 1. 确定保存路径: groups/<group_id>/
            target_group_dir = os.path.join(GROUPS_DIR, group_id)
            if not os.path.exists(target_group_dir):
                os.makedirs(target_group_dir)

            # 2. 统一重命名为 avatar.png
            filename = "avatar.png"
            file_path = os.path.join(target_group_dir, filename)

            # 保存文件
            file.save(file_path)

            # 3. 更新 groups.json 配置
            if os.path.exists(GROUPS_CONFIG_FILE):
                with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                    groups_config = json.load(f)

                if group_id in groups_config:
                    # 生成新的访问 URL (带时间戳防缓存)
                    timestamp = int(time.time())
                    new_url = f"/group_assets/{group_id}/{filename}?v={timestamp}"

                    groups_config[group_id]["avatar"] = new_url

                    with open(GROUPS_CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(groups_config, f, ensure_ascii=False, indent=2)

                    return jsonify({"status": "success", "url": new_url})
                else:
                    return jsonify({"error": "Group config not found"}), 404
            else:
                return jsonify({"error": "Groups config file missing"}), 500

        except Exception as e:
            print(f"Group Upload Error: {e}")
            return jsonify({"error": str(e)}), 500

# --- 【新增】上传用户全局头像 ---
@app.route("/api/upload_user_avatar", methods=["POST"])
def upload_user_avatar():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        try:
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            # 直接覆盖 static 文件夹下的 avatar_user.png
            save_path = os.path.join(BASE_DIR, "static", "avatar_user.png")

            file.save(save_path)

            # 添加时间戳参数，防止浏览器缓存旧图片
            timestamp = int(time.time())
            new_url = f"/static/avatar_user.png?v={timestamp}"

            return jsonify({"status": "success", "url": new_url})
        except Exception as e:
            print(f"User Avatar Upload Error: {e}")
            return jsonify({"error": str(e)}), 500

# --- 【新增】获取全局配置 (用户人设 & 格式) ---
@app.route("/api/global_config", methods=["GET"])
def get_global_config():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = os.path.join(BASE_DIR, "configs")

    data = {}
    files = {
        "user_persona": "global_user_persona.md",
        "system_format": "global_format.md"
    }

    for key, filename in files.items():
        path = os.path.join(CONFIG_DIR, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data[key] = f.read()
        else:
            data[key] = ""

    return jsonify(data)

# --- 【新增】保存全局配置 ---
@app.route("/api/save_global_config", methods=["POST"])
def save_global_config():
    key = request.json.get("key") # 'user_persona' or 'system_format'
    content = request.json.get("content")

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = os.path.join(BASE_DIR, "configs")

    filename_map = {
        "user_persona": "global_user_persona.md",
        "system_format": "global_format.md"
    }

    filename = filename_map.get(key)
    if not filename:
        return jsonify({"error": "Invalid key"}), 400

    try:
        with open(os.path.join(CONFIG_DIR, filename), "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【新增】创建新角色接口 ---
@app.route("/api/characters/add", methods=["POST"])
def add_character():
    try:
        data = request.json
        new_id = data.get("id", "").strip()
        new_name = data.get("name", "").strip()

        # 1. 基础校验
        if not new_id or not new_name:
            return jsonify({"error": "ID和名称不能为空"}), 400

        # ID 只能是英文、数字、下划线 (作为文件夹名)
        import re
        if not re.match(r'^[a-zA-Z0-9_]+$', new_id):
            return jsonify({"error": "ID 只能包含字母、数字或下划线"}), 400

        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
        CHAR_ROOT = os.path.join(BASE_DIR, "characters")

        # 2. 读取现有配置，检查 ID 是否重复
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        if new_id in all_config:
            return jsonify({"error": "该 ID 已存在"}), 400

        # 3. 创建文件夹结构
        target_char_dir = os.path.join(CHAR_ROOT, new_id)
        target_prompts_dir = os.path.join(target_char_dir, "prompts")

        if not os.path.exists(target_prompts_dir):
            os.makedirs(target_prompts_dir)

        # 4. 初始化数据库 (chat.db)
        # 直接调用我们要有的 init_char_db 函数
        init_char_db(new_id)

        # 5. 创建默认的空 Prompt 文件 (防止进入记忆页面报错)
        # 这些文件是必须存在的
        default_files = [
            "1_base_persona.md",
            "2_relationship.json",
            "3_user_persona.md", # 虽然有全局的，但局部文件最好也占个位
            "4_memory_long.json",
            "5_memory_medium.json",
            "6_memory_short.json",
            "7_schedule.json"
        ]

        for filename in default_files:
            file_path = os.path.join(target_prompts_dir, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                if filename.endswith(".json"):
                    f.write("{}") # JSON 写空对象
                else:
                    f.write("")   # MD 写空字符串

        # 6. 更新配置文件 (characters.json)
        all_config[new_id] = {
            "name": new_name,
            "remark": new_name, # 默认备注同名
            "avatar": "/static/default_avatar.png", # 默认头像
            "pinned": False,

            # --- 新增默认参数 ---
            "emotion": 1,
            "light_sleep": True,
            "deep_sleep": False,
            "ds_start": "23:00",
            "ds_end": "07:00"
        }

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_config, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Add Character Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】创建群聊接口 ---
@app.route("/api/groups/add", methods=["POST"])
def add_group():
    try:
        data = request.json
        new_id = data.get("id", "").strip()
        new_name = data.get("name", "").strip()
        members = data.get("members", []) # list of char_ids

        # 1. 校验
        if not new_id or not new_name:
            return jsonify({"error": "ID和名称不能为空"}), 400
        if len(members) < 2:
            return jsonify({"error": "群聊至少需要2名成员"}), 400

        import re
        if not re.match(r'^[a-zA-Z0-9_]+$', new_id):
            return jsonify({"error": "ID 只能包含字母、数字或下划线"}), 400

        # 2. 读取/初始化配置
        groups_config = {}
        if os.path.exists(GROUPS_CONFIG_FILE):
            with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                groups_config = json.load(f)

        if new_id in groups_config:
            return jsonify({"error": "该群聊ID已存在"}), 400

        # 3. 创建文件夹
        target_group_dir = os.path.join(GROUPS_DIR, new_id)
        if not os.path.exists(target_group_dir):
            os.makedirs(target_group_dir)

        # 4. 初始化群聊数据库
        db_path = os.path.join(target_group_dir, "chat.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # 群聊表结构与单人一致，但 role 字段可能会存具体的 char_id
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

        # 5. 更新配置
        groups_config[new_id] = {
            "name": new_name,
            "avatar": "/static/default_group.png", # 记得在static放个图
            "pinned": False,
            "members": members,
            "active_mode": True  # 【修改】新建群默认开启主动消息
        }

        with open(GROUPS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(groups_config, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Add Group Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】复制他人日程接口 ---
@app.route("/api/<target_char_id>/copy_schedule", methods=["POST"])
def copy_other_schedule(target_char_id):
    source_char_id = request.json.get("source_id")

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    # 1. 获取源路径 和 目标路径
    source_path = os.path.join(BASE_DIR, "characters", source_char_id, "prompts", "7_schedule.json")
    _, target_prompts_dir = get_paths(target_char_id)
    target_path = os.path.join(target_prompts_dir, "7_schedule.json")

    if not os.path.exists(source_path):
        return jsonify({"error": "源角色的日程文件不存在"}), 404

    try:
        # 2. 读取源文件
        with open(source_path, "r", encoding="utf-8-sig") as f:
            source_data = json.load(f)

        # 3. 写入目标文件 (覆盖)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(source_data, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "data": source_data})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 【新增】将日程批量分发给其他角色 ---
@app.route("/api/distribute_schedule", methods=["POST"])
def distribute_schedule():
    data = request.json
    target_ids = data.get("target_ids", []) # 目标角色ID列表
    schedule_content = data.get("content", {}) # 日程内容 (JSON对象)

    if not target_ids or not schedule_content:
        return jsonify({"error": "没有选择目标或日程为空"}), 400

    success_list = []
    error_list = []

    for char_id in target_ids:
        try:
            # 获取该角色的路径
            _, prompts_dir = get_paths(char_id)
            target_file = os.path.join(prompts_dir, "7_schedule.json")

            # 确保目录存在
            if not os.path.exists(prompts_dir):
                os.makedirs(prompts_dir)

            # 覆盖写入 (使用 utf-8-sig 防止编码问题)
            with open(target_file, "w", encoding="utf-8-sig") as f:
                json.dump(schedule_content, f, ensure_ascii=False, indent=2)

            success_list.append(char_id)

        except Exception as e:
            error_list.append(f"{char_id}: {str(e)}")

    return jsonify({
        "status": "success",
        "updated": success_list,
        "errors": error_list
    })

# --- 【新增】AI 自动生成人设接口 ---
@app.route("/api/generate_persona", methods=["POST"])
def generate_persona():
    data = request.json
    char_name = data.get("char_name")
    source_ip = data.get("source_ip")

    lang = get_ai_language()

    # 日语 Prompt
    prompt_ja = """
    あなたは熟練したキャラクター設定作家です。
    ユーザーから提供された「キャラクター名」と「作品名(IP)」に基づいて、以下の厳格なフォーマットに従ってキャラクター設定を作成してください。
    
    # 要件
    1. 言語：日本語
    2. 情報源：原作の公式設定やストーリーに基づき、正確かつ詳細に記述すること。
    3. 創作：もし情報が不足している部分は、キャラクターの性格に矛盾しない範囲で補完すること。
    4. フォーマット：以下の構造を厳守すること。
    
    # 出力フォーマット例
    # 役割
    (名前) (年齢/身長/誕生日)
    
    # 外見
    - 髪・瞳：(詳細な描写)
    - (その他の身体的特徴)
    
    # 経歴（年表）
    - (幼少期、学生時代、現在に至るまでの重要な出来事)
    
    # 生活状況
    - 拠点：(現在の住居や所属)
    - (寮や部屋割りなどの詳細があれば記述)
    - もしそのキャラクターがブルーロックの登場人物である場合：
    - 寮（ベッド順）：
        - ①潔世一(11)、千切豹馬(4)、御影玲王(14)、**國神錬介(50)**(現在のキャラクターをこのように示す)
        - ②烏旅人(6)、乙夜影汰(19)、雪宮剣優(5)、冰織羊(16)
        - ③黒名蘭世(96)、清羅刃(69)、雷市陣吾(22)、五十嵐栗夢(108)
        - ④糸師凛(9)、蜂楽廻(8)、七星虹郎(17)、（空）
        - ⑤我牙丸吟(1)、時光青志(20)、蟻生十兵衛(3)、（空）
        - ⑥オリーウェ・エゴ(2)、閃堂秋人(18)、士道龍聖(111)、（空）
        - ⑦馬狼照英(13)、凪誠士郎(7)、二子一揮(25)、剣城斬鉄(15)
    - 寮配置：①②③④/⑦⑥○⑤（①真正面は⑦）
    
    # 人間関係
    - (家族、友人、ライバル、敵対関係など)
    
    # 性格（キーワード）
    - 表面：(他人に見せる態度)
    - 内面：(隠された本音、デレ要素、執着など)
    - 特徴：
    - 弱点：
    
    # 好きなこと・詳細
    - 代表色：
    - 動物：
    - 好きな食べ物：
    - 苦手な食べ物：
    - 趣味：
    - 好きな季節/科目/座右の銘など：
    - 自認する長所/短所：
    - 嬉しいこと/悲しいこと：
    """

    # 中文 Prompt (结构一致，语言不同)
    prompt_zh = """
    你是一位资深的角色设定师。
    请根据用户提供的“角色名”和“作品名(IP)”，严格按照以下格式撰写角色设定。

    # 要求
    1. 语言：中文
    2. 信息源：基于原作官方设定，准确详细。
    3. 格式：严格遵守以下结构。

    # 输出格式示例
    # 角色
    (姓名) (年龄/身高/生日)

    # 外貌
    - 发型瞳色：(详细描写)
    - (其他特征)

    # 经历 (年表)
    - (重要生平事件)

    # 生活状况
    - 据点：
    - (宿舍/房间等细节)
    - 如果是蓝色监狱的角色：
    - 寝室（床位顺序）：
        - ①洁世一(11)、千切豹马(4)、御影玲王(14)、**国神炼介(50)**(当前角色像这样标出)
        - ②乌旅人(6)、乙夜影汰(19)、雪宫剑优(5)、冰织羊(16)
        - ③黑名兰世(96)、清罗刃(69)、雷市阵吾(22)、五十岚栗梦(108)
        - ④糸师凛(9)、蜂乐廻(8)、七星虹郎(17)、（空）
        - ⑤我牙丸吟(1)、时光青志(20)、蚁生十兵卫(3)、（空）
        - ⑥奥利维·埃戈(2)、闪堂秋人(18)、士道龙圣(111)、（空）
        - ⑦马狼照英(13)、凪诚士郎(7)、二子一挥(25)、剑城斩铁(15)
    - 寝室配置：①②③④/⑦⑥○⑤（①正对面是⑦）

    # 人际关系
    - (家族、朋友、宿敌等)

    # 性格 (关键词)
    - 表面：
    - 内心：
    - 特征：
    - 弱点：

    # 喜好与细节
    - 代表色：
    - 喜欢的食物：
    - 讨厌的食物：
    - 兴趣：
    - 特长/弱项：
    - 座右铭：
    """

    system_prompt = prompt_zh if lang == "zh" else prompt_ja

    if not char_name or not source_ip:
        return jsonify({"error": "请输入角色名和作品名"}), 400

    # 构造请求
    user_content = f"キャラクター名: {char_name}\n作品名: {source_ip}"

    # 这里的 PERSONA_GENERATION_PROMPT 就是上面定义的那一大段字符串
    # 请务必把它定义在文件顶部或这个函数外面
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    try:
        print(f"--- [Gen Persona] Generating for {char_name} ({source_ip}) ---")

        # 定义一个特殊的记账 ID
        log_id = f"System:GenPersona({char_name})"

        # 1. 获取当前配置
        route, current_model = get_model_config("gen_persona") # 任务类型是 chat

        print(f"--- [Dispatch] Route: {route}, Model: {current_model} ---")

        if route == "relay":
            generated_text = call_openrouter(messages, char_id=log_id, model_name=current_model)
        else:
            generated_text = call_gemini(messages, char_id=log_id, model_name=current_model)

        return jsonify({"status": "success", "content": generated_text})

    except Exception as e:
        print(f"Gen Persona Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】删除角色接口 ---
@app.route("/api/character/<char_id>/delete", methods=["DELETE"])
def delete_character_api(char_id):
    # 1. 读取配置
    if not os.path.exists(CONFIG_FILE):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        if char_id not in all_config:
            return jsonify({"error": "Character not found"}), 404

        # 2. 从配置中移除
        del all_config[char_id]

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_config, f, ensure_ascii=False, indent=2)

        # 3. 物理删除文件夹
        char_dir = os.path.join(CHARACTERS_DIR, char_id)
        if os.path.exists(char_dir):
            shutil.rmtree(char_dir) # 递归删除文件夹

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Delete Character Error: {e}")
        return jsonify({"error": str(e)}), 500


# --- 【新增】删除群聊接口 ---
@app.route("/api/group/<group_id>/delete", methods=["DELETE"])
def delete_group_api(group_id):
    if not os.path.exists(GROUPS_CONFIG_FILE):
        return jsonify({"error": "Config not found"}), 404

    try:
        with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
            groups_config = json.load(f)

        if group_id not in groups_config:
            return jsonify({"error": "Group not found"}), 404

        # 2. 从配置中移除
        del groups_config[group_id]

        with open(GROUPS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(groups_config, f, ensure_ascii=False, indent=2)

        # 3. 物理删除文件夹
        group_dir = os.path.join(GROUPS_DIR, group_id)
        if os.path.exists(group_dir):
            shutil.rmtree(group_dir)

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Delete Group Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【新增】指定日期重新生成短期记忆 ---
@app.route("/api/<char_id>/memory/regenerate_short", methods=["POST"])
def regenerate_short_memory_api(char_id):
    data = request.json
    target_date = data.get("date")
    force = data.get("force", False) # 是否强制重读

    if not target_date:
        return jsonify({"error": "日期不能为空"}), 400

    try:
        count, events = update_short_memory_for_date(char_id, target_date, force_reset=force)

        # 为了前端方便，返回最新的完整数据（因为update函数只返回了新增的）
        # 我们重新读一次文件返回给前端刷新
        _, prompts_dir = get_paths(char_id)
        short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")
        with open(short_mem_path, "r", encoding="utf-8") as f:
            full_data = json.load(f)
            day_data = full_data.get(target_date, {})
            # 统一返回 dict 格式
            if isinstance(day_data, list): day_data = {"events": day_data, "last_id": 0}

        return jsonify({
            "status": "success",
            "added_count": count,
            "data": day_data
        })

    except Exception as e:
        print(f"Regen Short Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 定时任务配置 ---
def scheduled_maintenance():
    """
    每天凌晨 04:00 运行一次
    顺序：群聊总结(分发) -> 个人总结(日记) -> 周结(若周一)
    """
    print("\n⏰ 正在执行每日后台维护...")

    # 【修改】在函数内部导入，避免循环引用
    import memory_jobs

    # 1. 【新增】先执行群聊日结 (把记忆分发给个人)
    memory_jobs.run_all_group_daily_rollovers()

    # 1. 执行全员日结
    # memory_jobs.process_daily_rollover()  <-- 旧的删掉
    memory_jobs.run_all_daily_rollovers()   # <-- 换成新的循环函数

    # 2. 如果今天是周一，执行全员周结
    if datetime.now().weekday() == 0:
        memory_jobs.run_all_weekly_rollovers() # <-- 换成新的循环函数

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

# --- 【终极版】日志记录：内容 + Token 账单 ---
def log_full_prompt(service_name, messages, response_text=None, usage=None):
    # 获取当前时间
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构造日志内容
    log_content = []
    log_content.append(f"\n{'='*20} [{timestamp}] {service_name} {'='*20}")

    # 1. 打印上下文
    for i, msg in enumerate(messages):
        role = msg.get('role', 'unknown').upper()
        content = msg.get('content', '')
        log_content.append(f"【{i}】<{role}>:\n{content}\n{'-'*30}")

    # 2. 打印 AI 回复
    if response_text:
        log_content.append(f"\n【🤖 AI REPLY】:\n{response_text}")

    # 3. 【新增】打印 Token 消耗 (这就是您要的！)
    if usage:
        # Gemini 的格式通常是: {'promptTokenCount': 100, 'candidatesTokenCount': 20, 'totalTokenCount': 120}
        input_tokens = usage.get('promptTokenCount', 0)      # 提问消耗 (便宜)
        output_tokens = usage.get('candidatesTokenCount', 0) # 回复消耗 (贵)
        total_tokens = usage.get('totalTokenCount', 0)       # 总计

        log_content.append(f"\n【💰 TOKEN BILL】:")
        log_content.append(f"   📥 输入(Prompt): {input_tokens}")
        log_content.append(f"   📤 输出(Reply):  {output_tokens}")
        log_content.append(f"   💎 总计(Total):  {total_tokens}")

    log_content.append(f"{'='*50}\n")

    final_log = "\n".join(log_content)

    # 打印到黑框框
    print(final_log)

    # 保存到文件
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        if not os.path.exists(log_dir): os.makedirs(log_dir)
        log_file = os.path.join(log_dir, "api.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(final_log)
    except: pass

# --- 【新增】Token 账单记录系统 ---
USAGE_LOG_FILE = "logs/usage_history.json"

def record_token_usage(char_id, model, input_tokens, output_tokens, total_tokens):
    """记录一次 API 调用的消耗"""
    try:
        # 1. 确保日志目录存在
        log_dir = os.path.dirname(USAGE_LOG_FILE)
        if not os.path.exists(log_dir): os.makedirs(log_dir)

        # 2. 读取现有日志
        logs = []
        if os.path.exists(USAGE_LOG_FILE):
            with open(USAGE_LOG_FILE, "r", encoding="utf-8") as f:
                try: logs = json.load(f)
                except: logs = []

        # 3. 追加新记录
        new_entry = {
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "char_id": char_id,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens
        }
        logs.append(new_entry)

        # 4. 只保留最近 50 条 (防止文件无限膨胀)
        if len(logs) > 50:
            logs = logs[-50:]

        # 5. 保存
        with open(USAGE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"Log Usage Error: {e}")

# --- 【新增】获取账单接口 ---
@app.route("/api/usage_logs")
def get_usage_logs():
    if not os.path.exists(USAGE_LOG_FILE):
        return jsonify([])
    try:
        with open(USAGE_LOG_FILE, "r", encoding="utf-8") as f:
            # 倒序返回，最新的在前面
            logs = json.load(f)
            return jsonify(logs[::-1])
    except:
        return jsonify([])

# --- 【修正版】单人主动消息 (伪装成 User 消息触发) ---
def trigger_active_chat(char_id):
    print(f"💓 [Active] 尝试触发 {char_id} 的主动消息...")

    db_path, _ = get_paths(char_id)
    if not os.path.exists(db_path): return False

    # 1. 获取基础 System Prompt (只包含人设、记忆，不包含主动指令)
    base_system_prompt = build_system_prompt(char_id)
    messages = [{"role": "system", "content": base_system_prompt}]

    # 2. 读取历史记录
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
    history_rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    # 3. 填充历史 (带智能时间戳)
    now = datetime.now()
    show_full_date = False
    if history_rows:
        try:
            first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
            if first_ts.date() != now.date(): show_full_date = True
        except: pass

    for row in history_rows:
        try:
            dt_object = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
            ts_str = dt_object.strftime('[%m-%d %H:%M]') if show_full_date else dt_object.strftime('[%H:%M]')
            formatted_content = f"{ts_str} {row['content']}"
            messages.append({"role": row['role'], "content": formatted_content})
        except:
            messages.append({"role": row['role'], "content": row['content']})

    # --- 4. 【关键修改】构造“伪造的”用户指令消息 ---
    # 这条消息只发给 AI 看，不会存入数据库

    lang = get_ai_language()
    hour = now.hour
    time_str = now.strftime('%H:%M')

    # 计算时间段
    if 5 <= hour < 11: period = "早上" if lang == "zh" else "朝"
    elif 11 <= hour < 13: period = "中午" if lang == "zh" else "昼"
    elif 13 <= hour < 18: period = "下午" if lang == "zh" else "午後"
    elif 18 <= hour < 23: period = "晚上" if lang == "zh" else "夜"
    else: period = "深夜" if lang == "zh" else "深夜"

    if lang == "zh":
        trigger_msg = (
            f"（系统提示：现在是{period} {time_str}。）\n"
            f"（用户已经很久没说话了。请你根据当前时间、之前的聊天内容，**主动**向用户发起一个新的话题。）\n"
            f"（要求：自然、简短，不要重复上一句话。）"
        )
    else:
        trigger_msg = (
            f"（システム通知：現在は{period} {time_str}です。）\n"
            f"（ユーザーからの返信が途絶えています。現在の時間帯やこれまでの会話を踏まえて、**自発的に**新しい話題を振ってください。）\n"
            f"（要件：自然で簡潔に。直前の発言を繰り返さないこと。）"
        )

    # 把它伪装成 User 发的消息
    messages.append({"role": "user", "content": trigger_msg})

    # 5. 调用 AI
    try:
        route, current_model = get_model_config("chat")
        print(f"   -> [Active] Calling AI ({route}/{current_model})...")

        if route == "relay":
            reply_text = call_openrouter(messages, char_id=char_id, model_name=current_model)
        else:
            reply_text = call_gemini(messages, char_id=char_id, model_name=current_model)

        # 清理
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()

        if not cleaned_reply: return False

        # 6. 存库
        ai_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       ("assistant", cleaned_reply, ai_ts))
        conn.commit()
        conn.close()

        print(f"💓 [Active] 发送成功: {cleaned_reply}")
        return True

    except Exception as e:
        print(f"💓 [Active] 发送失败: {e}")
        return False

# --- 【修正版】群聊主动消息 (伪装成 User 指令) ---
def trigger_group_active_chat(group_id):
    print(f"💓 [GroupActive] 尝试触发群 {group_id} 的主动消息...")

    group_dir = os.path.join(GROUPS_DIR, group_id)
    db_path = os.path.join(group_dir, "chat.db")

    # 1. 基础读取逻辑 (保持不变)
    if not os.path.exists(GROUPS_CONFIG_FILE): return False
    with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
        group_conf = json.load(f).get(group_id, {})

    group_name = group_conf.get("name", "Group")
    all_members = group_conf.get("members", [])
    ai_members_all = [m for m in all_members if m != "user"]
    if not ai_members_all: return False

    # 2. 筛选在线成员 (保持不变)
    online_members = []
    id_to_name = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for cid, cinfo in c_conf.items():
                id_to_name[cid] = cinfo.get("name", cid)
                if cid in ai_members_all:
                    if not cinfo.get("deep_sleep", False):
                        online_members.append(cid)

    if not online_members: return False

    # 3. 随机抽取 1 人
    speaker_id = random.choice(online_members)
    speaker_name = id_to_name.get(speaker_id, speaker_id)
    print(f"   -> 选中 [{speaker_name}] 发起话题")

    # 4. 构建 Prompt (System只放人设)
    sys_prompt = build_system_prompt(speaker_id)
    other_members = [m for m in all_members if m != speaker_id and m != "user"]
    rel_prompt = build_group_relationship_prompt(speaker_id, other_members)

    full_sys_prompt = sys_prompt + "\n\n" + rel_prompt + "\n【Current Situation】\n当前是在群聊中。"
    messages = [{"role": "system", "content": full_sys_prompt}]

    # 5. 读取历史 (保持不变)
    conn = sqlite3.connect(db_path)
    # 【关键修复】↓↓↓ 加上这一行！没有它，数据库读出来的就是乱码 ↓↓↓
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 10")
    rows = [dict(row) for row in cursor.fetchall()][::-1]
    conn.close()

    for row in rows:
        r_id = row['role']
        d_name = "User" if r_id == "user" else id_to_name.get(r_id, r_id)
        messages.append({"role": "user", "content": f"[{d_name}]: {row['content']}"})

    # --- 6. 【关键修改】构造伪造的指令消息 ---
    time_str = datetime.now().strftime('%H:%M')
    lang = get_ai_language()

    if lang == "zh":
        trigger_msg = (
            f"[System]: (现在是 {time_str}。群里很久没人说话了。)\n"
            f"(请你主动发起一个话题，或者对之前的话题进行延伸。)\n"
            f"(要求：自然、简短。)"
        )
    else:
        trigger_msg = (
            f"[System]: (現在は {time_str} です。グループチャットが静かです。)\n"
            f"(自発的に新しい話題を振ってください。)\n"
            f"(要件：自然で簡潔に。)"
        )

    # 这里的 role 是 user，模拟群里发了一条系统通知
    messages.append({"role": "user", "content": trigger_msg})

    # 7. 调用 AI (保持不变)
    try:
        route, current_model = get_model_config("chat")

        if route == "relay":
            reply_text = call_openrouter(messages, char_id=speaker_id, model_name=current_model)
        else:
            reply_text = call_gemini(messages, char_id=speaker_id, model_name=current_model)

        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()
        name_pattern = f"^\\[{speaker_name}\\][:：]\\s*"
        cleaned_reply = re.sub(name_pattern, '', cleaned_reply).strip()

        if not cleaned_reply: return False

        # 8. 存档
        ai_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                       (speaker_id, cleaned_reply, ai_ts))
        conn.commit()
        conn.close()

        log_event = f"[群聊: {group_name}] (主动) {cleaned_reply}"
        update_group_log(speaker_id, log_event, ai_ts)

        print(f"💓 [GroupActive] 发送成功: {cleaned_reply}")
        return True

    except Exception as e:
        print(f"Group Active Error: {e}")
        return False

# ---------------------- 启动 ----------------------

if __name__ == "__main__":
    init_db()
    # --- 【新增】启动后台定时任务 ---
    scheduler = BackgroundScheduler()
    # 每天凌晨 4 点 0 分自动运行 (这个时候您肯定睡了，适合整理记忆)
    scheduler.add_job(func=scheduled_maintenance, trigger="cron", hour=4, minute=0)
    # 2. 【新增】每分钟检查一次睡眠状态
    scheduler.add_job(func=memory_jobs.check_and_update_sleep_status, trigger="cron", minute='*')
    # 在 app.py 的 scheduler 启动部分
    # 3. 【新增】主动消息心跳 (每 10 分钟)
    scheduler.add_job(func=memory_jobs.run_active_messaging_check, trigger="cron", minute='*/10')
    scheduler.start()
    print("--- [Scheduler] 后台记忆整理服务已启动 (每天 04:00) ---")

    # 【关键修改】加上 use_reloader=False
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
