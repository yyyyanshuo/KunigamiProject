"""Manual group entries survive the real save/read API without resetting progress."""
import json

from flask import Flask


def test_add_entries_to_empty_group_and_preserve_existing_progress(tmp_path, monkeypatch):
    from blueprints import group
    folder = tmp_path / 'group'
    folder.mkdir()
    config = tmp_path / 'groups.json'
    config.write_text(json.dumps({'g': {'name': 'Test', 'members': []}}))
    monkeypatch.setattr(group, 'get_group_dir', lambda *_: str(folder))
    monkeypatch.setattr(group, '_get_groups_config_file', lambda: str(config))
    monkeypatch.setattr(group, 'memory_timezone', lambda: 'Asia/Shanghai')
    app = Flask(__name__)
    app.register_blueprint(group.group_bp)
    client = app.test_client()
    assert client.get('/api/group/g/prompts_data').json['short'] == {}
    events = [{'time': '10:00', 'event': '带有 "引号" 和 <b>原文</b>'}]
    payload = {'2026-10-28': {'events': events, 'last_id': 0, 'reset_progress': False}}
    response = client.post('/api/group/g/save_memory', json={'content': payload})
    assert response.status_code == 200
    saved = client.get('/api/group/g/prompts_data').json['short']
    assert saved['2026-10-28']['events'][0]['event'] == events[0]['event']
    original_timestamp = saved['2026-10-28']['events'][0]['timestamp']
    saved['2026-10-28']['last_id'] = 42
    (folder / 'memory_short.json').write_text(json.dumps(saved))
    payload['2026-10-28']['last_id'] = 42
    payload['2026-10-28']['events'].append({'time': '11:00', 'event': '第二条'})
    payload['2026-10-27'] = {'events': [{'time': '23:00', 'event': '前一天'}], 'last_id': 0}
    assert client.post('/api/group/g/save_memory', json={'content': payload}).status_code == 200
    saved = client.get('/api/group/g/prompts_data').json['short']
    assert len(saved) == 2 and len(saved['2026-10-28']['events']) == 2
    assert saved['2026-10-28']['last_id'] == 42
    assert saved['2026-10-28']['events'][0]['timestamp'] == original_timestamp
