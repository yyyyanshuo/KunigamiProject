import sqlite3
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from services.maintenance_recovery import attempt, initialize, latest_day, process_user, recover


def test_interruption_bounds_and_calendar(tmp_path):
    assert latest_day(datetime(2026, 11, 2, 3, 59)) == date(2026, 10, 31)
    day = latest_day(datetime(2026, 11, 2, 4))
    path = tmp_path / 'state.db'
    db = sqlite3.connect(path)
    initialize(db, date(2026, 10, 1))
    calls = []

    def worker(db, target, uid):
        key = (target.isoformat(), str(uid), 'daily', 'char')
        return attempt(db, key, lambda: calls.append(key))

    assert worker(db, day, 1)
    db.close()  # entity committed but the day did not finish
    db = sqlite3.connect(path)
    recover(db, day, [1, 2], worker)
    recover(db, day, [1, 2], worker)
    assert len(set(calls)) == len(calls) == 14
    assert min(k[0] for k in calls) == '2026-10-26'
    key = ('2026-11-02', '2', 'group', 'g')
    for _ in range(5):
        assert not attempt(db, key, lambda: 1 / 0)
    assert db.execute("SELECT attempts FROM work WHERE kind='group'").fetchone() == (3,)
    db.close()


def test_sunday_waits_for_daily_and_existing_summaries_survive(tmp_path):
    import json
    from services import memory
    from core.context import get_current_user_id
    db = sqlite3.connect(':memory:')
    sun = date(2026, 11, 1)
    initialize(db, sun)
    calls = []
    writes = {'5_memory_medium.json': {'2026-11-01': 'user day'},
              '4_memory_long.json': {'2026-11-Week1': 'user week'}}
    for name, data in writes.items():
        (tmp_path / name).write_text(json.dumps(data))
    def short(*args, **kwargs):
        calls.append(get_current_user_id())
        return SimpleNamespace(ok=len(calls) > 1, status='api_error')
    (tmp_path / 'groups').write_text('{}')
    (tmp_path / 'chars').write_text('{"same":{}}')
    with patch('app._get_groups_config_file', return_value=tmp_path/'groups'), \
         patch('app._get_characters_config_file', return_value=tmp_path/'chars'), \
         patch('app.get_paths', return_value=('', tmp_path)), \
         patch.object(memory, 'get_paths', return_value=('', tmp_path)), \
         patch.object(memory, 'update_short_memory_for_date', side_effect=short), \
         patch.object(memory, '_summary_with_output_retries', side_effect=AssertionError) as model:
        assert not process_user(db, sun, 7)
        assert not db.execute("SELECT 1 FROM work WHERE kind='weekly'").fetchone()
        assert process_user(db, sun, 7)
        assert process_user(db, sun, 7)
        assert calls == [7, 7]
        model.assert_not_called()
        for name, data in writes.items():
            assert json.loads((tmp_path / name).read_text()) == data


def test_group_delivery_resumes_without_model(tmp_path):
    import json
    from blueprints import group
    stamp = '2026-11-01'
    event = {'time': '12:00', 'event': 'synthetic'}
    path = tmp_path / 'memory_short.json'
    path.write_text(json.dumps({stamp: {'last_id': 1, 'events': [event],
        'pending_distribution': {'members': ['a'], 'name': 'G', 'events': [event]}}}))
    cfg = tmp_path / 'groups.json'
    cfg.write_text('{"g":{"members":["a","b"]}}')
    with patch.object(group, 'get_group_dir', return_value=str(tmp_path)), \
         patch.object(group, '_get_groups_config_file', return_value=str(cfg)), \
         patch.object(group, 'distribute_group_memory') as deliver:
        group.update_group_short_memory('g', stamp, scheduled=True)
        group.update_group_short_memory('g', stamp, scheduled=True)
        deliver.assert_called_once_with('g', 'G', ['a', 'b'], [event], stamp,
                                        target_member_ids=['a'], strict=True)
    assert 'pending_distribution' not in json.loads(path.read_text())[stamp]
