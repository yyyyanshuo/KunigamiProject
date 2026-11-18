"""Synthetic DNS, TCP and TLS fixtures for the model and image egress boundary."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import socket
import ssl
import threading
from unittest.mock import Mock

import pytest
import requests

from core import public_https, public_network


def address(ip, port=9443):
    if ':' in ip:
        return socket.AF_INET6, socket.SOCK_STREAM, 6, '', (ip, port, 0, 0)
    return socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)


@pytest.mark.parametrize('ip', [
    '127.0.0.1', '10.42.0.8', '169.254.169.254', '100.100.100.200', '0.0.0.0',
    '192.0.0.42', '192.88.99.1', '224.0.0.1', '::1', 'fe80::1', 'fd00::1234', 'ff02::1',
    '::ffff:8.8.8.8', '64:ff9b::a00:1', '2002:0808:0808::1', 'fec0::1', '3fff::1',
])
def test_mixed_answer_denied_before_tcp(monkeypatch, ip):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [address('8.8.8.8'), address(ip)])
    factory = Mock()
    monkeypatch.setattr(socket, 'socket', factory)
    with pytest.raises(ValueError, match='Private destination'):
        with public_https.open_response('GET', 'https://relay.example.invalid:9443/x', timeout=(1, 1)):
            pytest.fail('Unexpected response')
    factory.assert_not_called()


def test_single_lookup_pins_port_and_falls_back_only_to_checked_answers(monkeypatch):
    dns = Mock(side_effect=[[address('2001:4860:4860::8888'), address('8.8.8.8')], [address('10.0.0.1')]])
    v6, v4 = Mock(), Mock()
    v6.connect.side_effect = OSError('unavailable')
    factory = Mock(side_effect=[v6, v4])
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(socket, 'socket', factory)
    assert public_network.connect_public_socket('relay.invalid', 9443, 15) is v4
    dns.assert_called_once_with('relay.invalid', 9443, type=socket.SOCK_STREAM)
    v6.close.assert_called_once()
    v4.connect.assert_called_once_with(('8.8.8.8', 9443))
    with pytest.raises(ValueError):
        public_network.connect_public_socket('relay.invalid', 9443, 15)
    assert factory.call_count == 2


@pytest.mark.parametrize('url', ['http://relay.invalid', 'https://user@relay.invalid',
    'https://@relay.invalid', 'https://relay.invalid:0', 'https://relay.invalid:65536',
    'https://local.LOCALHOST./', 'https://relay.invalid\\@evil.invalid', 'https://relay.invalid/x\r\n',
    'https://[fe80::1%25eth0]/', 'https://relay.invalid/#fragment'])
def test_invalid_https_syntax_never_dials(monkeypatch, url):
    dial = Mock()
    monkeypatch.setattr(public_https, 'connect_public_socket', dial)
    with pytest.raises(ValueError):
        with public_https.open_response('GET', url, timeout=(1, 1)):
            pytest.fail('Unexpected response')
    dial.assert_not_called()


@pytest.fixture
def tls_context(tmp_path, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'relay.example.invalid')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('relay.example.invalid')]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path/'cert.pem', tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(certfile), str(keyfile))
    monkeypatch.setattr(requests.adapters, 'DEFAULT_CA_BUNDLE_PATH', str(certfile))
    return ctx


@pytest.mark.parametrize('wrong_name', [False, True])
def test_real_tls_host_sni_no_proxy_credentials_or_redirect(monkeypatch, tmp_path, tls_context, wrong_name):
    # Socketpair supplies the byte stream, with real TLS at both ends. No network listener or DNS.
    client, server = socket.socketpair()
    client.settimeout(3)
    server.settimeout(3)
    tested_host = 'mismatch.invalid' if wrong_name else 'relay.example.invalid'
    names, received, errors = [], [], []
    tls_context.set_servername_callback(lambda sock, name, ctx: names.append(name))
    def serve():
        try:
            with server, tls_context.wrap_socket(server, server_side=True) as wire:
                with wire.makefile('rb') as f:
                    while line := f.readline():
                        received.append(line)
                        if line == b'\r\n':
                            break
                wire.sendall(b'HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n')
        except (OSError, ssl.SSLError) as e:
            errors.append(type(e).__name__)
    thread = threading.Thread(target=serve)
    thread.start()
    dial = Mock(return_value=client)
    monkeypatch.setattr(public_https, 'connect_public_socket', dial)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:5000')
    netrc = tmp_path/'netrc'
    netrc.write_text('machine relay.example.invalid login dummy password test-only\n')
    monkeypatch.setenv('NETRC', str(netrc))
    try:
        url = 'https://' + tested_host + ':9443/signed?X-Signature=test-only'
        if wrong_name:
            with pytest.raises(requests.ConnectionError):
                with public_https.open_response('GET', url, timeout=(3, 3)):
                    pytest.fail('Mismatched certificate accepted')
            assert not received
        else:
            with public_https.open_response('GET', url, timeout=(3, 3)) as response:
                assert response.status_code == 302
            head = b''.join(received).lower()
            assert b'get /signed?x-signature=test-only http/1.1' in head
            assert b'host: relay.example.invalid:9443\r\n' in head
            assert b'authorization:' not in head and b'cookie:' not in head
    finally:
        client.close()
        thread.join(5)
    assert not thread.is_alive() and names == [tested_host]
    assert wrong_name or not errors
    dial.assert_called_once_with(tested_host, 9443, 3)


class Body:
    def __init__(self, *chunks, headers=None, status=200):
        self.headers = headers or {}
        self.status_code = status
        self.iter_content = Mock(return_value=iter(chunks))


def test_size_limits_check_before_read_and_during_stream(monkeypatch):
    response = Body(headers={'Content-Length':'17'})
    with pytest.raises(ValueError, match='too large'):
        list(public_https.limited_chunks(response, 16))
    response.iter_content.assert_not_called()
    with pytest.raises(ValueError, match='too large'):
        list(public_https.limited_chunks(Body(b'12345678', b'9'), 8))
    monkeypatch.setattr(public_https, 'MODEL_LIMIT', 8)
    with pytest.raises(ValueError, match='too large'):
        list(public_https.event_lines(Body(b'12345678', b'9')))
    assert list(public_https.event_lines(Body(b'x\r', b'\ny', b'\n', b'z'))) == [b'x', b'y', b'z']


@pytest.mark.parametrize('entry', ['call', 'stream', 'generate', 'url_image', 'legacy_call', 'legacy_stream'])
def test_private_answer_blocked_at_application_entry(monkeypatch, entry):
    from services import ai_client, model_provider as provider
    from blueprints import forum
    url = 'https://relay.example.invalid:9443/v1'
    monkeypatch.setattr(provider, 'LOCAL_TEST', False)
    monkeypatch.setattr(ai_client, 'LOCAL_TEST', False)
    monkeypatch.setattr(provider, 'connection', lambda uid: ('custom', 'openai_chat', url, 'synthetic'))
    monkeypatch.setattr(provider, 'check_circuit_breaker', lambda *a: None)
    monkeypatch.setattr(ai_client, 'check_circuit_breaker', lambda *a: None)
    monkeypatch.setattr(ai_client, 'get_relay_provider', lambda *a: url)
    monkeypatch.setattr(forum, 'get_relay_provider', lambda *a: url)
    monkeypatch.setattr(ai_client, 'get_effective_openrouter_key', lambda **kw: 'synthetic')
    monkeypatch.setattr(forum, 'get_effective_openrouter_key', lambda **kw: 'synthetic')
    monkeypatch.setattr(ai_client, 'log_full_prompt', lambda *a, **k: None)
    monkeypatch.setattr(ai_client, 'log_api_error', lambda *a, **k: None)
    dns, tcp = Mock(return_value=[address('10.2.3.4')]), Mock()
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(socket, 'socket', tcp)
    if entry == 'call':
        assert provider.call_provider([], model_name='m', user_id=1).error_code == 'provider_request_failed'
    elif entry == 'legacy_call':
        assert ai_client.call_openrouter([], model_name='m', user_id=1).error_code
    elif entry == 'legacy_stream':
        with pytest.raises(ValueError, match='Private destination'):
            list(forum.stream_openrouter_chunks([], 'm', 1))
    elif entry == 'stream':
        with pytest.raises(ValueError, match='Private destination'):
            list(provider.stream_provider([], 'm', 1))
    else:
        if entry == 'url_image':
            @contextmanager
            def generated(*args):
                yield Body(json.dumps({'data':[{'url': 'https://image.invalid:9443/x?sig=synthetic'}]}).encode())
            monkeypatch.setattr(provider, '_model_response', generated)
        with pytest.raises(ValueError, match='Private destination'):
            provider.generate_image_bytes('prompt', 'model', 1)
    assert dns.call_args.args[:2] == ('image.invalid' if entry == 'url_image' else 'relay.example.invalid', 9443)
    tcp.assert_not_called()


@pytest.mark.parametrize('status,expected', [(200, b'image'), (302, None)])
def test_signed_image_download_has_no_model_headers(monkeypatch, status, expected):
    from services import model_provider as provider
    signed = 'https://cdn.invalid:9443/render?expiry=123&sig=synthetic'
    monkeypatch.setattr(provider, 'LOCAL_TEST', False)
    monkeypatch.setattr(provider, 'connection', lambda uid: ('custom', 'openai_chat', 'https://model.invalid', 'model-secret'))
    calls = []
    @contextmanager
    def transport(method, url, **kwargs):
        calls.append(method)
        if method == 'POST':
            assert kwargs['headers']['Authorization'] == 'Bearer model-secret'
            yield Body(json.dumps({'data':[{'url':signed}]}).encode())
        else:
            assert url == signed and 'headers' not in kwargs and 'json' not in kwargs
            yield Body(b'image', status=status)
    monkeypatch.setattr(public_https, 'open_response', transport)
    assert provider.generate_image_bytes('prompt', 'model', 1) == expected
    assert calls == ['POST', 'GET']


def test_inline_image_limit(monkeypatch):
    import base64
    from services import model_provider as provider
    monkeypatch.setattr(public_https, 'IMAGE_LIMIT', 4)
    assert provider._image_bytes(base64.b64encode(b'1234')) == b'1234'
    for data in [b'12345', b'12345678']:
        with pytest.raises(ValueError, match='Image too large'):
            provider._image_bytes(base64.b64encode(data))
