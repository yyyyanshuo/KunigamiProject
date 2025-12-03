import os
import json
import sqlite3
import datetime
from datetime import timedelta
from app import call_ai_to_summarize

# 定义路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
SHORT_FILE = os.path.join(PROMPTS_DIR, "6_memory_short.json")
MEDIUM_FILE = os.path.join(PROMPTS_DIR, "5_memory_medium.json")
LONG_FILE = os.path.join(PROMPTS_DIR, "4_memory_long.json")
DATABASE_FILE = os.path.join(BASE_DIR, "chat_history.db") # 数据库路径

def auto_snapshot_from_db(target_date_str):
    """
    【新增】从数据库读取指定日期的聊天，自动生成短期记忆
    """
    print(f"   -> 正在从数据库补录 {target_date_str} 的记忆...")

    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ?", (start_time, end_time))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("   -> 数据库里这一天也没聊过天，彻底跳过。")
        return None

    # 拼凑文本
    chat_log = ""
    for ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        name = "用户" if role == "user" else "我"
        chat_log += f"[{time_part}] {name}: {content}\n"

    # 调用 AI
    summary_text = call_ai_to_summarize(chat_log, "short")
    if not summary_text: return None

    # 解析
    events = []
    import re
    for line in summary_text.split('\n'):
        line = line.strip()
        if line:
            match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
            event_time = match_time.group(1) if match_time else "00:00"
            event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
            events.append({"time": event_time, "event": event_text})

    return events

def process_daily_rollover(target_date_str=None):
    # 默认处理昨天
    if not target_date_str:
        target_date_str = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"⏰ [定时任务] 开始日结: {target_date_str}")

    # 1. 读取现有的 Short Memory
    short_data = {}
    if os.path.exists(SHORT_FILE):
        with open(SHORT_FILE, "r", encoding="utf-8") as f:
            try: short_data = json.load(f)
            except: pass

    # 2. 检查昨天有没有记录，如果没有，自动补录！(Auto-Snapshot)
    events = short_data.get(target_date_str)

    if not events:
        print(f"   - {target_date_str} 未发现手动整理的记忆，尝试自动补录...")
        events = auto_snapshot_from_db(target_date_str)
        if events:
            # 补录成功，保存回 Short 文件，方便人类查看
            short_data[target_date_str] = events
            with open(SHORT_FILE, "w", encoding="utf-8") as f:
                json.dump(short_data, f, ensure_ascii=False, indent=2)
            print("   - ✅ 自动补录成功！")
        else:
            print("   - ❌ 补录失败或无对话，结束日结。")
            return

    # 3. 开始日结 (Short -> Medium)
    text_to_summarize = "\n".join([f"[{e['time']}] {e['event']}" for e in events])
    summary = call_ai_to_summarize(text_to_summarize, "medium")

    if not summary: return

    # 4. 写入 Medium
    medium_data = {}
    if os.path.exists(MEDIUM_FILE):
        with open(MEDIUM_FILE, "r", encoding="utf-8") as f:
            try: medium_data = json.load(f)
            except: pass

    medium_data[target_date_str] = summary

    with open(MEDIUM_FILE, "w", encoding="utf-8") as f:
        json.dump(medium_data, f, ensure_ascii=False, indent=2)

    print("   - 📝 日结(Medium)写入完成。")

def process_weekly_rollover():
    print("⏰ [定时任务] 开始周结...")
    if not os.path.exists(MEDIUM_FILE): return

    with open(MEDIUM_FILE, "r", encoding="utf-8") as f:
        try: medium_data = json.load(f)
        except: return

    today = datetime.datetime.now()
    summary_buffer = []

    # 过去7天
    for i in range(7):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        if d in medium_data:
            summary_buffer.append(f"【{d}】: {medium_data[d]}")

    if not summary_buffer: return

    full_text = "\n".join(summary_buffer)
    long_summary = call_ai_to_summarize(full_text, "long")

    if not long_summary: return

    week_key = f"{today.strftime('%Y-%m')}-Week{ (today.day - 1) // 7 + 1}"

    long_data = {}
    if os.path.exists(LONG_FILE):
        with open(LONG_FILE, "r", encoding="utf-8") as f:
            try: long_data = json.load(f)
            except: pass

    long_data[week_key] = long_summary

    with open(LONG_FILE, "w", encoding="utf-8") as f:
        json.dump(long_data, f, ensure_ascii=False, indent=2)

    print("   - 📜 周结(Long)写入完成。")