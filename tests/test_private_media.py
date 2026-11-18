"""Private media isolation and streaming tests; no real COS or credentials."""
import io
import json
import sys
from types import SimpleNamespace

import pytest
from flask import Flask, Response, jsonify, redirect, session
from core.private_media import MEDIA_PREFIX, browser_urls, install_private_media, permitted_key
from qcloud_cos.cos_exception import CosServiceError

ORIGIN = 'https://synthetic.cos.test.myqcloud.com'


class FakeCOS:
    def __init__(self):
        self.calls = []
        self.streams = []
        self.failure = None
        self.head_failure = None

    def head_object(self, **options):
        self.calls.append(('HEAD', options))
        if self.head_failure:
            raise self.head_failure
        return {'Content-Length': '5'}

    def get_object(self, **options):
        self.calls.append(('GET', options))
        if self.failure:
            raise self.failure
        data = b'photo'
        headers = {'Content-Length': '5'}
        if options.get('Range') == 'bytes=0-1':
            data = b'ph'
            headers = {'Content-Length': '2', 'Content-Range': 'bytes 0-1/5'}
        if options.get('Range') == 'bytes=0-0':
            data = b'p'
            headers = {'Content-Length': '1', 'Content-Range': 'bytes 0-0/5'}
        stream = io.BytesIO(data)
        self.streams.append(stream)
        return {'Body': SimpleNamespace(get_raw_stream=lambda: stream), **headers}


@pytest.fixture
def site(monkeypatch):
    cos = FakeCOS()
    monkeypatch.setitem(sys.modules, 'cos_utils', SimpleNamespace(
        client=cos, bucket='synthetic', LOCAL_TEST=False))
    app = Flask(__name__)
    app.secret_key = 'synthetic-test-key'
    install_private_media(app, ORIGIN)

    @app.route('/links')
    def links():
        return jsonify(image=ORIGIN + '/users/1/avatar.png?t=1',
                       public=ORIGIN + '/square/avatars/a.png',
                       external='https://elsewhere.invalid/a.png')

    @app.route('/page')
    def page():
        return '<script>window.GLOBAL_COS_BASE_URL="' + ORIGIN + '";</script>'

    @app.route('/old-route')
    def old_route():
        return redirect(ORIGIN + '/users/1/avatar.png?t=1')

    @app.route('/events')
    def events():
        return Response(iter(['data: hi\n\n']), mimetype='text/event-stream')

    return app, cos


def login(app, uid=1):
    client = app.test_client()
    with client.session_transaction() as data:
        data['user_id'] = uid
    return client


def test_owner_stream_and_close(site):
    app, cos = site
    response = login(app).get(MEDIA_PREFIX + 'users/1/avatar.png')
    assert response.status_code == 200
    assert response.data == b'photo'
    assert response.content_type == 'image/png'
    assert 'no-store' in response.headers['Cache-Control']
    assert 'Cookie' in response.headers['Vary']
    assert response.headers['Cross-Origin-Resource-Policy'] == 'same-origin'
    assert 'sandbox' in response.headers['Content-Security-Policy']
    assert cos.streams[0].closed
    assert cos.calls[0][1] == {'Bucket': 'synthetic', 'Key': 'users/1/avatar.png'}


@pytest.mark.parametrize('uid,key,status', [
    (None, 'users/1/avatar.png', 401),
    (2, 'users/1/avatar.png', 404),
    (1, 'users/10/avatar.png', 404),
    (1, 'users/01/avatar.png', 404),
    (1, 'users/1/../2/avatar.png', 404),
    (1, 'users/1/%252e%252e/2/avatar.png', 404),
    (1, 'users/1/configs/credentials.json', 404),
    (1, 'configs/background.png', 404),
    (1, 'square/private/avatar.png', 404),
    (1, 'users/1/evil.html', 404),
])
def test_denials_never_contact_cos(site, uid, key, status):
    app, cos = site
    client = login(app, uid) if uid else app.test_client()
    assert client.get(MEDIA_PREFIX + key).status_code == status
    assert cos.calls == []


@pytest.mark.parametrize('key', ['square/avatars/public.png', 'stickers/pack/a.gif'])
def test_published_copies_shared_but_login_required(site, key):
    app, cos = site
    assert login(app, 2).get(MEDIA_PREFIX + key).data == b'photo'
    count = len(cos.calls)
    assert app.test_client().get(MEDIA_PREFIX + key).status_code == 401
    assert len(cos.calls) == count


def test_logout_stops_access(site):
    app, cos = site
    client = login(app)
    assert client.get(MEDIA_PREFIX + 'users/1/avatar.png').status_code == 200
    with client.session_transaction() as data:
        data.clear()
    assert client.get(MEDIA_PREFIX + 'users/1/avatar.png').status_code == 401
    assert len(cos.calls) == 1


def test_range_voice_and_head(site):
    app, cos = site
    client = login(app)
    response = client.get(MEDIA_PREFIX + 'users/1/voices/v.webm', headers={'Range': 'bytes=0-1'})
    assert response.status_code == 206
    assert response.data == b'ph'
    assert response.headers['Content-Range'] == 'bytes 0-1/5'
    response = client.head(MEDIA_PREFIX + 'users/1/voices/v.webm')
    assert response.status_code == 200
    assert response.data == b''
    assert response.headers['Content-Length'] == '5'
    assert cos.calls[-1][0] == 'HEAD'


def test_invalid_multirange(site):
    app, cos = site
    assert login(app).get(MEDIA_PREFIX + 'users/1/avatar.png', headers={'Range': 'bytes=0-1,3-4'}).status_code == 416
    assert not cos.calls


def test_early_disconnect_closes_stream(site):
    app, cos = site
    response = login(app).get(MEDIA_PREFIX + 'users/1/avatar.png', buffered=False)
    response.close()
    assert cos.streams[0].closed


def test_cloud_failure_does_not_expose_signed_details(site, caplog):
    app, cos = site
    cos.failure = RuntimeError('SECRET_SIGNED_URL')
    response = login(app).get(MEDIA_PREFIX + 'users/1/avatar.png')
    assert response.status_code == 502
    assert 'SECRET_SIGNED_URL' not in response.get_data(as_text=True) + caplog.text


def test_response_mapping_keeps_external_urls_and_streams(site):
    app, _ = site
    client = login(app)
    response = client.get('/links')
    assert response.json['image'] == MEDIA_PREFIX + 'users/1/avatar.png?t=1'
    assert response.json['public'] == MEDIA_PREFIX + 'square/avatars/a.png'
    assert response.json['external'] == 'https://elsewhere.invalid/a.png'
    assert 'no-store' in response.headers['Cache-Control']
    assert MEDIA_PREFIX.rstrip('/') in client.get('/page').get_data(as_text=True)
    assert client.get('/old-route').headers['Location'] == MEDIA_PREFIX + 'users/1/avatar.png?t=1'
    assert client.get('/events').data == b'data: hi\n\n'


def test_escaped_and_domain_boundary():
    raw = json.dumps({'image': ORIGIN + '/users/1/avatar.png'}).replace('/', '\\/')
    assert json.loads(browser_urls(raw, ORIGIN))['image'] == MEDIA_PREFIX + 'users/1/avatar.png'
    evil = ORIGIN + '.evil/users/1/avatar.png'
    assert browser_urls(evil, ORIGIN) == evil
    assert browser_urls(ORIGIN + ':8080/a.png', ORIGIN) == ORIGIN + ':8080/a.png'
    assert not permitted_key('users/1/\\../2/a.png', 1)
    assert not permitted_key('users/1/a.png\x00', 1)


def test_head_denied_uses_authorized_get_metadata(site):
    app, cos = site
    cos.head_failure = CosServiceError('HEAD', {}, 403)
    response = login(app).head(MEDIA_PREFIX + 'users/1/avatar.png')
    assert response.status_code == 200
    assert response.data == b''
    assert response.headers['Content-Length'] == '5'
    assert 'Content-Range' not in response.headers
    assert cos.calls == [
        ('HEAD', {'Bucket': 'synthetic', 'Key': 'users/1/avatar.png'}),
        ('GET', {'Bucket': 'synthetic', 'Key': 'users/1/avatar.png', 'Range': 'bytes=0-0'}),
    ]
    assert cos.streams[0].closed
    assert 'no-store' in response.headers['Cache-Control']


def test_head_fallback_cannot_bypass_get_permission(site):
    app, cos = site
    cos.head_failure = CosServiceError('HEAD', {}, 403)
    cos.failure = CosServiceError('GET', {}, 403)
    assert login(app).head(MEDIA_PREFIX + 'users/1/avatar.png').status_code == 502


def test_head_not_found_does_not_retry(site):
    app, cos = site
    cos.head_failure = CosServiceError('HEAD', {}, 404)
    assert login(app).head(MEDIA_PREFIX + 'users/1/avatar.png').status_code == 404
    assert len(cos.calls) == 1


def test_empty_object_head_fallback_closes_stream(site, monkeypatch):
    app, cos = site
    cos.head_failure = CosServiceError('HEAD', {}, 403)
    stream = io.BytesIO(b'')
    def get(**options):
        cos.calls.append(('GET', options))
        if 'Range' in options:
            raise CosServiceError('GET', {}, 416)
        return {'Body': SimpleNamespace(get_raw_stream=lambda: stream), 'Content-Length': '0'}
    monkeypatch.setattr(cos, 'get_object', get)
    response = login(app).head(MEDIA_PREFIX + 'users/1/avatar.png')
    assert response.status_code == 200
    assert response.headers['Content-Length'] == '0'
    assert response.data == b''
    assert stream.closed


def test_invalid_metadata_still_closes_stream(site, monkeypatch):
    app, cos = site
    cos.head_failure = CosServiceError('HEAD', {}, 403)
    stream = io.BytesIO(b'p')
    monkeypatch.setattr(cos, 'get_object', lambda **kw: {
        'Body': SimpleNamespace(get_raw_stream=lambda: stream),
        'Content-Length': '1', 'Content-Range': 'bytes 0-0/*',
    })
    assert login(app).head(MEDIA_PREFIX + 'users/1/avatar.png').status_code == 502
    assert stream.closed
