"""Beijing-day archives retain historical progress and absolute event instants."""
import json
from datetime import datetime
from unittest.mock import patch

from core.time_utils import parse_beijing_timestamp
from services import memory, memory_store
from services.memory_store import (
    reindex_memory_days, memory_day_bounds, memory_row_is_covered,
    dated_memory_event, load_calendar_memory, memory_file_lock,
)
from tests.test_memory_integrity import _make_chat_db, _patch_character, _write_json


def test_local_day_boundaries_include_dst():
    for day, hours in [('2026-03-08', 23), ('2026-11-01', 25)]:
        start, end = memory_day_bounds(day, 'America/Los_Angeles')
        assert (parse_beijing_timestamp(end) - parse_beijing_timestamp(start)).total_seconds() == hours * 3600
    assert memory_day_bounds('2026-09-24', 'Asia/Tokyo') == ('2026-09-23 23:00:00', '2026-09-24 23:00:00')


def test_rebucket_legacy_cursor_does_not_skip_other_part_of_day():
    old = {'2026-09-23': {'last_id': 100, 'events': [{'time': '01:00', 'event': 'early'}]}}
    data = reindex_memory_days(old, 'America/Los_Angeles')
    assert data['2026-09-22']['events'][0]['time'] == '10:00'
    day = data['2026-09-22']
    assert memory_row_is_covered((99, '2026-09-23 01:00:00'), day)
    assert not memory_row_is_covered((80, '2026-09-22 23:00:00'), day)
    assert not memory_row_is_covered((101, '2026-09-23 01:00:00'), day)
    assert old['2026-09-23']['events'][0]['time'] == '01:00'
    # A second timezone change preserves the same instant and source coverage.
    tokyo = reindex_memory_days(data, 'Asia/Tokyo')
    assert tokyo['2026-09-23']['events'][0]['time'] == '02:00'
    assert not memory_row_is_covered((80, '2026-09-22 23:00:00'), tokyo['2026-09-23'])


def test_dst_event_uses_actual_source_occurrence():
    event = dated_memory_event('2026-11-01', {'time': '01:30', 'event': 'second occurrence'}, 'America/Los_Angeles', [(10, '2026-11-01 17:30:00', 'user', 'hello')])
    assert parse_beijing_timestamp(event['timestamp']).hour == 17


def test_first_migration_backup_unchanged(tmp_path):
    path = tmp_path / '6_memory_short.json'
    old = {'2026-09-23': {'last_id': 9, 'events': [{'time': '00:00', 'event': 'old'}]}}
    _write_json(path, old)
    with memory_file_lock(str(path)):
        load_calendar_memory(str(path), 'Asia/Tokyo')
        load_calendar_memory(str(path), 'America/Los_Angeles')
    assert json.loads((tmp_path / '6_memory_short.json.before-timezone.json').read_text()) == old


def test_incremental_after_beijing_migration_has_no_duplicates_or_gaps(tmp_path, monkeypatch):
    db, prompts = tmp_path/'chat.db', tmp_path/'prompts'
    _make_chat_db(db, [(1, 'user', 'previous-day', '2026-09-22 23:59:59'),
                      (2, 'user', 'unsummarized-before-old-day', '2026-09-23 09:00:00'),
                      (3, 'assistant', 'already-summarized', '2026-09-23 16:00:00'),
                      (4, 'user', 'new-after-cursor', '2026-09-23 23:59:59'),
                      (5, 'user', 'outside-beijing-day', '2026-09-24 00:00:00')])
    _write_json(prompts/'6_memory_short.json', {'2026-09-23': {
        'timezone':'America/Los_Angeles', 'last_id':3,
        'events':[{'time':'01:00','event':'existing memory'}]}})
    _patch_character(monkeypatch, db, prompts)
    sources=[]
    def summary(source, *_args, **_kwargs):
        sources.append(source)
        return memory.MemorySummaryResult('success', parsed=[{'time':'09:00','event':'complete new event'}])
    monkeypatch.setattr(memory, '_summary_with_output_retries', summary)
    result=memory.update_short_memory_for_date('a','2026-09-23')
    assert result.ok
    joined='\n'.join(sources)
    assert 'already-summarized' not in joined
    assert 'unsummarized-before-old-day' in joined
    assert 'new-after-cursor' in joined
    assert '[09:00]' in joined and '[23:59]' in joined
    assert 'outside-beijing-day' not in joined and 'previous-day' not in joined
    data=json.loads((prompts/'6_memory_short.json').read_text())
    assert data['2026-09-23']['timezone']=='Asia/Shanghai'
    assert any(e['event']=='existing memory' and e['time']=='16:00' for e in data['2026-09-23']['events'])
    assert memory.update_short_memory_for_date('a','2026-09-23').status == 'up_to_date'


def test_external_event_uses_beijing_day_and_does_not_reset_cursor(tmp_path):
    path=tmp_path/'6_memory_short.json'
    _write_json(path, {'2026-09-23':{'events':[], 'last_id':10, 'timezone':'Asia/Shanghai'}})
    memory_store.append_short_memory_events(str(path),'2026-09-24',
        [{'time':'00:30','event':'[朋友圈] moment'}], source_timezone='Asia/Tokyo')
    day=json.loads(path.read_text())['2026-09-23']
    assert day['events'][0]['time']=='23:30'
    assert day['events'][0]['timestamp']=='2026-09-23T23:30:00+08:00'
    assert day['last_id']==10


def test_dst_fold_is_split_before_summary_batches():
    from services.memory_store import split_memory_clock_epochs
    rows=[(1,'2026-11-01 16:30:00','user','first'),(2,'2026-11-01 17:30:00','user','second')]
    assert split_memory_clock_epochs(rows,'America/Los_Angeles') == [[rows[0]],[rows[1]]]


def test_prompt_reads_mixed_legacy_and_local_dates_in_role_zone(tmp_path,monkeypatch):
    from services import prompt_builder
    prompts=tmp_path/'prompts'
    _write_json(prompts/'6_memory_short.json', {
        '2026-09-23':{'events':[{'time':'23:30','event':'legacy'}]},
        '2026-09-24':{'timezone':'Asia/Tokyo','events':[{'time':'01:00','event':'new','timestamp':'2026-09-23T16:00:00Z'}]},
    })
    monkeypatch.setattr(prompt_builder,'get_paths',lambda *_a,**_k:('',str(prompts)))
    monkeypatch.setattr(prompt_builder,'_get_character_time_info',lambda *_a,**_k:(None,'America/Los_Angeles',None))
    monkeypatch.setattr(prompt_builder,'utc_now',lambda:datetime.fromisoformat('2026-09-23T17:00:00+00:00'))
    events=prompt_builder.extract_short_memory_with_timeline_ts('a')
    assert [(e[0],e[2].strftime('%Y-%m-%d %H:%M')) for e in events] == [('legacy','2026-09-23 08:30'),('new','2026-09-23 09:00')]


def test_group_archive_and_distribution_share_beijing_day(tmp_path,monkeypatch):
    from blueprints import group
    folder=tmp_path/'group';prompts=tmp_path/'prompts'
    _make_chat_db(folder/'chat.db',[(1,'user','night event','2026-09-23 23:30:00'),(2,'a','complete reply','2026-09-23 23:31:00')])
    _write_json(tmp_path/'groups.json',{'g':{'name':'Test','members':['a']}})
    _write_json(tmp_path/'characters.json',{'a':{'name':'A'}})
    monkeypatch.setattr(group,'get_group_dir',lambda *_:str(folder))
    monkeypatch.setattr(group,'get_paths',lambda *_:('',str(prompts)))
    monkeypatch.setattr(group,'_get_groups_config_file',lambda:str(tmp_path/'groups.json'))
    monkeypatch.setattr(group,'_get_characters_config_file',lambda:str(tmp_path/'characters.json'))
    sources=[]
    def summarize(source,*_a,**_k):
        sources.append(source)
        return memory.MemorySummaryResult('success',parsed=[{'time':'23:30','event':'completed topic'}])
    monkeypatch.setattr(memory,'_summary_with_output_retries',summarize)
    assert group.update_group_short_memory('g','2026-09-23')[0]==1
    assert '[23:30]' in sources[0]
    personal=json.loads((prompts/'6_memory_short.json').read_text())
    assert list(personal)==['2026-09-23']
    assert personal['2026-09-23']['events'][0]['time']=='23:30'
    assert personal['2026-09-23']['timezone']=='Asia/Shanghai'
    assert group.update_group_short_memory('g','2026-09-23')[0]==0


def test_weekly_rollover_uses_beijing_monday_for_all_users(monkeypatch):
    import app
    import memory_jobs
    monkeypatch.setattr(app,'list_all_user_ids',lambda:[1,2])
    import core.time_utils
    monkeypatch.setattr(core.time_utils,'utc_now',lambda:datetime.fromisoformat('2026-09-27T20:00:00+00:00'))
    processed=[]
    monkeypatch.setattr(memory_jobs,'_process_single_user_weekly_rollovers',processed.append)
    memory_jobs.run_all_weekly_rollovers(local_calendar_only=True)
    assert processed==[1,2]
