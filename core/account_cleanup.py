"""Account-scoped cleanup. Public posts remain; account associations do not."""
import json
import sqlite3
import shutil
import uuid
from urllib.parse import urlsplit
from pathlib import Path
from core.utils import safe_save_json


def columns(conn, table):
    # Callers supply only fixed, internal table names.
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def clean_square(conn, user_id, email, users_root=None):
    uid = int(user_id)
    for table in ('favorites', 'likes', 'square_trial_sessions', 'square_trial_messages', 'square_trial_requests'):
        if 'user_id' in columns(conn, table):
            conn.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
    fields = columns(conn, 'characters')
    if users_root is not None and {'avatar', 'author_email'} <= fields:
        # Migrate local-test public avatars from the old per-user adapter before
        # deleting its directory. Do not read arbitrary paths or other accounts.
        if 'author_user_id' in fields:
            rows = conn.execute('SELECT id,avatar FROM characters WHERE author_user_id=? OR author_email=?', (uid, email))
        else:
            rows = conn.execute('SELECT id,avatar FROM characters WHERE author_email=?', (email,))
        for char_id, avatar in rows.fetchall():
            parsed = urlsplit(avatar or '')
            if parsed.netloc or not parsed.path.startswith('/local_uploads/'):
                continue
            filename = parsed.path.removeprefix('/local_uploads/')
            if not filename or Path(filename).name != filename:
                raise ValueError('Invalid local public avatar')
            source = Path(users_root) / str(uid) / 'uploads' / filename
            target_dir = Path(users_root).parent / 'public_uploads'
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / (uuid.uuid4().hex + source.suffix)
            shutil.copyfile(source, target)
            conn.execute('UPDATE characters SET avatar=? WHERE id=?', ('/public_uploads/' + target.name, char_id))
    associations = [field for field in ('author_email', 'author_user_id', 'source_character_id') if field in fields]
    clauses, args = [], []
    if 'author_user_id' in fields:
        clauses.append('author_user_id=?'); args.append(uid)
    if 'author_email' in fields and email:
        clauses.append('author_email=?'); args.append(email)
    if associations and clauses:
        conn.execute('UPDATE characters SET ' + ','.join(f'{field}=NULL' for field in associations)
                     + ' WHERE ' + ' OR '.join(clauses), args)
    if 'author_user_id' in columns(conn, 'comments'):
        affected = [row[0] for row in conn.execute('SELECT DISTINCT character_id FROM comments WHERE author_user_id=?', (uid,))]
        conn.execute('DELETE FROM comments WHERE author_user_id=?', (uid,))
        if 'comment_count' in fields:
            for char_id in affected:
                conn.execute('UPDATE characters SET comment_count=(SELECT COUNT(*) FROM comments WHERE character_id=?) WHERE id=?', (char_id, char_id))


def clean_image_usage(db_path, user_id):
    if not Path(db_path).is_file():
        return
    with sqlite3.connect(db_path) as conn:
        for table in ('image_days', 'image_reservations'):
            if 'user_id' in columns(conn, table):
                conn.execute(f'DELETE FROM {table} WHERE user_id=?', (int(user_id),))


def unlink_sticker_packs(stickers_root, user_id):
    root = Path(stickers_root).resolve()
    if not root.is_dir():
        return
    for path in root.glob('*/meta.json'):
        if not path.resolve().is_relative_to(root):
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if str(data.get('uploaded_by', '')) != str(int(user_id)):
            continue
        data.pop('uploaded_by', None)
        data.pop('uploaded_by_name', None)
        data['author_deleted'] = True
        safe_save_json(str(path), data)


def preserve_square_avatars(db_path, user_id, email):
    """Detach legacy public avatars from the private COS prefix before deletion."""
    if not Path(db_path).is_file():
        return
    from blueprints.square import _private_avatar_cos_keys, _materialize_square_avatar
    with sqlite3.connect(db_path) as conn:
        fields = columns(conn, 'characters')
        if not {'id', 'avatar', 'author_email'} <= fields:
            return
        if 'author_user_id' in fields:
            rows = conn.execute('SELECT id,avatar FROM characters WHERE author_user_id=? OR author_email=?', (int(user_id), email))
        else:
            rows = conn.execute('SELECT id,avatar FROM characters WHERE author_email=?', (email,))
        for char_id, avatar in rows.fetchall():
            if _private_avatar_cos_keys(user_id, avatar):
                public_url = _materialize_square_avatar(char_id, user_id, avatar_url=avatar)
                conn.execute('UPDATE characters SET avatar=? WHERE id=?', (public_url, char_id))

def prune_expired_subscriptions(path, uid, attempted, valid):
    """Remove only deliveries rejected for this user. Preserve newer subscriptions."""
    from services.character_state import character_state_lock
    from services.memory_store import load_json_object
    with character_state_lock(path):
        data = load_json_object(path)
        current = data.get(str(uid))
        if not isinstance(current, list):
            return
        keep = [item for item in current if item not in attempted or item in valid]
        if keep != current:
            data[str(uid)] = keep
            safe_save_json(path, data)
