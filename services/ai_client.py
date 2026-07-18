import os
import json
import random
import requests
import traceback
from datetime import datetime

from core.config import (
    GEMINI_KEY, OPENROUTER_KEY, OPENROUTER_BASE_URL, OPENROUTER_BASE_URL_OLD,
    SILICONFLOW_KEY, USE_OPENROUTER, COS_BASE_URL, BASE_DIR, USERS_ROOT,
)
from core.context import (
    get_current_user_id, mark_api_fatal_error,
    GEMINI_FATAL_CODES, RELAY_FATAL_CODES,
)
from core.circuit_breaker import (
    check_circuit_breaker, record_fatal_error as record_cb_fatal,
    clear_circuit_breaker_info, set_circuit_breaker_info,
    check_relay_global_pause, mark_relay_global_pause, _is_cloudflare_block,
    reset_route_success,
)
from core.utils import get_effective_gemini_key, get_effective_openrouter_key

API_CONFIG_FILE = os.path.join(BASE_DIR, "configs", "api_settings.json")


def _write_user_log(user_id, text):
    if user_id:
        log_dir = os.path.join(USERS_ROOT, str(user_id), "logs")
    else:
        log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "api.log")

    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = []

    lines.append(text)
    if len(lines) > 200:
        lines = lines[-200:]

    with open(log_file, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _get_usage_log_file() -> str:
    user_id = get_current_user_id()
    if user_id:
        base = os.path.join(USERS_ROOT, str(user_id), "logs")
        os.makedirs(base, exist_ok=True)
    else:
        base = os.path.join(BASE_DIR, "logs")
    return os.path.join(base, "usage_history.json")


def log_full_prompt(service_name, messages, response_text=None, usage=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log_content = []
    log_content.append(f"\n{'='*20} [{timestamp}] {service_name} {'='*20}")

    for i, msg in enumerate(messages):
        role = msg.get('role', 'unknown').upper()
        content = msg.get('content', '')
        log_content.append(f"【{i}】<{role}>:\n{content}\n{'-'*30}")

    if response_text:
        log_content.append(f"\n【🤖 AI REPLY】:\n{response_text}")

    if usage:
        input_tokens = usage.get('promptTokenCount', 0)
        output_tokens = usage.get('candidatesTokenCount', 0)
        total_tokens = usage.get('totalTokenCount', 0)

        log_content.append(f"\n【💰 TOKEN BILL】:")
        log_content.append(f"   📥 输入(Prompt): {input_tokens}")
        log_content.append(f"   📤 输出(Reply):  {output_tokens}")
        log_content.append(f"   💎 总计(Total):  {total_tokens}")

    log_content.append(f"{'='*50}\n")

    final_log = "\n".join(log_content)

    print(final_log)

    try:
        user_id = get_current_user_id()
        _write_user_log(user_id, final_log)
    except Exception as e:
        print(f"FAILED TO WRITE API LOG: {e}")
        pass


def log_api_error(service_name, status_code, response_text, messages=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_content = [
        f"\n{'!'*20} API ERROR: {service_name} {'!'*20}",
        f"Time: {timestamp}",
        f"Status Code: {status_code}",
        f"Response: {response_text}" # 打印完整的原始响应主体（包括长 HTML），供后端详细排查
    ]
    if messages:
        log_content.append("--- Last Prompt Sent ---")
        for i, m in enumerate(messages[-3:]):
            log_content.append(f"[{m.get('role')}]: {m.get('content')[:200]}")

    final_log = "\n".join(log_content) + f"\n{'!'*50}\n"
    print(final_log)

    try:
        user_id = get_current_user_id()
        _write_user_log(user_id, final_log)
    except:
        pass


def record_token_usage(char_id, model, input_tokens, output_tokens, total_tokens):
    try:
        usage_file = _get_usage_log_file()

        logs = []
        if os.path.exists(usage_file):
            with open(usage_file, "r", encoding="utf-8") as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []

        new_entry = {
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "char_id": char_id,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens
        }
        logs.append(new_entry)

        if len(logs) > 50:
            logs = logs[-50:]

        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"Log Usage Error: {e}")


def get_relay_provider(user_id=None):
    if user_id is None:
        user_id = get_current_user_id()

    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE

    if not os.path.exists(api_cfg_file):
        return "old"

    try:
        with open(api_cfg_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        relay_config = config.get("routes", {}).get("relay", {})
        provider = relay_config.get("relay_provider", "old")

        if provider == "custom":
            custom_url = relay_config.get("relay_custom_url")
            if custom_url:
                return custom_url

        return provider
    except:
        return "old"


def call_openrouter(messages, char_id="unknown", model_name="gpt-3.5-turbo", user_id=None, max_tokens=4096):
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0"
    ]

    log_full_prompt(f"OpenRouter ({model_name})", messages)

    relay_provider = get_relay_provider(user_id)
    if relay_provider.startswith("http://") or relay_provider.startswith("https://"):
        base_url = relay_provider
        print(f"--- [Debug] Using CUSTOM relay provider: {base_url}")
    elif relay_provider == "old":
        base_url = OPENROUTER_BASE_URL_OLD
        print(f"--- [Debug] Using OLD relay provider: {base_url}")
    else:
        base_url = OPENROUTER_BASE_URL
        print(f"--- [Debug] Using NEW relay provider: {base_url}")

    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {get_effective_openrouter_key(user_id=user_id)}",
        "Content-Type": "application/json",
    }

    # 只有在使用 OpenRouter 官方渠道时，才附加 OpenRouter 要求的特定 Header（如 Referer/X-Title）
    # 避免在第三方通用中转商（如 Api2D、VG 等）中因携带非期 Origin/Referer 被 Cloudflare WAF 安全策略拦截返回 403
    if "openrouter.ai" in base_url.lower():
        headers["HTTP-Referer"] = "https://kunigami-project-api.online/"
        headers["X-Title"] = "Kunigami Project"

    final_messages = []
    system_contents = []
    for m in messages:
        if m.get('role') == 'system':
            system_contents.append(m.get('content', ''))
        else:
            final_messages.append(m)

    if system_contents:
        merged_system = {"role": "system", "content": "\n\n".join(system_contents)}
        final_messages.insert(0, merged_system)

    payload = {
        "model": model_name,
        "messages": final_messages,
        "temperature": 1,
        "max_tokens": max_tokens
    }

    print(f"--- [Debug] Calling Compatible API at: {url}")
    print(f"--- [Debug] Using model: {payload['model']}")

    clear_circuit_breaker_info()

    gcb = check_relay_global_pause()
    if gcb:
        set_circuit_breaker_info(gcb)
        return f"（系统提示：{gcb['message']}）"

    _uid = user_id or get_current_user_id()
    if _uid:
        cb = check_circuit_breaker(_uid, "relay")
        if cb:
            set_circuit_breaker_info(cb)
            return f"（系统提示：{cb['message']}）"

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=300)

        if r.status_code != 200:
            log_api_error(f"OpenRouter ({model_name})", r.status_code, r.text, messages=messages)

            # 检测 Cloudflare WAF HTML 页面：触发全局暂停
            if r.status_code == 403 and _is_cloudflare_block(r.text):
                mark_relay_global_pause()
                set_circuit_breaker_info({
                    "type": "cooldown",
                    "message": "中转服务器触发安全拦截（Cloudflare），已暂停所有中转请求 3 分钟。",
                    "remaining_seconds": 180,
                })
                return "（系统提示：中转服务器触发安全拦截，已暂停所有中转请求 3 分钟，请稍后重试。）"

            if r.status_code in RELAY_FATAL_CODES:
                mark_api_fatal_error("relay", r.status_code)
                if _uid:
                    cb = record_cb_fatal(_uid, "relay", r.status_code)
                    if cb:
                        set_circuit_breaker_info(cb)

            if r.status_code == 401:
                return "（系统提示：身份验证失败。请检查是否在【个人主页-账号与通知设置-openrouter】中正确填写了 API Key。）"
            elif r.status_code == 402:
                return "（系统提示：账户点数不足，请前往 API 网站充值。）"
            elif r.status_code == 403:
                return "（系统提示：请正确填写模型代码，或检查 API Key 权限范围。）"
            elif r.status_code == 524:
                return "（系统提示：请求超时，AI 思考时间过长。请尝试缩短当前聊天内容或精简人设设定。）"
            elif r.status_code == 525:
                return "（系统提示：中转服务器连接异常，请稍后再试或联系管理员。）"

            # 非致命错误：提取 JSON 报错详情附加到提示中（不影响熔断计数）
            api_detail = None
            try:
                err_data = r.json()
                if isinstance(err_data, dict):
                    api_detail = err_data.get("message") or err_data.get("error", {}).get("message")
            except:
                pass

            if r.status_code == 429:
                return "（系统提示：请求过于频繁，AI 累了，请休息一分钟再聊哦。）"
            elif r.status_code >= 500:
                detail = api_detail or f"AI 服务商目前繁忙（{r.status_code}），请稍后再试。"
                return f"（系统提示：{detail}）"
            else:
                detail = api_detail or f"服务连接异常，错误码: {r.status_code}"
                return f"（系统提示：{detail}）"

        try:
            result = r.json()
        except Exception as parse_err:
            log_api_error(f"OpenRouter ({model_name})", "JSON_PARSE_ERROR", r.text, messages=messages)
            return "（系统提示：AI 返回了无法解析的异常信号，请重试。）"

        if "error" in result:
            err_msg = result["error"].get("message", "Unknown error")
            log_api_error(f"OpenRouter ({model_name})", "API_INTERNAL_ERROR", str(result["error"]), messages=messages)
            return f"（系统提示：AI 服务返回内部错误: {err_msg}）"

        if 'usage' in result:
            usage = result['usage']
            record_token_usage(
                char_id,
                model_name,
                usage.get('prompt_tokens', 0),
                usage.get('completion_tokens', 0),
                usage.get('total_tokens', 0)
            )

        if "choices" not in result or len(result["choices"]) == 0:
            print(f"⚠️ [Empty Response] API 返回了空列表。")
            return "（系统提示：AI 暂时陷入了沉思，请换个话题试试。）"

        import json as _json
        print("🔍 [DEBUG] API 原始完整响应:")
        print(_json.dumps(result, indent=2, ensure_ascii=False))

        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            print(f"⚠️ [Parse Error] 无法解析响应结构: {e}")
            return "（系统提示：数据结构解析失败，请重试。）"

        finish_reason = result["choices"][0].get("finish_reason", "")
        if finish_reason and finish_reason != "stop":
            print(f"⚠️ [Truncation] OpenRouter finish_reason={finish_reason}, 回复可能被截断 (max_tokens={max_tokens})")

        log_full_prompt(f"OpenRouter ({model_name})", messages, response_text=content)

        if _uid:
            reset_route_success(_uid, "relay")
        return content

    except requests.exceptions.Timeout:
        return "（系统提示：连接 AI 服务器超时，对方思考得太久了，请稍后重试。）"
    except Exception as e:
        print(f"[ERROR] API 调用异常: {e}\n{traceback.format_exc()}")
        return "（系统提示：网络链路不稳定，请稍后再试。）"


def call_gemini(messages, char_id="unknown", model_name="gemini-2.0-flash", user_id=None):
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
    api_key = get_effective_gemini_key(user_id=user_id)
    url = f"{base_url}/v1beta/models/{model_name}:generateContent?key={api_key}"

    gemini_contents = []
    system_parts = []

    for msg in messages:
        if msg['role'] == 'system':
            system_parts.append(msg['content'])
        else:
            role = 'model' if msg['role'] == 'assistant' else 'user'
            gemini_contents.append({"role": role, "parts": [{"text": msg['content']}]})

    system_instruction = None
    if system_parts:
        system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]}

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    }

    payload = {
        "contents": gemini_contents,
        "generationConfig": {
            "temperature": 1,
            "maxOutputTokens": 4096
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    import time
    max_retries = 2
    r = None

    clear_circuit_breaker_info()
    _uid = user_id or get_current_user_id()
    if _uid:
        cb = check_circuit_breaker(_uid, "gemini")
        if cb:
            set_circuit_breaker_info(cb)
            return f"（系统提示：{cb['message']}）"

    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, headers=headers, proxies={"http": None, "https": None}, timeout=30)

            if r.status_code == 200:
                break

            # 如果是临时服务载荷过载 (503), 超时 (504) 或内部服务器错误 (500)
            if r.status_code in [500, 503, 504]:
                if attempt < max_retries - 1:
                    sleep_time = min(8, 2 ** attempt)
                    print(f"⚠️ [Gemini {r.status_code}] 谷歌服务端临时故障。第 {attempt+1}/{max_retries} 次尝试失败，{sleep_time} 秒后自动重试...")
                    time.sleep(sleep_time)
                    continue

            # 对于其它不可恢复的状态码（如 400, 403, 429）或已达到最大重试次数，执行以下退出逻辑
            log_api_error(f"Gemini {model_name}", r.status_code, r.text, messages=messages)

            if r.status_code in GEMINI_FATAL_CODES:
                mark_api_fatal_error("gemini", r.status_code)
                if _uid:
                    cb = record_cb_fatal(_uid, "gemini", r.status_code)
                    if cb:
                        set_circuit_breaker_info(cb)

            if r.status_code == 400:
                return "（系统提示：请求参数异常（400），请联系管理员检查配置。）"
            elif r.status_code == 403:
                return "（系统提示：访问被拒绝（403）。请检查是否在【个人主页-账号与通知设置-gemini】中正确填写了 API Key。）"
            elif r.status_code == 429:
                return "（系统提示：请求过于频繁（429），谷歌端限制了访问频率，请发慢一点哦。）"
            elif r.status_code in [500, 504]:
                return f"（系统提示：服务器响应超时或内部错误（{r.status_code}），请尝试精简聊天内容或缩减人设设定。）"
            elif r.status_code == 503:
                return "（系统提示：谷歌服务端当前过载（503），请稍后再试。）"
            else:
                return f"（系统提示：AI 暂时无法连接，错误码: {r.status_code}）"

        except requests.exceptions.Timeout as t_err:
            if attempt < max_retries - 1:
                sleep_time = min(8, 2 ** attempt)
                print(f"⚠️ [Gemini Timeout] 连接超时。第 {attempt+1}/{max_retries} 次尝试失败，{sleep_time} 秒后自动重试...")
                time.sleep(sleep_time)
                continue
            return "（系统提示：AI 思考太久啦，连接超时，请重试。）"
        except Exception as e:
            if attempt < max_retries - 1:
                sleep_time = min(8, 2 ** attempt)
                print(f"⚠️ [Gemini Exception] 未知连接异常: {e}。第 {attempt+1}/{max_retries} 次尝试失败，{sleep_time} 秒后自动重试...")
                time.sleep(sleep_time)
                continue
            print(f"🔥 [Gemini 未知异常]: {e}")
            return "（系统提示：网络连接波动，请稍后再试。）"

    # 如果成功获取到了 200 响应
    if r and r.status_code == 200:
        try:
            result = r.json()
        except Exception as parse_err:
            log_api_error(f"Gemini {model_name}", "JSON_PARSE_ERROR", r.text, messages=messages)
            return "（系统提示：接收到了异常信号，请重试。）"

        if "error" in result:
            err_info = str(result["error"])
            log_api_error(f"Gemini {model_name}", "API_INTERNAL_ERROR", err_info, messages=messages)
            return f"（系统提示：API 内部错误: {result['error'].get('message', 'Unknown')}）"

        token_usage = result.get('usageMetadata', {})
        if token_usage:
            record_token_usage(
                char_id,
                model_name,
                token_usage.get('promptTokenCount', 0),
                token_usage.get('candidatesTokenCount', 0),
                token_usage.get('totalTokenCount', 0)
            )

        if 'candidates' not in result or not result['candidates']:
            return "（AI 陷入了沉默，没有给出回复。）"

        candidate = result['candidates'][0]
        text = ""

        try:
            if 'content' in candidate and 'parts' in candidate['content']:
                text = candidate['content']['parts'][0]['text']
            else:
                finish_reason = candidate.get('finishReason', 'UNKNOWN')
                text = f"（由于系统限制，AI 无法生成此段对话。原因: {finish_reason}）"
        except (KeyError, IndexError, TypeError) as e:
            print(f"⚠️ [Gemini 解析错误]: {e}")
            return "（系统提示：回复解析失败。）"

        log_full_prompt(f"Gemini Interaction ({model_name})", messages, response_text=text, usage=token_usage)

        if _uid:
            reset_route_success(_uid, "gemini")
        return text


def get_model_config(task_type="chat", user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    if user_id is None:
        user_id = get_current_user_id()

    if user_id:
        user_cfg_dir = os.path.join(USERS_ROOT, str(user_id), "configs")
        api_cfg_file = os.path.join(user_cfg_dir, "api_settings.json")
    else:
        api_cfg_file = API_CONFIG_FILE

    if not os.path.exists(api_cfg_file):
        return "relay", "gpt-3.5-turbo"

    try:
        with open(api_cfg_file, "r", encoding="utf-8") as f:
            config = json.load(f)

        route = config.get("active_route", "gemini")
        models = config.get("routes", {}).get(route, {}).get("models", {})
        if task_type == "moments" and "moments" not in models:
            model_name = models.get("chat", "gpt-3.5-turbo")
        elif task_type == "translation" and "translation" not in models:
            model_name = models.get("chat", "gpt-3.5-turbo")
        elif task_type == "summary" and "summary" not in models:
            model_name = models.get("chat", "gpt-3.5-turbo")
        elif task_type == "forum" and "forum" not in models:
            model_name = models.get("chat", "gpt-3.5-turbo")
        else:
            model_name = models.get(task_type, "gpt-3.5-turbo")

        return route, model_name
    except:
        return "gemini", "gemini-2.5-pro"
