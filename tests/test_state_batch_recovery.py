import json
import multiprocessing
import os
from pathlib import Path

from flask import Flask
import pytest

from blueprints import chat
from core import utils
from tests.test_relationship_reverse_editor import _prepare_relationship_files


def crash_after(action):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip("Linux worker exit test")
    child = multiprocessing.get_context('fork').Process(target=action)
    child.start()
    child.join(10)
    assert child.exitcode == 37


def test_relationship_worker_exit(tmp_path, monkeypatch):
    _, hero, source, _ = _prepare_relationship_files(tmp_path, monkeypatch)
    original = [json.loads(path.read_text()) for path in (hero, source)]
    app = Flask(__name__)
    def interrupt():
        write = chat._write_json_atomic
        def stop(path, value):
            write(path, value)
            os._exit(37)
        chat._write_json_atomic = stop
        with app.test_request_context(json={'normal': {}, 'reverse': {'isagi': {'score': 7}}}):
            chat.save_all_relationship_views('hero')
    crash_after(interrupt)
    with app.test_request_context():
        assert chat.get_relationship_reverse('hero').status_code == 200
    assert [json.loads(path.read_text()) for path in (hero, source)] == original


def test_map_worker_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, 'USERS_ROOT', str(tmp_path))
    catalog = Path(utils._get_characters_config_file(7))
    catalog.write_text('{"hero":{"name":"Hero"}}')
    utils.init_map_data(7)
    utils.normalize_map_state(7)
    original = utils.load_character_positions(7)
    def interrupt():
        def stop(**kw):
            os._exit(37)
        utils.auto_toggle_chat_mode_on_move = stop
        utils.move_character_position('hero', 1.0, 2.0, force=True, user_id=7)
    crash_after(interrupt)
    assert utils.normalize_map_state(7)[0] == original


def test_stale_relationship_draft_does_not_commit(tmp_path, monkeypatch):
    _, hero, source, _ = _prepare_relationship_files(tmp_path, monkeypatch)
    app = Flask(__name__)
    original = json.loads(hero.read_text())
    with app.test_request_context():
        reverse = chat.get_relationship_reverse('hero').get_json()
    draft = {'normal': {}, 'reverse': reverse, 'baseline': {'normal': original, 'reverse': reverse}}
    latest = json.loads(source.read_text())
    latest['主角']['description'] = 'newer'
    source.write_text(json.dumps(latest))
    with app.test_request_context(json=draft):
        assert chat.save_all_relationship_views('hero')[1] == 409
    assert json.loads(source.read_text()) == latest
    assert json.loads(hero.read_text()) == original


def test_failed_batch_leaves_other_account_untouched(tmp_path):
    from services.character_state import state_batch, remember_state_files, character_state_lock
    from core.json_storage import atomic_write_json
    a = tmp_path / '1/configs/characters.json'
    b = tmp_path / '2/configs/characters.json'
    for path in (a, b):
        atomic_write_json(path, {'state': 'original'})
    def stop():
        with state_batch(a):
            remember_state_files([a])
            atomic_write_json(a, {'state': 'partial'})
            os._exit(37)
    crash_after(stop)
    with character_state_lock(b):
        atomic_write_json(b, {'state': 'committed'})
    with character_state_lock(a):
        assert json.loads(a.read_text()) == {'state': 'original'}
    assert json.loads(b.read_text()) == {'state': 'committed'}
