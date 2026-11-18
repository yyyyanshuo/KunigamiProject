"""Exercise real contact APIs in isolated data; no production data or model calls."""
import tempfile
import unittest
from tests.test_saki_local import child


class ContactsTimezoneTests(unittest.TestCase):
    def test_contacts_use_account_calendar_and_absolute_sorting(self):
        with tempfile.TemporaryDirectory(prefix='saki-contacts-time-') as root:
            result = child(root, r'''
import os, time, sqlite3
from datetime import datetime
from unittest.mock import patch
import app as module
from app import app
from saki_local import seed
from core.utils import get_group_dir
seed()
c=app.test_client()
assert c.post('/api/login',json={'username':'test@saki.invalid','password':'synthetic-test-password-only'}).status_code==200
assert c.post('/api/characters/add',json={'id':'time_other','name':'Other'}).status_code==200
assert c.post('/api/groups/add',json={'id':'time_group','name':'Group','members':['saki_demo','time_other']}).status_code==200
with app.test_request_context('/'):
    from flask import session
    session['user_id']=1
    single,_=module.get_paths('saki_demo')
    group=os.path.join(get_group_dir('time_group'),'chat.db')

def put(path, timestamp, role):
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM messages')
        conn.execute('INSERT INTO messages(role,content,timestamp) VALUES(?,?,?)',(role,'timezone fixture',timestamp))

def snapshot(path):
    with sqlite3.connect(path) as conn:
        return conn.execute('SELECT id,role,content,timestamp FROM messages').fetchall()

# Same instant gives Tokyo next-day time and Los Angeles same-day time.
put(single,'2026-09-23 23:30:00','assistant')
put(group,'2026-09-23 12:00:00','saki_demo')
original=(snapshot(single),snapshot(group))
expected_ts=datetime.fromisoformat('2026-09-23T15:30:00+00:00').timestamp()
with patch.object(module,'utc_now',return_value=datetime.fromisoformat('2026-09-23T16:30:00+00:00')):
 for server_zone in ['UTC','Asia/Tokyo','America/Los_Angeles']:
    os.environ['TZ']=server_zone;time.tzset()
    for zone, single_time, group_time in [('Asia/Tokyo','00:30','09-23'),('America/Los_Angeles','08:30','09-22'),('Asia/Shanghai','09-23','09-23')]:
        assert c.post('/api/user/profile_settings',json={'timezone':zone}).status_code==200
        r=c.get('/api/contacts');assert r.status_code==200,r.json
        contacts={x['id']:x for x in r.json}
        assert contacts['saki_demo']['last_time']==single_time,(zone,contacts['saki_demo'])
        assert contacts['time_group']['last_time']==group_time,(zone,contacts['time_group'])
        assert contacts['saki_demo']['timestamp']==expected_ts
        assert contacts['saki_demo']['unread']==1 and contacts['time_group']['unread']==1
        ids=[x['id'] for x in r.json];assert ids.index('saki_demo')<ids.index('time_group')
assert (snapshot(single),snapshot(group))==original
# Repeated DST hour has the same label but distinct sortable instants.
put(single,'2026-11-01 16:30:00','assistant')
put(group,'2026-11-01 17:30:00','saki_demo')
c.post('/api/user/profile_settings',json={'timezone':'America/Los_Angeles'})
with patch.object(module,'utc_now',return_value=datetime.fromisoformat('2026-11-01T12:00:00+00:00')):
    r=c.get('/api/contacts');assert r.status_code==200,r.json
    contacts={x['id']:x for x in r.json}
    assert contacts['saki_demo']['last_time']==contacts['time_group']['last_time']=='01:30'
    assert contacts['time_group']['timestamp']-contacts['saki_demo']['timestamp']==3600
    assert [x['id'] for x in r.json][:2]==['time_group','saki_demo']
print('PASS single/group local-day labels, three server zones, DST sorting, unread counts and unchanged chat storage')
''')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':
    unittest.main()
