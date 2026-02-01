import requests
import os

# 目标地址 (您的新域名)
TARGET_URL = "https://api.kunigami-project-api.online"

print(f"--- 正在测试连接: {TARGET_URL} ---")

# 1. 尝试直连 (忽略系统代理)
session = requests.Session()
session.trust_env = False  # 【关键】强制不使用系统代理/VPN

try:
    print("1. 尝试【直连】(不走梯子)...")
    # 访问根目录，Worker 应该返回 404 (正常) 或 Hello World
    resp = session.get(TARGET_URL, timeout=10)
    print(f"   ✅ 连接成功！状态码: {resp.status_code}")
    print(f"   返回内容片段: {resp.text[:50]}")
    print("   -> 结论: 您的域名在国内可以直接访问，配置成功！")

except Exception as e:
    print(f"   ❌ 直连失败: {e}")

    # 2. 如果直连失败，尝试走系统代理 (如果有的话)
    print("\n2. 尝试【走系统代理】...")
    try:
        resp = requests.get(TARGET_URL, timeout=10)
        print(f"   ✅ 代理连接成功！状态码: {resp.status_code}")
    except Exception as e2:
        print(f"   ❌ 代理连接也失败: {e2}")

print("\n--- 测试结束 ---")