import os
import sqlite3

# 以当前脚本所在目录为项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_DB = os.path.join(BASE_DIR, "configs", "users.db")
USERS_ROOT = os.path.join(BASE_DIR, "users")


def get_user_ids_excluding_1_to_5():
    """从 users.db 中取出所有用户 ID（排除 1~5 号用户）"""
    if not os.path.exists(USERS_DB):
        print("users.db 不存在，无法统计。")
        return []

    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE id NOT BETWEEN 1 AND 5 ORDER BY id ASC")
        rows = cur.fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def count_messages_in_db(db_path: str) -> int:
    """统计指定 chat.db 中 messages 表的总行数，不存在或错误返回 0"""
    if not os.path.exists(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        count = cur.fetchone()[0] or 0
        return count
    except Exception as e:
        print(f"[警告] 统计 {db_path} 失败: {e}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def count_user_single_chats(user_id: int) -> int:
    """
    统计某个用户下所有【单聊角色】的消息条数之和：
    users/<user_id>/characters/<char_id>/chat.db
    """
    chars_root = os.path.join(USERS_ROOT, str(user_id), "characters")
    if not os.path.exists(chars_root):
        return 0

    total = 0
    for name in os.listdir(chars_root):
        char_dir = os.path.join(chars_root, name)
        if not os.path.isdir(char_dir):
            continue
        db_path = os.path.join(char_dir, "chat.db")
        total += count_messages_in_db(db_path)
    return total


def count_user_group_chats(user_id: int) -> int:
    """
    统计某个用户下所有【群聊】的消息条数之和：
    users/<user_id>/groups/<group_id>/chat.db
    """
    groups_root = os.path.join(USERS_ROOT, str(user_id), "groups")
    if not os.path.exists(groups_root):
        return 0

    total = 0
    for name in os.listdir(groups_root):
        group_dir = os.path.join(groups_root, name)
        if not os.path.isdir(group_dir):
            continue
        db_path = os.path.join(group_dir, "chat.db")
        total += count_messages_in_db(db_path)
    return total


def main():
    user_ids = get_user_ids_excluding_1_to_5()
    if not user_ids:
        print("没有找到除用户 1 以外的用户。")
        return

    per_user_counts = {}
    for uid in user_ids:
        single_cnt = count_user_single_chats(uid)
        group_cnt = count_user_group_chats(uid)
        total_cnt = single_cnt + group_cnt
        per_user_counts[uid] = total_cnt
        print(f"用户 {uid} 对话数: 单聊={single_cnt}, 群聊={group_cnt}, 合计={total_cnt}")

    if not per_user_counts:
        print("没有可统计的数据。")
        return

    total = sum(per_user_counts.values())
    max_uid, max_cnt = max(per_user_counts.items(), key=lambda kv: kv[1])
    avg = total / len(per_user_counts)

    print("\n====== 统计结果（排除用户 1，含单聊+群聊）======")
    print(f"总对话数: {total}")
    print(f"最高对话数: {max_cnt} （用户 {max_uid}）")
    print(f"平均对话数: {avg:.2f} （按用户维度平均）")


if __name__ == "__main__":
    main()