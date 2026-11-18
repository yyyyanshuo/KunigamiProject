"""Local substitute for COS, scoped to the logged-in synthetic user."""
import shutil
import uuid
from pathlib import Path
from core.runtime import DATA_ROOT
from core.context import get_current_user_id


def store_upload(source, object_key):
    uid = get_current_user_id()
    if not uid:
        raise ValueError('Local uploads require login')
    name = uuid.uuid4().hex + Path(object_key).suffix
    public = object_key.startswith(('square/avatars/', 'stickers/'))
    target = (Path(DATA_ROOT) / 'public_uploads' / name) if public else (Path(DATA_ROOT) / 'users' / str(uid) / 'uploads' / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return ('/public_uploads/' if public else '/local_uploads/') + name


def install_local_routes(app):
    from flask import send_from_directory, jsonify

    @app.route('/local_uploads/<filename>')
    def local_upload(filename):
        uid = get_current_user_id()
        if not uid:
            return jsonify(error='Login required'), 401
        return send_from_directory(Path(DATA_ROOT) / 'users' / str(uid) / 'uploads', filename)

    @app.route('/public_uploads/<filename>')
    def public_upload(filename):
        # Shared assets survive deletion of their uploader's private workspace.
        if not get_current_user_id():
            return jsonify(error='Login required'), 401
        return send_from_directory(Path(DATA_ROOT) / 'public_uploads', filename)

    @app.after_request
    def offline_browser_policy(response):
        # Prevent page scripts/media/PWA from calling production services.
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob: data:; font-src 'self' data:; connect-src 'self'; "
            "frame-src 'none'; form-action 'self'"
        )
        return response
