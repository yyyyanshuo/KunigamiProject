"""Isolated subprocess tests: do not import the app into the pytest parent."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def child(data_root, code, *, timeout=90, **extra):
    # Never inherit service credentials or proxy settings from the developer shell.
    env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL') if name in os.environ}
    env.update(SAKI_LOCAL_TEST='true', SAKI_DATA_ROOT=str(data_root), KUNIGAMI_ENV='test',
               PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1',
               SAKI_TEST_EMAIL='test@saki.invalid', SAKI_TEST_PASSWORD='synthetic-test-password-only',
               FLASK_SECRET_KEY='test-session-key-with-at-least-32-characters')
    env.update(extra)
    # Patch before importing runtime: it normally reads the project's real .env.
    prelude = 'import dotenv\ndotenv.load_dotenv = lambda *a, **kw: False\n'
    return subprocess.run([sys.executable, '-c', prelude + code], cwd='/tmp', env=env,
                          text=True, capture_output=True, timeout=timeout)


class LocalIsolationTests(unittest.TestCase):
    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_two_roots_auth_chat_and_upload(self):
        with tempfile.TemporaryDirectory(prefix='saki-isolation-') as temp:
            roots = [Path(temp) / 'a', Path(temp) / 'b']
            for i, root in enumerate(roots):
                result = child(root, '''
import io, json, sqlite3
from pathlib import Path
from saki_local import seed
from app import app
from core.config import USERS_DB, USERS_ROOT
seed()
seed()  # Must preserve existing users, passwords and persona.
with sqlite3.connect(USERS_DB) as conn:
    assert conn.execute('SELECT id FROM users').fetchall() == [(1,)]
c = app.test_client()
assert c.get('/api/saki_demo/history').status_code == 401
assert c.get('/').status_code == 302
assert c.get('/login').status_code == 200
assert c.post('/api/login', json={'username':'test@saki.invalid','password':'wrong'}).status_code == 401
r = c.post('/api/login', json={'username':'test@saki.invalid','password':'synthetic-test-password-only'})
assert r.status_code == 200, r.json
assert 'saki_global_session=' in str(r.headers)
assert c.get('/').status_code == 200
page = c.get('/chat/saki_demo')
assert page.status_code == 200
assert 'cdnjs.cloudflare.com/ajax/libs/html2canvas' not in page.get_data(as_text=True)
r = c.post('/api/characters/add', json={'id':'own_character','name':'Synthetic'})
assert r.status_code == 200, r.json
r = c.post('/api/saki_demo/chat_v2', json={'message': 'ROOT_MARKER'})
assert r.status_code == 200, r.json
assert 'MOCK' in r.get_data(as_text=True), r.json
r = c.get('/api/saki_demo/history')
assert 'ROOT_MARKER' in r.get_data(as_text=True), r.json
from PIL import Image
image = io.BytesIO()
Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')
image.seek(0)
r = c.post('/api/upload_user_avatar', data={'file':(image, 'test.png')})
assert r.status_code == 200, r.json
assert c.get(r.json['url']).status_code == 200
assert app.test_client().get(r.json['url']).status_code == 302
print('PASS auth, pages, character, MOCK, history, upload')
'''.replace('ROOT_MARKER', f'isolated_root_{i}').replace('test@saki.invalid', f'root_{i}@saki.invalid'), SAKI_TEST_EMAIL=f'root_{i}@saki.invalid')
                self.ok(result)
            for i, root in enumerate(roots):
                self.ok(child(root, f'''
from app import app
c = app.test_client()
assert c.post('/api/login', json={{'username':'root_{i}@saki.invalid','password':'synthetic-test-password-only'}}).status_code == 200
assert c.post('/api/login', json={{'username':'root_{1-i}@saki.invalid','password':'synthetic-test-password-only'}}).status_code == 401
history = c.get('/api/saki_demo/history').get_data(as_text=True)
assert 'isolated_root_{i}' in history
assert 'isolated_root_{1-i}' not in history
'''))

    def test_guards_and_paths(self):
        with tempfile.TemporaryDirectory(prefix='saki-guards-') as temp:
            self.ok(child(temp, '''
import socket, sqlite3
from pathlib import Path
from core.runtime import DATA_ROOT
import core.config as cfg
import app
import weather_api, music_manager
from services import character_assessment
from blueprints import forum
for path in (cfg.USERS_DB, cfg.DATABASE_FILE, app.USERS_ROOT, app.SQUARE_DB,
             app.API_CONFIG_FILE, cfg.GROUPS_DIR, cfg.CHARACTERS_DIR,
             weather_api.WEATHER_CACHE_FILE, character_assessment.USERS_ROOT,
             forum.FORUMS_DB, music_manager._get_music_dir(1)):
    assert Path(path).is_relative_to(DATA_ROOT), path
for action in (lambda: socket.create_connection(('example.invalid',443)),
               lambda: Path('/tmp/saki-outside.txt').write_text('blocked'),
               lambda: sqlite3.connect('/tmp/saki-outside.db')):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError('Isolation guard failed')
from cos_utils import client
assert client is None
'''))
            r = child(temp, 'import core.runtime', KUNIGAMI_ENV='production')
            self.assertNotEqual(r.returncode, 0)

    def test_legacy_paths_without_opt_in(self):
        self.ok(child('', '''
from pathlib import Path
from core.config import BASE_DIR, USERS_DB, USERS_ROOT, DATABASE_FILE
assert USERS_DB == str(Path(BASE_DIR) / 'configs/users.db')
assert USERS_ROOT == str(Path(BASE_DIR) / 'users')
assert DATABASE_FILE == 'chat_history.db'
''', SAKI_LOCAL_TEST='false'))


if __name__ == '__main__':
    unittest.main()
