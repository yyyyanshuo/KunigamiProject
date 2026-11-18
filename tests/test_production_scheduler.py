from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from run_scheduler import build_scheduler


def test_schedule_construction_does_not_execute_jobs():
    calls = []
    def job():
        calls.append(True)
    jobs = SimpleNamespace(**{name: job for name in (
        'run_yearly_age_increment', 'check_and_update_sleep_status',
        'run_active_messaging_check', 'run_active_moments_check', 'run_pending_call_memory_jobs')})
    scheduler = build_scheduler(job, jobs)
    assert not scheduler.running
    assert len(scheduler.get_jobs()) == 6
    assert not calls
    assert scheduler._job_defaults == {'misfire_grace_time': 300, 'coalesce': True, 'max_instances': 1}


def test_next_runs_preserve_maintenance_clock():
    def job():
        raise AssertionError('Jobs must not run in configuration tests')
    jobs = SimpleNamespace(**{name: job for name in (
        'run_yearly_age_increment', 'check_and_update_sleep_status',
        'run_active_messaging_check', 'run_active_moments_check', 'run_pending_call_memory_jobs')})
    scheduler = build_scheduler(job, jobs)
    now = datetime(2026, 9, 23, 19, 14, 35, tzinfo=ZoneInfo('Asia/Shanghai'))
    actual = {j.id: j.trigger.get_next_fire_time(None, now).isoformat() for j in scheduler.get_jobs()}
    assert actual == {
        'daily_maintenance': '2026-09-23T20:00:00+08:00',
        'yearly_age': '2027-01-01T00:05:00+08:00',
        'sleep_status': '2026-09-23T19:15:00+08:00',
        'active_messages': '2026-09-23T19:20:00+08:00',
        'active_moments': '2026-09-23T19:30:00+08:00',
        'call_memory': '2026-09-23T19:15:00+08:00',
    }
