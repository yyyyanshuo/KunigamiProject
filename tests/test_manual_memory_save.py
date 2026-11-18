import errno
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from core.json_storage import StorageWriteError, atomic_write_json
from services.memory_editor import EditConflict, save_memory_edits
from services.memory_store import MemoryStoreBusy, MemoryStoreError, memory_file_lock


def edit(path, changes, kind='medium'):
    return save_memory_edits(kind, str(path), str(path.parent/'chat.db'), {'changes': changes})


def change(before, after):
    return {'before': before, 'after': after}


def test_distinct_edits_merge_and_overlap_conflicts(tmp_path):
    path = tmp_path/'medium.json'
    atomic_write_json(path, {'a':'old', 'b':'other'})
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(edit, path, {k:change(v, v+'!')}) for k,v in [('a','old'),('b','other')]]
        for f in futures:
            f.result()
    # Replay an acknowledged or unacknowledged edit without disturbing the other one.
    assert edit(path, {'a':change('old','old!')}) == {'a':'old!'}
    with pytest.raises(EditConflict):
        edit(path, {'c':change(None,'new'), 'b':change('other','overwritten')})
    assert json.loads(path.read_text()) == {'a':'old!', 'b':'other!'}
    assert edit(path, {'a':change('old!',None)}) == {'a':None}
    assert edit(path, {'a':change('old!',None)}) == {'a':None}


def test_same_entry_concurrent_writes_only_one_wins(tmp_path):
    path = tmp_path/'long.json'
    def submit(value):
        try:
            return edit(path, {'week':change(None,value)}, 'long')
        except EditConflict:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, ('one','two')))
    assert results.count(None) == 1


def test_short_day_cursors_and_unrelated_history(tmp_path):
    path = tmp_path/'short.json'
    day = '2026-11-01'
    old = {'time':'09:04','event':'a'}
    keep = dict(old, source='call', timestamp='2026-11-01T09:04:00+08:00')
    previous = {'events':[keep], 'last_id':4, 'call_last_id':8, 'call_coverage':[],
                'timezone':'Asia/Shanghai'}
    atomic_write_json(path, {day:previous, '2026-10-30':previous})
    with sqlite3.connect(tmp_path/'chat.db') as db:
        db.execute('CREATE TABLE messages(id INTEGER, timestamp TEXT)')
        db.execute("INSERT INTO messages VALUES (9, '2026-11-01 10:30:21')")
    new = [old, {'time':'10:30','event':'new'}]
    payload = {day:change([old],new)}
    applied = edit(path,payload,'short')[day]
    assert applied['events'][0] == keep
    assert (applied['last_id'],applied['call_last_id']) == (9,8)
    assert edit(path,payload,'short')[day] == applied
    assert json.loads(path.read_text())['2026-10-30'] == previous
    reset = {day:{**change(new,new),'reset':True,'progress':[9,8,[],[]]}}
    assert edit(path,reset,'short')[day]['last_id'] == 0
    assert edit(path,reset,'short')[day]['call_last_id'] == 0
    state = json.loads(path.read_text());state[day]['call_last_id']=11;atomic_write_json(path,state)
    with pytest.raises(EditConflict):
        edit(path,reset,'short')


def test_short_without_chat_db_and_dirty_storage(tmp_path):
    path = tmp_path/'short.json'
    data = {'2026-09-01':change([], [{'time':'12:00','event':'manual'}])}
    assert edit(path,data,'short')['2026-09-01']['last_id'] == 0
    assert not (tmp_path/'chat.db').exists()
    path.write_text('{"broken"')
    with pytest.raises(MemoryStoreError, match='unable to read memory file'):
        edit(path,data,'short')
    assert path.read_text() == '{"broken"'


def test_lock_budget_covers_same_process_and_disk_lock(tmp_path):
    path=str(tmp_path/'short.json')
    with ThreadPoolExecutor(1) as pool:
        def short_wait():
            with memory_file_lock(path, timeout=.05):
                pytest.fail('acquired occupied memory lock')
        with memory_file_lock(path):
            with pytest.raises(MemoryStoreBusy):
                pool.submit(short_wait).result(timeout=1)
        Path(path+'.lock').touch()
        with pytest.raises(MemoryStoreBusy):
            short_wait()


@pytest.mark.parametrize('stage',['mkstemp','dump','fsync','replace'])
def test_writer_failure_is_visible_and_storage_unchanged(tmp_path, monkeypatch, stage):
    from core import json_storage
    from core import utils
    import app, memory_jobs
    assert app.safe_save_json is utils.safe_save_json is memory_jobs.safe_save_json is atomic_write_json
    path=tmp_path/'saved.json';path.write_bytes(b'{"old":true}')
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC,'synthetic storage failure')
    owner = {'mkstemp':json_storage.tempfile, 'dump':json, 'fsync':os, 'replace':os}[stage]
    monkeypatch.setattr(owner,stage,fail)
    with pytest.raises(StorageWriteError):
        utils.safe_save_json(path, {'new':True})
    assert path.read_bytes() == b'{"old":true}'
    assert list(tmp_path.iterdir()) == [path]


def test_flask_save_ownership_validation_conflict_and_disk_error(tmp_path, monkeypatch):
    from app import app
    from saki_local import seed
    from blueprints import chat
    seed()
    client=app.test_client()
    assert client.post('/api/login',json={'username':'test@saki.invalid','password':'synthetic-test-password-only'}).status_code == 200
    cid='saki_demo'
    data=client.get(f'/api/{cid}/prompts_data').get_json()
    owner=data['_editor_user']
    monkeypatch.setattr(chat, 'get_paths', lambda *a: (str(tmp_path/'chat.db'), str(tmp_path)))
    def send(**overrides):
        return client.post(f'/api/{cid}/memory/edits',json={'key':'medium','owner':owner,'changes':{'day':change(None,'saved')},**overrides})
    assert app.test_client().post(f'/api/{cid}/memory/edits',json={}).status_code == 401
    assert send(owner='another-account').status_code == 403
    assert send(changes={'day':{'after':'new'}}).status_code == 400
    assert send().json == {'status':'success','applied':{'day':'saved'}}
    assert send(changes={'day':change('stale','new')}).status_code == 409
    with patch('os.replace',side_effect=OSError(errno.ENOSPC,'synthetic')):
        res=send(changes={'day':change('saved','new')})
        assert (res.status_code,res.json['code'])==(503,'storage_write_failed')
        res=client.post('/api/user/profile_settings',json={'name':'synthetic-new-name'})
        assert (res.status_code,res.json['code'])==(503,'storage_write_failed')
    assert json.loads((tmp_path/'5_memory_medium.json').read_text())=={'day':'saved'}
    assert (client.post(f'/api/{cid}/save_prompt',json={'key':'long','content':{'w':'legacy'}}).status_code)==200


def test_background_yearly_marker_is_not_completed_on_failed_commit(tmp_path, monkeypatch):
    import app
    import memory_jobs
    path = tmp_path/'characters.json'
    atomic_write_json(path, {'demo': {'age':20}})
    monkeypatch.setattr(app, '_get_characters_config_file', lambda: str(path))
    monkeypatch.setattr(app, '_get_user_settings_file', lambda: str(tmp_path/'absent.json'))
    with patch('os.replace', side_effect=OSError(errno.ENOSPC,'synthetic')):
        with pytest.raises(StorageWriteError):
            memory_jobs._process_single_user_yearly_age_increment(1, '2026')
    assert json.loads(path.read_text()) == {'demo': {'age':20}}
