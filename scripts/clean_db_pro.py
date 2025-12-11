import sqlite3
import re
from datetime import datetime, timedelta

DATABASE_FILE = "chat.db"

def clean_and_correct_database():
    print("--- 数据库高级清洁程序启动 (v2.1) ---")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # 【核心修正】我们使用一个更精确、更宽容的 LIKE 模式！
        # '[`____-__-__ %:%`]' 匹配 YYYY-MM-DD 和时间
        # 最后的 '%' 匹配后面任意的字符，这才是关键！
        sql_query = "SELECT id, content FROM messages WHERE role = 'user' AND content LIKE '[____-__-__ %:%] %'"
        cursor.execute(sql_query)
        dirty_rows = cursor.fetchall()

        if not dirty_rows:
            print(">>> 检查完毕，没有发现需要清理和修正的记录。")
            return

        print(f">>> 发现了 {len(dirty_rows)} 条可能需要修正的 user 记录，开始处理...")
        
        updated_count = 0
        timestamp_pattern = re.compile(r'\[(\d{4}-\d{2}-\d{2})\s[A-Za-z]+\s(\d{2}:\d{2}:\d{2})\]')

        for row in dirty_rows:
            user_id, original_content = row
            
            match = timestamp_pattern.match(original_content)
            
            if match:
                date_part = match.group(1)
                time_part = match.group(2)
                correct_timestamp_str = f"{date_part} {time_part}"
                
                try:
                    correct_datetime = datetime.strptime(correct_timestamp_str, '%Y-%m-%d %H:%M:%S')
                    clean_content = original_content[match.end():].strip()
                    
                    print("-" * 20)
                    print(f"处理 User ID: {user_id}")
                    print(f"  原始内容: {original_content}")
                    print(f"  提取的时间: {correct_timestamp_str}")
                    print(f"  清理后内容: {clean_content}")

                    cursor.execute("UPDATE messages SET content = ?, timestamp = ? WHERE id = ?", 
                                   (clean_content, correct_timestamp_str, user_id))
                    print(f"  -> 已修正 User ID {user_id} 的内容和时间戳。")
                    updated_count += 1

                    assistant_id = user_id + 1
                    assistant_timestamp = correct_datetime + timedelta(seconds=1)
                    assistant_timestamp_str = assistant_timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    
                    cursor.execute("UPDATE messages SET timestamp = ? WHERE id = ? AND role = 'assistant'", 
                                   (assistant_timestamp_str, assistant_id))
                    
                    if cursor.rowcount > 0:
                        print(f"  -> 已修正 Assistant ID {assistant_id} 的时间戳为 {assistant_timestamp_str}。")
                        updated_count += 1
                
                except ValueError:
                    print(f"  [警告] ID {user_id} 的时间戳格式无法解析，跳过。")

        if updated_count > 0:
            conn.commit()
            print(f"\n>>> 清理完毕！总共更新了 {updated_count} 处数据。")
        else:
            print("\n>>> 检查完毕，所有记录的格式都是正确的，无需修正。")

    except sqlite3.Error as e:
        print(f"[错误] 操作数据库时发生错误: {e}")
    finally:
        if conn:
            conn.close()
            print("--- 数据库连接已关闭 ---")

if __name__ == "__main__":
    clean_and_correct_database()
