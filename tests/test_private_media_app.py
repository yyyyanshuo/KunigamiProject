"""Run the real application's session checks with fake COS in an offline child."""
from pathlib import Path
import tempfile
import unittest

from tests.test_saki_local import child


class PrivateMediaAppTests(unittest.TestCase):
    def test_real_session_revocation_avatar_and_publication(self):
        with tempfile.TemporaryDirectory(prefix='saki-private-media-') as directory:
            result = child(Path(directory), r'''
import io, os, sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from saki_local import seed
import app as application
from core.config import USERS_DB
import cos_utils
from blueprints import square
seed()
app = application.app
class Cloud:
    def __init__(self): self.calls = []
    def get_object(self, **kw):
        self.calls.append(kw)
        stream = io.BytesIO(b'photo')
        return {'Body': SimpleNamespace(get_raw_stream=lambda: stream), 'Content-Length':'5'}
cloud = Cloud()
cos_utils.LOCAL_TEST = False
cos_utils.client = cloud
cos_utils.bucket = 'synthetic'
url = '/api/media/cos/users/1/avatar.png'
c = app.test_client()
assert c.get(url).status_code == 401
assert c.post('/api/login', json={'username':'test@saki.invalid','password':'synthetic-test-password-only'}).status_code == 200
assert c.get(url).data == b'photo'
assert c.get('/api/media/cos/users/2/avatar.png').status_code == 404
application.LOCAL_TEST = False
os.environ['COS_BUCKET'] = 'synthetic'
os.environ['COS_REGION'] = 'test'
assert c.get('/user_avatar').data == b'photo'
with patch.object(square, 'download_from_cos', return_value=b'image'), patch.object(square, '_save_square_avatar_image', return_value='public-copy'):
    assert square._materialize_square_avatar('public1', 1, avatar_url='/api/media/cos/users/1/characters/hero/avatar.png') == 'public-copy'
assert square._private_avatar_cos_keys(2, '/api/media/cos/users/1/characters/hero/avatar.png') == []
assert square._private_avatar_cos_keys(1, '/api/media/cos/users/1/../2/avatar.png') == []
public = '/api/media/cos/square/avatars/public1.png'
assert square._materialize_square_avatar('public1', 2, avatar_url=public) == public
before = len(cloud.calls)
with sqlite3.connect(USERS_DB) as db:
    db.execute('UPDATE users SET auth_version=auth_version+1 WHERE id=1')
assert c.get(url).status_code == 401
assert c.get(public).status_code == 401
assert len(cloud.calls) == before
print('PASS real login, owner isolation, avatar proxy, public copy, session revocation')
''')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
