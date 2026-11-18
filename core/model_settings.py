"""Versioned per-user model settings; one source for UI and runtime defaults."""
import copy
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from core.runtime import DATA_ROOT
from core.model_catalog import MODEL_CATALOG, PROVIDER_ADVICE

TASKS = ('chat', 'translation', 'vision', 'gen_persona', 'call', 'moments', 'summary', 'image', 'forum')
PROVIDERS = {
    'gemini': {'name': 'Gemini', 'protocol': 'gemini', 'base_url': 'https://generativelanguage.googleapis.com', 'credential': 'gemini'},
    'openai': {'name': 'ChatGPT', 'protocol': 'openai_responses', 'base_url': 'https://api.openai.com/v1', 'credential': 'openai'},
    'anthropic': {'name': 'Claude', 'protocol': 'anthropic', 'base_url': 'https://api.anthropic.com/v1', 'credential': 'anthropic'},
    'xai': {'name': 'Grok', 'protocol': 'openai_responses', 'base_url': 'https://api.x.ai/v1', 'credential': 'xai'},
    'custom': {'name': '自填写', 'protocol': 'openai_chat', 'base_url': '', 'credential': 'openrouter'},
}
PROTOCOLS = ('openai_chat', 'openai_responses', 'anthropic', 'gemini')
GEMINI_MODELS = dict(zip(TASKS, (
    'gemini-3.8-flash', 'gemini-3.1-flash-lite', 'gemini-3.8-flash',
    'gemini-3.1-pro-preview', 'gemini-3.8-flash', 'gemini-3.8-flash',
    'gemini-3-flash-preview', 'gemini-nano-banana-2.1', 'gemini-3.8-flash',
)))
DEFAULT_MODELS = {
    'gemini': GEMINI_MODELS,
    'openai': {**dict.fromkeys(TASKS, 'gpt-6-luna'), 'chat': 'gpt-6-sol', 'vision': 'gpt-6-astra', 'gen_persona': 'gpt-6-astra', 'image': 'gpt-image-2.5-sunburst'},
    'anthropic': {**dict.fromkeys(TASKS, 'claude-sonnet-5'), 'translation': 'claude-haiku-4-5-20251001', 'gen_persona': 'claude-opus-5-5', 'image': ''},
    'xai': {**dict.fromkeys(TASKS, 'grok-4.3'), 'chat': 'grok-4.7', 'vision': 'grok-4.7', 'gen_persona': 'grok-4.7', 'image': 'grok-imagine-image-2.0'},
    'custom': {**GEMINI_MODELS, 'image': ''},
}
IMAGE_OPTIONS = {
    'gemini': ['gemini-nano-banana-2.1', 'gemini-3-pro-image', 'gemini-3.1-flash-image'],
    'openai': ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare'],
    'anthropic': [], 'xai': ['grok-imagine-image-2.0'],
}


def defaults():
    return {
        'version': 2, 'active_route': 'gemini', 'enable_system_prompt_v2': True, 'image_daily_limit': 10,
        'routes': {key: {**info, 'models': dict(DEFAULT_MODELS[key])} for key, info in PROVIDERS.items()},
        'model_options': {key: list(dict.fromkeys([*MODEL_CATALOG.get(key, {}), *(m for m in models.values() if m)])) for key, models in DEFAULT_MODELS.items()},
    }


def config_path(user_id=None):
    if user_id is None:
        from core.context import get_current_user_id
        user_id = get_current_user_id()
    root = Path(DATA_ROOT)
    if user_id:
        root = root / 'users' / str(int(user_id))
    return root / 'configs' / 'api_settings.json'


def normalize_config(raw):
    """Migrate in memory, preserving model IDs and explicit empty image values."""
    result = defaults()
    if not isinstance(raw, dict):
        return result
    limit = raw.get('image_daily_limit', 10)
    if type(limit) is int and 1 <= limit <= 2147483647:
        result['image_daily_limit'] = limit
    result['enable_system_prompt_v2'] = raw.get('enable_system_prompt_v2', True) is not False
    route = raw.get('active_route', 'gemini')
    if not isinstance(route, str):
        route = 'gemini'
    result['active_route'] = 'custom' if route == 'relay' else route if route in PROVIDERS else 'gemini'
    routes = raw.get('routes') if isinstance(raw.get('routes'), dict) else {}
    options = raw.get('model_options') if isinstance(raw.get('model_options'), dict) else {}
    for key in PROVIDERS:
        old_key = 'relay' if key == 'custom' and 'custom' not in routes else key
        old = routes.get(old_key, {})
        if not isinstance(old, dict):
            continue
        dest = result['routes'][key]
        if key == 'custom':
            dest['name'] = str(old.get('name') or PROVIDERS[key]['name'])[:80]
            dest['protocol'] = old.get('protocol') if old.get('protocol') in PROTOCOLS else 'openai_chat'
            dest['base_url'] = str(old.get('base_url') or '').rstrip('/')
            if old_key == 'relay':
                provider = old.get('relay_provider', 'old')
                dest['base_url'] = (old.get('relay_custom_url', '') if provider == 'custom' else os.getenv('OPENROUTER_BASE_URL_OLD' if provider == 'old' else 'OPENROUTER_BASE_URL', '')).rstrip('/')
        models = old.get('models', {})
        if isinstance(models, dict):
            for task in TASKS:
                if task in models and isinstance(models[task], str):
                    dest['models'][task] = models[task].strip()
        if key == 'anthropic':
            dest['models']['image'] = ''
        old_options = options.get(old_key, [])
        if isinstance(old_options, list):
            result['model_options'][key] = list(dict.fromkeys(result['model_options'][key] + [m.strip() for m in old_options if isinstance(m, str) and m.strip()]))
        for model in dest['models'].values():
            if model and model not in result['model_options'][key]:
                result['model_options'][key].append(model)
    return result


def model_settings_lock(user_id=None):
    from contextlib import nullcontext
    from core.credentials import default_store
    from core.context import get_current_user_id
    uid = get_current_user_id() if user_id is None else user_id
    return default_store.locked(uid) if uid else nullcontext()


def read_config(user_id=None):
    from core.credentials import default_store
    from core.context import get_current_user_id
    uid = get_current_user_id() if user_id is None else user_id
    path = config_path(uid)
    if uid:
        with default_store.locked(uid):
            return default_store._read_json(path)
    return default_store._read_json(path)


def load_config(user_id=None):
    return normalize_config(read_config(user_id))


def validate_base_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请填写有效的 HTTPS API 地址')
    host = parsed.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('请填写有效的 HTTPS API 地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError('请填写有效的 HTTPS API 地址')
    try:
        parsed.port
    except ValueError:
        raise ValueError('请填写有效的 HTTPS API 地址') from None
    return value.rstrip('/')


def validate_config(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('active_route'), str) or raw.get('active_route') not in (*PROVIDERS, 'relay'):
        raise ValueError('请选择有效线路')
    limit = raw.get('image_daily_limit', 10)
    if type(limit) is not int or not 1 <= limit <= 2147483647:
        raise ValueError('每日生图上限需为正整数')
    routes = raw.get('routes')
    if not isinstance(routes, dict):
        raise ValueError('线路配置无效')
    for key, info in routes.items():
        if key not in (*PROVIDERS, 'relay') or not isinstance(info, dict) or not isinstance(info.get('models', {}), dict):
            raise ValueError('线路配置无效')
        for task, model in info.get('models', {}).items():
            if task not in TASKS or not isinstance(model, str) or len(model) > 200 or any(ord(c) < 32 for c in model):
                raise ValueError('模型 ID 无效')
        if key == 'custom' and info.get('protocol', 'openai_chat') not in PROTOCOLS:
            raise ValueError('接口格式无效')
    result = normalize_config(raw)
    active = result['active_route']
    for task, model in result['routes'][active]['models'].items():
        if task != 'image' and not model:
            raise ValueError('请填写模型 ID')
    custom = result['routes']['custom']
    if custom['base_url'] or active == 'custom':
        custom['base_url'] = validate_base_url(custom['base_url'])
    return result


class SettingsConflict(ValueError):
    pass


def _revision(user_id, config):
    from core.credentials import default_store, _digest
    return _digest([config, default_store._load_store(user_id)])


def public_config(user_id=None):
    from core.context import get_current_user_id
    uid = get_current_user_id() if user_id is None else user_id
    with model_settings_lock(uid):
        cfg = load_config(uid)
        if uid:
            cfg['revision'] = _revision(uid, cfg)
    cfg['recommendations'] = copy.deepcopy(DEFAULT_MODELS)
    cfg['image_options'] = copy.deepcopy(IMAGE_OPTIONS)
    cfg['model_catalog'] = copy.deepcopy(MODEL_CATALOG)
    cfg['provider_advice'] = copy.deepcopy(PROVIDER_ADVICE)
    return cfg


def save_config(raw, user_id):
    """Commit settings and staged credential edits together, with rollback."""
    from core.credentials import default_store
    config = validate_config(raw)
    updates = raw.get('credential_updates', {})
    allowed = {info['credential'] for info in PROVIDERS.values()}
    if not isinstance(updates, dict) or any(p not in allowed for p in updates):
        raise ValueError('不支持的凭证类型')
    for value in updates.values():
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 4096):
            raise ValueError('API Key 长度不正确')
    # Check encryption before changing any file.
    if any(value is not None for value in updates.values()):
        default_store._fernet()
    with default_store.locked(user_id):
        if 'revision' in raw and raw['revision'] != _revision(user_id, load_config(user_id)):
            raise SettingsConflict('模型配置或 API Key 已更新，请刷新后重新编辑。')
        default_store.save_changes(user_id, updates, config)
    return config
