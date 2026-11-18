"""Protocol adapters for official providers and user-selected custom endpoints."""
import base64
import json
from urllib.parse import quote
import requests
from core import public_https
from core.runtime import LOCAL_TEST
from core.context import get_current_user_id, mark_api_fatal_error
from core.credentials import get_user_credential, CredentialError
from core.circuit_breaker import (check_circuit_breaker, record_fatal_error,
    reset_route_success, clear_circuit_breaker_info, set_circuit_breaker_info)
from core.model_settings import load_config, validate_base_url, model_settings_lock


def connection(user_id=None):
    uid = user_id or get_current_user_id()
    with model_settings_lock(uid):
        config = load_config(uid)
        route = config['active_route']
        info = config['routes'][route]
        key = get_user_credential(uid, info['credential']) if uid else ''
    # User-selected endpoints never receive an operator's fallback key.
    base = validate_base_url(info['base_url'])
    if not key:
        raise ValueError('Missing API key')
    return route, info['protocol'], base, key


def _blocks(content, protocol, role='user'):
    if isinstance(content, str):
        return content
    out = []
    for part in content:
        if part.get('type') == 'text':
            out.append({'type': ('output_text' if role == 'assistant' else 'input_text') if protocol == 'openai_responses' else 'text', 'text': part['text']})
        elif part.get('type') == 'image_url':
            url = part['image_url']['url']
            if protocol == 'anthropic':
                if url.startswith('data:'):
                    metadata, data = url.split(',', 1)
                    source = {'type': 'base64', 'media_type': metadata[5:].split(';')[0], 'data': data}
                else:
                    source = {'type': 'url', 'url': url}
                out.append({'type': 'image', 'source': source})
            elif protocol == 'openai_responses':
                out.append({'type': 'input_image', 'image_url': url})
            else:
                out.append(part)
    return out


def _gemini_parts(content):
    if isinstance(content, str):
        return [{'text': content}]
    parts = []
    for block in content:
        if block.get('type') == 'text':
            parts.append({'text': block['text']})
        elif block.get('type') == 'image_url':
            url = block['image_url']['url']
            if url.startswith('data:'):
                meta, data = url.split(',', 1)
                parts.append({'inlineData': {'mimeType': meta[5:].split(';')[0], 'data': data}})
            else:
                parts.append({'fileData': {'mimeType': 'image/jpeg', 'fileUri': url}})
    return parts


def build_request(protocol, base, key, model, messages, max_tokens=4096, temperature=1, stream=False):
    headers = {'Content-Type': 'application/json'}
    systems = '\n\n'.join(str(m['content']) for m in messages if m['role'] in ('system', 'developer'))
    normal = [m for m in messages if m['role'] not in ('system', 'developer')]
    if protocol == 'anthropic':
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        payload = {'model': model, 'messages': [{'role': m['role'], 'content': _blocks(m['content'], protocol, m['role'])} for m in normal], 'max_tokens': max_tokens}
        if systems:
            payload['system'] = systems
        endpoint = '/messages'
    elif protocol == 'gemini':
        headers['x-goog-api-key'] = key
        payload = {'contents': [{'role': 'model' if m['role'] == 'assistant' else 'user', 'parts': _gemini_parts(m['content'])} for m in normal], 'generationConfig': {'maxOutputTokens': max_tokens, 'temperature': temperature}}
        if systems:
            payload['systemInstruction'] = {'parts': [{'text': systems}]}
        # Accept either the API origin or an explicitly versioned Gemini base.
        version = '' if base.endswith(('/v1beta', '/v1')) else '/v1beta'
        endpoint = version + '/models/' + quote(model, safe='') + (':streamGenerateContent?alt=sse' if stream else ':generateContent')
    elif protocol == 'openai_responses':
        headers['Authorization'] = 'Bearer ' + key
        payload = {'model': model, 'input': [{'role': m['role'], 'content': _blocks(m['content'], protocol, m['role'])} for m in normal], 'max_output_tokens': max_tokens, 'store': False}
        if systems:
            payload['instructions'] = systems
        if model.startswith(('gpt-5', 'gpt-6')):
            payload['reasoning'] = {'effort': 'low'}
        endpoint = '/responses'
    else:
        headers['Authorization'] = 'Bearer ' + key
        payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature}
        endpoint = '/chat/completions'
    if stream and protocol != 'gemini':
        payload['stream'] = True
        if protocol == 'openai_chat':
            payload['stream_options'] = {'include_usage': True}
    return base.rstrip('/') + endpoint, headers, payload


def parse_response(protocol, data):
    if protocol == 'anthropic':
        text = ''.join(p.get('text', '') for p in data.get('content', []) if p.get('type') == 'text')
        finish = data.get('stop_reason', '')
        return text, finish, finish in ('end_turn', 'stop_sequence'), data.get('usage', {})
    if protocol == 'openai_responses':
        text = ''.join(p.get('text', '') for item in data.get('output', []) if item.get('type') == 'message' for p in item.get('content', []) if p.get('type') == 'output_text')
        status = data.get('status', '')
        return text, (data.get('incomplete_details') or {}).get('reason', status), status == 'completed', data.get('usage', {})
    if protocol == 'gemini':
        candidate = (data.get('candidates') or [{}])[0]
        text = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', []) if not p.get('thought'))
        finish = candidate.get('finishReason', '')
        return text, finish, finish == 'STOP', data.get('usageMetadata', {})
    choice = (data.get('choices') or [{}])[0]
    finish = choice.get('finish_reason', '')
    return choice.get('message', {}).get('content') or '', finish, finish == 'stop', data.get('usage', {})


def _usage(char_id, model, usage, user_id=None):
    from services.ai_client import record_token_usage
    input_tokens = usage.get('input_tokens', usage.get('prompt_tokens', usage.get('promptTokenCount', 0))) or 0
    output_tokens = usage.get('output_tokens', usage.get('completion_tokens', usage.get('candidatesTokenCount', 0))) or 0
    record_token_usage(char_id, model, input_tokens, output_tokens, input_tokens + output_tokens, user_id=user_id)


def _model_response(route, url, headers, payload):
    options = {'headers': headers, 'json': payload, 'timeout': (15, 180)}
    if route == 'custom':
        return public_https.open_response('POST', url, **options)
    # Official origins are fixed by normalize_config. Preserve their configured transport.
    return requests.post(url, allow_redirects=False, stream=True, **options)


def _image_bytes(encoded):
    maximum = public_https.IMAGE_LIMIT
    if len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError('Image too large')
    data = base64.b64decode(encoded, validate=True)
    if len(data) > maximum:
        raise ValueError('Image too large')
    return data


def call_provider(messages, char_id='unknown', model_name='', user_id=None, max_tokens=4096, temperature=1):
    from services.ai_client import AIResponseText, ai_error_text
    if LOCAL_TEST:
        return AIResponseText('【本地模拟回复 MOCK】已收到消息；未调用真实模型。')
    route = ''
    try:
        route, protocol, base, key = connection(user_id)
        uid = user_id or get_current_user_id()
        clear_circuit_breaker_info()
        breaker = check_circuit_breaker(uid, route) if uid else None
        if breaker:
            set_circuit_breaker_info(breaker)
            return ai_error_text('', code='circuit_breaker', provider=route, status_code=503)
        if not model_name:
            raise ValueError('Missing model')
        url, headers, payload = build_request(protocol, base, key, model_name, messages, max_tokens, temperature)
        with _model_response(route, url, headers, payload) as response:
            if response.status_code != 200:
                if response.status_code in (400, 401, 402, 403):
                    mark_api_fatal_error(route, response.status_code)
                    if uid:
                        set_circuit_breaker_info(record_fatal_error(uid, route, response.status_code))
                return ai_error_text('', code='authentication_failed' if response.status_code == 401 else 'provider_http_error', provider=route, status_code=response.status_code if response.status_code >= 400 else 502, retryable=response.status_code in (429, 500, 502, 503, 504))
            data = json.loads(b''.join(public_https.limited_chunks(response, public_https.MODEL_LIMIT)))
        text, finish, complete, usage = parse_response(protocol, data)
        _usage(char_id, model_name, usage, user_id)
        if not text:
            return ai_error_text('', code='empty_response', provider=route, status_code=502)
        if uid:
            reset_route_success(uid, route)
        return AIResponseText(text, complete=complete, finish_reason=finish, provider=route)
    except requests.Timeout:
        return ai_error_text('', code='ai_timeout', status_code=504, retryable=True, provider=route)
    except (requests.RequestException, ValueError, KeyError, TypeError, CredentialError):
        return ai_error_text('', code='provider_request_failed', status_code=503, provider=route)


def stream_provider(messages, model_name, user_id=None, max_tokens=8192):
    if LOCAL_TEST:
        yield '【本地模拟回复 MOCK】未调用真实模型。'
        return
    route, protocol, base, key = connection(user_id)
    url, headers, payload = build_request(protocol, base, key, model_name, messages, max_tokens, stream=True)
    uid = user_id or get_current_user_id()
    clear_circuit_breaker_info()
    breaker = check_circuit_breaker(uid, route) if uid else None
    if breaker:
        set_circuit_breaker_info(breaker)
        raise ValueError('AI connection is cooling down')
    finished = False
    usage = {}
    with _model_response(route, url, headers, payload) as response:
        if response.status_code != 200:
            if response.status_code in (400, 401, 402, 403):
                mark_api_fatal_error(route, response.status_code)
                if uid:
                    set_circuit_breaker_info(record_fatal_error(uid, route, response.status_code))
            raise ValueError('AI service unavailable')
        for line in public_https.event_lines(response):
            if not line or not line.startswith(b'data:'):
                continue
            value = line[5:].strip()
            if value == b'[DONE]':
                finished = True
                break
            data = json.loads(value)
            kind = data.get('type', '')
            if data.get('error') or kind in ('error', 'response.failed', 'response.incomplete'):
                raise ValueError('AI stream interrupted')
            text = ''
            if protocol == 'anthropic':
                usage.update(data.get('usage', {}))
                usage.update(data.get('message', {}).get('usage', {}))
                if kind == 'message_stop':
                    finished = True
                if kind == 'content_block_delta' and data.get('delta', {}).get('type') == 'text_delta':
                    text = data['delta']['text']
                if kind == 'message_delta' and data.get('delta', {}).get('stop_reason') == 'max_tokens':
                    raise ValueError('AI stream truncated')
            elif protocol == 'openai_responses':
                if kind == 'response.output_text.delta':
                    text = data.get('delta', '')
                elif kind == 'response.completed':
                    usage = data.get('response', {}).get('usage', {})
                    finished = True
            elif protocol == 'gemini':
                candidate = (data.get('candidates') or [{}])[0]
                text = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', []) if not p.get('thought'))
                usage.update(data.get('usageMetadata', {}))
                if candidate.get('finishReason') == 'STOP':
                    finished = True
                if candidate.get('finishReason') not in (None, 'STOP'):
                    raise ValueError('AI stream interrupted')
            else:
                choice = (data.get('choices') or [{}])[0]
                text = choice.get('delta', {}).get('content', '')
                if choice.get('finish_reason') not in (None, 'stop'):
                    raise ValueError('AI stream truncated')
                if choice.get('finish_reason') == 'stop':
                    finished = True
                usage.update(data.get('usage') or {})
            if text:
                yield text
    if not finished:
        raise ValueError('AI stream interrupted')
    if usage:
        _usage('forum_generator', model_name, usage, user_id)
    if uid:
        reset_route_success(uid, route)


def generate_image_bytes(prompt, model, user_id=None):
    """No cross-provider fallback; an empty model explicitly disables generation."""
    if LOCAL_TEST or not model:
        return None
    route, protocol, base, key = connection(user_id)
    if protocol == 'anthropic':
        return None
    if protocol == 'gemini':
        url, headers, payload = build_request(protocol, base, key, model, [{'role': 'user', 'content': prompt}])
        payload['generationConfig'] = {'responseModalities': ['TEXT', 'IMAGE']}
        if model.startswith('imagen-'):
            url = url.rsplit(':', 1)[0] + ':predict'
            payload = {'instances': [{'prompt': prompt}], 'parameters': {'sampleCount': 1}}
    else:
        url = base + '/images/generations'
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
        payload = {'model': model, 'prompt': prompt, 'n': 1}
        if route == 'xai':
            payload['response_format'] = 'b64_json'
    with _model_response(route, url, headers, payload) as response:
        if response.status_code != 200:
            return None
        data = json.loads(b''.join(public_https.limited_chunks(response, public_https.IMAGE_JSON_LIMIT)))
    if protocol == 'gemini':
        if model.startswith('imagen-'):
            encoded = (data.get('predictions') or [{}])[0].get('bytesBase64Encoded')
            return _image_bytes(encoded) if encoded else None
        for part in (data.get('candidates') or [{}])[0].get('content', {}).get('parts', []):
            inline = part.get('inlineData', {})
            if inline.get('data'):
                return _image_bytes(inline['data'])
    else:
        item = (data.get('data') or [{}])[0]
        if item.get('b64_json'):
            return _image_bytes(item['b64_json'])
        if item.get('url'):
            # Download the generated result without forwarding provider credentials.
            image_url = item['url']
            with public_https.open_response('GET', image_url, timeout=(15, 60)) as image:
                if image.status_code == 200:
                    return b''.join(public_https.limited_chunks(image, public_https.IMAGE_LIMIT))
    return None
