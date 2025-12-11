import sqlite3
import os
from datetime import datetime

# ================= 配置区域 =================
# 目标日期范围 (闭区间，包含首尾两天)
START_DATE = "2025-11-17"
END_DATE   = "2025-11-23"

# 角色名映射
ROLE_MAPPING = {
    "user": "桐奈",
    "assistant": "錬介"
}

# 数据库路径 (自动定位到上级目录的 chat.db)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "..", "chat.db")

# ===========================================

def export_chat():
    print(f"正在读取数据库: {os.path.abspath(DB_PATH)}")

    if not os.path.exists(DB_PATH):
        print("❌ 错误：找不到数据库文件！请确保在 scripts 目录下运行此脚本，或者数据库文件存在。")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 构造 SQL 查询
        # 注意：我们需要加上时间部分，以确保覆盖当天的 00:00:00 到 23:59:59
        query_start = f"{START_DATE} 00:00:00"
        query_end   = f"{END_DATE} 23:59:59"

        sql = """
        SELECT role, content, timestamp 
        FROM messages 
        WHERE timestamp >= ? AND timestamp <= ? 
        ORDER BY timestamp ASC
        """

        cursor.execute(sql, (query_start, query_end))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print(f"⚠️  {START_DATE} 到 {END_DATE} 期间没有找到聊天记录。")
            return

        print(f"✅ 找到 {len(rows)} 条记录，生成导出结果：\n")
        print("=" * 40)

        current_day_tracker = None

        for role, content, timestamp_str in rows:
            # timestamp_str 格式通常为 "2025-10-30 22:50:48"
            # 我们提取日期部分 "2025-10-30"
            try:
                msg_date = timestamp_str.split(' ')[0]
            except IndexError:
                msg_date = "未知日期"

            # 如果日期变化了，打印新的日期标题
            if msg_date != current_day_tracker:
                if current_day_tracker is not None:
                    print("") # 天与天之间空一行

                # 尝试解析星期几
                try:
                    dt = datetime.strptime(msg_date, "%Y-%m-%d")
                    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
                    date_header = f"📅 {msg_date} ({weekday})"
                except:
                    date_header = f"📅 {msg_date}"

                print(f"--- {date_header} ---")
                current_day_tracker = msg_date

            # 获取映射后的名字，默认保留原始 role
            name = ROLE_MAPPING.get(role, role)

            # 打印消息内容
            print(f"{name}：{content}")

        print("=" * 40)
        print("\n导出完成。")

    except sqlite3.Error as e:
        print(f"❌ 数据库错误: {e}")

if __name__ == "__main__":
    export_chat()