import requests
import os
from dotenv import load_dotenv

load_dotenv()

# 1. 获取配置
API_KEY = os.getenv("GEMINI_API_KEY")
BASE_URL = os.getenv("GEMINI_BASE_URL") # 应该是 Cloudflare 地址
MODEL = "gemini-3-pro-preview" # 测试 Pro 模型

print(f"--- 正在测试 Google 官方连接 (付费/绑卡版) ---")
print(f"代理地址: {BASE_URL}")
print(f"模型: {MODEL}")
print(f"Key: {API_KEY[:5]}...")

# 2. 构造请求
url = f"{BASE_URL}/v1beta/models/{MODEL}:generateContent?key={API_KEY}"

payload = {
    "contents": [{
        "parts": [{"text": "你好，Gemini！我现在已经绑定了信用卡，你能收到我的消息吗？请回复一段简短的日语。"}]
    }]
}

try:
    # 发送请求
    response = requests.post(url, json=payload, timeout=30)

    print(f"\n状态码: {response.status_code}")

    if response.status_code == 200:
        result = response.json()
        text = result['candidates'][0]['content']['parts'][0]['text']
        print(f"✅ 成功！回复内容:\n{text}")
    else:
        print(f"❌ 失败: {response.text}")

except Exception as e:
    print(f"❌ 请求发生错误: {e}")