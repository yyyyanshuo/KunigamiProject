import sqlite3
from datetime import datetime

DATABASE_FILE = "../chat_history.db" # 注意，因为脚本在 scripts/ 文件夹里，所以要用 ../ 返回上一级
TABLE_NAME = "messages"

def fix_date_format_error():
    """
    查找所有 'YYYY-MM-d' 格式的错误时间戳，并将其修正为 'YYYY-MM-01'。
    """
    print("--- 数据库日期格式修复程序启动 ---")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # 1. 找出所有日期部分是 '-d ' 的错误记录
        #    LIKE '%-d %' 是一种模糊匹配，非常适合这个场景
        sql_query = f"SELECT id, timestamp FROM {TABLE_NAME} WHERE timestamp LIKE '%-d %'"
        cursor.execute(sql_query)
        rows_to_fix = cursor.fetchall()

        if not rows_to_fix:
            print(">>> 检查完毕，没有发现需要修复的记录。")
            return

        print(f">>> 发现了 {len(rows_to_fix)} 条日期格式错误的记录，开始修复...")
        
        fixed_count = 0
        for row in rows_to_fix:
            record_id, wrong_timestamp = row
            
            # 2. 在 Python 中执行简单的字符串替换
            #    我们假设所有的 '-d' 都应该是一号
            correct_timestamp = wrong_timestamp.replace('-d ', '-01 ')
            
            # 3. 写回数据库
            cursor.execute(f"UPDATE {TABLE_NAME} SET timestamp = ? WHERE id = ?", (correct_timestamp, record_id))
            print(f"  - ID: {record_id} | 原时间: {wrong_timestamp} -> 新时间: {correct_timestamp}")
            fixed_count += 1
        
        conn.commit()
        print(f"\n>>> 修复完毕！成功更新了 {fixed_count} 条记录。")

    except sqlite3.Error as e:
        print(f"[错误] 修复时发生错误: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    fix_date_format_error()
