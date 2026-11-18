"""Run real helpers and Flask requests only in guarded synthetic subprocesses."""
import tempfile
import unittest

from test_saki_local import child


SETUP = '''
import os, json, sqlite3, sys
from pathlib import Path
from unittest.mock import patch
from saki_local import seed
from app import app
from core.config import USERS_ROOT, USERS_DB, CHARACTERS_DIR
from core.user_resources import character_paths, require_owned_group, initialize_character_template
from werkzeug.exceptions import NotFound
assert not os.getenv('OPENROUTER_KEY') and not os.getenv('COS_SECRET_KEY')
seed()
os.environ['SAKI_TEST_EMAIL'] = 'second@saki.invalid'
seed()
clients = []
for email in ('test@saki.invalid', 'second@saki.invalid'):
    c = app.test_client()
    assert c.post('/api/login', json={'username': email, 'password': 'synthetic-test-password-only'}).status_code == 200
    clients.append(c)
a, b = clients
'''


class SecurityBoundaryTests(unittest.TestCase):
    def run_case(self, code):
        with tempfile.TemporaryDirectory(prefix='saki-security-') as root:
            result = child(root, SETUP + code)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_user_resource_isolation_and_creation(self):
        self.run_case('''
assert b.post('/api/characters/add', json={'id': 'b_only', 'name': 'B private'}).status_code == 200
assert a.post('/api/characters/add', json={'id': 'a_only', 'name': 'A private'}).status_code == 200
assert a.post('/api/characters/add', json={'id': 'a_other', 'name': 'A other'}).status_code == 200
assert a.post('/api/groups/add', json={'id': 'bad_group', 'name': 'Invalid', 'members': ['saki_demo', 'b_only']}).status_code == 404
assert not Path(USERS_ROOT, '1/groups/bad_group').exists()
for c, extra in ((a, 'a_only'), (b, 'b_only')):
    assert c.post('/api/groups/add', json={'id': 'shared_group', 'name': 'Synthetic group', 'members': ['saki_demo', extra]}).status_code == 200
for uid in (1, 2):
    db, prompts = character_paths('saki_demo', uid)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO messages(role,content) VALUES('assistant',?)", (f'PRIVATE_{uid}',))
    Path(prompts, '7_schedule.json').write_text(json.dumps({'_undated': [f'ONLY_{uid}']}))
    with sqlite3.connect(require_owned_group('shared_group', uid) / 'chat.db') as conn:
        conn.execute("INSERT INTO messages(role,content) VALUES('saki_demo',?)", (f'GROUP_{uid}',))
assert 'PRIVATE_1' in a.get('/api/saki_demo/history').text
assert 'PRIVATE_2' not in a.get('/api/saki_demo/history').text
assert 'GROUP_1' in a.get('/api/group/shared_group/history').text
assert 'GROUP_2' not in a.get('/api/group/shared_group/history').text
assert character_paths('saki_demo', 1) != character_paths('saki_demo', 2)
from core.context import set_background_user, clear_background_user
from core.utils import get_group_dir, get_paths
from blueprints.group import distribute_group_memory
set_background_user(1)
try:
    assert get_paths('saki_demo') == character_paths('saki_demo', 1)
    assert get_group_dir('shared_group', user_id=2) != get_group_dir('shared_group')
    from app import ensure_directive_chat
    directive = {'member_ids': ['a_other'], 'include_user': True}
    new_group = ensure_directive_chat(directive, 'saki_demo')
    assert Path(get_group_dir(new_group), 'chat.db').exists()
    assert ensure_directive_chat(directive, 'saki_demo') == new_group
    group_config = Path(USERS_ROOT, '1/configs/groups.json').read_bytes()
    try:
        ensure_directive_chat({'member_ids': ['b_only']}, 'saki_demo')
    except NotFound:
        pass
    else:
        raise AssertionError('Unowned directive member accepted')
    assert Path(USERS_ROOT, '1/configs/groups.json').read_bytes() == group_config
    distribute_group_memory('shared_group', 'Synthetic group', ['saki_demo', 'a_only'],
                            [{'time': '12:00', 'event': 'A private group event'}], '2026-10-16')
    for cid in ('saki_demo', 'a_only'):
        assert 'A private group event' in Path(character_paths(cid, 1)[1], '6_memory_short.json').read_text()
    assert 'A private group event' not in Path(character_paths('saki_demo', 2)[1], '6_memory_short.json').read_text()
finally:
    clear_background_user()

# Any read of B's private tree from this point is an actual test failure.
b_root = Path(USERS_ROOT, '2').resolve()
def no_b_reads(event, args):
    if event in ('open', 'sqlite3.connect') and isinstance(args[0], (str, bytes)):
        if Path(os.fsdecode(args[0])).resolve().is_relative_to(b_root):
            raise AssertionError('Attempted read of another account')
sys.addaudithook(no_b_reads)
bad_ids = ['b_only', '../2/characters/saki_demo', '/tmp/escape', 'a/b', 'a\\\\b', '%2e%2e', '', None, 2]
for value in bad_ids:
    try:
        character_paths(value, 1)
    except NotFound:
        pass
    else:
        raise AssertionError(value)
for uid in (None, 0, -1, True, '../2', '2'):
    try:
        character_paths('saki_demo', uid)
    except NotFound:
        pass
    else:
        raise AssertionError(uid)
assert a.get('/api/b_only/history').status_code == 404
assert a.get('/chat/b_only').status_code == 404
schedule = Path(character_paths('saki_demo', 1)[1], '7_schedule.json')
before = schedule.read_bytes()
for target in ('b_only', '../2/characters/saki_demo', '/tmp/escape'):
    r = a.post('/api/distribute_schedule', json={'target_ids': ['saki_demo', target], 'content': {'_undated': ['new']}})
    assert r.status_code == 404, r.json
    assert schedule.read_bytes() == before
import app as app_module
def forbidden_model(*args, **kwargs):
    raise AssertionError('Model called for unauthorized resource')
with patch.object(app_module, 'call_gemini', forbidden_model), patch.object(app_module, 'call_openrouter', forbidden_model):
    for key in ('char_id', 'group_id'):
        for value in ('../2/characters/saki_demo', 'b_only', '%2e%2e', ''):
            r = a.post('/api/translate', json={key: value, 'message_id': 1, 'text': 'x'})
            assert r.status_code == 404, r.json
    assert a.get('/api/%2e%2e/history').status_code == 404
    assert a.post('/api/memory/ai_complete_relation', json={'char_id': 'b_only', 'name': 'B'}).status_code == 404
    assert a.post('/api/generate_persona', json={'char_id': 'b_only', 'char_name': 'B', 'source_ip': 'Synthetic'}).status_code == 404

# A configured symlink and a symlinked database are both rejected.
cfg = Path(USERS_ROOT, '1/configs/characters.json')
catalog = json.loads(cfg.read_text()); catalog['linked'] = {}; catalog['lazy_template'] = {}
cfg.write_text(json.dumps(catalog))
Path(USERS_ROOT, '1/characters/linked').symlink_to(b_root / 'characters/saki_demo', target_is_directory=True)
assert a.get('/api/linked/history').status_code == 404
db, _ = character_paths('a_only', 1)
Path(db).unlink(); Path(db).symlink_to(b_root / 'characters/saki_demo/chat.db')
assert a.get('/api/a_only/history').status_code == 404
Path(USERS_ROOT, '1/groups/shared_group/memory_short.json').symlink_to(b_root / 'groups/shared_group/memory_short.json')
assert a.get('/api/group/shared_group/prompts_data').status_code == 404
linked_schedule = Path(character_paths('a_other', 1)[1], '7_schedule.json')
linked_schedule.unlink(); linked_schedule.symlink_to(b_root / 'characters/saki_demo/prompts/7_schedule.json')
assert a.get('/api/a_other/prompts_data').status_code == 404
db, prompts = character_paths('lazy_template', 1)
assert not Path(db).parent.exists()  # Lookup is side-effect free.
template = Path(CHARACTERS_DIR, 'lazy_template'); (template / 'prompts').mkdir(parents=True)
(template / 'prompts/1_base_persona.json').write_text('{}')
(template / 'chat.db').write_text('MUST NOT COPY')
initialize_character_template('lazy_template', 1)
assert Path(prompts, '1_base_persona.json').exists() and not Path(db).exists()
assert a.post('/api/distribute_schedule', json={'target_ids': ['saki_demo'], 'content': {'_undated': ['owned']}}).status_code == 200
print('PASS same-root ownership, traversal, symlinks, atomic authorization, creation, template initialization')
''')

    def test_forum_task_owner_lifecycle_and_error_branches(self):
        self.run_case('''
import blueprints.forum as forum
# All providers below are fakes. Keep runtime network/write guards and real auth.
app.before_request_funcs[None] = [f for f in app.before_request_funcs[None] if f.__name__ != 'check_feature_configuration']
class HeldThread:
    def __init__(self, **kwargs): pass
    def start(self): pass
payload = {'topic': 'Synthetic', 'chat_segments': [{'messages': [{'role': 'user', 'content': 'sample'}]}]}
with patch.object(forum.threading, 'Thread', HeldThread):
    r = a.post('/api/forums/generate', json=payload)
assert r.status_code == 200, r.json
tid = r.json['task_id']; assert len(tid) == 36
url = '/api/forums/task/' + tid
assert a.get(url).json == {'status': 'working'}
assert b.get(url).status_code == b.get('/api/forums/task/missing').status_code == 404
assert app.test_client().get(url).status_code == 401
tasks = forum.forum_tasks
db_path = forum.FORUMS_DB
def snapshot(task_id):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT * FROM forum_tasks WHERE id=?', (task_id,)).fetchone()
tasks.fail(db_path, tid, 2, 'wrong owner')
assert a.get(url).json == {'status':'working'}
text = 'Generation interrupted; check your forum history before retrying.'
tasks.fail(db_path, tid, 1, text)
from core.localization import CATALOG
assert a.get(url).json['error'] in CATALOG[text].values()
assert a.get(url).json['status'] == 'error'
assert b.get(url).status_code == 404
tasks.release(db_path, tid, 1)
def broken_thread(**kwargs):
    raise RuntimeError('Synthetic thread creation failure')
with patch.object(forum.threading, 'Thread', broken_thread):
    assert a.post('/api/forums/generate', json=payload).status_code == 503
with sqlite3.connect(db_path) as db:
    assert not db.execute("SELECT COUNT(*) FROM forum_tasks WHERE active=1").fetchone()[0]

good = json.dumps({'title': 'Synthetic', 'posts': [{'name': 'Author', 'content': 'Text', 'floor': 0}]})
for mode in ('history_missing', 'parse', 'validation', 'exception', 'done'):
    task_id = tasks.create(db_path, 1)
    def fake_call(*args, **kwargs):
        if mode == 'exception': raise RuntimeError('synthetic provider failure')
        return 'bad' if mode == 'parse' else ('{"title":"x","posts":"bad"}' if mode == 'validation' else good)
    with patch.object(forum, 'call_active_model', fake_call):
        forum._do_generate(task_id, 1, 'Synthetic', payload['chat_segments'], 'novel_readers', '', 1, '', 99999 if mode == 'history_missing' else None, 'en')
    task = snapshot(task_id)
    assert task['user_id'] == 1 and task['status'] == ('done' if mode == 'done' else 'error'), (mode, task)
    assert not task["active"]
    assert a.get('/api/forums/task/' + task_id).status_code == 200
    assert b.get('/api/forums/task/' + task_id).status_code == 404

late = tasks.create(db_path, 1)
started = snapshot(late)['created']
with patch.object(tasks.time, 'time', return_value=started + tasks.TIMEOUT + 1):
    r = a.get('/api/forums/task/' + late)
    assert r.json['status'] == 'error'
    assert snapshot(late)['status']=='error'
    assert snapshot(late)["active"]  # A timed-out worker still consumes its slot.
with patch.object(tasks, 'CAPACITY', 1):
    assert a.post('/api/forums/generate', json=payload).status_code == 429
with patch.object(tasks.time, 'time', return_value=started + tasks.TIMEOUT + tasks.TTL + 2):
    assert a.get('/api/forums/task/' + late).status_code == 404
with sqlite3.connect(USERS_DB) as conn:
    conn.execute('UPDATE users SET auth_version=auth_version+1 WHERE id=1')
assert a.get(url).status_code == 401
print('PASS forum ownership, error branches, bounded state, visible timeout, no late write, revoked session')
''')


if __name__ == '__main__':
    unittest.main()
