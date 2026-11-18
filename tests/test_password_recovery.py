"""Independent processes and temporary accounts. Never send real mail."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.password_recovery import RecoveryStore, Limited, source_address

SECRET = b'synthetic-database-test-only'
EMAIL = 'recovery@saki.invalid'
IP = '192.0.2.8'


def _claim_other(path):
    return RecoveryStore(path, SECRET).claim(EMAIL, IP, '083651')


def _reserve_other(path):
    try:
        return bool(RecoveryStore(path, SECRET).reserve(EMAIL, IP))
    except Limited:
        return False


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='saki-recovery-unit-'); self.addCleanup(tmp.cleanup)
        self.user_path = Path(tmp.name) / 'users.db'
        self.s = RecoveryStore(self.user_path, SECRET)

    def issue(self, value='083651'):
        ticket = self.s.reserve(EMAIL, IP)
        self.assertTrue(self.s.finish(EMAIL, ticket, accepted=(12, 7, value)))

    def test_restart_and_process_races(self):
        with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context('spawn')) as procs:
            admitted = list(procs.map(_reserve_other, [self.user_path] * 6)); self.assertEqual(sum(admitted), 1)
            with closing(sqlite3.connect(self.s.path)) as db:
                ticket, = db.execute("SELECT ticket FROM challenges WHERE stage='pending'").fetchone()
            self.s.finish(EMAIL, ticket, accepted=(12, 7, '083651'))
            results = list(procs.map(_claim_other, [self.user_path] * 6))
        self.assertEqual(results.count((12, 7)), 1); self.assertEqual(results.count(None), 5)
        self.assertIsNone(RecoveryStore(self.user_path, SECRET).claim(EMAIL, IP, '083651'))

    def test_digest_only_and_fail_limit(self):
        self.issue()
        with closing(sqlite3.connect(self.s.path)) as db:
            rows = '\n'.join(db.iterdump()); self.assertNotIn('083651', rows)
            self.assertNotIn(EMAIL, rows); self.assertNotIn(IP, rows)
        self.assertEqual(self.s.path.stat().st_mode & 0o777, 0o600)
        for _ in range(5): self.assertIsNone(self.s.claim(EMAIL, IP, '000000'))
        self.assertIsNone(self.s.claim(EMAIL, IP, '083651'))

    def test_failed_resend_retains_old_and_superseded_delivery_cannot_publish(self):
        with patch('core.password_recovery.time.time',return_value=1000): self.issue()
        with patch('core.password_recovery.time.time',return_value=1060):
            late=self.s.reserve(EMAIL, IP); self.s.finish(EMAIL,late)  # failed SMTP
        with patch('core.password_recovery.time.time',return_value=1120): pending=self.s.reserve(EMAIL,IP)
        with patch('core.password_recovery.time.time',return_value=1180):
            newer=self.s.reserve(EMAIL,IP); self.assertFalse(self.s.finish(EMAIL,pending,accepted=(12,7,'111111')))
            self.assertTrue(self.s.finish(EMAIL,newer,accepted=(12,7,'222222')))
            self.assertIsNone(self.s.claim(EMAIL,IP,'083651')); self.assertEqual(self.s.claim(EMAIL,IP,'222222'),(12,7))
        with patch('core.password_recovery.time.time',return_value=6000): self.issue()
        with patch('core.password_recovery.time.time',return_value=6060):
            self.s.finish(EMAIL,self.s.reserve(EMAIL,IP)); self.assertEqual(self.s.claim(EMAIL,IP,'083651'),(12,7))

    def test_cooldown_expiry_and_address_budget(self):
        with patch('core.password_recovery.time.time', return_value=1000): self.issue()
        with patch('core.password_recovery.time.time', return_value=1059):
            with self.assertRaises(Limited) as e: self.s.reserve(EMAIL,IP)
            self.assertEqual(e.exception.retry_after,1)
        with patch('core.password_recovery.time.time', return_value=1600): self.assertIsNone(self.s.claim(EMAIL,IP,'083651'))
        for i in range(4):
            with patch('core.password_recovery.time.time', return_value=1700+i*60): self.issue()
        with patch('core.password_recovery.time.time', return_value=2000):
            with self.assertRaises(Limited): self.s.reserve(EMAIL,'192.0.2.9')

    def test_unknown_targets_charge_equal_ip_budget_and_guess_limits(self):
        for i in range(20):
            addr=f'{i}@saki.invalid'; self.s.finish(addr,self.s.reserve(addr,IP))
        with self.assertRaises(Limited): self.s.reserve(EMAIL,IP)
        for i in range(30):self.assertIsNone(self.s.claim(f'{i}@saki.invalid',IP,'000000'))
        with self.assertRaises(Limited):self.s.claim(EMAIL,IP,'000000')
        for i in range(10):self.assertIsNone(self.s.claim(EMAIL,f'198.51.100.{i}','000000'))
        with self.assertRaises(Limited):self.s.claim(EMAIL,'192.0.2.55','000000')
        self.assertEqual(source_address('192.0.2.1','198.51.100.1'),'192.0.2.1')
        self.assertEqual(source_address('127.0.0.1','198.51.100.1'),'198.51.100.1')
        self.assertEqual(source_address('127.0.0.1','a,b'),'127.0.0.1')


class ApplicationTests(unittest.TestCase):
    def test_real_routes_transport_and_revocation(self):
        from test_saki_local import child
        with tempfile.TemporaryDirectory(prefix='saki-recovery-http-') as data:
            result = child(data, r'''
from unittest.mock import patch
import smtplib, os, sqlite3
from app import app
from saki_local import seed
from blueprints import auth
from core.config import USERS_DB
seed(); os.environ['SAKI_TEST_EMAIL']='second@saki.invalid';seed()
a='test@saki.invalid'; b='second@saki.invalid'; c=app.test_client(); old=app.test_client()
def login(client,email,password='synthetic-test-password-only'):
 return client.post('/api/login',json={'username':email,'password':password}).status_code
assert login(c,a)==login(c,b)==login(old,a)==200
assert c.post('/api/forgot_password/send_code',json={'email':a}).status_code==503
app.before_request_funcs[None]=[f for f in app.before_request_funcs[None] if f.__name__!='check_feature_configuration']
def send(em):return c.post('/api/forgot_password/send_code',json={'email':em})
def use(em,code,new='synthetic-reset-password'):
 return c.post('/api/forgot_password/reset',json={'email':em,'code':code,'password':new})
class Inline:
 def __init__(self,target,args,**kw):self.invoke=lambda:target(*args)
 def start(self):self.invoke()
with patch.object(auth.threading,'Thread',Inline), patch.object(auth,'_reset_mail') as mail:
 assert c.get('/api/forgot_password/send_code').status_code==204
 assert not __import__('pathlib').Path(USERS_DB).with_name('password-recovery.sqlite3').exists()
 reg=send(a); assert reg.status_code==202 and reg.json['status']=='accepted'
 code=mail.call_args.args[2]; assert len(code)==6 and code.isascii() and code.isdigit()
 unknown=send('unknown@saki.invalid'); assert reg.json==unknown.json and reg.status_code==unknown.status_code and reg.headers['Retry-After']==unknown.headers['Retry-After'];assert mail.call_count==1
 assert send(a).status_code==429 and send('unknown@saki.invalid').status_code==429
 assert use(a,code,'short').status_code==400
 assert use(b,code).status_code==400
 assert use(a,code).status_code==200; assert use(a,code).status_code==400
 assert old.get('/api/user/profile_settings').status_code==401
 assert c.post('/api/accounts/switch',json={'user_id':1}).status_code==403
 assert login(old,a)==401; assert login(old,a,'synthetic-reset-password')==200
 # Password changes between issue and claim invalidate the old version.
 assert send(b).status_code==202; old_code=mail.call_args.args[2]
 with sqlite3.connect(USERS_DB) as db:db.execute('UPDATE users SET auth_version=auth_version+1 WHERE email=?',(b,))
 assert use(b,old_code).status_code==400
 # A transport outage yields no usable new code and a service-wide, non-enumerating backoff.
 with patch('core.password_recovery.time.time',return_value=__import__('time').time()+80),patch.object(auth,'_reset_mail',side_effect=smtplib.SMTPAuthenticationError(535,b'test')) as broken:
  receipt=send(a); assert receipt.status_code==202 and receipt.json['status']=='accepted'
  assert use(a,broken.call_args.args[2]).status_code==400
  assert c.get('/api/forgot_password/send_code').status_code==503
  for email in (a,'not-on-file@saki.invalid'):assert send(email).status_code==503
 # A failure to start the dispatcher is reported directly, not as admission.
 with patch.object(Inline,'start',side_effect=RuntimeError('synthetic dispatch failure')),patch('core.password_recovery.time.time',return_value=__import__('time').time()+250):assert send(a).status_code==503
# A login already checking the previous password cannot mint a new-version session.
real_check=auth.check_password_hash
def raced(digest,pwd):
 good=real_check(digest,pwd)
 with sqlite3.connect(USERS_DB) as db:db.execute('UPDATE users SET auth_version=auth_version+1 WHERE email=?',(a,))
 return good
with patch.object(auth,'check_password_hash',raced):assert login(app.test_client(),a,'synthetic-reset-password')==401
# Verify the real mail builder/transport order with fake SMTP (no network or credentials).
with app.app_context(), patch.dict(os.environ,{'MAIL_SERVER':'smtp.example.invalid','MAIL_SENDER':'sender@example.invalid','MAIL_PASSWORD':'synthetic','MAIL_PORT':'465'}):
 with patch.object(auth.smtplib,'SMTP_SSL') as smtp:
  server=smtp.return_value;server.sendmail.return_value={};auth._reset_mail(a,1,'012345')
  import ssl,email as email_module
  assert smtp.call_args.kwargs['context'].verify_mode==ssl.CERT_REQUIRED and smtp.call_args.kwargs['context'].check_hostname
  mail=server.sendmail.call_args.args;assert mail[1]==[a] and '012345' in email_module.message_from_bytes(mail[2]).get_payload(decode=True).decode()
  assert [x[0] for x in server.method_calls]==['login','sendmail','close']
 with patch.dict(os.environ,{'MAIL_PORT':'587'}),patch.object(auth.smtplib,'SMTP') as smtp:
  smtp.return_value.sendmail.return_value={};auth._reset_mail(a,1,'012345');assert [x[0] for x in smtp.return_value.method_calls]==['starttls','login','sendmail','close']
print('PASS: reset and device grant revoked; fake SMTP only')
''')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
