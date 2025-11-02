import sqlite3

DATABASE_FILE = "chat_history.db"


def delete_specific_records():
    """
    任务一：根据提供的 ID 列表，删除指定的聊天记录。
    """
    # --- 你要删除的记录ID，都放在这里 ---
    ids_to_delete = [119, 120]

    print("--- 任务一：开始删除指定记录 ---")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # 使用 '?' 占位符可以安全地处理列表
        # 'IN' 关键字可以一次性匹配列表中的所有ID
        placeholders = ', '.join('?' for _ in ids_to_delete)
        sql_query = f"DELETE FROM messages WHERE id IN ({placeholders})"

        cursor.execute(sql_query, ids_to_delete)

        # cursor.rowcount 会返回被删除的行数
        deleted_count = cursor.rowcount
        conn.commit()

        print(f">>> 成功删除 {deleted_count} 条记录 (ID: {ids_to_delete})。")

    except sqlite3.Error as e:
        print(f"[错误] 删除记录时发生错误: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def replace_all_backslashes():
    """
    任务二：查找所有 content 字段中的反斜杠(\)，并替换为正斜杠(/)。
    """
    print("\n--- 任务二：开始全局替换反斜杠 ---")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # 1. 找出所有 content 包含 '\' 的记录
        cursor.execute("SELECT id, content FROM messages WHERE content LIKE '%\\%'")
        rows_to_update = cursor.fetchall()

        if not rows_to_update:
            print(">>> 检查完毕，没有发现任何包含反斜杠的记录。")
            return

        print(f">>> 发现了 {len(rows_to_update)} 条记录需要更新，开始处理...")

        updated_count = 0
        for row in rows_to_update:
            record_id, original_content = row
            # 2. 在 Python 中执行替换
            new_content = original_content.replace('\\', '/')

            # 3. 写回数据库
            cursor.execute("UPDATE messages SET content = ? WHERE id = ?", (new_content, record_id))
            updated_count += 1
            print(f"  - 已更新 ID: {record_id}")

        conn.commit()
        print(f">>> 替换完毕！成功更新了 {updated_count} 条记录。")

    except sqlite3.Error as e:
        print(f"[错误] 替换反斜杠时发生错误: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# --- 主程序入口 ---
if __name__ == "__main__":
    print("====== 数据库维护程序启动 ======")
    delete_specific_records()
#    replace_all_backslashes()
    print("\n====== 所有维护任务已完成！ ======")
    print("--- 你现在可以用 check_db.py 来验证结果。 ---")