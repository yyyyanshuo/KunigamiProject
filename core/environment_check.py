"""Value-redacted configuration checks; safe to run before importing the app."""
import math
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SERVICE_VARIABLES = {
    'gemini': ('GEMINI_API_KEY',),
    'service_gemini': ('SAKI_SERVICE_GEMINI_API_KEY',),
    'relay': ('OPENROUTER_KEY', 'OPENROUTER_BASE_URL', 'OPENROUTER_BASE_URL_OLD'),
    'cos': ('COS_SECRET_ID', 'COS_SECRET_KEY', 'COS_REGION', 'COS_BUCKET'),
    'mail': ('MAIL_SENDER', 'MAIL_PASSWORD', 'MAIL_SERVER', 'MAIL_PORT'),
    'push': ('VAPID_PRIVATE_KEY', 'VAPID_PUBLIC_KEY'),
    'stt': ('ELEVENLABS_API_KEY',),
    'tts': (),  # Per-user ElevenLabs credential + character voice ID, not env STT key.
    'music': ('NCM_APP_ID', 'NCM_PRIVATE_KEY'),
    'image_generation': ('SILICONFLOW_API_KEY',),
    'search': ('SERPER_API_KEY',),
    'weather': (),
}
INTEGER_RANGES = {
    'SAKI_PORT': (1, 65535), 'SESSION_LIFETIME_DAYS': (1, None),
    'MAIL_PORT': (1, 65535), 'SQUARE_TRIAL_RATE_LIMIT_PER_MINUTE': (1, None),
    'SQUARE_TRIAL_PENDING_TIMEOUT_SECONDS': (30, None),
    'VOICE_STT_MAX_REQUESTS_PER_HOUR': (0, None),
    'VOICE_STT_MAX_REALTIME_SESSIONS_PER_HOUR': (0, None),
    'VOICE_CALL_STT_SESSIONS_PER_HOUR': (0, None),
    'VOICE_CALL_BATCH_STT_PER_HOUR': (0, None), 'VOICE_CALL_STT_MAX_BYTES': (1024, None),
}


def configuration_errors(env=None, *, local_launcher=False):
    env = os.environ if env is None else env
    errors = []
    def error(name, remedy):
        errors.append(f'{name}: {remedy}')
    local = env.get('SAKI_LOCAL_TEST', 'true').lower() == 'true'
    mode = (env.get('KUNIGAMI_ENV') or env.get('FLASK_ENV') or 'development').strip().lower()
    production = mode in {'production', 'prod'}
    for name in ('SAKI_LOCAL_TEST', 'FLASK_DEBUG', 'ALLOW_LEGACY_PLAINTEXT_LOGIN'):
        if name in env and env[name].lower() not in {'true', 'false'}:
            error(name, '请填写 true 或 false，不要留空或包含空格。')
    if local_launcher and not local:
        error('SAKI_LOCAL_TEST', '本地启动器要求 true；请保留离线模式。')
    if local and mode not in {'development', 'test', 'testing'}:
        error('KUNIGAMI_ENV / FLASK_ENV', '本地模式要求 development 或 test；不能与 production 同时启用。')
    if production and env.get('ALLOW_LEGACY_PLAINTEXT_LOGIN', 'false').lower() == 'true':
        error('ALLOW_LEGACY_PLAINTEXT_LOGIN', '生产环境必须为 false；不允许旧明文账号登录。')
    admin_ids = env.get('SAKI_ADMIN_USER_IDS', '').strip()
    if admin_ids and any(not part.strip().isascii() or not part.strip().isdigit() or int(part.strip()) <= 0 for part in admin_ids.split(',')):
        error('SAKI_ADMIN_USER_IDS', '请填写逗号分隔的正整数用户 ID；留空表示没有管理员。')
    valid_local_data = False
    if local:
        raw = env.get('SAKI_DATA_ROOT', '')
        path = (SOURCE_ROOT / raw).resolve()
        allowed = path.is_relative_to(SOURCE_ROOT / '.local-data') or (
            path.is_relative_to('/tmp') and len(path.parts) > 2 and path.parts[2].startswith('saki-'))
        valid_local_data = bool(raw and allowed)
        if not valid_local_data:
            error('SAKI_DATA_ROOT', '请填写 .local-data/global，或本项目 .local-data 内的独立目录。')
    if local or production:
        if len(env.get('FLASK_SECRET_KEY', '').strip()) < 32:
            error('FLASK_SECRET_KEY', '需要至少 32 字符的独立随机值。首次初始化用 saki_local.py init；已有环境请恢复原密钥，勿覆盖。')
    key = env.get('CREDENTIAL_ENCRYPTION_KEYS', '')
    if key:
        try:
            from cryptography.fernet import Fernet
            for part in key.split(','):
                Fernet(part.strip().encode('ascii'))
        except (ValueError, TypeError, UnicodeError):
            error('CREDENTIAL_ENCRYPTION_KEYS', '应为 Fernet.generate_key() 生成的 URL-safe base64 密钥，多个以逗号分隔；恢复原密钥，勿替换已有加密数据的密钥。')
    elif production or (valid_local_data and any(
            (SOURCE_ROOT / env['SAKI_DATA_ROOT'] / 'users').glob('*/configs/credentials.json'))):
        error('CREDENTIAL_ENCRYPTION_KEYS', '生产或已有加密数据时必须恢复原 Fernet 密钥；禁止自动生成替换。')
    for name, (minimum, maximum) in INTEGER_RANGES.items():
        if name not in env:
            continue
        try:
            value = int(env[name])
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError
        except (ValueError, TypeError):
            error(name, f'请填写整数，最小 {minimum}' + (f'，最大 {maximum}' if maximum else '') + '；不用时删除该项或注释，不能填空值。')
    for name in ('GEMINI_CONNECT_TIMEOUT_SECONDS', 'GEMINI_READ_TIMEOUT_SECONDS'):
        if name in env:
            try:
                value = float(env[name])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                error(name, '请填写大于 0 的有限秒数；不用时注释该项。')
    for name in ('GEMINI_BASE_URL', 'OPENROUTER_BASE_URL', 'OPENROUTER_BASE_URL_OLD', 'PUBLIC_BASE_URL', 'SITE_URL'):
        if env.get(name):
            parsed = urlparse(env[name])
            if parsed.scheme not in {'https', 'http'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                error(name, '请填写不含用户名、密码、query 或 fragment 的 http(s) 基础 URL。')
    for name in ('VOICE_ANNOUNCEMENT_START', 'VOICE_ANNOUNCEMENT_END', 'VOICE_LEGACY_DELETE_AT',
                 'PERSONA_LOCK_ANNOUNCEMENT_START', 'PERSONA_LOCK_ANNOUNCEMENT_END'):
        if name in env:
            try:
                datetime.fromisoformat(env[name])
            except (ValueError, TypeError):
                error(name, '请填写 ISO 8601 日期时间，建议带时区；沿用代码默认日期时删除或注释该项。')
    return errors


def service_status(env=None):
    env = os.environ if env is None else env
    local = env.get('SAKI_LOCAL_TEST', 'true').lower() == 'true'
    return {name: {
        'status': ('mock' if name in {'gemini', 'relay', 'service_gemini'} else 'local_storage' if name == 'cos' else 'disabled') if local else 'not_verified',
        'environment_present': [key for key in keys if env.get(key)],
        'environment_missing': [key for key in keys if not env.get(key)],
        'note': '用户界面 Key、线路及角色 voice_id 单独配置；存在值不代表服务验证通过。',
    } for name, keys in SERVICE_VARIABLES.items()}


def unavailable_payload(service, *, offline=False):
    keys = SERVICE_VARIABLES.get(service, ())
    return {
        'code': 'local_feature_unavailable' if offline else 'service_not_configured',
        'error': ('本地离线模式禁用此真实服务。' if offline else '此服务尚未配置。') +
                 ' 请按 CONFIGURATION_CHECKLIST_ZH.md 配置独立测试服务；不要使用旧站凭据。',
        'feature': service, 'configuration_variables': list(keys),
    }


def install_feature_checks(app, local):
    """Report unavailable external features before work or background threads start."""
    from flask import request, jsonify

    @app.route('/api/local/config-status')
    def local_config_status():
        return jsonify(services=service_status(), real_services_verified=False)

    @app.before_request
    def check_feature_configuration():
        path = request.path
        service = None
        if path in {'/api/forgot_password/send_code', '/api/forgot_password/reset'}:
            service = 'mail'
        elif path in {'/api/subscribe', '/api/vapid_public_key'}:
            service = 'push'
        elif path.startswith('/api/voice-messages') or path.endswith(('/stt-token', '/transcribe')):
            service = 'stt'
        elif path.endswith(('/tts', '/tts_voice')) or path == '/api/voice_clone':
            service = 'tts'
        elif path in {'/api/music/search', '/api/music/play', '/api/music/next', '/api/music/recommend'} or path.startswith('/api/music/lyric/'):
            service = 'music'
        elif path.startswith(('/api/map/weather', '/api/map/geocode')):
            service = 'weather'
        if local and path in {'/api/forums/generate', '/api/forums/generate-stream'}:
            return jsonify(unavailable_payload('relay', offline=True)), 503
        if path == '/api/music/playlist/add' and not (request.get_json(silent=True) or {}).get('title'):
            service = 'music'
        if not service:
            return None
        if local:
            return jsonify(unavailable_payload(service, offline=True)), 503
        keys = SERVICE_VARIABLES[service]
        # TTS has its existing per-user key and voice_id checks. Weather has no key.
        required = [key for key in keys if key != 'MAIL_PORT']
        if required and any(not os.getenv(key) for key in required):
            return jsonify(unavailable_payload(service)), 503
