import multiprocessing
import time
import json

from core.json_storage import update_user_settings


def _settings_writer(path, gate, field):
    deadline = time.monotonic() + 8
    while not gate.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    update_user_settings(path, {field: True})


def test_settings_preserve_fields_across_processes(tmp_path):
    path = tmp_path / 'user_settings.json'
    path.write_text('{"existing": true}')
    ctx = multiprocessing.get_context('fork')
    gate = tmp_path / "gate"
    workers = [ctx.Process(target=_settings_writer, args=(path, gate, field))
               for field in ('furigana_enabled', 'bedtime_diary_enabled')]
    for worker in workers:
        worker.start()
    gate.touch()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert json.loads(path.read_text()) == {
        'existing': True, 'furigana_enabled': True, 'bedtime_diary_enabled': True,
    }


def test_moment_edits_preserve_concurrent_reactions_and_other_posts(tmp_path):
    from blueprints.moments import _load_moments_snapshot, _save_moments
    path = tmp_path / 'moments.json'
    path.write_text('[{"char_id":"user","timestamp":"1","content":"old","comments":[]}]')
    text = _load_moments_snapshot(path)
    reactions = _load_moments_snapshot(path)
    text[0]['content'] = 'edited'
    reactions[0]['comments'].append({'commenter_id': 'ch', 'content': 'comment'})
    reactions.append({'char_id': 'ch', 'timestamp': '2', 'content': 'new'})
    _save_moments(path, reactions)
    _save_moments(path, text)
    saved = json.loads(path.read_text())
    assert saved[0]['content'] == 'edited'
    assert saved[0]['comments'] == reactions[0]['comments']
    assert saved[1] == reactions[1]


def test_stale_edits_cannot_restore_deleted_post(tmp_path):
    import pytest
    from werkzeug.exceptions import Conflict
    from blueprints.moments import _load_moments_snapshot, _save_moments
    path = tmp_path / 'moments.json'
    path.write_text('[{"char_id":"user","timestamp":"1","content":"old"}]')
    edit = _load_moments_snapshot(path)
    delete = _load_moments_snapshot(path)
    edit[0]['content'] = 'edited'
    delete.clear()
    _save_moments(path, delete)
    with pytest.raises(Conflict):
        _save_moments(path, edit)
    assert json.loads(path.read_text()) == []


def test_same_moment_content_updates_conflict(tmp_path):
    import pytest
    from werkzeug.exceptions import Conflict
    from blueprints.moments import _load_moments_snapshot, _save_moments
    path = tmp_path / 'moments.json'
    path.write_text('[{"char_id":"user","timestamp":"1","content":"old"}]')
    first = _load_moments_snapshot(path)
    second = _load_moments_snapshot(path)
    first[0]['content'] = 'first'
    second[0]['content'] = 'second'
    _save_moments(path, first)
    with pytest.raises(Conflict):
        _save_moments(path, second)
    assert json.loads(path.read_text())[0]['content'] == 'first'
