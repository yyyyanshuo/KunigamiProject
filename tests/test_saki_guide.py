"""Public guide and password recovery checks using synthetic, isolated data."""
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class GuideTests(unittest.TestCase):
    def test_locales_public_navigation_and_password_recovery(self):
        with tempfile.TemporaryDirectory(prefix="saki-guide-test-") as data:
            script = r"""
from html.parser import HTMLParser
from unittest.mock import patch
import time
from app import app
from saki_local import seed
from blueprints import auth
seed()

class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.ids = []
        self.links = []
        self.sections = []
        self.feed(source)
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs: self.ids.append(attrs['id'])
        if tag == 'a': self.links.append(attrs.get('href', ''))
        if tag == 'section' and attrs.get('class') == 'doc-section':
            self.sections.append(attrs['id'])

client = app.test_client()
baseline = None
for language, brand, title in [('ja', '咲き', '使い方ガイド'),
                                ('en', 'Saki', 'User guide'),
                                ('zh-CN', '花期', '使用文档')]:
    assert client.post('/api/ui/language', json={'user_language': language}).status_code == 200
    response = client.get('/guide')
    assert response.status_code == 200
    assert response.headers['Content-Language'] == language
    source = response.get_data(as_text=True)
    page = Page(source)
    for anchor in ('chat-transfers', 'vision-prompt-presets', 'moments'):
        assert anchor in page.ids, (language, anchor)
    for model in ('gemini-3.8-flash', 'gemini-3-flash-preview', 'gemini-nano-banana-2.1', 'gemini-4-hydrogen', 'gemini-4-carbon', 'gemini-4-argon'):
        assert model in source, (language, model)
    assert '<title>' + title + ' | ' + brand + '</title>' in source
    assert 'name="apple-mobile-web-app-title" content="' + brand + '"' in source
    manifest = client.get('/manifest.json').get_json()
    assert manifest['name'] == manifest['short_name'] == brand
    assert len(page.ids) == len(set(page.ids)), 'Duplicate anchor'
    assert len(page.sections) == 21
    if baseline is None: baseline = page.sections
    assert baseline == page.sections, 'Section parity across languages'
    assert all(link[1:] in page.ids for link in page.links if link.startswith('#'))
    assert 'persona-lock' in page.ids and 'password-recovery' in page.ids
    assert '[[LOCK]]' in source and '[[/LOCK]]' in source
    assert 'Kunigami' not in source and 'Sakura' not in source
    for path in ('/login', '/forgot_password'):
        response = client.get(path)
        assert response.status_code == 200, path
        source = response.get_data(as_text=True)
        assert 'class="auth-guide-link"' in source and title in source
        assert '/guide' in Page(source).links
    assert '/forgot_password' in Page(client.get('/login').get_data(as_text=True)).links

# Validate the documented flow without sending an email or contacting any service.
old_session = app.test_client()
assert old_session.post('/api/login', json={'username':'guide@saki.invalid',
                                           'password':'synthetic-guide-old-password'}).status_code == 200
# Offline mode must block real email. After checking the guard, remove only that
# guard in this isolated test app; the sender below remains mocked throughout.
assert client.post('/api/forgot_password/send_code', json={'email':'guide@saki.invalid'}).status_code == 503
app.before_request_funcs[None] = [
    hook for hook in app.before_request_funcs[None]
    if hook.__name__ != 'check_feature_configuration'
]
assert client.post('/api/forgot_password/send_code', json={'email':'invalid'}).status_code == 400
class Inline:
    def __init__(self, target, args, **kwargs): self.invoke = lambda: target(*args)
    def start(self): self.invoke()
with patch.object(auth.threading, 'Thread', Inline), patch.object(auth, '_reset_mail') as sender:
    response = client.post('/api/forgot_password/send_code', json={'email':'guide@saki.invalid'})
    assert response.status_code == 202
    code = sender.call_args.args[2]
assert len(code) == 6 and code.isdigit()
payload = {'email':'guide@saki.invalid', 'code':'invalid', 'password':'synthetic-guide-new-password'}
assert client.post('/api/forgot_password/reset', json=payload).status_code == 400
payload['code'] = code
assert client.post('/api/forgot_password/reset', json=payload).status_code == 200
assert client.post('/api/forgot_password/reset', json=payload).status_code == 400
assert old_session.get('/api/user/profile_settings').status_code == 401
assert client.post('/api/login', json={'username':'guide@saki.invalid',
                                      'password':'synthetic-guide-old-password'}).status_code == 401
assert client.post('/api/login', json={'username':'guide@saki.invalid',
                                      'password':'synthetic-guide-new-password'}).status_code == 200
print('PASS: 3 complete public locales, navigation, account entries, reset and expired sessions; no mail sent')
"""
            from test_saki_local import child
            result = child(data, script, SAKI_TEST_EMAIL='guide@saki.invalid',
                           SAKI_TEST_PASSWORD='synthetic-guide-old-password')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
