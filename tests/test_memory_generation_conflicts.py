import json
import sqlite3
from types import SimpleNamespace

import pytest

from blueprints import group
from services import memory
from services.memory_store import atomic_write_json, memory_file_lock


@pytest.mark.parametrize('level', ['medium', 'long'])
@pytest.mark.parametrize('edit', ['source', 'target', 'second_generation'])
def test_stale_summary_is_discarded(tmp_path, monkeypatch, level, edit):
    day = '2026-11-01'
    week = '2026-11-Week1'
    short = tmp_path / '6_memory_short.json'
    medium = tmp_path / '5_memory_medium.json'
    long = tmp_path / '4_memory_long.json'
    atomic_write_json(short, {day: {'events': [{'time': '12:00', 'event': 'source'}]}})
    atomic_write_json(medium, {day: 'original day'})
    atomic_write_json(long, {week: 'original week'})
    monkeypatch.setattr(memory, 'get_paths', lambda *a, **k: ('', str(tmp_path)))
    source, target, key = (short, medium, day) if level == 'medium' else (medium, long, week)
    generate = (lambda: memory.generate_medium_memory_for_date('c', day)) if level == 'medium' else (
        lambda: memory.generate_long_memory_for_week('c', week))
    def reply(text):
        return SimpleNamespace(ok=True, text=text)
    def slow(*args, **kwargs):
        if edit == 'second_generation':
            monkeypatch.setattr(memory, '_summary_with_output_retries', lambda *a, **k: reply('- newer'))
            assert generate().ok
        else:
            path = source if edit == 'source' else target
            atomic_write_json(path, {day if edit == 'source' else key: {'events': []} if path == short else 'edited'})
        return reply('- stale')
    monkeypatch.setattr(memory, '_summary_with_output_retries', slow)
    assert generate().status == 'conflict'
    expected = ('original ' + ('day' if level == 'medium' else 'week')) if edit == 'source' else (
        '- newer' if edit == 'second_generation' else 'edited')
    assert json.loads(target.read_text())[key] == expected


def test_group_editor_can_commit_during_model_call(tmp_path, monkeypatch):
    day = '2026-11-01'
    path = tmp_path / 'memory_short.json'
    config = tmp_path / 'groups.json'
    atomic_write_json(config, {'g': {'members': []}})
    monkeypatch.setattr(group, 'get_group_dir', lambda _: str(tmp_path))
    monkeypatch.setattr(group, '_get_groups_config_file', lambda: str(config))
    with sqlite3.connect(tmp_path / 'chat.db') as db:
        db.execute('CREATE TABLE messages(id INTEGER, timestamp TEXT, role TEXT, content TEXT)')
        db.execute("INSERT INTO messages VALUES(1, '2026-11-01 10:00:00', 'user', 'synthetic')")
    edited = {'events': [{'time': '09:00', 'event': 'edited'}], 'last_id': 0}
    def slow(*args, **kwargs):
        with memory_file_lock(path, timeout=0.15):
            atomic_write_json(path, {day: edited})
        return SimpleNamespace(ok=True, parsed=[{'time': '10:00', 'event': 'generated'}])
    monkeypatch.setattr(memory, '_summary_with_output_retries', slow)
    monkeypatch.setattr(memory, '_short_summary_is_compressed', lambda *args: True)
    with pytest.raises(RuntimeError, match='记忆已'):
        group.update_group_short_memory('g', day)
    assert json.loads(path.read_text())[day] == edited
