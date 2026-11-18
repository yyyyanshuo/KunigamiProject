#!/usr/bin/env python3
"""Run SakiGlobal's scheduled jobs in one independent production process."""
import argparse
import fcntl
import os
from pathlib import Path
import signal
import threading

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler


def build_scheduler(maintenance, jobs):
    # Preserve the old deployment's maintenance clock. Character-local sleep
    # and activity times are resolved by memory_jobs, independently of UI language.
    scheduler = BackgroundScheduler(
        timezone='Asia/Shanghai',
        executors={'default': ThreadPoolExecutor(max_workers=2)},
        job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 300},
    )
    scheduler.add_job(maintenance, 'cron', id='daily_maintenance', minute=0)
    scheduler.add_job(jobs.run_yearly_age_increment, 'cron', id='yearly_age',
                      month=1, day=1, hour=0, minute=5)
    scheduler.add_job(jobs.check_and_update_sleep_status, 'cron', id='sleep_status', minute='*')
    scheduler.add_job(jobs.run_active_messaging_check, 'cron', id='active_messages', minute='*/10')
    scheduler.add_job(jobs.run_active_moments_check, 'cron', id='active_moments', minute='*/30')
    scheduler.add_job(jobs.run_pending_call_memory_jobs, 'cron', id='call_memory', minute='*/5')
    return scheduler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Load jobs without starting or executing them')
    args = parser.parse_args()
    from core.runtime import APP_ENV, DATA_ROOT, LOCAL_TEST
    if LOCAL_TEST or APP_ENV not in {'production', 'prod'}:
        raise RuntimeError('The production scheduler requires production mode and SAKI_LOCAL_TEST=false')
    from services.maintenance_recovery import maintenance_callback
    from services.memory_store import memory_now
    import memory_jobs
    if args.check:
        build_scheduler(lambda: None, memory_jobs)
        print('PASS：6 项后台任务加载成功；未启动调度、未执行任务。', flush=True)
        return
    os.umask(0o077)
    with (Path(DATA_ROOT) / '.scheduler.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another scheduler already owns this data directory') from None
        stopping = threading.Event()
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: stopping.set())
        scheduler = build_scheduler(maintenance_callback(DATA_ROOT, memory_now), memory_jobs)
        scheduler.start()
        print('SakiGlobal 后台任务已启动：日维护、年度年龄、睡眠状态、主动消息、主动朋友圈、通话记忆补录。', flush=True)
        try:
            stopping.wait()
        finally:
            scheduler.shutdown(wait=True)
            print('SakiGlobal 后台任务已停止。', flush=True)


if __name__ == '__main__':
    main()
