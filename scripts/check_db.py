import sqlite3
import os

# --- 【核心修正 1：路径感知】 ---
# 这段代码会自动计算出项目根目录的位置，无论脚本在哪里运行
# 1. 获取当前脚本文件所在的目录 (e.g., /path/to/project/scripts)
script_dir = os.path.dirname(os.path.abspath(__file__))
# 2. 获取上一级目录，也就是项目根目录 (e.g., /path/to/project)
project_root = os.path.dirname(script_dir)
# 3. 构造数据库文件的绝对路径
DATABASE_FILE = os.path.join(project_root, "chat_history.db")

TABLE_NAME = "messages"
RECORD_LIMIT = 20 # 设置我们想要查看的记录数量

print(f"--- 数据库检查程序 (v2.0) ---")
print(f"--- 目标数据库: {DATABASE_FILE} ---")

try:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # --- 【核心修正 2：精准查询】 ---
    # 1. 我们按照 ID 倒序排列，然后用 LIMIT 获取最新的 20 条记录
    sql_query = f"SELECT id, role, content, timestamp FROM {TABLE_NAME} ORDER BY id DESC LIMIT ?"
    
    print(f"--- 正在读取最后 {RECORD_LIMIT} 条记录 ---")
    cursor.execute(sql_query, (RECORD_LIMIT,))
    
    # fetchall() 会得到一个从最新到最旧的列表
    last_rows = cursor.fetchall()

    if not last_rows:
        print(">>> 数据库是空的，没有任何聊天记录。")
    else:
        # 2. 在 Python 中将列表反转，这样打印出来就是按时间正序了
        print(f">>> 成功读取到 {len(last_rows)} 条记录 (按时间顺序显示):")
        for row in reversed(last_rows):
            print("-" * 20)
            print(f"  ID: {row[0]}")
            print(f"  角色: {row[1]}")
            print(f"  内容: {row[2]}")
            print(f"  时间: {row[3]}")

except sqlite3.Error as e:
    print(f"[错误] 操作数据库时发生错误: {e}")
finally:
    if 'conn' in locals() and conn:
        conn.close()
        print("\n--- 数据库连接已关闭 ---")
