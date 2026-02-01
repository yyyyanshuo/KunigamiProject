import os
import time
import re
import json
import sqlite3 # 导入 sqlite3 库
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, render_template # <--- 加上这个
from dotenv import load_dotenv
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler # 新增
import memory_jobs # 导入刚才那个模块
import shutil # 如果以后需要创建新角色用

# 这是在 app.py 文件的开头部分

load_dotenv()  # 从 .env 读取环境变量

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
# 新增下面这行，来读取我们配置的 API 地址
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

app = Flask(__name__, static_folder='static', template_folder='templates')

# 配置项
MAX_CONTEXT_LINES = 10
MODEL_NAME = "gemini-2.5-pro"
# MODEL_NAME = "gemini-3-pro-preview"gemini-3-flash-preview gemini-2.5-pro gemini-2.5-flash-lite

DATABASE_FILE = "chat_history.db"

# 当前对话的用户名字 (用于读取关系 JSON)
CURRENT_USER_NAME = "篠原桐奈"

# 定义基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHARACTERS_DIR = os.path.join(BASE_DIR, "characters")
CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
# 【新增】群聊配置路径
GROUPS_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "groups.json")
GROUPS_DIR = os.path.join(BASE_DIR, "groups")

PERSONA_GENERATION_PROMPT = """
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
如果是蓝色监狱的角色：
- 寮（ベッド順）：
    - ①潔世一(11)、千切豹馬(4)、御影玲王(14)、**國神錬介(50)**
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

# --- 【修正版】AI 总结专用函数 (第一人称 + 纯净事实版) ---
def call_ai_to_summarize(text_content, prompt_type="short", char_id="kunigami"):
    if not text_content:
        return None

    # 获取角色名字 (用于辅助定位，虽说是第一人称，但AI知道自己是谁更好)
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
    char_name = "私" # 默认自称

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            if char_id in chars_config:
                char_name = chars_config[char_id]["name"]
    except: pass

    system_instruction = ""

    # 1. 短期记忆 (保持时间点列表，强调第一人称事实)
    if prompt_type == "short":
        system_instruction = (
            f"あなたは{char_name}本人として、自身の記憶を整理しています。"
            "以下の会話ログから、重要な出来事を抽出してください。"
            "感情的な感想は不要です。「誰と何をしたか」「何が起きたか」という事実のみを簡潔に記録してください。"
            "出力フォーマット：\n- [HH:MM] (自分または相手の行動・会話の要点)"
        )

    # 2. 【新增】群聊记录模式 (纯客观、上帝视角)
    elif prompt_type == "group_log":
        system_instruction = (
            "あなたはグループチャットの書記係（第三者）です。"
            "以下の会話ログから、重要なトピックや出来事を**客観的に**抽出してください。"
            "**要件**：\n"
            "1. 特定のキャラクターの視点（私/俺）を使わないでください。\n"
            "2. 「[名前]が〜と言った」「全員で〜に行くことになった」のように、主語を明確にしてください。\n"
            "3. 感情的な装飾は省き、事実のみを記録してください。\n"
            "出力フォーマット：\n- [HH:MM] 出来事の内容"
        )

    # 2. 中期记忆 (日结) - 【修改】去时间戳，变段落
    elif prompt_type == "medium":
        system_instruction = (
            f"あなたは{char_name}本人です。この一日の出来事を振り返り、**一つの繋がった文章（段落形式）**で要約してください。"
            "**要件**：\n"
            "1. **時間表記（[HH:MM]など）は一切含めないでください**。\n"
            "2. 箇条書きは禁止です。\n"
            "3. **一人称視点**（俺/私）で、起きた事実のみを淡々と記述してください（感情的なポエムは不可）。\n"
            "4. ユーザーとの会話や活動内容を中心に、300文字以内でまとめてください。"
        )

    # 3. 长期记忆 (周结) - 【修改】去时间戳，变段落
    elif prompt_type == "long":
        system_instruction = (
            f"あなたは{char_name}本人です。この一週間の記録を振り返り、全体的な流れを要約してください。"
            "**要件**：\n"
            "1. **具体的な日時や時間表記は不要**です。\n"
            "2. 箇条書きは禁止です。**一つのまとまった文章**にしてください。\n"
            "3. ユーザーとの関係性の変化や、重要な出来事の因果関係を一人称で客観的に記述してください。\n"
            "4. 200文字程度。"
        )

    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"記憶ログ：\n{text_content}"}
    ]

    print(f"--- 正在进行记忆总结 ({prompt_type}) [第一人称事实模式] ---")

    if USE_OPENROUTER:
        return call_openrouter(messages, char_id=char_id)
    else:
        return call_gemini(messages, char_id=char_id)

# --- 【修正版】核心逻辑：增量更新 (支持多角色) ---
def update_short_memory_for_date(char_id, target_date_str):
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

    if isinstance(day_data, list):
        existing_events = day_data
        last_id = 0
    elif isinstance(day_data, dict):
        existing_events = day_data.get("events", [])
        last_id = day_data.get("last_id", 0)

    # 3. 查询数据库 (连接动态 DB)
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return 0, []

    conn = sqlite3.connect(db_path) # <--- 使用动态 db_path
    cursor = conn.cursor()

    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return 0, []

    new_max_id = rows[-1][0]

    # 4. 拼接文本
    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        # 这里的称呼也可以根据 char_id 优化，但暂时用通用的
        name = "ユーザー" if role == "user" else "私"
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 5. 调用 AI 总结
    try:
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

        # 合并逻辑 (带 last_id 判断)
        all_events = []
        if last_id > 0:
            all_events = existing_events + new_events_raw
        else:
            all_events = new_events_raw

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

    # --- A. 处理单人角色 ---
    for char_id, info in chars_config.items():
        db_path = os.path.join(BASE_DIR, "characters", char_id, "chat.db")

        last_msg = ""
        last_time = ""
        timestamp_val = 0

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
            "pinned": info.get("pinned", False)
        })

    # --- B. 处理群聊 (新增部分) ---
    if os.path.exists(GROUPS_CONFIG_FILE):
        try:
            with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                groups_config = json.load(f)

            for group_id, info in groups_config.items():
                # 群聊数据库路径
                db_path = os.path.join(GROUPS_DIR, group_id, "chat.db")

                last_msg = ""
                last_time = ""
                timestamp_val = 0

                if os.path.exists(db_path):
                    try:
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()
                        cursor.execute("SELECT content, timestamp FROM messages ORDER BY id DESC LIMIT 1")
                        row = cursor.fetchone()
                        conn.close()
                        if row:
                            # 群聊消息可能需要显示是谁发的，这里暂时只取内容
                            last_msg = row[0]
                            timestamp_val = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S').timestamp()
                            dt = datetime.fromtimestamp(timestamp_val)
                            if dt.date() == datetime.now().date():
                                last_time = dt.strftime('%H:%M')
                            else:
                                last_time = dt.strftime('%m-%d')
                    except: pass

                contact_list.append({
                    "type": "group", # 标记类型
                    "id": group_id,
                    "avatar": info.get("avatar", "/static/default_group.png"), # 需要准备个群聊默认头像
                    "name": info.get("name"),
                    "remark": info.get("name"), # 群聊一般就叫群名
                    "last_msg": last_msg,
                    "last_time": last_time,
                    "timestamp": timestamp_val,
                    "pinned": info.get("pinned", False),
                    "members": info.get("members", [])
                })
        except Exception as e:
            print(f"Error loading groups: {e}")

    # 4. 统一排序
    contact_list.sort(key=lambda x: (1 if x['pinned'] else 0, x['timestamp']), reverse=True)

    return jsonify(contact_list)

# --- 【修改】历史记录接口 (支持定位) ---
# 请找到原来的 get_history 函数，完全替换为下面这个：
# --- 【修改】历史记录接口 (支持多角色 + 定位) ---
@app.route("/api/<char_id>/history", methods=["GET"])
def get_history(char_id):
    limit = request.args.get('limit', 20, type=int)
    page = request.args.get('page', 1, type=int)
    # 新增参数：target_id
    target_id = request.args.get('target_id', type=int)

    # 1. 【修正】正确的数据库路径构建
    # 使用 os.path.join 且文件名必须是 chat.db
    db_path = os.path.join(BASE_DIR, "characters", char_id, "chat.db")

    # 2. 防御性检查：如果数据库不存在，先初始化
    if not os.path.exists(db_path):
        init_char_db(char_id)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 默认 offset
    offset = (page - 1) * limit

    # --- 核心：定位逻辑 ---
    if target_id:
        # A. 获取目标消息的时间戳
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']

            # B. 计算有多少条消息比它“新”
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]

            # C. 【优化】计算目标消息所在的“标准页码”
            # 例如：limit=20，前面有 25 条，则它在第 2 页 (20-40条)
            # 算法：(前面数量 // 每页数量) + 1
            page = (count_newer // limit) + 1

            # D. 【优化】根据标准页码反推标准 Offset
            # 这样保证加载的是整页数据，不会导致分页错位
            offset = (page - 1) * limit

    # 常规查询
    cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset))
    messages = [dict(row) for row in cursor.fetchall()][::-1]

    cursor.execute("SELECT COUNT(id) FROM messages")
    total_messages = cursor.fetchone()[0]
    conn.close()

    return jsonify({
        "messages": messages,
        "total": total_messages,
        "page": page  # 返回修正后的页码给前端
    })

# --- 【新增】群聊历史记录接口 ---
@app.route("/api/group/<group_id>/history", methods=["GET"])
def get_group_history(group_id):
    # 逻辑与单人 get_history 几乎一样，只是数据库路径不同
    db_path = os.path.join(GROUPS_DIR, group_id, "chat.db")

    if not os.path.exists(db_path):
        return jsonify({"messages": [], "total": 0, "page": 1})

    limit = request.args.get('limit', 20, type=int)
    page = request.args.get('page', 1, type=int)
    target_id = request.args.get('target_id', type=int)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    offset = (page - 1) * limit

    if target_id:
        cursor.execute("SELECT timestamp FROM messages WHERE id = ?", (target_id,))
        res = cursor.fetchone()
        if res:
            target_ts = res['timestamp']
            cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (target_ts,))
            count_newer = cursor.fetchone()[0]
            page = (count_newer // limit) + 1
            offset = (page - 1) * limit

    cursor.execute("SELECT id, role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset))
    messages = [dict(row) for row in cursor.fetchall()][::-1]

    cursor.execute("SELECT COUNT(id) FROM messages")
    total = cursor.fetchone()[0]
    conn.close()

    return jsonify({"messages": messages, "total": total, "page": page})

# 这是在 app.py 文件中

# ---------------------- 核心聊天接口 (时间感知注入版) ----------------------
# --- 核心聊天接口 (多角色适配 + 返回ID修正版) ---
@app.route("/api/<char_id>/chat", methods=["POST"])
def chat(char_id):
    # 1. 动态获取路径
    db_path, prompts_dir = get_paths(char_id)

    # 2. 防御性初始化
    if not os.path.exists(db_path):
        init_char_db(char_id)

    # 数据准备
    data = request.json or {}
    user_msg_raw = data.get("message", "").strip()
    if not user_msg_raw:
        return jsonify({"error": "empty message"}), 400

    # 3. 动态构建 Prompt
    system_prompt = build_system_prompt(char_id)

    # 4. 构建消息历史 (读取最近20条)
    messages = [{"role": "system", "content": system_prompt}]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
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

    # --- Part 3: 添加当前用户消息 ---
    if show_full_date:
        current_time_str = now.strftime('[%m-%d %H:%M]')
    else:
        current_time_str = now.strftime('[%H:%M]')

    messages.append({"role": "user", "content": f"{current_time_str} {user_msg_raw}"})

    # 5. 核心交互 (API调用)
    try:
        if USE_OPENROUTER:
            reply_text_raw = call_openrouter(messages)
        else:
            reply_text_raw = call_gemini(messages, char_id=char_id)

        # 清理时间戳
        timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
        cleaned_reply_text = re.sub(timestamp_pattern, '', reply_text_raw).strip()

        # 6. 存入数据库 (关键修改在这里！)
        now = datetime.now()
        user_ts = now.strftime('%Y-%m-%d %H:%M:%S')
        ai_ts = (now + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 存用户消息
        cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", ("user", user_msg_raw, user_ts))

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
            "id": ai_msg_id  # <--- 这行是能够删除新消息的关键
        })

    except Exception as e:
        print(f"Chat Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 【修正版】群聊核心接口 (串行上下文 + N倍回复) ---
import random
@app.route("/api/group/<group_id>/chat", methods=["POST"])
def group_chat(group_id):
    # 1. 基础准备
    data = request.json
    user_msg = data.get("message", "").strip()
    if not user_msg: return jsonify({"error": "empty"}), 400

    group_dir = os.path.join(GROUPS_DIR, group_id)
    db_path = os.path.join(group_dir, "chat.db")

    # 读取群成员
    members = []
    if os.path.exists(GROUPS_CONFIG_FILE):
        with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
            all_groups = json.load(f)
            if group_id in all_groups:
                members = all_groups[group_id].get("members", [])

    ai_members = [m for m in members if m != "user"]
    if not ai_members: return jsonify({"error": "No AI members"}), 404

    # 2. 存入用户消息
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    now = datetime.now()
    user_ts = now.strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                   ("user", user_msg, user_ts))
    conn.commit()
    conn.close()

    # 3. 决定回复次数
    N = len(ai_members)
    max_replies = 2 * N
    num_replies = random.randint(1, max_replies)

    print(f"--- [GroupChat] 成员: {len(ai_members)}人, 计划回复: {num_replies} 次 ---")

    replies_for_frontend = []

    # 加载名字映射
    id_to_name = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for k, v in c_conf.items(): id_to_name[k] = v["name"]
    except: pass

    # --- 4. 循环生成 ---
    # 这里的 context_buffer 存放的是本轮对话中产生的新内容
    context_buffer = []

    for i in range(num_replies):
        speaker_id = random.choice(ai_members)
        speaker_name = id_to_name.get(speaker_id, speaker_id)

        print(f"   -> 第 {i+1} 轮: 由 [{speaker_name}] 发言")

        # A. 构建 Prompt
        sys_prompt = build_system_prompt(speaker_id)
        other_members = [m for m in members if m != speaker_id]
        rel_prompt = build_group_relationship_prompt(speaker_id, other_members)

        full_sys_prompt = sys_prompt + "\n\n" + rel_prompt + "\n【Current Situation】\n当前是在群聊中。请注意上下文，与其他成员自然互动。"

        messages = [{"role": "system", "content": full_sys_prompt}]

        # --- B. 读取并处理历史记录 (关键修改点: 时间戳) ---
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # 读取最近 20 条 (包含刚才用户的发言)
        cursor.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp DESC LIMIT 20")
        history_rows = [dict(row) for row in cursor.fetchall()][::-1]
        conn.close()

        # 判断时间跨度
        show_full_date = False
        if history_rows:
            try:
                first_ts = datetime.strptime(history_rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if first_ts.date() != now.date():
                    show_full_date = True
            except: pass

        for row in history_rows:
            # 1. 处理时间戳
            try:
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                if show_full_date:
                    ts_str = dt_obj.strftime('[%m-%d %H:%M]')
                else:
                    ts_str = dt_obj.strftime('[%H:%M]')
            except:
                ts_str = ""

            # 2. 处理名字 (群聊必须带名字)
            r_id = row['role']
            d_name = "User" if r_id == "user" else id_to_name.get(r_id, r_id)

            # 3. 组合 Content
            msg_role = "user" # 对当前AI来说都是外部输入
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

        # --- D. 调用 AI ---
        try:
            if USE_OPENROUTER:
                reply_text = call_openrouter(messages, char_id=speaker_id)
            else:
                reply_text = call_gemini(messages, char_id=speaker_id)

            timestamp_pattern = r'\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*'
            cleaned_reply = re.sub(timestamp_pattern, '', reply_text).strip()

            # 去除 AI 可能自带的名字前缀 "[国神]:"
            name_pattern = f"^\\[{speaker_name}\\][:：]\\s*"
            cleaned_reply = re.sub(name_pattern, '', cleaned_reply).strip()

            if not cleaned_reply: continue

            # --- E. 存档 ---
            ai_ts = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                           (speaker_id, cleaned_reply, ai_ts))
            conn.commit()
            conn.close()

            # 更新 Buffer
            context_buffer.append({
                "role_id": speaker_id,
                "display_name": speaker_name,
                "content": cleaned_reply
            })

            replies_for_frontend.append({
                "char_id": speaker_id,
                "name": speaker_name,
                "content": cleaned_reply,
                "timestamp": ai_ts
            })

        except Exception as e:
            print(f"Group Chat Error ({speaker_id}): {e}")

    return jsonify({"replies": replies_for_frontend})

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

# 这是在 app.py 文件中的 call_openrouter 函数

# ---------------------- OpenRouter / Compatible API ----------------------

def call_openrouter(messages, char_id="unknown"):
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
        r = requests.post(url, json=payload, headers=headers, timeout=100)
        # 打印出服务端的原始报错信息，方便调试
        if r.status_code != 200:
            return f"[ERROR] API call failed with status {r.status_code}: {r.text}"

        r.raise_for_status()
        jr = r.json()
        return jr["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] API request failed: {e}"

# ---------------------- Gemini ----------------------
# 【修改】增加 char_id 参数
def call_gemini(messages, char_id="unknown"):
    """
    Google 官方直连 (配合 Cloudflare Worker) - 增强版
    """
    import requests
    import json

    # 1. 动态获取 Cloudflare 地址
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
    url = f"{base_url}/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_KEY}"

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
                MODEL_NAME,
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
        log_full_prompt(f"Gemini Interaction ({MODEL_NAME})", messages, response_text=text, usage=token_usage)

        return text

    except Exception as e:
        log_full_prompt(f"Gemini ERROR ({MODEL_NAME})", messages, response_text=str(e))
        raise e

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

# 3. 保存群聊记忆 (仅 memory_short)
@app.route("/api/group/<group_id>/save_memory", methods=["POST"])
def save_group_memory(group_id):
    new_content = request.json.get("content")

    group_dir = os.path.join(GROUPS_DIR, group_id)
    memory_file = os.path.join(group_dir, "memory_short.json")

    try:
        with open(memory_file, "w", encoding="utf-8") as f:
            json.dump(new_content, f, ensure_ascii=False, indent=2)
        return jsonify({"status": "success"})
    except Exception as e:
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
            return jsonify(char_info)
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
            "pinned": False
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
            "members": members
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

# --- 【新增】AI 自动生成人设接口 ---
@app.route("/api/generate_persona", methods=["POST"])
def generate_persona():
    data = request.json
    char_name = data.get("char_name")
    source_ip = data.get("source_ip")

    if not char_name or not source_ip:
        return jsonify({"error": "请输入角色名和作品名"}), 400

    # 构造请求
    user_content = f"キャラクター名: {char_name}\n作品名: {source_ip}"

    # 这里的 PERSONA_GENERATION_PROMPT 就是上面定义的那一大段字符串
    # 请务必把它定义在文件顶部或这个函数外面
    messages = [
        {"role": "system", "content": PERSONA_GENERATION_PROMPT},
        {"role": "user", "content": user_content}
    ]

    try:
        print(f"--- [Gen Persona] Generating for {char_name} ({source_ip}) ---")

        # 定义一个特殊的记账 ID
        log_id = f"System:GenPersona({char_name})"

        # 复用现有的 LLM 调用函数
        if USE_OPENROUTER:
            generated_text = call_openrouter(messages)
        else:
            generated_text = call_gemini(messages, char_id=log_id)

        return jsonify({"status": "success", "content": generated_text})

    except Exception as e:
        print(f"Gen Persona Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- 定时任务配置 ---
def scheduled_maintenance():
    """
    每天凌晨 04:00 运行一次
    """
    print("\n⏰ 正在执行每日后台维护...")

    # 【修改】在函数内部导入，避免循环引用
    import memory_jobs

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

# ---------------------- 启动 ----------------------

if __name__ == "__main__":
    init_db()
    # --- 【新增】启动后台定时任务 ---
    scheduler = BackgroundScheduler()
    # 每天凌晨 4 点 0 分自动运行 (这个时候您肯定睡了，适合整理记忆)
    scheduler.add_job(func=scheduled_maintenance, trigger="cron", hour=4, minute=0)
    scheduler.start()
    print("--- [Scheduler] 后台记忆整理服务已启动 (每天 04:00) ---")

    # 【关键修改】加上 use_reloader=False
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
