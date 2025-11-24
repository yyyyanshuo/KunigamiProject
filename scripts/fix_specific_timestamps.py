import sqlite3

DATABASE_FILE = "chat_history.db"

# --- 我们的手术任务清单 ---
# 格式: (要修改的记录ID, "新的正确时间戳")
corrections = [
    (31, '2025-10-31 13:00:00'),
    (32, '2025-10-31 13:00:01'), # 助手比用户晚1秒
    (33, '2025-10-31 13:03:00'),
    (34, '2025-10-31 13:03:01')  # 助手比用户晚1秒
]

def run_specific_fixes():
    """
    根据 `corrections` 列表，精准地修正特定记录的时间戳。
    """
    print("--- 数据库精准修复程序启动 ---")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        print(f">>> 准备执行 {len(corrections)} 项修复任务...")

        for record_id, new_timestamp in corrections:
            print(f"  - 正在将 ID: {record_id} 的时间戳更新为 -> {new_timestamp}")
            cursor.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (new_timestamp, record_id))
        
        # 提交所有更改
        conn.commit()
        print(f"\n>>> 修复完毕！成功提交了 {cursor.rowcount * len(corrections)} 项（估算）更改。")
        print("--- 你现在可以用 check_db.py 来验证结果。 ---")

    except sqlite3.Error as e:
        print(f"[错误] 操作数据库时发生错误: {e}")
        # 如果出错，回滚所有未提交的更改
        if conn:
            conn.rollback()
            print("--- 由于发生错误，所有更改已被回滚。---")
    finally:
        if conn:
            conn.close()
            print("--- 数据库连接已关闭 ---")

# 当直接运行这个文件时，执行修复函数
if __name__ == "__main__":
    run_specific_fixes()
