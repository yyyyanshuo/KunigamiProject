import sqlite3

DATABASE_FILE = "chat_history.db"

# --- 我们的手术目标 ---
TARGET_ID = 121
CORRECT_TIMESTAMP = '2025-11-01 19:59:34'

def run_specific_fix():
    """
    根据设定的目标 ID 和正确的时间戳，精准地修正一条记录。
    """
    print("--- 数据库单条记录精准修复程序启动 ---")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        print(f">>> 准备将 ID: {TARGET_ID} 的时间戳修正为 -> '{CORRECT_TIMESTAMP}'")
        
        # 先检查一下这条记录是否存在
        cursor.execute("SELECT content FROM messages WHERE id = ?", (TARGET_ID,))
        record = cursor.fetchone()

        if record is None:
            print(f"[错误] 找不到 ID 为 {TARGET_ID} 的记录，操作已取消。")
            return

        print(f"  - 找到了记录，内容为: '{record[0]}'")

        # 执行更新操作
        cursor.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (CORRECT_TIMESTAMP, TARGET_ID))
        
        # 提交更改
        conn.commit()
        
        print(f"\n>>> 修复完毕！ID {TARGET_ID} 的时间戳已成功更新。")
        print("--- 你现在可以用 check_db.py 来验证结果。 ---")

    except sqlite3.Error as e:
        print(f"[错误] 操作数据库时发生错误: {e}")
        if conn:
            conn.rollback()
            print("--- 由于发生错误，所有更改已被回滚。---")
    finally:
        if conn:
            conn.close()
            print("--- 数据库连接已关闭 ---")

# 当直接运行这个文件时，执行修复函数
if __name__ == "__main__":
    run_specific_fix()
