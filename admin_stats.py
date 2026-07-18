import os
import sqlite3
import json
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_DB = os.path.join(BASE_DIR, "configs", "users.db")
USERS_ROOT = os.path.join(BASE_DIR, "users")
MOMENTS_DATA = os.path.join(BASE_DIR, "configs", "moments_data.json")

def get_all_users():
    """获取所有用户信息(id, created_at)，排除 1-5 号用户"""
    if not os.path.exists(USERS_DB):
        return []
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, created_at FROM users WHERE id NOT BETWEEN 1 AND 5")
        return cur.fetchall()
    finally:
        conn.close()

def parse_time(ts_str):
    if not ts_str:
        return None
    try:
        if "T" in ts_str:
            return datetime.fromisoformat(ts_str.split(".")[0])
        return datetime.strptime(ts_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
    except:
        return None

def analyze_user_messages(user_id):
    """
    分析单个用户的消息记录
    返回:
    - 活跃天数集合 (用于计算DAU/WAU和连续天数)
    - 单聊消息量
    - 群聊消息量
    - 角色发送量
    - 用户发送量
    - 角色主动消息量
    - 各角色消息统计 (用于TOP 10)
    - 角色数
    - 群聊数
    """
    stats = {
        'active_dates': set(),
        'single_chat_count': 0,
        'group_chat_count': 0,
        'ai_sent': 0,
        'user_sent': 0,
        'ai_proactive': 0,
        'char_counts': {},
        'char_num': 0,
        'group_num': 0
    }

    user_dir = os.path.join(USERS_ROOT, str(user_id))
    if not os.path.exists(user_dir):
        return stats

    # 1. 统计单聊
    chars_dir = os.path.join(user_dir, "characters")
    if os.path.exists(chars_dir):
        for char_id in os.listdir(chars_dir):
            if not os.path.isdir(os.path.join(chars_dir, char_id)): continue
            stats['char_num'] += 1
            db_path = os.path.join(chars_dir, char_id, "chat.db")
            if not os.path.exists(db_path): continue

            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("SELECT role, timestamp FROM messages ORDER BY id ASC")
                rows = cur.fetchall()

                stats['single_chat_count'] += len(rows)

                char_msg_count = 0
                prev_role = None
                for role, ts in rows:
                    if ts:
                        dt = parse_time(ts)
                        if dt:
                            stats['active_dates'].add(dt.date())

                    if role == 'user':
                        stats['user_sent'] += 1
                    else:
                        stats['ai_sent'] += 1
                        char_msg_count += 1
                        # 如果连续两条都是AI发的，说明是主动消息
                        if prev_role != 'user' and prev_role is not None:
                            stats['ai_proactive'] += 1

                    prev_role = role

                stats['char_counts'][char_id] = stats['char_counts'].get(char_id, 0) + char_msg_count

            except Exception:
                pass
            finally:
                conn.close()

    # 2. 统计群聊
    groups_dir = os.path.join(user_dir, "groups")
    if os.path.exists(groups_dir):
        for group_id in os.listdir(groups_dir):
            if not os.path.isdir(os.path.join(groups_dir, group_id)): continue
            stats['group_num'] += 1
            db_path = os.path.join(groups_dir, group_id, "chat.db")
            if not os.path.exists(db_path): continue

            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("SELECT role, timestamp FROM messages ORDER BY id ASC")
                rows = cur.fetchall()

                stats['group_chat_count'] += len(rows)

                prev_role = None
                for role, ts in rows:
                    if ts:
                        dt = parse_time(ts)
                        if dt:
                            stats['active_dates'].add(dt.date())

                    if role == 'user':
                        stats['user_sent'] += 1
                    else:
                        stats['ai_sent'] += 1
                        # 简单判断群聊主动消息：不是用户发的，且上一条也不是用户发的
                        if prev_role != 'user' and prev_role is not None:
                            stats['ai_proactive'] += 1

                    prev_role = role

            except Exception:
                pass
            finally:
                conn.close()

    return stats

def calculate_streak(dates_set):
    """计算连续活跃天数 (极简算法：最长连续段)"""
    if not dates_set: return 0
    sorted_dates = sorted(list(dates_set))
    max_streak = 1
    current_streak = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i-1]).days == 1:
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
        else:
            current_streak = 1
    return max_streak

def get_moments_data_for_user(user_id):
    """获取特定用户的朋友圈数据"""
    user_configs = os.path.join(USERS_ROOT, str(user_id), "configs")
    moments_path = os.path.join(user_configs, "moments_data.json")
    if os.path.exists(moments_path):
        try:
            with open(moments_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []

def generate_admin_stats():
    users = get_all_users()
    total_users = len(users)
    if total_users == 0:
        return {
            "users": {"total": 0, "new_today": 0, "dau": 0, "wau": 0, "retention_1d": 0, "retention_7d": 0, "avg_streak": 0},
            "messages": {"total": 0, "single_chat": 0, "group_chat": 0, "ai_sent": 0, "user_sent": 0, "ai_proactive": 0, "avg_per_user": 0, "daily_sessions": 0},
            "characters": {"total": 0, "avg_per_user": 0, "top_10": []},
            "groups": {"total": 0},
            "moments": {"user_posts": 0, "ai_posts": 0}
        }

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    new_today = 0
    dau = set()
    wau = set()

    # 留存统计 - 全局平均
    eligible_d1 = 0
    retained_d1 = 0
    eligible_d7 = 0
    retained_d7 = 0

    global_stats = {
        'single_chat': 0,
        'group_chat': 0,
        'ai_sent': 0,
        'user_sent': 0,
        'ai_proactive': 0,
        'char_counts': {},
        'char_num': 0,
        'group_num': 0,
        'streaks': []
    }

    moments_user = 0
    moments_ai = 0

    for uid, created_at in users:
        create_dt = parse_time(created_at)
        create_date = create_dt.date() if create_dt else today

        if create_date == today:
            new_today += 1

        u_stats = analyze_user_messages(uid)

        # 活跃度
        if today in u_stats['active_dates']:
            dau.add(uid)
        for d in u_stats['active_dates']:
            if d >= week_ago:
                wau.add(uid)
                break

        # 留存计算 (全局)
        if (today - create_date).days >= 1:
            eligible_d1 += 1
            if (create_date + timedelta(days=1)) in u_stats['active_dates']:
                retained_d1 += 1

        if (today - create_date).days >= 7:
            eligible_d7 += 1
            if (create_date + timedelta(days=7)) in u_stats['active_dates']:
                retained_d7 += 1

        global_stats['single_chat'] += u_stats['single_chat_count']
        global_stats['group_chat'] += u_stats['group_chat_count']
        global_stats['ai_sent'] += u_stats['ai_sent']
        global_stats['user_sent'] += u_stats['user_sent']
        global_stats['ai_proactive'] += u_stats['ai_proactive']
        global_stats['char_num'] += u_stats['char_num']
        global_stats['group_num'] += u_stats['group_num']
        global_stats['streaks'].append(calculate_streak(u_stats['active_dates']))

        for cid, cnt in u_stats['char_counts'].items():
            global_stats['char_counts'][cid] = global_stats['char_counts'].get(cid, 0) + cnt

        # 朋友圈统计 (Per-user)
        user_moments = get_moments_data_for_user(uid)
        for m in user_moments:
            if m.get('author_id') == 'user' or m.get('char_id') == 'user':
                moments_user += 1
            else:
                moments_ai += 1

    # 汇总留存率
    retention_1d = (retained_d1 / eligible_d1 * 100) if eligible_d1 > 0 else 0
    retention_7d = (retained_d7 / eligible_d7 * 100) if eligible_d7 > 0 else 0

    # 汇总
    total_msgs = global_stats['single_chat'] + global_stats['group_chat']
    top_chars = sorted(global_stats['char_counts'].items(), key=lambda x: x[1], reverse=True)[:10]
    avg_streak = sum(global_stats['streaks']) / len(global_stats['streaks']) if global_stats['streaks'] else 0

    return {
        "users": {
            "total": total_users,
            "new_today": new_today,
            "dau": len(dau),
            "wau": len(wau),
            "retention_1d": round(retention_1d, 1),
            "retention_7d": round(retention_7d, 1),
            "avg_streak": round(avg_streak, 1)
        },
        "messages": {
            "total": total_msgs,
            "single_chat": global_stats['single_chat'],
            "group_chat": global_stats['group_chat'],
            "ai_sent": global_stats['ai_sent'],
            "user_sent": global_stats['user_sent'],
            "ai_proactive": global_stats['ai_proactive'],
            "avg_per_user": round(total_msgs / total_users, 1) if total_users else 0,
            "daily_sessions": round(total_msgs / 30, 1)
        },
        "characters": {
            "total": global_stats['char_num'],
            "avg_per_user": round(global_stats['char_num'] / total_users, 1) if total_users else 0,
            "top_10": [{"char_id": k, "count": v} for k, v in top_chars]
        },
        "groups": {
            "total": global_stats['group_num']
        },
        "moments": {
            "user_posts": moments_user,
            "ai_posts": moments_ai
        }
    }

if __name__ == "__main__":
    print(json.dumps(generate_admin_stats(), indent=2, ensure_ascii=False))
