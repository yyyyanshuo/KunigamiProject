import os
import json
import datetime
from datetime import timedelta
import time
from concurrent.futures import ThreadPoolExecutor

# 这里的引用非常关键
# 我们从 app 导入 AI 总结功能 和 增量更新功能
# --- 【修改】导入 update_group_short_memory ---
#from app import call_ai_to_summarize, update_short_memory_for_date, update_group_short_memory, trigger_active_chat, get_char_db_path

import random
import sqlite3

import tempfile # <--- 记得在最上面加这个 import

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

# --- 辅助函数：角色 Prompt 路径改为从 app.get_paths 获取（支持 per-user） ---
# get_all_char_ids 等由 app 的 get_all_char_ids_for_current_user 替代，此处不再定义

# ================= 日结逻辑 (Daily) =================

def _process_single_char_daily(char_id, target_date_str):
    """处理单个角色的日结（调用时需已 set_background_user）"""
    from app import call_ai_to_summarize, update_short_memory_for_date, get_paths
    print(f"   > 正在处理角色: [{char_id}]")

    _, prompts_dir = get_paths(char_id)
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

def _process_single_user_group_daily_rollovers(user_id, target_date_str):
    """处理单个用户的所有群聊日结（供线程池调用）"""
    from app import update_group_short_memory, set_background_user, clear_background_user, get_all_group_ids_for_current_user

    try:
        set_background_user(user_id)
        group_ids = get_all_group_ids_for_current_user()
        for group_id in group_ids:
            print(f"   > 用户 {user_id} 群聊: [{group_id}]")
            try:
                count, _ = update_group_short_memory(group_id, target_date_str)
                if count > 0:
                    print(f"     ✅ 总结并分发了 {count} 条群消息")
                else:
                    print(f"     - 无新消息")
                time.sleep(1)
            except Exception as e:
                print(f"     ❌ 群聊 {group_id} 处理失败: {e}")
    except Exception as e:
        print(f"   ❌ 用户 {user_id} 群聊日结失败: {e}")
    finally:
        clear_background_user()


# --- 【新增】全员群聊日结 (Group Daily)，按 user_id 拆分（并行） ---
def run_all_group_daily_rollovers(target_date_str=None):
    """遍历所有用户及其群聊，执行总结并分发给成员（多用户并行）"""
    from app import list_all_user_ids

    if not target_date_str:
        target_date_str = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"⏰ [定时任务] 开始群聊日结: {target_date_str}")

    user_ids = list_all_user_ids()
    if not user_ids:
        print("   - 无用户，跳过")
        return

    def _worker(uid):
        _process_single_user_group_daily_rollovers(uid, target_date_str)

    max_workers = min(8, len(user_ids), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_worker, user_ids))

    print("✅ 群聊日结结束 (已同步至个人)。")

def _process_single_user_daily_rollovers(user_id, target_date_str):
    """处理单个用户的所有角色日结（供线程池调用）"""
    from app import set_background_user, clear_background_user, get_all_char_ids_for_current_user

    try:
        set_background_user(user_id)
        char_ids = get_all_char_ids_for_current_user()
        for char_id in char_ids:
            try:
                _process_single_char_daily(char_id, target_date_str)
                time.sleep(2)
            except Exception as e:
                print(f"     ❌ 处理角色 {char_id} 时崩溃: {e}")
    except Exception as e:
        print(f"   ❌ 用户 {user_id} 日结失败: {e}")
    finally:
        clear_background_user()


def run_all_daily_rollovers(target_date_str=None):
    """【入口】按 user_id 遍历，为每个用户的每个角色执行日结（多用户并行）"""
    from app import list_all_user_ids

    if not target_date_str:
        target_date_str = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"⏰ [定时任务] 开始全员日结: {target_date_str}")

    user_ids = list_all_user_ids()
    if not user_ids:
        print("   - 无用户，跳过")
        return

    def _worker(uid):
        _process_single_user_daily_rollovers(uid, target_date_str)

    max_workers = min(8, len(user_ids), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_worker, user_ids))

    print("✅ 全员日结结束。")

# ================= 周结逻辑 (Weekly) =================

def _process_single_char_weekly(char_id):
    """处理单个角色的周结（调用时需已 set_background_user）"""
    from app import call_ai_to_summarize, get_paths

    print(f"   > 正在处理角色: [{char_id}] (周结)")

    _, prompts_dir = get_paths(char_id)
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


def _process_single_user_weekly_rollovers(user_id):
    """处理单个用户的所有角色周结（供线程池调用）"""
    from app import set_background_user, clear_background_user, get_all_char_ids_for_current_user

    try:
        set_background_user(user_id)
        char_ids = get_all_char_ids_for_current_user()
        for char_id in char_ids:
            try:
                _process_single_char_weekly(char_id)
                time.sleep(2)
            except Exception as e:
                print(f"     ❌ 处理角色 {char_id} 时崩溃: {e}")
    except Exception as e:
        print(f"   ❌ 用户 {user_id} 周结失败: {e}")
    finally:
        clear_background_user()


def run_all_weekly_rollovers():
    """【入口】按 user_id 遍历，为每个用户的每个角色执行周结（多用户并行）"""
    from app import list_all_user_ids

    print("⏰ [定时任务] 开始全员周结...")

    user_ids = list_all_user_ids()
    if not user_ids:
        print("   - 无用户，跳过")
        return

    max_workers = min(8, len(user_ids), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_process_single_user_weekly_rollovers, user_ids))

    print("✅ 全员周结结束。")

# ================= 每年年龄 +1 =================

def _process_single_user_yearly_age_increment(user_id, current_year: str) -> int:
    """处理单个用户的年度年龄递增，返回更新数量（供线程池调用）"""
    from app import set_background_user, clear_background_user, _get_characters_config_file, _get_user_settings_file

    updated_count = 0
    try:
        set_background_user(user_id)
        cfg_file = _get_characters_config_file()
        user_settings_file = _get_user_settings_file()

        updated_chars = []
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                all_config = json.load(f)
            for char_id, info in all_config.items():
                age = info.get("age")
                if age is None:
                    continue
                last_inc = info.get("age_last_incremented")
                if last_inc == current_year:
                    continue
                try:
                    info["age"] = int(age) + 1
                    info["age_last_incremented"] = current_year
                    updated_chars.append(char_id)
                    print(f"   > 用户 {user_id} {char_id}: {age} → {age + 1} 歳")
                except (ValueError, TypeError):
                    pass
            if updated_chars:
                safe_save_json(cfg_file, all_config)
                updated_count += len(updated_chars)

        if os.path.exists(user_settings_file):
            with open(user_settings_file, "r", encoding="utf-8") as f:
                user_data = json.load(f)
            age = user_data.get("user_age")
            last_inc = user_data.get("user_age_last_incremented")
            if age is not None and last_inc != current_year:
                try:
                    user_data["user_age"] = int(age) + 1
                    user_data["user_age_last_incremented"] = current_year
                    safe_save_json(user_settings_file, user_data)
                    updated_count += 1
                    print(f"   > 用户 {user_id}: {age} → {age + 1} 歳")
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"   ❌ 用户 {user_id} 年龄递增出错: {e}")
    finally:
        clear_background_user()

    return updated_count


def run_yearly_age_increment():
    """每年 1 月 1 日执行。按 user_id 为每个用户的角色和用户年龄 +1（多用户并行）。"""
    from app import list_all_user_ids

    print("⏰ [定时任务] 开始年度年龄递增...")

    user_ids = list_all_user_ids()
    if not user_ids:
        print("   - 无用户，跳过")
        return

    current_year = datetime.datetime.now().strftime("%Y")

    def _worker(uid):
        return _process_single_user_yearly_age_increment(uid, current_year)

    max_workers = min(8, len(user_ids), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        counts = list(executor.map(_worker, user_ids))

    total_updated = sum(counts)
    print(f"✅ 年度年龄递增结束，共更新 {total_updated} 人")

# --- 【新增】自动睡眠/唤醒检查，按 user_id 拆分 ---
# 容错策略：
# - 将原来的“精确等于 HH:MM”改为“在目标时间点附近的容错窗口内触发”
# - 每个角色每天最多自动入睡一次、自动起床一次（用 *_last_applied_date 记录）
DEEP_SLEEP_TIME_TOLERANCE_MINUTES = 2


def _parse_hhmm_to_seconds(hhmm: str) -> int | None:
    """将 'HH:MM' 转成从当天 00:00 起算的秒数，格式错误返回 None。"""
    try:
        parts = hhmm.split(":")
        if len(parts) != 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        return hour * 3600 + minute * 60
    except Exception:
        return None


def _within_tolerance(now_seconds: int, target_seconds: int, tolerance_seconds: int) -> bool:
    """判断当前时间（秒）是否在目标时间点的容错范围内。"""
    return abs(now_seconds - target_seconds) <= tolerance_seconds


def _process_single_user_sleep_status(user_id, now_time: str):
    """处理单个用户的自动睡眠/唤醒检查（供线程池调用）

    设计要点：
    - 不按“区间内就强制睡/醒”，只在时间点附近的一小段时间内尝试一次
    - 每个角色每天对 start/end 各自动触发一次，避免多次覆盖用户手动设置
    """
    from app import set_background_user, clear_background_user, _get_characters_config_file

    try:
        set_background_user(user_id)
        cfg_file = _get_characters_config_file()
        if not os.path.exists(cfg_file):
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        updated = False
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        now_dt = datetime.datetime.now()
        now_seconds = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
        tolerance_seconds = DEEP_SLEEP_TIME_TOLERANCE_MINUTES * 60

        for char_id, info in all_config.items():
            start_time = info.get("ds_start")
            end_time = info.get("ds_end")
            current_status = info.get("deep_sleep", False)

            # 记录每天是否已经自动处理过 start/end
            start_last_date = info.get("ds_start_last_applied_date")
            end_last_date = info.get("ds_end_last_applied_date")

            # --- 自动入睡 ---
            if start_time and start_last_date != today_str and not current_status:
                start_seconds = _parse_hhmm_to_seconds(start_time)
                if start_seconds is not None and _within_tolerance(now_seconds, start_seconds, tolerance_seconds):
                    info["deep_sleep"] = True
                    info["ds_start_last_applied_date"] = today_str
                    print(f"💤 [自动睡眠] 用户 {user_id} {char_id} 到点睡觉了 (配置:{start_time}, 当前:{now_time})")
                    updated = True

            # --- 自动唤醒 ---
            if end_time and end_last_date != today_str and current_status:
                end_seconds = _parse_hhmm_to_seconds(end_time)
                if end_seconds is not None and _within_tolerance(now_seconds, end_seconds, tolerance_seconds):
                    info["deep_sleep"] = False
                    info["ds_end_last_applied_date"] = today_str
                    print(f"☀️ [自动唤醒] 用户 {user_id} {char_id} 到点起床了 (配置:{end_time}, 当前:{now_time})")
                    updated = True

        if updated:
            safe_save_json(cfg_file, all_config)
    except Exception as e:
        print(f"❌ 用户 {user_id} 睡眠检查出错: {e}")
    finally:
        clear_background_user()


def check_and_update_sleep_status():
    """每分钟运行一次。按 user 并行检查每个用户角色的入睡/起床时间。"""
    from app import list_all_user_ids

    now_time = datetime.datetime.now().strftime("%H:%M")
    user_ids = list_all_user_ids()

    if not user_ids:
        return

    def _worker(uid):
        _process_single_user_sleep_status(uid, now_time)

    max_workers = min(8, len(user_ids), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_worker, user_ids))

def _process_single_user_active_messaging(user_id):
    """处理单个用户的主动消息检测（供线程池调用）"""
    from app import (
        trigger_active_chat, trigger_group_active_chat,
        set_background_user, clear_background_user,
        get_characters_config_for_current_user, get_groups_config_for_current_user,
        get_paths, get_group_dir,
    )

    try:
        set_background_user(user_id)
        chars_config = get_characters_config_for_current_user()

        for char_id, info in chars_config.items():
            if info.get("light_sleep", False) or info.get("deep_sleep", False):
                continue

            db_path, _ = get_paths(char_id)
            if not os.path.exists(db_path):
                continue

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, role FROM messages ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()

            if not row:
                continue

            last_ts_str, last_role = row
            last_dt = datetime.datetime.strptime(last_ts_str, '%Y-%m-%d %H:%M:%S')
            minutes_diff = (datetime.datetime.now() - last_dt).total_seconds() / 60

            if minutes_diff < 10:
                continue

            p_time = 0.005 * minutes_diff
            emotion = info.get("emotion", 0.5)
            p_final = p_time * emotion
            dice = random.random()
            print(f"   > 用户 {user_id} [{char_id}] 距上次 {int(minutes_diff)}分, 情绪 {emotion}, 概率 {p_final:.2f}, 骰子 {dice:.2f}")

            if dice < p_final:
                trigger_active_chat(char_id, user_id=user_id)

        groups_config = get_groups_config_for_current_user()
        for group_id, info in groups_config.items():
            if not info.get("active_mode", False):
                continue

            group_dir_path = get_group_dir(group_id)
            db_path = os.path.join(group_dir_path, "chat.db")
            if not os.path.exists(db_path):
                continue

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM messages ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()

            if not row: continue  # 没聊过的群不主动

            last_ts_str = row[0]
            last_dt = datetime.datetime.strptime(last_ts_str, '%Y-%m-%d %H:%M:%S')
            minutes_diff = (datetime.datetime.now() - last_dt).total_seconds() / 60

            if minutes_diff < 10: continue

            p_final = 0.005 * minutes_diff
            if p_final > 1.0: p_final = 1.0
            dice = random.random()
            print(f"   > [群:{group_id}] 距上次 {int(minutes_diff)}分, 概率 {p_final:.2f}, 骰子 {dice:.2f}")

            if dice < p_final:
                # 【修正】群聊主动消息也必须传递 user_id，否则无法读取 API Key 和用户配置
                trigger_group_active_chat(group_id, user_id=user_id)

    except Exception as e:
        print(f"❌ 用户 {user_id} 心跳检测出错: {e}")
    finally:
        clear_background_user()


def run_active_messaging_check():
    """心跳任务：每10分钟运行一次。按 user_id 并行检测每个用户的单聊与群聊主动消息机会。"""
    from app import list_all_user_ids

    print("\n💓 [Heartbeat] 开始检测主动消息机会（并行）...")

    user_ids = list_all_user_ids()
    if not user_ids:
        return

    max_workers = min(8, len(user_ids), 4)  # 最多 4 个用户并行，避免 API 限流
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_process_single_user_active_messaging, user_ids))


def _process_single_user_active_moments(user_id):
    """处理单个用户的主动发朋友圈检测（供线程池调用）"""
    from app import (
        trigger_active_moments, set_background_user, clear_background_user,
        get_characters_config_for_current_user, get_moments_paths, _get_active_moments_enabled,
    )

    try:
        set_background_user(user_id)

        if not _get_active_moments_enabled():
            print(f"   - 用户 {user_id} 主动朋友圈已关闭，跳过")
            return

        chars_config = get_characters_config_for_current_user()
        if not chars_config:
            return

        moments_path, last_post_path = get_moments_paths()
        last_post = {}
        if os.path.exists(last_post_path):
            try:
                with open(last_post_path, "r", encoding="utf-8-sig") as f:
                    last_post = json.load(f)
            except Exception:
                pass

        now = datetime.datetime.now()

        for char_id, info in chars_config.items():
            if info.get("deep_sleep", False):
                continue

            last_ts_str = last_post.get(char_id)
            if last_ts_str:
                try:
                    last_dt = datetime.datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                    hours_since = (now - last_dt).total_seconds() / 3600
                except Exception:
                    hours_since = 100.0
            else:
                hours_since = 100.0

            time_prob = min(1.0, hours_since / 100.0)
            moments_index = float(info.get("moments_index", 1))
            p_final = min(1.0, time_prob * moments_index)
            dice = random.random()

            print(f"   > 用户 {user_id} [{char_id}] 距上次发圈 {hours_since:.1f}h, 性格指数 {moments_index}, 概率 {p_final:.2f}, 骰子 {dice:.2f}")

            if dice < p_final:
                trigger_active_moments(char_id)

    except Exception as e:
        print(f"❌ 用户 {user_id} 朋友圈检测出错: {e}")
    finally:
        clear_background_user()


def run_active_moments_check():
    """每 30 分钟执行：按 user_id 并行对每个用户的每个角色判定是否主动发朋友圈。"""
    from app import list_all_user_ids

    print("\n📷 [Moments] 开始检测主动发朋友圈机会（并行）...")

    user_ids = list_all_user_ids()
    if not user_ids:
        return

    max_workers = min(8, len(user_ids), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_process_single_user_active_moments, user_ids))