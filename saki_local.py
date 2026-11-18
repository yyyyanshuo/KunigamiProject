"""WSL entry point: init / run / doctor. Never imports the original worktree."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import sys

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def bootstrap():
    path = ROOT / '.env'
    if path.exists():
        return
    from cryptography.fernet import Fernet
    port = 5001
    while port < 5100:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
                break
            except OSError:
                port += 1
    else:
        raise RuntimeError('No free local port between 5001 and 5099')
    text = (ROOT / '.env.example').read_text()
    text = text.replace('SAKI_PORT=5001', f'SAKI_PORT={port}')
    text = text.replace('FLASK_SECRET_KEY=\n', f'FLASK_SECRET_KEY={secrets.token_hex(32)}\n')
    text = text.replace('CREDENTIAL_ENCRYPTION_KEYS=\n', f'CREDENTIAL_ENCRYPTION_KEYS={Fernet.generate_key().decode()}\n')
    text = text.replace('SAKI_TEST_PASSWORD=\n', f'SAKI_TEST_PASSWORD={secrets.token_urlsafe(18)}\n')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)


def seed():
    import json
    import sqlite3
    from datetime import datetime
    from werkzeug.security import generate_password_hash
    from core.config import USERS_DB, USERS_ROOT
    from core.legal import record_current_legal_consents
    from blueprints.auth import init_user_workspace
    from app import app
    email = os.getenv('SAKI_TEST_EMAIL', 'test@saki.invalid')
    password = os.getenv('SAKI_TEST_PASSWORD', '')
    if not email.endswith('.invalid') or len(password) < 12:
        raise RuntimeError('Use a synthetic .invalid email and a password of at least 12 characters')
    with sqlite3.connect(USERS_DB) as conn:
        row = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if row:
            uid = row[0]
        else:
            uid = conn.execute('INSERT INTO users(email,password_hash,display_name,created_at) VALUES(?,?,?,?)',
                               (email, generate_password_hash(password), 'Saki 本地测试', datetime.now().isoformat())).lastrowid
            record_current_legal_consents(conn, uid)
    init_user_workspace(uid)
    cfg = Path(USERS_ROOT) / str(uid) / 'configs' / 'characters.json'
    chars = json.loads(cfg.read_text()) if cfg.exists() else {}
    if 'saki_demo' not in chars:
        client = app.test_client()
        result = client.post('/api/login', json={'username': email, 'password': password})
        if result.status_code != 200:
            raise RuntimeError('Existing password differs; initialization did not overwrite it')
        result = client.post('/api/characters/add', json={'id': 'saki_demo', 'name': '小咲（本地测试）'})
        if result.status_code != 200:
            raise RuntimeError('Could not create synthetic character')
        persona = Path(USERS_ROOT) / str(uid) / 'characters/saki_demo/prompts/1_base_persona.json'
        persona.write_text(json.dumps({'system_prompt': '原创合成测试角色小咲，喜欢散步和园艺。此角色只用于本地测试。'}, ensure_ascii=False))
    print('初始化完成；测试账号：' + email + '；密码保存在本目录 .env 的 SAKI_TEST_PASSWORD。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'run', 'doctor'))
    args = parser.parse_args()
    if args.command == 'init':
        bootstrap()
    from dotenv import load_dotenv
    from core.environment_check import configuration_errors, service_status
    load_dotenv(ROOT / '.env', override=False)
    errors = configuration_errors(local_launcher=True)
    if args.command == 'doctor':
        print('配置检查（只显示名称/状态，不验证第三方服务）：', ROOT / '.env')
        for issue in errors:
            print('ERROR:', issue)
        for feature, status in service_status().items():
            print(feature + ': ' + status['status'] + '; 未填环境变量: ' + ', '.join(status['environment_missing']))
        print('用户界面模型/语音 Key 不属于 .env；请在个人设置查看。')
        print('真实第三方服务：未验证；配置存在与否不表示凭据有效。')
        if errors:
            raise SystemExit(1)
    elif errors:
        raise SystemExit('配置检查未通过（不显示值）：\n' + '\n'.join(errors))
    # Always choose the explicit, project-scoped config, never cwd/parent dotenv.
    from core.runtime import LOCAL_TEST, DATA_ROOT
    if not LOCAL_TEST:
        raise RuntimeError('This launcher requires SAKI_LOCAL_TEST=true')
    if args.command == 'doctor':
        print('Source:', ROOT)
        print('Config:', ROOT / '.env')
        print('Data:', DATA_ROOT)
        print('Logs:', Path(DATA_ROOT) / 'logs')
        print('URL: http://127.0.0.1:' + os.getenv('SAKI_PORT', '5001'))
        print('Models: MOCK; outbound network: blocked; schedulers: disabled')
        print('Session/encryption keys:', 'configured' if all(os.getenv(k) for k in ('FLASK_SECRET_KEY', 'CREDENTIAL_ENCRYPTION_KEYS')) else 'missing')
        return
    import logging
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(Path(DATA_ROOT) / 'logs' / 'local.log', maxBytes=2_000_000, backupCount=2)
    logging.getLogger().addHandler(handler)
    from app import app
    if args.command == 'init':
        seed()
    else:
        print('Saki 本地离线测试：模拟回复，不调用真实服务。')
        app.run(host='127.0.0.1', port=int(os.getenv('SAKI_PORT', '5001')), debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
