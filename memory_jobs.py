import os
import json
import datetime
from datetime import timedelta
import time

# 这里的引用非常关键
# 我们从 app 导入 AI 总结功能 和 增量更新功能
from app import call_ai_to_summarize, update_short_memory_for_date

# 定义基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")

# --- 辅助函数：获取指定角色的 Prompt 路径 ---
def get_char_prompts_dir(char_id):
    return os.path.join(BASE_DIR, "characters", char_id, "prompts")

# --- 辅助函数：获取所有角色 ID ---
def get_all_char_ids():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 找不到配置文件: {CONFIG_FILE}")
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return list(data.keys())
    except:
        return []

# ================= 日结逻辑 (Daily) =================

def _process_single_char_daily(char_id, target_date_str):
    """处理单个角色的日结"""
    print(f"   > 正在处理角色: [{char_id}]")

    prompts_dir = get_char_prompts_dir(char_id)
    short_file = os.path.join(prompts_dir, "6_memory_short.json")
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")

    # --- 1. 自动补漏 (调用 app.py 的函数) ---
    try:
        # 注意：这里的 update_short_memory_for_date 必须在 app.py 里支持 char_id 参数
        count, _ = update_short_memory_for_date(char_id, target_date_str)
        if count > 0:
            print(f"     ✅ [补录] 自动补录了 {count} 条记忆")
    except Exception as e:
        print(f"     ❌ [补录] 出错: {e}")

    # --- 2. 生成日记 (Short -> Medium) ---
    if not os.path.exists(short_file):
        print("     - 无短期记忆文件，跳过")
        return

    with open(short_file, "r", encoding="utf-8") as f:
        try: short_data = json.load(f)
        except: return

    # 兼容新旧格式获取事件
    day_data = short_data.get(target_date_str)
    events = []
    if isinstance(day_data, list):
        events = day_data
    elif isinstance(day_data, dict):
        events = day_data.get("events", [])

    if not events:
        print(f"     - {target_date_str} 无事件记录，跳过日结")
        return

    # 拼凑文本
    text_to_summarize = "\n".join([f"[{e['time']}] {e['event']}" for e in events])

    # 调用 AI (注意：这里如果是多角色，AI 可能会根据 System Prompt 里的设定来写日记)
    # 为了保证人设不串，理想情况下应该把 char_id 传给 call_ai_to_summarize 来切换 System Prompt
    # 但目前 call_ai_to_summarize 是通用的，我们先这样用
    summary = call_ai_to_summarize(text_to_summarize, "medium", char_id)

    if not summary:
        print("     ⚠️ AI 返回为空，日结失败")
        return

    # 写入 Medium
    medium_data = {}
    if os.path.exists(medium_file):
        with open(medium_file, "r", encoding="utf-8") as f:
            try: medium_data = json.load(f)
            except: pass

    medium_data[target_date_str] = summary

    with open(medium_file, "w", encoding="utf-8") as f:
        json.dump(medium_data, f, ensure_ascii=False, indent=2)

    print("     📝 日记写入完成")


def run_all_daily_rollovers(target_date_str=None):
    """【入口】遍历所有角色执行日结"""
    if not target_date_str:
        target_date_str = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"⏰ [定时任务] 开始全员日结: {target_date_str}")

    char_ids = get_all_char_ids()
    for char_id in char_ids:
        try:
            _process_single_char_daily(char_id, target_date_str)
            # 休息一下，防止并发请求太多被封号
            time.sleep(2)
        except Exception as e:
            print(f"     ❌ 处理角色 {char_id} 时崩溃: {e}")

    print("✅ 全员日结结束。")


# ================= 周结逻辑 (Weekly) =================

def _process_single_char_weekly(char_id):
    """处理单个角色的周结"""
    print(f"   > 正在处理角色: [{char_id}] (周结)")

    prompts_dir = get_char_prompts_dir(char_id)
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")
    long_file = os.path.join(prompts_dir, "4_memory_long.json")

    if not os.path.exists(medium_file): return

    with open(medium_file, "r", encoding="utf-8") as f:
        try: medium_data = json.load(f)
        except: return

    today = datetime.datetime.now()
    summary_buffer = []

    # 过去7天
    for i in range(7):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        if d in medium_data:
            summary_buffer.append(f"【{d}】: {medium_data[d]}")

    if not summary_buffer:
        print("     - 近7天无日记，跳过")
        return

    full_text = "\n".join(summary_buffer)
    long_summary = call_ai_to_summarize(full_text, "long", char_id)

    if not long_summary: return

    # 计算 Week Key (以昨天/周日为准)
    target_sunday = today - timedelta(days=1)
    target_month_str = target_sunday.strftime('%Y-%m')
    week_num = (target_sunday.day - 1) // 7 + 1
    week_key = f"{target_month_str}-Week{week_num}"

    long_data = {}
    if os.path.exists(long_file):
        with open(long_file, "r", encoding="utf-8") as f:
            try: long_data = json.load(f)
            except: pass

    long_data[week_key] = long_summary

    with open(long_file, "w", encoding="utf-8") as f:
        json.dump(long_data, f, ensure_ascii=False, indent=2)

    print(f"     📜 周报写入完成: {week_key}")


def run_all_weekly_rollovers():
    """【入口】遍历所有角色执行周结"""
    print("⏰ [定时任务] 开始全员周结...")

    char_ids = get_all_char_ids()
    for char_id in char_ids:
        try:
            _process_single_char_weekly(char_id)
            time.sleep(2)
        except Exception as e:
            print(f"     ❌ 处理角色 {char_id} 时崩溃: {e}")

    print("✅ 全员周结结束。")