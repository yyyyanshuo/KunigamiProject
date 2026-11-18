"""Direct, verified HTTPS to user-selected destinations. No ambient credentials or proxies."""
from contextlib import closing, contextmanager
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from core.public_network import connect_public_socket


MODEL_LIMIT = 8 * 1024 * 1024
IMAGE_LIMIT = 32 * 1024 * 1024
IMAGE_JSON_LIMIT = 48 * 1024 * 1024


class _PublicConnection(HTTPSConnection):
    def _new_conn(self):
        # The pool keeps the original hostname for Host, TLS SNI and certificate
        # verification. Only the TCP socket is replaced with a pinned connection.
        try:
            return connect_public_socket(self.host, self.port, self.timeout)
        except TimeoutError:
            raise ConnectTimeoutError('Destination timed out') from None
        except OSError:
            raise NewConnectionError(self, 'Destination unavailable') from None


class _PublicPool(HTTPSConnectionPool):
    ConnectionCls = _PublicConnection


class _DirectAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'https': _PublicPool}


@contextmanager
def open_response(method, url, *, timeout, headers=None, json=None):
    if not isinstance(url, str) or '\\' in url or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError('Invalid HTTPS destination')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or '%' in parsed.netloc or parsed.fragment or parsed.port == 0):
        raise ValueError('Invalid HTTPS destination')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        raise ValueError('Invalid HTTPS destination')
    # Prepare without a Session to exclude cookies, .netrc, environment proxies
    # and automatic redirect handling (including draining a redirect body).
    prepared = requests.Request(method, url, headers={'Accept-Encoding': 'identity', **(headers or {})}, json=json).prepare()
    try:
        with closing(_DirectAdapter()) as adapter:
            with adapter.send(prepared, timeout=timeout, stream=True, verify=True, proxies={}) as response:
                yield response
    except requests.Timeout:
        raise requests.Timeout('Public HTTPS request timed out') from None
    except requests.RequestException:
        raise requests.ConnectionError('Public HTTPS request failed') from None


def limited_chunks(response, limit, chunk_size=65536):
    length = response.headers.get('Content-Length')
    if length is not None and int(length) > limit:
        raise ValueError('Response too large')
    total = 0
    for chunk in response.iter_content(chunk_size):
        total += len(chunk)
        if total > limit:
            raise ValueError('Response too large')
        yield chunk


def event_lines(response):
    # Enforce the cap before looking for a newline, including unterminated events.
    pending = bytearray()
    for chunk in limited_chunks(response, MODEL_LIMIT, 512):
        pieces = chunk.split(b'\n')
        pending.extend(pieces[0])
        for piece in pieces[1:]:
            yield bytes(pending).rstrip(b'\r')
            pending = bytearray(piece)
    if pending:
        yield bytes(pending).rstrip(b'\r')


def post_json(url, *, headers, json, timeout, limit=MODEL_LIMIT):
    with open_response('POST', url, timeout=timeout, headers=headers, json=json) as response:
        # Cache the bounded bytes for the usual Requests .json() and .text APIs.
        response._content = b''.join(limited_chunks(response, limit))
        return response
