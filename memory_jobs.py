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
from core.time_utils import (
    BEIJING_TZ,
    ensure_character_time_defaults,
    get_zone,
    sleep_event_datetime,
    sleep_event_key,
    sleep_source_timezone,
    utc_now,
)
from core.memory_periods import completed_week_before, week_key_for_end_date

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

def _process_single_char_daily(char_id, target_date_str, user_id=None):
    """处理单个角色的日结（调用时需已 set_background_user）"""
    from services.memory import generate_medium_memory_for_date, update_short_memory_for_date
    print(f"   > 正在处理角色: [{char_id}]")

    # 1. 先完整补录私聊。429、截断、解析或存储失败时绝不能拿
    # 残缺的短期记忆继续覆盖中期记忆。
    short_result = update_short_memory_for_date(
        char_id, target_date_str, user_id=user_id
    )
    if not short_result.ok:
        print(f"     ❌ [补录] {short_result.status}: {short_result.message}；跳过中期记忆")
        return
    if short_result.count:
        print(f"     ✅ [补录] 私聊补录 {short_result.count} 条")

    # 2. 中期记忆也按批次完成后一次性提交，不再硬截前 8 条。
    medium_result = generate_medium_memory_for_date(
        char_id, target_date_str, user_id=user_id
    )
    if not medium_result.ok or medium_result.status != "success":
        print(f"     ❌ [中期] {medium_result.status}: {medium_result.message}")
        return
    print(f"     📝 日记写入完成（{medium_result.count} 段）")

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
        target_date_str = (datetime.datetime.now(BEIJING_TZ) - timedelta(days=1)).strftime('%Y-%m-%d')

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
                _process_single_char_daily(char_id, target_date_str, user_id=user_id)
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
        target_date_str = (datetime.datetime.now(BEIJING_TZ) - timedelta(days=1)).strftime('%Y-%m-%d')

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

def _process_single_char_weekly(char_id, user_id=None):
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

    today = datetime.datetime.now().date()
    start_date, end_date = completed_week_before(today)
    summary_buffer = []

    # 上一个已完整结束的周一至周日，按时间正序提供给模型。
    for offset in range(7):
        d = (start_date + timedelta(days=offset)).strftime('%Y-%m-%d')
        if d in medium_data:
            summary_buffer.append(f"【{d}】: {medium_data[d]}")

    if not summary_buffer:
        print("     - 近7天无日记，跳过")
        return

    full_text = "\n".join(summary_buffer)
    long_summary = call_ai_to_summarize(full_text, "long", char_id, user_id=user_id)

    if not long_summary: return

    week_key = week_key_for_end_date(end_date)

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
                _process_single_char_weekly(char_id, user_id=user_id)
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
BEDTIME_DIARY_MAX_USER_WORKERS = 2
BEDTIME_DIARY_MAX_PER_USER_PER_CHECK = 1
BEDTIME_DIARY_RETRY_DELAY_MINUTES = 10
BEDTIME_DIARY_CHAR_DELAY_SECONDS = 2


def _now_timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mark_bedtime_diary_pending(info: dict, target_date_str: str, reason: str) -> bool:
    """Mark a character for bedtime diary generation without looking at chat DB."""
    if info.get("bedtime_diary_date") != target_date_str:
        info["bedtime_diary_attempts"] = 0
        info["bedtime_diary_last_error"] = None

    if (
        info.get("bedtime_diary_date") == target_date_str
        and info.get("bedtime_diary_status") in ("success", "running", "pending")
    ):
        return False

    info["bedtime_diary_date"] = target_date_str
    info["bedtime_diary_status"] = "pending"
    info["bedtime_diary_pending_reason"] = reason
    info["bedtime_diary_updated_at"] = _now_timestamp()
    return True


def _parse_timestamp(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _should_run_bedtime_diary(info: dict, target_date_str: str, now_dt: datetime.datetime) -> bool:
    """Retry a diary job that was explicitly queued by scheduled sleep.

    Merely finding a character in deep sleep is intentionally insufficient:
    user-triggered deep sleep must never create a diary job.
    """
    if info.get("bedtime_diary_enabled", True) is False:
        return False
    if not info.get("deep_sleep", False):
        return False
    if info.get("deep_sleep_source") != "schedule":
        return False

    diary_date = info.get("bedtime_diary_date")
    status = info.get("bedtime_diary_status")
    if diary_date != target_date_str:
        return False
    if status in ("success", "skipped"):
        return False
    if status == "pending":
        return True

    if status in ("running", "failed"):
        updated_at = _parse_timestamp(info.get("bedtime_diary_updated_at"))
        if updated_at is None:
            return True
        retry_after = datetime.timedelta(minutes=BEDTIME_DIARY_RETRY_DELAY_MINUTES)
        return now_dt - updated_at >= retry_after

    return True


def _update_bedtime_diary_status(cfg_file: str, char_id: str, target_date_str: str, status: str, error: str | None = None):
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)
        if char_id not in all_config:
            return
        info = all_config[char_id]
        info["bedtime_diary_date"] = target_date_str
        info["bedtime_diary_status"] = status
        info["bedtime_diary_updated_at"] = _now_timestamp()
        if error:
            info["bedtime_diary_last_error"] = str(error)[:500]
        elif status == "success":
            info["bedtime_diary_last_error"] = None
        safe_save_json(cfg_file, all_config)
    except Exception as e:
        print(f"🌙 [Diary] 写回状态失败 {char_id}: {e}")


def _has_short_memory_events_for_date(char_id: str, target_date_str: str, user_id=None) -> bool:
    try:
        from app import get_paths

        _, prompts_dir = get_paths(char_id, user_id=user_id)
        short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")
        if not os.path.exists(short_mem_path):
            return False
        with open(short_mem_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        day_data = data.get(target_date_str)
        if isinstance(day_data, list):
            return len(day_data) > 0
        if isinstance(day_data, dict):
            events = day_data.get("events", [])
            return isinstance(events, list) and len(events) > 0
    except Exception as e:
        print(f"🌙 [Diary] 检查短期记忆失败 {char_id}: {e}")
    return False


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


def _process_single_user_sleep_status(user_id, now_time: str = None):
    """处理单个用户的自动睡眠/唤醒检查（供线程池调用）

    设计要点：
    - 不按“区间内就强制睡/醒”，只在时间点附近的一小段时间内尝试一次
    - 每个角色每天对 start/end 各自动触发一次，避免多次覆盖用户手动设置
    """
    from app import (
        set_background_user, clear_background_user, _get_characters_config_file,
        is_bedtime_diary_global_enabled, _load_user_settings,
    )

    try:
        set_background_user(user_id)
        bedtime_diary_global_enabled = is_bedtime_diary_global_enabled()
        cfg_file = _get_characters_config_file()
        if not os.path.exists(cfg_file):
            return
        with open(cfg_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        updated = False
        current_utc = utc_now()
        current_beijing = current_utc.astimezone(BEIJING_TZ)
        today_str = current_beijing.strftime("%Y-%m-%d")
        now_time = current_beijing.strftime("%H:%M")
        tolerance = timedelta(minutes=DEEP_SLEEP_TIME_TOLERANCE_MINUTES)
        user_settings = _load_user_settings()

        for char_id, info in all_config.items():
            if not isinstance(info, dict):
                continue
            if ensure_character_time_defaults(info, existing_character=True):
                updated = True

            source_tz = sleep_source_timezone(info, user_settings)
            local_today = current_utc.astimezone(get_zone(source_tz)).date()
            due_events = []
            for local_date in (local_today - timedelta(days=1), local_today):
                for event_type in ("sleep", "wake"):
                    event_dt = sleep_event_datetime(
                        info,
                        user_settings,
                        local_date,
                        event_type,
                    )
                    if event_dt is None:
                        continue
                    if abs(current_utc - event_dt.astimezone(datetime.timezone.utc)) <= tolerance:
                        due_events.append((event_dt, local_date, event_type))

            for event_dt, local_date, event_type in sorted(
                due_events, key=lambda item: item[0]
            ):
                event_key = sleep_event_key(
                    info,
                    user_settings,
                    local_date,
                    event_type,
                )
                if info.get("sleep_last_event_key") == event_key:
                    continue

                old_status = bool(info.get("deep_sleep", False))
                new_status = event_type == "sleep"
                info["deep_sleep"] = new_status
                info["deep_sleep_source"] = "schedule"
                info["sleep_manual_override"] = False
                info["sleep_last_event_key"] = event_key
                legacy_date_field = (
                    "ds_start_last_applied_date"
                    if event_type == "sleep"
                    else "ds_end_last_applied_date"
                )
                info[legacy_date_field] = local_date.isoformat()
                updated = True

                beijing_event = event_dt.astimezone(BEIJING_TZ).strftime(
                    "%Y-%m-%d %H:%M"
                )
                if event_type == "sleep":
                    print(
                        f"💤 [自动睡眠] 用户 {user_id} {char_id} 到点睡觉了 "
                        f"(来源:{source_tz} {local_date} {info.get('ds_start')}, "
                        f"北京:{beijing_event}, 当前:{now_time})"
                    )
                    if (
                        not old_status
                        and bedtime_diary_global_enabled
                        and info.get("bedtime_diary_enabled", True) is not False
                        and _mark_bedtime_diary_pending(
                            info,
                            today_str,
                            "auto_sleep",
                        )
                    ):
                        print(
                            f"🌙 [Diary] 用户 {user_id} {char_id} "
                            "已加入睡前日记待生成队列"
                        )
                else:
                    print(
                        f"☀️ [自动唤醒] 用户 {user_id} {char_id} 到点起床了 "
                        f"(来源:{source_tz} {local_date} {info.get('ds_end')}, "
                        f"北京:{beijing_event}, 当前:{now_time})"
                    )

        if updated:
            safe_save_json(cfg_file, all_config)
    except Exception as e:
        print(f"❌ 用户 {user_id} 睡眠检查出错: {e}")
    finally:
        clear_background_user()


def _process_single_user_bedtime_diaries(user_id, target_date_str: str):
    """Process pending bedtime diary jobs for one user, using characters.json as the source of truth."""
    from app import (
        set_background_user, clear_background_user, _get_characters_config_file,
        trigger_bedtime_diary, update_short_memory_for_date, is_bedtime_diary_global_enabled
    )

    processed_count = 0
    try:
        set_background_user(user_id)
        if not is_bedtime_diary_global_enabled():
            return 0
        cfg_file = _get_characters_config_file()
        if not os.path.exists(cfg_file):
            return 0

        with open(cfg_file, "r", encoding="utf-8") as f:
            all_config = json.load(f)

        now_dt = datetime.datetime.now()
        pending_char_ids = []
        for char_id, info in all_config.items():
            if _should_run_bedtime_diary(info, target_date_str, now_dt):
                pending_char_ids.append(char_id)

        for char_id in pending_char_ids[:BEDTIME_DIARY_MAX_PER_USER_PER_CHECK]:
            with open(cfg_file, "r", encoding="utf-8") as f:
                latest_config = json.load(f)
            info = latest_config.get(char_id)
            if not info or not _should_run_bedtime_diary(info, target_date_str, datetime.datetime.now()):
                continue

            info["bedtime_diary_date"] = target_date_str
            info["bedtime_diary_status"] = "running"
            info["bedtime_diary_attempts"] = int(info.get("bedtime_diary_attempts") or 0) + 1
            info["bedtime_diary_updated_at"] = _now_timestamp()
            safe_save_json(cfg_file, latest_config)

            try:
                try:
                    short_result = update_short_memory_for_date(
                        char_id, target_date_str, user_id=user_id
                    )
                    if not short_result.ok:
                        _update_bedtime_diary_status(
                            cfg_file,
                            char_id,
                            target_date_str,
                            "failed",
                            f"short_memory_{short_result.status}",
                        )
                        print(
                            f"🌙 [Diary] 用户 {user_id} {char_id} 短期记忆不完整，"
                            "本次不生成睡前日记"
                        )
                        processed_count += 1
                        continue
                except Exception as e:
                    _update_bedtime_diary_status(
                        cfg_file, char_id, target_date_str, "failed", e
                    )
                    print(f"🌙 [Diary] 用户 {user_id} {char_id} 睡前短期记忆补录异常: {e}")
                    processed_count += 1
                    continue

                if not _has_short_memory_events_for_date(char_id, target_date_str, user_id=user_id):
                    _update_bedtime_diary_status(cfg_file, char_id, target_date_str, "skipped", "no_short_memory_today")
                    print(f"🌙 [Diary] 用户 {user_id} {char_id} 今天没有短期记忆，跳过睡前日记")
                    processed_count += 1
                    continue

                ok = trigger_bedtime_diary(char_id, user_id=user_id)
                if not ok:
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        after_config = json.load(f)
                    after_info = after_config.get(char_id, {})
                    if after_info.get("bedtime_diary_status") == "running":
                        _update_bedtime_diary_status(cfg_file, char_id, target_date_str, "failed", "trigger_returned_false")
                processed_count += 1
            except Exception as e:
                _update_bedtime_diary_status(cfg_file, char_id, target_date_str, "failed", e)
                print(f"🌙 [Diary] 用户 {user_id} {char_id} 生成睡前日记出错: {e}")

            time.sleep(BEDTIME_DIARY_CHAR_DELAY_SECONDS)

    except Exception as e:
        print(f"❌ 用户 {user_id} 睡前日记队列处理失败: {e}")
    finally:
        clear_background_user()

    return processed_count


def run_pending_bedtime_diary_jobs(target_date_str=None):
    """Process bedtime diary backlog with bounded concurrency, like the stable daily rollover jobs."""
    from app import list_all_user_ids

    if not target_date_str:
        target_date_str = datetime.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    user_ids = list_all_user_ids()
    if not user_ids:
        return

    def _worker(uid):
        return _process_single_user_bedtime_diaries(uid, target_date_str)

    max_workers = min(BEDTIME_DIARY_MAX_USER_WORKERS, len(user_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        counts = list(executor.map(_worker, user_ids))

    total = sum(counts)
    if total:
        print(f"🌙 [Diary] 本轮处理睡前日记 {total} 条")


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

    run_pending_bedtime_diary_jobs()

def _process_single_user_active_messaging(user_id):
    """处理单个用户的主动消息检测（供线程池调用）"""
    from app import (
        trigger_active_chat, trigger_group_active_chat,
        set_background_user, clear_background_user,
        get_characters_config_for_current_user, get_groups_config_for_current_user,
        get_paths, get_group_dir,
    )
    from core.circuit_breaker import is_user_frozen

    try:
        set_background_user(user_id)
        chars_config = get_characters_config_for_current_user()

        if is_user_frozen(user_id):
            print(f"   ⚠️ 用户 {user_id} 已被冻结，跳过主动消息")
            return

        for char_id, info in chars_config.items():
            if info.get("light_sleep", False) or info.get("deep_sleep", False):
                continue

            if info.get("chat_mode", "online") == "offline":
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
        set_background_user, clear_background_user,
        get_characters_config_for_current_user, get_moments_paths, _get_active_moments_enabled,
    )
    from blueprints.moments import trigger_active_moments

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
                trigger_active_moments(char_id, user_id=user_id)
                # 若因 API 致命错误自动关闭了主动朋友圈，则立即停止本轮后续角色
                if not _get_active_moments_enabled():
                    print(f"   - 用户 {user_id} 主动朋友圈已被自动停止，结束本轮检测")
                    break

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
