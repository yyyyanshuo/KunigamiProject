import requests
import os
from dotenv import load_dotenv

load_dotenv()

# 读取配置
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://vg.v1api.cc/v1")
API_KEY = os.getenv("OPENROUTER_KEY", "")
# 这里的模型改成一个绝对存在的，防止因模型名错误导致的断连
TEST_MODEL = "gpt-3.5-turbo"

print(f"--- 网络诊断开始 ---")
print(f"目标地址: {BASE_URL}")
print(f"API Key (前5位): {API_KEY[:5]}...")

def test_connection(name, proxies=None, verify=True, use_http=False):
    print(f"\n🧪 测试方案 [{name}]...")

    target_url = BASE_URL
    if use_http:
        target_url = target_url.replace("https://", "http://")
        print(f"   -> 尝试降级为 HTTP: {target_url}")

    full_url = f"{target_url}/chat/completions"

    payload = {
        "model": TEST_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5
    }

    try:
        resp = requests.post(
            full_url,
            json=payload,
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
            proxies=proxies,
            verify=verify
        )
        print(f"   ✅ 连接成功! 状态码: {resp.status_code}")
        if resp.status_code == 200:
            print(f"   🎉 回复内容: {resp.json()['choices'][0]['message']['content']}")
            return True
        else:
            print(f"   ⚠️ 服务器返回错误: {resp.text[:100]}")
    except Exception as e:
        print(f"   ❌ 失败: {str(e)[:150]}...")
    return False

# --- 开始测试 ---

# 1. 直连测试 (默认)
test_connection("直连 (默认配置)")

# 2. 忽略证书测试
test_connection("忽略 SSL 证书", verify=False)

# 3. 强制 HTTP 测试 (绕过 SSL)
test_connection("强制 HTTP", use_http=True)

# 4. 尝试检测本地代理 (如果您开了 VPN)
proxies = {
    "http": "http://127.0.0.1:10090",
    "https": "http://127.0.0.1:10090",
}
test_connection("尝试本地代理 (端口10090)", proxies=proxies, verify=False)

print("\n--- 诊断结束 ---")