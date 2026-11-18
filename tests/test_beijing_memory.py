"""Memory keeps Beijing instants; role prompts derive date-aware local clocks."""
import json
from datetime import datetime

import pytest

from core import time_utils
from services import memory, memory_store, prompt_builder
from tests.test_memory_integrity import _make_chat_db, _write_json


def test_account_timezone_does_not_change_archive_calendar(tmp_path, monkeypatch):
    import core.config
    import core.utils
    monkeypatch.setattr(core.config, 'USERS_ROOT', str(tmp_path))
    for uid, zone in [(1, 'America/Los_Angeles'), (2, 'Asia/Tokyo')]:
        _write_json(tmp_path/str(uid)/'configs/user_settings.json', {'timezone': zone})
    monkeypatch.setattr(core.utils, '_load_user_settings', lambda: {'timezone': 'America/Los_Angeles'})
    monkeypatch.setattr(time_utils, 'utc_now', lambda: datetime.fromisoformat('2026-11-01T16:30:00+00:00'))
    for uid in (None, 1, 2):
        assert memory_store.memory_timezone(uid) == 'Asia/Shanghai'
        assert memory_store.memory_now(uid).isoformat() == '2026-11-02T00:30:00+08:00'


def test_beijing_migration_preserves_fold_events_cursors_and_previous_backup(tmp_path):
    path = tmp_path/'6_memory_short.json'
    old = {'2026-11-01': {
        'timezone': 'America/Los_Angeles', 'last_id': 20, 'call_last_id': 7,
        'events': [
            {'time': '01:30', 'event': 'first', 'timestamp': '2026-11-01T08:30:00Z'},
            {'time': '01:30', 'event': 'second', 'timestamp': '2026-11-01T09:30:00Z'},
        ],
    }}
    _write_json(path, old)
    original_backup = path.with_name(path.name + '.before-timezone.json')
    original_backup.write_text('{"prior": "backup"}')
    with memory_store.memory_file_lock(str(path)):
        data = memory_store.load_calendar_memory(str(path), memory_store.memory_timezone())
        assert memory_store.load_calendar_memory(str(path), memory_store.memory_timezone()) == data
    assert json.loads(path.with_name(path.name+'.before-beijing.json').read_text()) == old
    assert original_backup.read_text() == '{"prior": "backup"}'
    day = data['2026-11-01']
    assert [e['time'] for e in day['events']] == ['16:30', '17:30']
    assert all(e['timestamp'].endswith('+08:00') for e in day['events'])
    assert memory_store.memory_row_is_covered((20, '2026-11-01 16:30:00'), day)
    assert memory_store.memory_row_is_covered((7, '2026-11-01 17:30:00'), day, source='call')
    assert not memory_store.memory_row_is_covered((20, '2026-11-01 14:59:59'), day)
    assert not memory_store.memory_row_is_covered((8, '2026-11-01 17:30:00'), day, source='call')


@pytest.mark.parametrize('source_day, role_clock', [('2026-07-15', '08:30'), ('2026-12-15', '07:30')])
def test_group_distribution_keeps_beijing_and_prompt_converts_per_member(tmp_path, monkeypatch, source_day, role_clock):
    from blueprints import group
    folder = tmp_path/'group'
    _make_chat_db(folder/'chat.db', [
        (1, 'user', 'shared event', source_day+' 23:30:00'),
        (2, 'west', 'complete reply', source_day+' 23:31:00'),
    ])
    _write_json(tmp_path/'groups.json', {'g': {'name': 'Test', 'members': ['west', 'east']}})
    _write_json(tmp_path/'characters.json', {'west': {'name': 'West'}, 'east': {'name': 'East'}})
    paths = lambda char_id, **_k: ('', str(tmp_path/char_id/'prompts'))
    monkeypatch.setattr(group, 'get_group_dir', lambda *_: str(folder))
    monkeypatch.setattr(group, 'get_paths', paths)
    monkeypatch.setattr(group, '_get_groups_config_file', lambda: str(tmp_path/'groups.json'))
    monkeypatch.setattr(group, '_get_characters_config_file', lambda: str(tmp_path/'characters.json'))
    sources = []
    def summarize(source, *_a, **_k):
        sources.append(source)
        return memory.MemorySummaryResult('success', parsed=[{'time': '23:30', 'event': 'shared event summary'}])
    monkeypatch.setattr(memory, '_summary_with_output_retries', summarize)
    assert group.update_group_short_memory('g', source_day)[0] == 1
    assert '[23:30]' in sources[0]
    monkeypatch.setattr(prompt_builder, 'get_paths', paths)
    monkeypatch.setattr(prompt_builder, '_get_character_time_info', lambda char_id, **_k:
        (None, 'America/Los_Angeles' if char_id == 'west' else 'Asia/Tokyo', None))
    monkeypatch.setattr(prompt_builder, 'utc_now', lambda: datetime.fromisoformat(source_day+'T16:00:00+00:00'))
    for member in ('west', 'east'):
        path = tmp_path/member/'prompts/6_memory_short.json'
        original = path.read_bytes()
        saved = json.loads(original)[source_day]
        assert saved['timezone'] == 'Asia/Shanghai'
        assert saved['events'][0]['time'] == '23:30'
        assert saved['events'][0]['timestamp'] == source_day+'T23:30:00+08:00'
        events = prompt_builder.extract_short_memory_with_timeline_ts(member)
        assert len(events) == 1
        local = events[0][2]
        assert local.strftime('%H:%M') == (role_clock if member == 'west' else '00:30')
        assert local.day == (15 if member == 'west' else 16)
        assert path.read_bytes() == original, 'Prompt reading must not rewrite stored memory'
    assert json.loads((folder/'memory_short.json').read_text())[source_day]['events'][0]['time'] == '23:30'
    assert group.update_group_short_memory('g', source_day)[0] == 0


@pytest.mark.parametrize('instants, labels', [
    (['2026-03-08 17:59:00', '2026-03-08 18:00:00'], ['01:59', '03:00']),
    (['2026-11-01 16:45:00', '2026-11-01 17:15:00'], ['01:45 UTC-07:00', '01:15 UTC-08:00']),
])
def test_prompt_timeline_orders_real_instants_across_dst(tmp_path, monkeypatch, instants, labels):
    db = tmp_path/'chat.db'
    _make_chat_db(db, [(1, 'user', 'first', instants[0]), (2, 'assistant', 'second', instants[1])])
    import core.utils
    monkeypatch.setattr(core.utils, 'get_paths', lambda *_a, **_k: (str(db), str(tmp_path)))
    monkeypatch.setattr(prompt_builder, '_get_character_time_info', lambda *_a, **_k: (None, 'America/Los_Angeles', None))
    messages = prompt_builder.extract_recent_messages_with_labels('hero')
    assert len(messages) == 2
    for entry, label in zip(messages, labels):
        assert '['+label+']' in entry[1]
    timeline = [('message', message[1], message[2]) for message in messages]
    timeline += [('short_memory', 'memory-'+str(i), message[2]) for i, message in enumerate(messages)]
    rendered = prompt_builder.build_timeline_section(list(reversed(timeline)))
    assert rendered.index('first') < rendered.index('second')
    assert rendered.index('memory-0') < rendered.index('memory-1')
    for label in labels:
        assert label in rendered


@pytest.mark.parametrize('lang', ['zh', 'en', 'ja'])
@pytest.mark.parametrize('kind', ['short', 'group_log'])
def test_summary_prompt_explicitly_keeps_source_beijing_time(monkeypatch, lang, kind):
    monkeypatch.setattr(memory, '_get_memory_character_name', lambda *_a, **_k: 'Role')
    prompt = memory.get_memory_summary_prompt(kind, lang=lang)
    assert 'Asia/Shanghai' in prompt
    assert 'UTC+08:00' in prompt
    assert 'HH:MM' in prompt


def test_memory_page_calendar_does_not_use_account_display_zone(monkeypatch):
    from blueprints import views, group
    monkeypatch.setattr(views, 'render_template', lambda template, **context: context)
    monkeypatch.setattr(group, 'render_template', lambda template, **context: context)
    assert views.memory_view('hero') == {'memory_timezone': 'Asia/Shanghai'}
    assert group.group_memory_view('g') == {'memory_timezone': 'Asia/Shanghai'}
