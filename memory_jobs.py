import os
import json
import sqlite3
import datetime
from datetime import timedelta
# --- 【修改】这里加上 update_short_memory_for_date ---
from app import call_ai_to_summarize, update_short_memory_for_date

# 定义路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
SHORT_FILE = os.path.join(PROMPTS_DIR, "6_memory_short.json")
MEDIUM_FILE = os.path.join(PROMPTS_DIR, "5_memory_medium.json")
LONG_FILE = os.path.join(PROMPTS_DIR, "4_memory_long.json")
DATABASE_FILE = os.path.join(BASE_DIR, "chat_history.db") # 数据库路径

def process_daily_rollover(target_date_str=None):
    """
    日结流程：
    1. 自动补录：检查昨天还有没有未总结的消息，有的话先总结进 Short。
    2. 汇总：把 Short 里昨天的所有事件，合并成一篇 Medium 日记。
    """
    # 默认处理昨天
    if not target_date_str:
        target_date_str = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"⏰ [定时任务] 开始日结流程: {target_date_str}")

    # --- 步骤 1: 自动补漏 (Catch-up) ---
    print(f"   -> 1. 检查是否有未总结的残留消息...")
    try:
        # 调用 app.py 里的增量更新函数，把昨天剩下的全处理了
        count, _ = update_short_memory_for_date(target_date_str)
        if count > 0:
            print(f"      ✅ 自动补录完成，追加了 {count} 条记忆。")
        else:
            print(f"      - 无需补录。")
    except Exception as e:
        print(f"      ❌ 补录出错: {e}")

    # --- 步骤 2: 开始生成中期记忆 (Medium) ---
    print(f"   -> 2. 生成日记 (Short -> Medium)...")

    if not os.path.exists(SHORT_FILE): return

    with open(SHORT_FILE, "r", encoding="utf-8") as f:
        try: short_data = json.load(f)
        except: return

    # 获取数据 (兼容新旧格式)
    day_data = short_data.get(target_date_str)
    events = []
    if isinstance(day_data, list):
        events = day_data
    elif isinstance(day_data, dict):
        events = day_data.get("events", [])

    if not events:
        print(f"      - {target_date_str} 没有任何短期记忆，跳过日结。")
        return

    # 拼凑完整文本 (把这一天累积的所有事件都给 AI)
    text_to_summarize = "\n".join([f"[{e['time']}] {e['event']}" for e in events])
    summary = call_ai_to_summarize(text_to_summarize, "medium")

    if not summary: return

    # 写入 Medium
    medium_data = {}
    if os.path.exists(MEDIUM_FILE):
        with open(MEDIUM_FILE, "r", encoding="utf-8") as f:
            try: medium_data = json.load(f)
            except: pass

    medium_data[target_date_str] = summary

    with open(MEDIUM_FILE, "w", encoding="utf-8") as f:
        json.dump(medium_data, f, ensure_ascii=False, indent=2)

    print("      📝 日记写入完成。")
    print("✅ 日结流程结束。")

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