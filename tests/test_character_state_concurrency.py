"""Exercise the actual catalog/map writers in independent worker processes."""
import errno
import json
import multiprocessing
import os
from pathlib import Path

import pytest

import agent_utils
import app as application
from blueprints import lbs
from core import utils
from core.json_storage import StorageWriteError, atomic_write_json
from services.character_state import character_state_lock, finish_diary_attempt


@pytest.fixture
def workers():
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Production Linux worker concurrency test")
    return multiprocessing.get_context("fork")


def run_workers(workers, *jobs):
    gate = workers.Event()

    def run(job):
        assert gate.wait(5)
        job()

    processes = [workers.Process(target=run, args=(job,)) for job in jobs]
    try:
        for process in processes:
            process.start()
        gate.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)


def test_affinity_web_meta_and_map_in_independent_workers(workers, tmp_path, monkeypatch):
    uid = 7
    monkeypatch.setattr(utils, "USERS_ROOT", str(tmp_path))
    catalog = Path(utils._get_characters_config_file(uid))
    atomic_write_json(catalog, {"demo": {"intimacy": 60}})
    monkeypatch.setattr(application, "_get_characters_config_file", lambda **kw: str(catalog))
    from blueprints import chat
    monkeypatch.setattr(chat, "_get_characters_config_file", lambda: str(catalog))

    def add_affinity():
        for _ in range(8):
            assert agent_utils._update_user_affinity("demo", 1, uid)

    def update_meta():
        with application.app.test_request_context(json={"remark": "new nickname"}):
            assert chat.update_char_meta("demo").get_json()["status"] == "success"

    def move():
        utils.move_character_position("demo", 0.2, 0.1, user_id=uid)

    run_workers(workers, add_affinity, add_affinity, move, update_meta)
    info = json.loads(catalog.read_text())["demo"]
    assert info["intimacy"] == 76
    assert info["remark"] == "new nickname"
    assert utils.load_character_positions(uid)["demo"]["x"] == 0.2


def test_location_http_writers_and_agent_exploration(workers, tmp_path, monkeypatch):
    uid = 7
    catalog = tmp_path / str(uid) / 'configs' / 'characters.json'
    atomic_write_json(catalog, {"demo": {"name":"demo"}})
    monkeypatch.setattr(utils, "USERS_ROOT", str(tmp_path))
    from core import context
    monkeypatch.setattr(context, "get_current_user_id", lambda: uid)
    monkeypatch.setattr(utils, "get_current_user_id", lambda: uid)
    monkeypatch.setattr(application, "_get_characters_config_file", lambda **kw: str(catalog))
    monkeypatch.setattr(application, "get_char_name", lambda char_id: char_id)

    def add(name, x):
        with application.app.test_request_context(json={"id":name, "name":name, "x":x, "y":0}):
            _, status = lbs.api_add_location()
            assert status == 201

    def explore():
        result = agent_utils._process_location_tags("demo", '[EXPLORE: 0.3, 0.0, "Park", "test"]', uid)
        assert result["status"] == "success", result

    run_workers(workers, lambda:add("west",-3), lambda:add("east",3), explore)
    locations = utils.load_locations(uid)["locations"]
    assert {loc["name"] for loc in locations} == {"家","Park","west","east"}
    assert utils.load_character_positions(uid)["demo"]["location_id"] == next(loc["id"] for loc in locations if loc["name"]=="Park")


def test_timeout_retry_process_exit_and_other_user(workers, tmp_path):
    catalog = tmp_path / "a" / "characters.json"
    ready, leave = workers.Event(), workers.Event()
    def hold():
        with character_state_lock(catalog):
            ready.set()
            assert leave.wait(5)
            os._exit(0)  # No Python cleanup on worker death.

    holder = workers.Process(target=hold)
    holder.start()
    try:
        assert ready.wait(5)
        with character_state_lock(tmp_path / "b" / "characters.json", timeout=0.1):
            with pytest.raises(StorageWriteError) as ex:
                with character_state_lock(catalog, timeout=0.1):
                    pytest.fail("same user entered an occupied section")
            assert ex.value.errno == errno.EBUSY
    finally:
        leave.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join(2)
    assert holder.exitcode == 0
    with character_state_lock(catalog, timeout=0.2), character_state_lock(catalog, timeout=0.1):
        atomic_write_json(catalog, {"retried": True})
    assert json.loads(catalog.read_text()) == {"retried": True}


def test_diary_claim_releases_lock_and_rejects_late_result(workers, tmp_path, monkeypatch):
    import memory_jobs
    day = "2026-11-02"
    catalog = tmp_path / "characters.json"
    atomic_write_json(catalog, {"demo":{"deep_sleep":True, "deep_sleep_source":"schedule",
                                       "bedtime_diary_date":day, "bedtime_diary_status":"pending"}})
    monkeypatch.setattr(application, "_get_characters_config_file", lambda **kw:str(catalog))
    monkeypatch.setattr(application, "is_bedtime_diary_global_enabled", lambda:True)
    monkeypatch.setattr(memory_jobs, "_has_short_memory_events_for_date", lambda *a, **kw:True)
    monkeypatch.setattr(memory_jobs, "BEDTIME_DIARY_CHAR_DELAY_SECONDS", 0)
    from types import SimpleNamespace
    monkeypatch.setattr(application, "update_short_memory_for_date", lambda *a, **kw: SimpleNamespace(ok=True))
    attempts = []

    def generate(char_id, *, user_id, diary_date, attempt):
        attempts.append(attempt)
        # Another scheduler cannot claim the running attempt. A web writer can
        # still acquire the catalog while the external work is in flight.
        def web_write():
            with character_state_lock(catalog, timeout=0.5):
                current=json.loads(catalog.read_text())
                current[char_id]["remark"]="from web"
                atomic_write_json(catalog,current)
        run_workers(workers, web_write, lambda: memory_jobs._process_single_user_bedtime_diaries(user_id, diary_date))
        return finish_diary_attempt(catalog, char_id, diary_date, "success", "done", attempt=attempt)

    monkeypatch.setattr(application, "trigger_bedtime_diary", generate)
    assert memory_jobs._process_single_user_bedtime_diaries(7,day) == 1
    assert attempts == [1]
    assert not finish_diary_attempt(catalog, "demo", day, "failed", "late", attempt=1)
    info=json.loads(catalog.read_text())["demo"]
    assert info["remark"] == "from web" and info["bedtime_diary_status"] == "success"
    info.update(bedtime_diary_status="running",bedtime_diary_attempts=2)
    atomic_write_json(catalog,{"demo":info})
    assert not finish_diary_attempt(catalog,"demo",day,"success","late",attempt=1)
    assert not finish_diary_attempt(catalog,"demo","2026-11-01","success","late",attempt=2)
    assert json.loads(catalog.read_text())["demo"] == info


def test_corrupt_map_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "USERS_ROOT", str(tmp_path))
    uid = 8
    catalog = utils._get_characters_config_file(uid)
    atomic_write_json(catalog, {"demo": {}})
    utils.init_map_data(uid)
    positions=Path(utils._get_character_positions_file(uid))
    positions.write_text('{"truncated":')
    from services.memory_store import MemoryStoreError
    with pytest.raises(MemoryStoreError):
        utils.move_character_position("demo", 0.1, 0, user_id=uid)
    assert positions.read_text() == '{"truncated":'


def test_superseded_model_output_cannot_apply_actions_or_append_diary(workers, tmp_path, monkeypatch):
    import sqlite3
    from datetime import datetime
    from types import SimpleNamespace

    day = "2026-11-02"
    catalog = tmp_path / "characters.json"
    database = tmp_path / "chat.db"
    atomic_write_json(catalog, {"demo":{"bedtime_diary_date":day, "bedtime_diary_status":"running",
                                       "bedtime_diary_attempts":1}})
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE messages (role TEXT, content TEXT, timestamp TEXT)")
    monkeypatch.setattr(application, "_get_characters_config_file", lambda **kw: str(catalog))
    monkeypatch.setattr(application, "get_paths", lambda *a, **kw: (str(database), str(tmp_path)))
    monkeypatch.setattr(application, "is_bedtime_diary_global_enabled", lambda: True)
    monkeypatch.setattr(application, "sync_memory_before_single_chat", lambda *a, **kw: None)
    monkeypatch.setattr(application, "update_short_memory_for_date", lambda *a, **kw: SimpleNamespace(ok=True))
    monkeypatch.setattr(application, "_has_short_memory_events_for_date", lambda *a, **kw: True)
    monkeypatch.setattr(application, "build_system_prompt_v2", lambda *a, **kw: "test")
    monkeypatch.setattr(application, "get_ai_language", lambda *a, **kw: "zh")
    monkeypatch.setattr(application, "get_character_local_now", lambda *a, **kw: datetime(2026,11,2))
    monkeypatch.setattr(application, "get_model_config", lambda *a, **kw: ("relay","fake"))
    monkeypatch.setattr(application, "process_agent_actions", lambda *a, **kw: pytest.fail("old response applied actions"))
    calls = []

    def delayed_reply(*a, **kw):
        calls.append(True)
        def retry():
            with character_state_lock(catalog, timeout=0.3):
                state=json.loads(catalog.read_text())
                state["demo"]["bedtime_diary_attempts"] = 2
                atomic_write_json(catalog,state)
        run_workers(workers,retry)
        return '[THOUGHTS]test[/THOUGHTS]\n[MAINTENANCE_REVIEW: {"persona":"keep","relation":"keep","plan":"keep"}]'

    monkeypatch.setattr(application, "call_openrouter", delayed_reply)
    try:
        assert not application.trigger_bedtime_diary("demo", user_id=7, diary_date=day, attempt=1)
    finally:
        application.clear_background_user()
    assert calls == [True]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone() == (0,)
    assert json.loads(catalog.read_text())["demo"]["bedtime_diary_status"] == "running"


def test_concurrent_create_delete_and_catalog_ownership(workers, tmp_path, monkeypatch):
    from blueprints import chat
    from core import user_resources
    catalog = tmp_path / "7" / "configs" / "characters.json"
    atomic_write_json(catalog, {})
    monkeypatch.setattr(user_resources, "USERS_ROOT", str(tmp_path))
    for mod in (application, chat):
        monkeypatch.setattr(mod, "_get_characters_config_file", lambda **kw: str(catalog))
        monkeypatch.setattr(mod, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(chat, "_get_groups_config_file", lambda **kw:str(catalog.parent / "groups.json"))
    monkeypatch.setattr(chat, "_get_character_positions_file", lambda **kw:str(catalog.parent / "positions.json"))
    monkeypatch.setattr(chat, "_get_read_status_file", lambda:str(catalog.parent / "read_status.json"))
    from services import voice_calls
    monkeypatch.setattr(voice_calls, "delete_calls_for_character", lambda *a:None)

    def create(cid):
        with application.app.test_request_context(json={"id":cid,"name":cid}):
            assert application.add_character().get_json()["status"] == "success"

    def delete(cid):
        with application.app.test_request_context():
            assert chat.delete_character_api(cid).get_json()["status"] == "success"

    run_workers(workers,lambda:create("first"),lambda:create("second"))
    assert set(json.loads(catalog.read_text())) == {"first","second"}
    run_workers(workers,lambda:delete("first"),lambda:create("third"))
    assert set(json.loads(catalog.read_text())) == {"second","third"}
    assert not (tmp_path / "7" / "characters" / "first").exists()
