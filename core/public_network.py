"""Resolve once, validate the whole DNS answer, and dial only validated addresses."""
import ipaddress
import socket
import time


def public_address(value):
    ip = ipaddress.ip_address(value)
    if '%' in value or not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if ip.version == 4:
        # Some Python versions classify most of the IETF protocol block as global.
        if ip in ipaddress.ip_network('192.0.0.0/24'):
            return ip.packed[-1] in (9, 10)
        return ip not in ipaddress.ip_network('192.88.99.0/24')
    return (ip in ipaddress.ip_network('2000::/3') and not ip.sixtofour
            and not ip.teredo and ip not in ipaddress.ip_network('3fff::/20'))


def connect_public_socket(host, port, timeout):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not public_address(addr[0]) for _, _, _, _, addr in addresses):
        raise ValueError('Private destination denied')
    deadline = time.monotonic() + timeout
    for family, kind, protocol, _, addr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Connection timed out')
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(remaining)
            sock.connect(addr)
            return sock
        except OSError:
            sock.close()
    if time.monotonic() >= deadline:
        raise TimeoutError('Connection timed out')
    raise OSError('Destination unavailable')
