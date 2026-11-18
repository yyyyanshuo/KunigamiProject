"""Authenticated streaming access to the configured private COS bucket.

Stored object URLs stay stable. Only browser responses are mapped to this
same-origin endpoint; no bearer download URLs or COS credentials are exposed.
"""
import mimetypes
import re
from urllib.parse import urlsplit

from flask import Response, current_app, request, session

MEDIA_PREFIX = '/api/media/cos/'
SHARED_PREFIXES = ('square/avatars/', 'stickers/')
MEDIA_SUFFIXES = {
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.bmp', '.ico', '.svg',
    '.mp3', '.wav', '.ogg', '.oga', '.opus', '.webm', '.m4a', '.mp4', '.aac', '.flac',
}


def permitted_key(key, user_id):
    if not user_id or not isinstance(key, str) or len(key.encode('utf-8')) > 1024:
        return False
    if any(ord(char) < 32 for char in key) or any(char in key for char in ('\\', '%', '?', '#')):
        return False
    if any(part in ('', '.', '..') for part in key.split('/')):
        return False
    if '.' not in key or '.' + key.rsplit('.', 1)[-1].lower() not in MEDIA_SUFFIXES:
        return False
    # Published copies are shared with signed-in users, independently of authors.
    return key.startswith(f'users/{user_id}/') or key.startswith(SHARED_PREFIXES)


def browser_urls(text, origin):
    """Map only this bucket's exact origin, including JSON-escaped URL strings."""
    host = urlsplit(origin).netloc
    if not host:
        return text
    slash = r'\\?/'
    pattern = re.compile(r'(?:https?:)?' + slash + slash + re.escape(host)
                         + r'(?=' + slash + r'|[\s\x22\x27<>?#)]|$)')
    return pattern.sub(lambda match: MEDIA_PREFIX.rstrip('/').replace('/', '\\/')
                       if '\\/' in match.group() else MEDIA_PREFIX.rstrip('/'), text)


def _private_headers(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    response.vary.add('Cookie')
    return response


def _object_headers(client, options):
    """Some COS policies allow GetObject but deny HeadObject."""
    from qcloud_cos.cos_exception import CosServiceError
    try:
        return client.head_object(**options)
    except CosServiceError as exc:
        if int(exc.get_status_code()) != 403:
            raise
    try:
        obj = client.get_object(**options, Range='bytes=0-0')
    except CosServiceError as exc:
        if int(exc.get_status_code()) != 416:
            raise
        # Empty objects have no satisfiable byte range. Fetch headers only and
        # close without consuming a body, also covering a concurrent replacement.
        obj = client.get_object(**options)
    stream = obj['Body'].get_raw_stream()
    try:
        headers = dict(obj)
        headers.pop('Body', None)
        content_range = headers.pop('Content-Range', None)
        if content_range:
            match = re.fullmatch(r'bytes 0-0/(\d+)', str(content_range))
            if not match:
                raise ValueError('Unexpected media range metadata')
            # HEAD describes the entire object, not the one-byte GET response.
            headers['Content-Length'] = match.group(1)
        return headers
    finally:
        stream.close()


def serve_cos_media(object_key):
    # The application's before_request validates the session's auth_version.
    uid = session.get('user_id')
    if not uid:
        return _private_headers(Response(status=401))
    if not permitted_key(object_key, uid):
        return _private_headers(Response(status=404))
    import cos_utils
    if cos_utils.LOCAL_TEST or cos_utils.client is None or not cos_utils.bucket:
        return _private_headers(Response(status=503))
    options = {'Bucket': cos_utils.bucket, 'Key': object_key}
    byte_range = request.headers.get('Range') if request.method != 'HEAD' else None
    if byte_range:
        if not re.fullmatch(r'bytes=(?:\d{1,20}-\d{0,20}|-\d{1,20})', byte_range):
            return _private_headers(Response(status=416))
        options['Range'] = byte_range
    try:
        if request.method == 'HEAD':
            obj = _object_headers(cos_utils.client, options)
            response = Response(status=200)
        else:
            obj = cos_utils.client.get_object(**options)
            stream = obj['Body'].get_raw_stream()
            def chunks():
                try:
                    while True:
                        chunk = stream.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    stream.close()
            response = Response(chunks(), status=206 if obj.get('Content-Range') else 200)
            response.call_on_close(stream.close)
    except Exception as exc:
        from qcloud_cos.cos_exception import CosServiceError
        status = int(exc.get_status_code()) if isinstance(exc, CosServiceError) else 502
        status = status if status in (404, 416) else 502
        # SDK exception strings can contain signed request details; never log them.
        current_app.logger.warning('Private media unavailable (%s)', type(exc).__name__)
        return _private_headers(Response(status=status))
    # Never serve user-uploaded active content as an application page.
    response.headers['Content-Type'] = mimetypes.guess_type(object_key)[0] or 'application/octet-stream'
    for name in ('Content-Length', 'Content-Range'):
        if name in obj:
            response.headers[name] = str(obj[name])
    response.headers['Accept-Ranges'] = 'bytes'
    return _private_headers(response)


def install_private_media(app, origin):
    app.add_url_rule(MEDIA_PREFIX + '<path:object_key>', 'private_cos_media', serve_cos_media,
                     methods=['GET', 'HEAD'])

    @app.after_request
    def map_browser_media(response):
        # Existing dynamic redirects (avatars, chat images, voice) pass here too.
        location = response.headers.get('Location')
        changed = False
        if location:
            mapped = browser_urls(location, origin)
            if mapped != location:
                response.headers['Location'] = mapped
                changed = True
        if (origin and not response.is_streamed and not response.direct_passthrough
                and not response.headers.get('Content-Encoding')
                and (response.is_json or response.mimetype == 'text/html')):
            original = response.get_data(as_text=True)
            mapped = browser_urls(original, origin)
            if mapped != original:
                response.set_data(mapped)
                response.headers.pop('ETag', None)
                changed = True
        if changed or request.endpoint == 'private_cos_media':
            response.headers['Cache-Control'] = 'private, no-store'
            response.vary.add('Cookie')
        return response
