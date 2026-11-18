from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core import web_agent as queue
from services import agent_proxy
import run_agent
from tests.test_saki_local import child


def job_kwargs(uid=1):
    return dict(user_id=uid, char_id='hero', url='https://example.org/', cookie='session=synthetic',
                ui_language='ja', output_language='Spanish')


def test_queue_busy_credential_lifetime_and_recovery(tmp_path):
    root = queue.runtime_root(tmp_path)
    with pytest.raises(queue.AgentUnavailable):
        queue.enqueue(tmp_path, **job_kwargs())
    queue.set_worker_heartbeat(root)
    def submit(uid):
        try:
            return queue.enqueue(tmp_path, **job_kwargs(uid))
        except queue.AgentBusy:
            return None
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(submit, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert (root / 'pending.json').stat().st_mode & 0o777 == 0o600
    job = queue.take_job(root)
    assert job['cookie'] == 'session=synthetic'
    assert not (root / 'pending.json').exists()
    assert 'synthetic' not in (root / 'active.json').read_text()
    with pytest.raises(queue.AgentBusy):
        queue.enqueue(tmp_path, **job_kwargs(3))
    run_agent.recover(tmp_path, root)
    assert not (root / 'active.json').exists()
    state = queue.read_json(tmp_path / 'users' / str(job['user_id']) / 'agent_state.json')
    assert state['status'] == 'interrupted' and state['running'] is False


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com', 'http://127.0.0.1',
    'http://[::1]/', 'http://169.254.169.254/', 'http://localhost/', 'http://host.local/',
    'https://user:password@example.org/', 'https://example.org:8010', 'https://example.org/\n'])
def test_invalid_initial_urls(url):
    with pytest.raises(ValueError):
        queue.validate_url(url)


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.168.0.1',
    '172.16.0.1', '0.0.0.0', '224.0.0.1', '::1', 'fc00::1', 'fe80::1', '::ffff:8.8.8.8',
    '2002:0808:0808::1', '64:ff9b::808:808', '64:ff9b:1::1'])
def test_proxy_rejects_private_and_ip_translation(ip):
    assert not agent_proxy.public_address(ip)


def test_proxy_pins_dns_and_rejects_mixed_answers():
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))
    private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
    with patch.object(socket, 'getaddrinfo', return_value=[public, private]), patch.object(socket, 'socket') as factory:
        with pytest.raises(ValueError):
            agent_proxy.public_connect('host.example', 443)
        factory.assert_not_called()
    with patch.object(socket, 'getaddrinfo', return_value=[public]) as dns, patch.object(socket, 'socket') as factory:
        agent_proxy.public_connect('host.example', 443)
        dns.assert_called_once()
        factory.return_value.connect.assert_called_once_with(('93.184.215.14', 443))


def test_live_proxy_blocks_loopback_http_and_connect():
    with agent_proxy.PublicWebProxy() as endpoint:
        port = int(endpoint.rsplit(':', 1)[1])
        for line in ('GET http://127.0.0.1/ HTTP/1.1', 'CONNECT 127.0.0.1:443 HTTP/1.1'):
            with socket.create_connection(('127.0.0.1', port), timeout=3) as conn:
                conn.sendall((line + '\r\nHost: 127.0.0.1\r\n\r\n').encode())
                assert conn.recv(512).startswith(b'HTTP/1.1 403')


def test_child_environment_contains_no_application_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('COS_SECRET_KEY', 'not-for-browser')
    monkeypatch.setenv('FLASK_SECRET_KEY', 'not-for-browser')
    monkeypatch.setenv('CREDENTIAL_ENCRYPTION_KEYS', 'not-for-browser')
    monkeypatch.setenv('NODE_OPTIONS', '--require=unsafe')
    env = run_agent.child_environment(tmp_path)
    assert not {'COS_SECRET_KEY', 'FLASK_SECRET_KEY', 'CREDENTIAL_ENCRYPTION_KEYS', 'NODE_OPTIONS'} & env.keys()
    assert env['SAKI_DATA_ROOT'] == str(tmp_path)


def test_worker_cleans_slot_on_launch_failure(tmp_path):
    root = queue.runtime_root(tmp_path)
    queue.set_worker_heartbeat(root)
    queue.enqueue(tmp_path, **job_kwargs())
    job = queue.take_job(root)
    with patch.object(run_agent, 'browser_process', side_effect=OSError('launch failed')):
        with pytest.raises(OSError):
            run_agent.execute_job('node', tmp_path, root, job, threading.Event())
    assert not (root / 'active.json').exists()
    assert queue.read_json(tmp_path / 'users/1/agent_state.json')['status'] == 'error'


def test_actual_routes_owner_languages_stale_state_and_revoked_session():
    with tempfile.TemporaryDirectory(prefix='saki-agent-test-') as folder:
        result = child(Path(folder), r'''
import json, sqlite3, time
from pathlib import Path
from unittest.mock import patch
from saki_local import seed
import app as application
from blueprints import chat
from core.config import USERS_DB
from core.runtime import DATA_ROOT
from core.web_agent import atomic_json, read_json, runtime_root, set_worker_heartbeat
from core.localization import save_ui_language
seed()
c = application.app.test_client()
assert c.post('/api/run_agent', json={}).status_code == 401
assert c.post('/api/login', json={'username':'test@saki.invalid','password':'synthetic-test-password-only'}).status_code == 200
root = runtime_root(DATA_ROOT)
set_worker_heartbeat(root)
for endpoint in ('stop', 'reply', 'web_action', 'notify_user'):
    response = c.post('/api/agent/'+endpoint, json={'user_id':2, 'char_id':'saki_demo', 'message':'test', 'content':'test'})
    assert response.status_code == 403, (endpoint, response.status_code)
assert c.get('/api/agent/state?user_id=2').status_code == 403
assert c.post('/api/agent/reply', json={'char_id':'../another', 'message':'x'}).status_code == 404
assert c.post('/api/run_agent', json={'char_id':'../another', 'url':'https://example.org'}).status_code == 404
assert c.post('/api/run_agent', json=[]).status_code in (400,404)
chars = Path(DATA_ROOT)/'users/1/configs/characters.json'
config=json.loads(chars.read_text()); config['saki_demo']['language']='Spanish'; atomic_json(chars, config)
for locale in ('ja','en','zh-CN'):
    save_ui_language(1,locale)
    with patch.object(chat,'LOCAL_TEST',False):
        response=c.post('/api/run_agent', json={'user_id':1,'char_id':'saki_demo','url':'https://example.org'})
    assert response.status_code == 202, response.json
    pending = read_json(root/'pending.json')
    assert pending['ui_language']==locale and pending['output_language']=='Spanish', pending.keys()
    assert pending['cookie'].startswith('saki_global_session=')
    with patch.object(chat,'LOCAL_TEST',False):
        assert c.post('/api/run_agent',json={'char_id':'saki_demo','url':'https://example.org'}).status_code==409
    (root/'pending.json').unlink()
atomic_json(root/'heartbeat.json', {'updated':0})
state=c.get('/api/agent/state').json
assert state['running'] is False and state['status']=='interrupted',state
with sqlite3.connect(USERS_DB) as db:
    db.execute('UPDATE users SET auth_version=auth_version+1 WHERE id=1')
assert c.get('/api/agent/state').status_code == 401
assert c.post('/api/agent/stop',json={}).status_code == 401
print('PASS real Agent authentication, owner checks, language selection and revocation')
''')
        assert result.returncode == 0, result.stdout + result.stderr


def test_live_proxy_forwards_public_http_and_tunnels_without_interception():
    # Substitute only the verified remote socket; exercise the real parser and relay.
    captured = []
    workers = []
    def connect(host, port):
        client, remote = socket.socketpair()
        def respond():
            with remote:
                remote.settimeout(3)
                if port == 80:
                    request = b''
                    while b'\r\n\r\n' not in request:
                        request += remote.recv(4096)
                    captured.append(request)
                    remote.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
                else:
                    remote.sendall(remote.recv(4))
        worker = threading.Thread(target=respond)
        workers.append(worker)
        worker.start()
        return client
    with patch.object(agent_proxy, 'public_connect', side_effect=connect), agent_proxy.PublicWebProxy() as endpoint:
        port=int(endpoint.rsplit(':',1)[1])
        with socket.create_connection(('127.0.0.1',port),timeout=3) as conn:
            conn.sendall(b'GET http://public.example/read?a=1 HTTP/1.1\r\nHost: forged.example\r\nProxy-Authorization: do-not-forward\r\n\r\n')
            response=b''
            while True:
                data=conn.recv(4096)
                if not data: break
                response+=data
            assert response.endswith(b'OK') and response.startswith(b'HTTP/1.1 200')
        with socket.create_connection(('127.0.0.1',port),timeout=3) as conn:
            conn.sendall(b'CONNECT public.example:443 HTTP/1.1\r\nHost: public.example\r\n\r\n')
            assert b'200 Connection Established' in conn.recv(4096)
            conn.sendall(b'TLS!')
            assert conn.recv(4)==b'TLS!'
    for worker in workers: worker.join(3)
    assert b'GET /read?a=1 HTTP/1.1' in captured[0]
    assert b'Host: public.example' in captured[0]
    assert b'forged.example' not in captured[0] and b'do-not-forward' not in captured[0]
