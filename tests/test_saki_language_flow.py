"""Offline language integration checks. Each child has an isolated data root."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class LanguageFlowTests(unittest.TestCase):
    def test_preference_handoff_background_and_rendering(self):
        with tempfile.TemporaryDirectory(prefix='saki-language-') as temp:
            env=os.environ.copy()
            env.update(SAKI_LOCAL_TEST='true',SAKI_DATA_ROOT=temp,KUNIGAMI_ENV='test',PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE='1',FLASK_SECRET_KEY='synthetic-language-test-secret-not-a-real-key',SAKI_TEST_EMAIL='test@saki.invalid',SAKI_TEST_PASSWORD='synthetic-language-password')
            code=r'''
import json
from pathlib import Path
from app import app
from saki_local import seed
from core.localization import user_language, read_settings, save_ui_language, tr
from core.runtime import DATA_ROOT
from core.context import set_background_user, clear_background_user
from services.ai_client import ai_error_text
seed()
c=app.test_client()
assert c.get('/api/ui/language',headers={'Accept-Language':'fr'}).json['user_language']=='ja'
assert c.get('/api/ui/language',headers={'Accept-Language':'en-US'}).json['user_language']=='en'
assert c.post('/api/ui/language',json={'user_language':'xx'}).status_code==400
assert c.post('/api/ui/language',json={'user_language':'en'}).json['user_language']=='en'
r=c.post('/api/login',json={'username':'test@saki.invalid','password':'synthetic-language-password'})
assert r.status_code==200,r.json
assert read_settings(1)['user_language']=='en'
assert c.get('/api/user/profile_settings').json['user_language']=='en'
assert c.post('/api/user/profile_settings',json={'ai_language':'ja','timezone':'Europe/London'}).status_code==200
assert c.post('/api/ui/language',json={'user_language':'zh-CN'}).status_code==200
settings=read_settings(1)
assert settings['default_chat_language']=='ja' and settings['timezone']=='Europe/London'
set_background_user(1)
assert user_language()=='zh-CN'
assert '超时' in ai_error_text('secret=never-display-this',code='ai_timeout',status_code=504)
clear_background_user()
# A fresh browser with no explicit choice must honor the saved account language.
d=app.test_client()
r=d.post('/api/login',json={'username':'test@saki.invalid','password':'synthetic-language-password'},headers={'Accept-Language':'en'})
assert r.status_code==200
assert d.get('/api/ui/language').json['user_language']=='zh-CN'
# A new explicit anonymous choice is handed off on login.
e=app.test_client();e.post('/api/ui/language',json={'user_language':'en'})
e.post('/api/login',json={'username':'test@saki.invalid','password':'synthetic-language-password'})
assert read_settings(1)['user_language']=='en'
for lang in ('ja','en','zh-CN'):
 c.post('/api/ui/language',json={'user_language':lang})
 for page in ['/','/profile','/chat/saki_demo','/memory/saki_demo','/moments','/forum','/map','/square','/square/upload','/terms','/privacy']:
  r=c.get(page)
  assert r.status_code==200,(page,r.status_code,r.get_data(as_text=True)[:200])
  assert 'lang="'+lang+'"' in r.get_data(as_text=True),(page,lang)
 with app.test_request_context('/'): assert tr('用户语言',lang)!='用户语言' or lang=='zh-CN'
assert 'never-display-this' not in str(ai_error_text('secret=never-display-this'))
print('PASS UI language: preference, old fields, login handoff, saved account priority, background context, 33 page renders, safe errors')
'''
            result=subprocess.run([sys.executable,'-c',code],env=env,cwd=temp,text=True,capture_output=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
