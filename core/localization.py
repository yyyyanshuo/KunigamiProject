"""UI localization only. Never translates stored user/model content."""
from __future__ import annotations
import json
import html
import re
from pathlib import Path
from flask import has_request_context, request, session, jsonify, g
from jinja2.ext import Extension
from core.runtime import SOURCE_ROOT, DATA_ROOT

SUPPORTED = ('ja', 'en', 'zh-CN')
CATALOG = json.loads((SOURCE_ROOT / 'static/locales/ui.json').read_text(encoding='utf-8'))


def normalize_ui_language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    if value.startswith('zh'): return 'zh-CN'
    if value.split('-')[0] in ('ja', 'en'): return value.split('-')[0]
    return None


def settings_path(user_id):
    if user_id is None: return Path(DATA_ROOT) / 'configs/user_settings.json'
    return Path(DATA_ROOT) / 'users' / str(int(user_id)) / 'configs/user_settings.json'


def read_settings(user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    try:
        data = json.loads(settings_path(user_id).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError): return {}


def save_ui_language(user_id, language):
    from core.json_storage import update_user_settings
    update_user_settings(settings_path(user_id), {'user_language': language})
    if has_request_context(): g.pop('saki_language_cache', None)


def user_language(user_id=None):
    """Account wins unless the visitor explicitly chose a language before login."""
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    cache = getattr(g, "saki_language_cache", {}) if has_request_context() else {}
    if user_id in cache: return cache[user_id]
    value = normalize_ui_language(read_settings(user_id).get('user_language'))
    if value:
        if has_request_context():
            cache[user_id] = value
            g.saki_language_cache = cache
        return value
    if has_request_context():
        pending = normalize_ui_language(session.get('pending_ui_language'))
        if pending: return pending
        for language, quality in request.accept_languages:
            normalized = normalize_ui_language(language)
            if quality > 0 and normalized: return normalized
    return 'ja'


def reconcile_login_language(user_id):
    pending = normalize_ui_language(session.pop('pending_ui_language', None))
    existing = normalize_ui_language(read_settings(user_id).get('user_language'))
    if pending or not existing:
        save_ui_language(user_id, pending or user_language(user_id))


def tr(text, language=None, **values):
    language = normalize_ui_language(language) or user_language()
    entry = CATALOG.get(text)
    result = entry.get(language, text) if entry else text
    if language == 'zh-CN':
        result = re.sub(r'\s*[（(](?=[^()（）]*[A-Za-z])[A-Za-z0-9\s/_.:,=+\-–—]+[)）]', '', result)
    result = re.sub(r'\{(\w+)\}', lambda match: str(values[match[1]]) if match[1] in values else match[0], result)
    if not entry and '\n' in text:
        return '\n'.join(tr(line, language, **values) for line in text.split('\n'))
    return result


def public_error(status=500, code='', language=None):
    if code == 'authentication_failed': return tr('AI 服务认证失败，请检查凭证。', language)
    if 'timeout' in code or status in (408, 504, 524): key = '请求超时，请稍后重试。'
    elif status == 429 or code == 'rate_limited': key = '请求过于频繁，请稍后重试。'
    elif status == 401: key = '请先登录'
    elif status == 403: key = '没有权限执行此操作。'
    elif status == 404: key = '内容不存在或已被删除。'
    elif status == 402: key = '服务余额不足，请检查账户。'
    elif status < 500: key = '请求无效，请检查输入后重试。'
    else: key = '服务暂时不可用，请稍后重试。'
    return tr(key, language)


class UIExtension(Extension):
    """Mark only author-written HTML text/attributes, never rendered content.

    Script strings are handled explicitly at their call sites. Stored content,
    Jinja expressions, code, textarea values and the usage guide are excluded.
    """
    def preprocess(self, source, name, filename=None):
        if not name or name == 'guide.html' or name.startswith('guide/'): return source
        protected = re.compile(r'(<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>|<textarea\b.*?</textarea>|<pre\b.*?</pre>)', re.S | re.I)
        def mark(value):
            stripped = value.strip()
            decoded = html.unescape(stripped)
            if decoded != stripped and decoded in CATALOG:
                return value.replace(stripped, '{{ ui_t(' + json.dumps(decoded, ensure_ascii=False) + ') }}')
            if stripped in CATALOG:
                return value.replace(stripped, '{{ ui_t(' + json.dumps(stripped, ensure_ascii=False) + ') }}')
            if stripped.endswith(' |') and stripped[:-2] in CATALOG:
                return value.replace(stripped, '{{ ui_t(' + json.dumps(stripped[:-2], ensure_ascii=False) + ') }} |')
            if re.search(r'[\u4e00-\u9fff]', stripped) and re.search(r'[（(][A-Za-z][A-Za-z0-9 /_.:,-]*[)）]', stripped):
                return value.replace(stripped, '{{ ui_t(' + json.dumps(stripped, ensure_ascii=False) + ') }}')
            return value
        parts = protected.split(source)
        def attrs(piece):
            return re.sub(r'\b(placeholder|title|aria-label|alt)="([^"{}]*)"', lambda m: m[1] + '="' + mark(m[2]).replace('ui_t("', "ui_t('").replace('") }}', "') }}") + '"', piece)
        for i in range(1, len(parts), 2):
            if parts[i].lower().startswith('<textarea'):
                end = parts[i].find('>') + 1
                parts[i] = attrs(parts[i][:end]) + parts[i][end:]
        for i in range(0, len(parts), 2):
            pieces = re.split(r'({{.*?}}|{%.*?%}|<[^>]*>)', parts[i], flags=re.S)
            for j, piece in enumerate(pieces):
                if piece.startswith(('<', '{{', '{%')):
                    if piece.startswith('<'):
                        piece = re.sub(r'\b(placeholder|title|aria-label|alt)="([^"{}]*)"',
                            lambda m: m[1] + '="' + mark(m[2]).replace('ui_t("', "ui_t('").replace('") }}', "') }}") + '"', piece)
                    pieces[j] = piece
                else: pieces[j] = mark(piece)
            parts[i] = ''.join(pieces)
        return ''.join(parts)


def install_localization(app):
    app.jinja_env.add_extension(UIExtension)
    app.jinja_env.globals['ui_t'] = tr

    @app.context_processor
    def ui_context():
        lang = user_language()
        return {'ui_furigana_enabled':lang != 'ja' and read_settings().get('furigana_enabled',True) is not False, 'ui_language':lang, 'ui_catalog':{key:entry.get(lang,key) for key,entry in CATALOG.items()}}

    @app.route('/api/ui/language', methods=['GET', 'POST'], endpoint='ui_language_settings')
    def language_settings():
        from core.context import get_current_user_id
        if request.method == 'POST':
            value = (request.get_json(silent=True) or {}).get('user_language')
            if value not in SUPPORTED:
                return jsonify(error=tr('不支持的界面语言')), 400
            uid = get_current_user_id()
            if uid is not None: save_ui_language(uid, value)
            else: session['pending_ui_language'] = value
        response = jsonify(user_language=user_language())
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.after_request
    def localize_response(response):
        if response.is_json and not response.is_streamed:
            payload = response.get_json(silent=True)
            if isinstance(payload, dict):
                failed = response.status_code >= 400 or payload.get('status') == 'error'
                if payload.get('status') and isinstance(payload.get('message'), str) and payload['message'] in CATALOG:
                    payload['message'] = tr(payload['message'])
                    response.set_data(app.json.dumps(payload))
                # Never traverse nested user/model content.
                if failed:
                    for key in ('message', 'error'):
                        value = payload.get(key)
                        if isinstance(value, str):
                            machine_code = re.fullmatch(r'[a-z][a-z0-9_]*', value)
                            if key == 'error' and machine_code and 'message' in payload: continue
                            payload[key] = tr(value) if value in CATALOG else (value if value in {entry.get(user_language()) for entry in CATALOG.values()} else public_error(response.status_code, str(payload.get('code',''))))
                    response.set_data(app.json.dumps(payload))
        if response.mimetype == 'text/html' or request.path in ('/api/ui/language','/manifest.json'):
            response.headers['Content-Language'] = user_language()
            response.vary.add('Cookie'); response.vary.add('Accept-Language')
        return response
