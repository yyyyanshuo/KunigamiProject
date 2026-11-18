"""Read-only, fail-closed resolution of private character and group resources.

Creation is explicit and separate from lookup. No implicit global template fallback.
The application owns the filesystem; symlinks within an account are not supported.
"""
import json
import re
import shutil
from pathlib import Path

from werkzeug.exceptions import NotFound

from core.config import USERS_ROOT, CHARACTERS_DIR


def _missing():
    return NotFound('Resource not found')


def _user_root(user_id):
    # Identity is supplied by the request/background adapter, never inferred here.
    uid = user_id
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise _missing()
    return _checked(Path(USERS_ROOT).resolve(), str(uid))


def _checked(root, *parts):
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise _missing()
    if not path.resolve().is_relative_to(root.resolve()):
        raise _missing()
    return path


def _resource_path(kind, resource_id, user_id):
    # Hyphens are used by imported UUIDs; existing underscore IDs remain valid.
    if not isinstance(resource_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', resource_id):
        raise _missing()
    root = _user_root(user_id)
    path = _checked(root, kind, resource_id)
    if path.is_dir() and any(p.is_symlink() for p in path.iterdir()):
        raise _missing()
    _checked(path, 'chat.db')
    prompts = _checked(path, 'prompts')
    if prompts.is_dir() and any(p.is_symlink() for p in prompts.iterdir()):
        raise _missing()
    return path


def _catalog(kind, user_id, *, allow_missing=False):
    path = _checked(_user_root(user_id), 'configs', kind + '.json')
    # After an interrupted batch even ordinary character lookups must first
    # let the catalog lock owner restore this account's files.
    if kind == 'characters' and path.with_suffix('.pending.json').exists():
        from services.character_state import character_state_lock
        with character_state_lock(path):
            pass
    try:
        with path.open(encoding='utf-8-sig') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        if allow_missing:
            return {}
        raise _missing() from None
    except (OSError, ValueError):
        raise _missing() from None
    if not isinstance(value, dict):
        raise _missing()
    return value


def _owned(kind, resource_id, user_id):
    path = _resource_path(kind, resource_id, user_id)
    if resource_id not in _catalog(kind, user_id):
        raise _missing()
    return path


def require_owned_character(char_id, user_id=None):
    return _owned('characters', char_id, user_id)


def require_owned_group(group_id, user_id=None):
    return _owned('groups', group_id, user_id)


def iter_owned_chat_databases(user_id):
    """Read each account catalog once and apply the normal path checks to its entries."""
    for kind, directory in (('chat', 'characters'), ('group', 'groups')):
        for cid, info in _catalog(directory, user_id, allow_missing=True).items():
            yield kind, cid, info, _resource_path(directory, cid, user_id) / 'chat.db'


def new_resource_directory(kind, resource_id, user_id=None):
    """For authenticated creation handlers only, before catalog insertion."""
    if kind not in {'characters', 'groups'}:
        raise _missing()
    path = _resource_path(kind, resource_id, user_id)
    if resource_id in _catalog(kind, user_id, allow_missing=True):
        raise _missing()
    return path


def character_paths(char_id, user_id=None):
    directory = require_owned_character(char_id, user_id)
    return str(directory / 'chat.db'), str(directory / 'prompts')


def initialize_character_template(char_id, user_id=None):
    """Explicit lazy initialization for an already-owned legacy template entry.

    Never copy another account's history, and never follow template symlinks.
    Ordinary path lookup does not call this function.
    """
    target = require_owned_character(char_id, user_id)
    if target.exists():
        return target
    template = _checked(Path(CHARACTERS_DIR).resolve(), char_id)
    if not template.is_dir():
        return target
    if any(p.is_symlink() for p in template.rglob('*')):
        raise _missing()
    shutil.copytree(template, target, ignore=shutil.ignore_patterns('chat.db', 'chat.db-*'))
    return target
