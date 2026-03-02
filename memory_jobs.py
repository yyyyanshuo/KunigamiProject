import os
import json
import datetime
from datetime import timedelta
import time

# 这里的引用非常关键
# 我们从 app 导入 AI 总结功能 和 增量更新功能
# --- 【修改】导入 update_group_short_memory ---
#from app import call_ai_to_summarize, update_short_memory_for_date, update_group_short_memory, trigger_active_chat, get_char_db_path

import random
import sqlite3

import tempfile # <--- 记得在最上面加这个 import

# 定义基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "configs", "characters.json")
GROUPS_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "groups.json")
USER_SETTINGS_FILE = os.path.join(BASE_DIR, "configs", "user_settings.json")

# --- 【新增】安全保存 JSON (防止文件损坏) ---
def safe_save_json(filepath, data):
    """
    原子化写入：先写临时文件，再重命名。
    防止多线程写入导致文件损坏 (Extra data 错误)。
    """
    dir_name = os.path.dirname(filepath)
    # 创建临时文件
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)

    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 瞬间替换 (Atomic Operation)
        os.replace(temp_path, filepath)
    except Exception as e:
        print(f"❌ Save JSON Error: {e}")
        os.remove(temp_path) # 出错则删掉临时文件

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
    # 【修复】在这里局部导入，避开启动时的循环依赖
    from app import call_ai_to_summarize, update_short_memory_for_date
    print(f"   > 正在处理角色: [{char_id}]")

    prompts_dir = get_char_prompts_dir(char_id)
    short_file = os.path.join(prompts_dir, "6_memory_short.json")
    medium_file = os.path.join(prompts_dir, "5_memory_medium.json")

    # 1. 自动补录 (只补录私聊的，群聊的已经实时进去了)
    try:
        count, _ = update_short_memory_for_date(char_id, target_date_str)
        if count > 0: print(f"     ✅ [补录] 私聊补录 {count} 条")
    except Exception as e: print(f"     ❌ [补录] 出错: {e}")

    # 2. 生成日记
    if not os.path.exists(short_file): return

    with open(short_file, "r", encoding="utf-8") as f:
        try: short_data = json.load(f)
        except: return

    # 获取事件 (此时这里面已经包含了 私聊 + 群聊 的混合时间线)
    day_data = short_data.get(target_date_str)
    events = []
    if isinstance(day_data, list): events = day_data
    elif isinstance(day_data, dict): events = day_data.get("events", [])

    if not events:
        print(f"     - {target_date_str} 无事件，跳过")
        return

    # 拼凑文本
    text_to_summarize = "\n".join([f"[{e['time']}] {e['event']}" for e in events])

    # 调用 AI 总结 (medium模式)
    summary = call_ai_to_summarize(text_to_summarize, "medium", char_id)

    if not summary: return

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

# --- 【新增】全员群聊日结 (Group Daily) ---
def run_all_group_daily_rollovers(target_date_str=None):
    """
    遍历所有群聊，执行总结并分发给成员
    """
    # 【修复】局部导入群聊记忆更新函数
    from app import update_group_short_memory

    if not target_date_str:
        target_date_str = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"⏰ [定时任务] 开始群聊日结: {target_date_str}")

    if not os.path.exists(GROUPS_CONFIG_FILE):
        print("   - 暂无群聊配置，跳过")
        return

    try:
        with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
            groups_config = json.load(f)

        for group_id in groups_config.keys():
            print(f"   > 处理群聊: [{group_id}]")
            try:
                # 调用 app.py 里的函数：总结 -> 存群记忆 -> 分发给个人
                count, _ = update_group_short_memory(group_id, target_date_str)
                if count > 0:
                    print(f"     ✅ 总结并分发了 {count} 条群消息")
                else:
                    print(f"     - 无新消息")

                # 休息一下防止 API 过载
                time.sleep(1)
            except Exception as e:
                print(f"     ❌ 群聊 {group_id} 处理失败: {e}")

    except Exception as e:
        print(f"   ❌ 读取群配置失败: {e}")

    print("✅ 群聊日结结束 (已同步至个人)。")

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
    # 【修复】局部导入 AI 总结函数
    from app import call_ai_to_summarize

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

# ================= 每年年龄 +1 =================

def run_yearly_age_increment():
    """
    每年 1 月 1 日执行。为配置了 age 的角色年龄 +1。
    """
    print("⏰ [定时任务] 开始年度年龄递增...")

    if not os.path.exists(CONFIG_FILE):
        print("   - 无配置文件，跳过")
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        current_year = datetime.datetime.now().strftime("%Y")
        updated = []

        for char_id, info in all_config.items():
            age = info.get("age")
            if age is None:
                continue
            last_inc = info.get("age_last_incremented")
            if last_inc == current_year:
                continue  # 今年已递增过
            try:
                info["age"] = int(age) + 1
                info["age_last_incremented"] = current_year
                updated.append(char_id)
                print(f"   > {char_id}: {age} → {age + 1} 歳")
            except (ValueError, TypeError):
                pass

        if updated:
            safe_save_json(CONFIG_FILE, all_config)

        # --- 用户年龄 +1 ---
        if os.path.exists(USER_SETTINGS_FILE):
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_data = json.load(f)
            age = user_data.get("user_age")
            last_inc = user_data.get("user_age_last_incremented")
            if age is not None and last_inc != current_year:
                try:
                    user_data["user_age"] = int(age) + 1
                    user_data["user_age_last_incremented"] = current_year
                    safe_save_json(USER_SETTINGS_FILE, user_data)
                    updated.append("(用户)")
                    print(f"   > 用户: {age} → {age + 1} 歳")
                except (ValueError, TypeError):
                    pass

        print(f"✅ 年度年龄递增结束，共更新 {len(updated)} 人")
    except Exception as e:
        print(f"❌ 年龄递增出错: {e}")

# --- 【新增】自动睡眠/唤醒检查 ---
def check_and_update_sleep_status():
    """
    每分钟运行一次。
    如果当前时间 == 设定入睡时间 -> 开启深睡眠
    如果当前时间 == 设定起床时间 -> 关闭深睡眠
    """
    # 1. 获取当前时间 HH:MM
    now_time = datetime.datetime.now().strftime("%H:%M")

    if not os.path.exists(CONFIG_FILE): return

    try:
        # 2. 读取配置
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        updated = False

        for char_id, info in all_config.items():
            start_time = info.get("ds_start")
            end_time = info.get("ds_end")
            current_status = info.get("deep_sleep", False)

            # 触发入睡
            if start_time and start_time == now_time:
                if not current_status: # 只有当前没睡时才操作，防止重复写入
                    info["deep_sleep"] = True
                    # 联动：深睡眠开 -> 浅睡眠必开
                    # info["light_sleep"] = True  <--- 【删除这行！】不要改数据库里的浅睡眠
                    print(f"💤 [自动睡眠] {char_id} 到点睡觉了 ({now_time})")
                    updated = True

            # 触发起床
            elif end_time and end_time == now_time:
                if current_status:
                    info["deep_sleep"] = False
                    print(f"☀️ [自动唤醒] {char_id} 到点起床了 ({now_time})")
                    updated = True

        # 3. 如果有变化，保存文件
        if updated:
            # 使用安全保存
            safe_save_json(CONFIG_FILE, all_config)

    except Exception as e:
        print(f"❌ 睡眠检查出错: {e}")

def run_active_messaging_check():
    """
    心跳任务：每10分钟运行一次。
    计算概率，决定是否发起主动消息。
    """
    # 【修复】局部导入触发主动消息的函数
    from app import trigger_active_chat

    print("\n💓 [Heartbeat] 开始检测主动消息机会...")

    if not os.path.exists(CONFIG_FILE): return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            chars_config = json.load(f)

        for char_id, info in chars_config.items():
            # 1. 检查浅睡眠 (Light Sleep)
            if info.get("light_sleep", False) or info.get("deep_sleep", False):
                # print(f"   - {char_id}: 睡眠中，跳过")
                continue

            # 2. 获取最后一条消息时间
            db_path = os.path.join(BASE_DIR, "characters", char_id, "chat.db")
            if not os.path.exists(db_path): continue

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, role FROM messages ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()

            # 【关键修复】↓↓↓ 加上这一行！↓↓↓
            if not row:
                # print(f"   - {char_id}: 没有聊天记录，跳过")
                continue
                # ----------------------------------

            last_ts_str, last_role = row

            # 3. 计算时间差 (分钟)
            last_dt = datetime.datetime.strptime(last_ts_str, '%Y-%m-%d %H:%M:%S')
            minutes_diff = (datetime.datetime.now() - last_dt).total_seconds() / 60

            if minutes_diff < 10: continue # 没到10分钟CD

            # 4. 计算概率 P
            # 算法：P_time = 0.005 * t (t >= 10)
            p_time = 0.005 * minutes_diff

            # 情绪指数 (0.0 ~ 20.0)
            emotion = info.get("emotion", 0.5)

            # 最终概率
            p_final = p_time * emotion

            # 随机判定
            dice = random.random() # 0.0 ~ 1.0

            print(f"   > [{char_id}] 距上次 {int(minutes_diff)}分, 情绪 {emotion}, 概率 {p_final:.2f}, 骰子 {dice:.2f}")

            if dice < p_final:
                # 中奖了！触发发送！
                trigger_active_chat(char_id)

        # ... 在 run_active_messaging_check 函数内，角色遍历结束后 ...

        # --- 群聊主动消息检测 ---
        if os.path.exists(GROUPS_CONFIG_FILE):
            with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                groups_config = json.load(f)

            # 导入群聊触发函数
            from app import trigger_group_active_chat

            for group_id, info in groups_config.items():
                # 1. 检查开关
                if not info.get("active_mode", False):
                    continue

                # 2. 获取最后一条消息时间
                group_dir = os.path.join(BASE_DIR, "groups", group_id)
                db_path = os.path.join(group_dir, "chat.db")

                if not os.path.exists(db_path): continue

                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT timestamp FROM messages ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                conn.close()

                if not row: continue # 没聊过的群不主动

                last_ts_str = row[0]
                last_dt = datetime.datetime.strptime(last_ts_str, '%Y-%m-%d %H:%M:%S')
                minutes_diff = (datetime.datetime.now() - last_dt).total_seconds() / 60

                if minutes_diff < 10: continue

                # 3. 计算概率 (不乘情绪指数，只看时间)
                # 逻辑：10min -> 5%, 60min -> 30%, 200min -> 100%
                # 公式: p = 0.005 * t
                p_final = 0.005 * minutes_diff
                if p_final > 1.0: p_final = 1.0 # 封顶 100%

                dice = random.random()
                print(f"   > [群:{group_id}] 距上次 {int(minutes_diff)}分, 概率 {p_final:.2f}, 骰子 {dice:.2f}")

                if dice < p_final:
                    # 触发！
                    trigger_group_active_chat(group_id)

    except Exception as e:
        print(f"❌ 心跳检测出错: {e}")