"""Loopback HTTP proxy for the isolated browser; pin each hop to public IPs.

No localhost/private/link-local/cloud metadata access, even after redirects or
DNS rebinding. HTTPS travels through CONNECT without TLS interception.
"""
import select
import socketserver
import threading
from urllib.parse import urlsplit

from core.public_network import connect_public_socket, public_address


def public_connect(host, port):
    if port not in (80, 443):
        raise ValueError('Port not allowed')
    return connect_public_socket(host, port, 15)


def relay(left, right):
    while True:
        readable, _, _ = select.select([left, right], [], [], 30)
        if not readable:
            return
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            target = right if source is left else left
            target.sendall(data)


class Handler(socketserver.StreamRequestHandler):
    timeout = 15

    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        upstream = None
        connected = False
        try:
            line = self.rfile.readline(8193)
            if len(line) > 8192:
                raise ValueError('Request line too long')
            method, target, version = line.decode('ascii').strip().split(' ')
            headers = []
            total = 0
            while True:
                line = self.rfile.readline(8193)
                total += len(line)
                if total > 32768:
                    raise ValueError('Headers too long')
                if line in (b'\r\n', b'\n'):
                    break
                if not line or b':' not in line or line[:1] in (b' ', b'\t'):
                    raise ValueError('Invalid headers')
                headers.append(line)
            parsed = urlsplit(('https://' if method == 'CONNECT' else '') + target)
            if parsed.username or parsed.password or not parsed.hostname or '\\' in target:
                raise ValueError('Invalid authority')
            if method == 'CONNECT':
                if parsed.path or parsed.query or parsed.fragment or parsed.port != 443:
                    raise ValueError('Invalid tunnel')
                upstream = public_connect(parsed.hostname, 443)
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self.wfile.flush()
                connected = True
                relay(self.connection, upstream)
            else:
                if parsed.scheme != 'http' or parsed.port not in (None, 80):
                    raise ValueError('Invalid HTTP destination')
                # One HTTP request per connection. HTTPS pages use pinned tunnels.
                upstream = public_connect(parsed.hostname, 80)
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                host = parsed.netloc.encode('ascii')
                request_headers = []
                lengths = []
                for header in headers:
                    name, value = header.split(b':', 1)
                    name = name.lower()
                    if name == b'transfer-encoding':
                        raise ValueError('Chunked HTTP upload not supported')
                    if name == b'content-length':
                        lengths.append(int(value.strip()))
                    if name not in (b'host', b'connection', b'proxy-connection', b'proxy-authorization'):
                        request_headers.append(header)
                if len(lengths) > 1 or any(length < 0 or length > 2 * 1024 * 1024 for length in lengths):
                    raise ValueError('Invalid upload length')
                upstream.sendall((method + ' ' + path + ' HTTP/1.1\r\n').encode('ascii')
                                 + b'Host: ' + host + b'\r\nConnection: close\r\n'
                                 + b''.join(request_headers) + b'\r\n')
                remaining = lengths[0] if lengths else 0
                while remaining:
                    body = self.rfile.read(min(remaining, 65536))
                    if not body:
                        raise ValueError('Incomplete upload')
                    upstream.sendall(body)
                    remaining -= len(body)
                connected = True
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    self.connection.sendall(chunk)
        except (OSError, ValueError, UnicodeError):
            if not connected:
                try:
                    self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    self.wfile.flush()
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()
            self.server.slots.release()


class PublicWebProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f'http://127.0.0.1:{self.server_address[1]}'

    def __exit__(self, *_):
        self.shutdown()
        self.server_close()
