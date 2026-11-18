import json
import sqlite3
from types import SimpleNamespace

import pytest
from flask import Flask

import blueprints.forum as forum
import services.forum_backgrounds as backgrounds


@pytest.fixture(params=['generate', 'generate-stream'])
def api(tmp_path, monkeypatch, request):
    db = tmp_path / 'forums.db'
    users = tmp_path / 'users'
    monkeypatch.setattr(forum, 'FORUMS_DB', str(db))
    monkeypatch.setattr(backgrounds, 'USERS_ROOT', str(users))
    forum.init_forums_db()
    state = SimpleNamespace(uid=1, fail=False, prompts=[])
    monkeypatch.setattr(forum, 'get_current_user_id', lambda: state.uid)
    monkeypatch.setattr(forum, 'user_language', lambda *args: 'zh')

    def call(messages, **kwargs):
        state.prompts.append(messages[0]['content'])
        if state.fail:
            raise RuntimeError('Simulated generation failure')
        return json.dumps({'title': '测试论坛', 'posts': [
            {'name': '常驻网友', 'role': 'npc', 'floor': len(state.prompts) - 1, 'content': '继续讨论'},
        ]})

    def stream(messages, **kwargs):
        result = json.loads(call(messages))
        yield f"[FORUM:测试论坛][POST:常驻网友:npc:{result['posts'][0]['floor']}]继续讨论[/POST][/FORUM]"

    class ImmediateThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(forum, 'call_active_model', call)
    monkeypatch.setattr(forum, 'stream_ai_call', stream)
    monkeypatch.setattr(forum.threading, 'Thread', ImmediateThread)
    app = Flask(__name__)
    app.register_blueprint(forum.forum_bp)
    client = app.test_client()
    payload = {'topic': '测试主题', 'chat_segments': [{'messages': [{'role': 'user', 'content': '公开事件'}]}]}

    def post(**extra):
        return client.post('/api/forums/' + request.param, json={**payload, **extra})

    def generate(expect_error=False, **extra):
        response = post(**extra)
        assert response.status_code == 200, response.json
        if request.param == 'generate':
            result = client.get('/api/forums/task/' + response.json['task_id']).json
            if expect_error:
                assert result['status'] == 'error', result
                return
            assert result['status'] == 'done', result
            return result['result']
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith('data: ')]
        if expect_error:
            assert events[-1].get('error'), events
            return
        assert events[-1].get('done'), events
        return events[-1]['forum']

    return SimpleNamespace(client=client, generate=generate, post=post, state=state, db=db, users=users)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def add_personas(api, uid):
    root = api.users / str(uid)
    write_json(root / 'configs/user_settings.json', {'current_user_name': f'用户{uid}', 'api_key': 'never-return-this'})
    (root / 'configs/global_user_persona.md').write_text(f'用户背景{uid}', encoding='utf-8')
    write_json(root / 'configs/characters.json', {'rin': {'name': '凛'}, 'sae': {'name': '冴'}, '../2': {'name': 'invalid'}})
    for cid in ('rin', 'sae'):
        write_json(root / f'characters/{cid}/prompts/1_base_persona.json', {'system_prompt': f'{cid}旧设定{uid}'})


def test_background_source_selection_snapshots_and_reusable_library(api):
    add_personas(api, 1)
    add_personas(api, 2)
    sources = api.client.get('/api/forums/background-sources').json['sources']
    assert {s['id'] for s in sources} == {'user', 'character:rin', 'character:sae'}
    assert sources[0]['label'] == '用户1'
    assert all('2' not in s['content'] and 'never-return-this' not in s['content'] for s in sources)
    snapshot = {'items': sources, 'note': '只有社团成员知道关系'}
    saved = api.generate(background={**snapshot, 'mode': 'save', 'name': '校园背景'})
    assert saved['background_config'] == snapshot
    for source in sources:
        assert source['content'] in api.state.prompts[-1]
    assert '只有社团成员知道关系' in api.state.prompts[-1]
    assert saved['saved_background']['label'] == '校园背景'
    library = saved['saved_background']
    assert library in api.client.get('/api/forums/background-sources').json['sources']

    # Changing a live persona never changes a saved snapshot or named bundle.
    write_json(api.users / '1/characters/rin/prompts/1_base_persona.json', {'system_prompt': '凛已修改的设定'})
    continued = api.generate(historical_forum_id=saved['id'])
    assert continued['background_config'] == snapshot
    assert 'rin旧设定1' in api.state.prompts[-1]
    assert '凛已修改的设定' not in api.state.prompts[-1]
    assert api.client.get(f"/api/forums/{saved['id']}").json['background_config'] == snapshot
    assert api.client.get('/api/forums/list').json[0]['background_config'] == snapshot
    reused = api.generate(background={'items': [library], 'note': '', 'mode': 'save'})
    assert reused['id'] != saved['id']
    assert reused['background_config']['items'] == [library]

    api.state.uid = 2
    other_sources = api.client.get('/api/forums/background-sources').json['sources']
    assert all(s['kind'] != 'saved' for s in other_sources)
    assert api.client.get(f"/api/forums/{saved['id']}").status_code == 404
    assert api.client.get('/api/forums/list').json == []
    api.state.uid = None
    assert api.client.get('/api/forums/background-sources').status_code == 401


def test_temporary_background_does_not_replace_saved_config_or_leak_to_next_round(api):
    once = {'items': [], 'note': '临时发布会背景', 'mode': 'once'}
    fresh = api.generate(background=once)
    assert '临时发布会背景' in api.state.prompts[-1]
    assert fresh['background_config'] == {'items': [], 'note': ''}
    assert fresh['supplementary_info'] == ''
    assert fresh['saved_background'] is None

    saved = api.generate(background={'items': [], 'note': '长期学校背景', 'mode': 'save'})
    transient = api.generate(historical_forum_id=saved['id'], background=once)
    assert '临时发布会背景' in api.state.prompts[-1]
    assert '长期学校背景' not in api.state.prompts[-1]
    assert transient['background_config'] == saved['background_config']
    assert transient['supplementary_info'] == '长期学校背景'
    api.generate(historical_forum_id=saved['id'])
    assert '长期学校背景' in api.state.prompts[-1]
    assert '临时发布会背景' not in api.state.prompts[-1]

    cleared = api.generate(historical_forum_id=saved['id'], background={'mode': 'save', 'items': [], 'note': ''})
    assert cleared['background_config'] == {'items': [], 'note': ''}
    assert api.generate(historical_forum_id=saved['id'])['supplementary_info'] == ''


def test_failed_generation_preserves_previous_background_and_library(api):
    saved = api.generate(supplementary_info='已保存的背景')
    api.state.fail = True
    api.generate(expect_error=True, historical_forum_id=saved['id'],
                 background={'mode': 'save', 'items': [], 'note': '不能保存这个', 'name': '失败的组合'})
    assert api.client.get(f"/api/forums/{saved['id']}").json['supplementary_info'] == '已保存的背景'
    assert api.client.get('/api/forums/background-sources').json['sources'] == []


def test_invalid_backgrounds_fail_before_generation(api):
    item = {'id': 'user', 'kind': 'user', 'label': '用户', 'content': '设定'}
    invalid = [None, [], {'mode': []}, {'mode': 'other'}, {'mode': 'save', 'items': 'bad'},
               {'mode': 'save', 'items': [item, item]}, {'mode': 'save', 'note': 'a' * 200001},
               {'mode': 'once', 'name': '不能保存', 'note': '临时'}, {'mode': 'save', 'name': '空背景'}]
    for background in invalid:
        assert api.post(background=background).status_code == 400
    assert api.state.prompts == []


def test_migration_keeps_legacy_background_and_account_cleanup(tmp_path, monkeypatch):
    db = tmp_path / 'legacy.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE forums (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, topic TEXT, '
                     'forum_type TEXT, custom_type_description TEXT, supplementary_info TEXT, posts_json TEXT, created_at TEXT)')
        conn.execute("INSERT INTO forums VALUES (1,1,'旧论坛','旧主题','novel_readers','','旧版背景','[]','2026-10-01')")
    monkeypatch.setattr(forum, 'FORUMS_DB', str(db))
    forum.init_forums_db()
    forum.init_forums_db()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute('SELECT * FROM forums').fetchone())
        assert backgrounds.stored_config(row) == {'items': [], 'note': '旧版背景'}
        assert row['created_at'] == '2026-10-01'
        for uid in (1, 2):
            conn.execute('INSERT INTO forum_backgrounds (user_id,name,snapshot_json,created_at) VALUES (?,?,?,?)',
                         (uid, '保留背景', '{"items":[],"note":"内容"}', '2026-10-01'))
    from blueprints import auth
    monkeypatch.setattr(auth, 'FORUMS_DB', str(db))
    auth._delete_user_forums(1)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM forums').fetchone()[0] == 0
        assert conn.execute('SELECT user_id FROM forum_backgrounds').fetchall() == [(2,)]
