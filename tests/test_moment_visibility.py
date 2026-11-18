"""Visibility enforced before generation, across persistent post and interaction entry points."""
import copy
import json
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from services.moment_visibility import extract_block_lines, normalize_blocked_ids

CHARACTERS = {
    'author': {'name': '发布者'},
    'blocked': {'name': '角色甲', 'remark': '甲的备注'},
    'allowed': {'name': '角色乙'},
}
STAMP = '2026-10-19 10:00:00'


def test_block_lines_are_exact_multiline_controls_with_unambiguous_names():
    text, blocked = extract_block_lines('正文 ~~普通文字~~\n  ~~甲的备注~~  \n~~blocked~~\n~~author~~\n结尾', CHARACTERS, 'author')
    assert text == '正文 ~~普通文字~~\n结尾'
    assert blocked == ['blocked']
    assert extract_block_lines('~~角色乙~~\r\n照片', CHARACTERS, 'author') == ('照片', ['allowed'])
    duplicate = dict(CHARACTERS, other={'name': '角色甲'})
    with pytest.raises(ValueError):
        extract_block_lines('~~角色甲~~', duplicate, 'author')
    with pytest.raises(ValueError):
        extract_block_lines('~~不存在~~', CHARACTERS, 'author')
    assert extract_block_lines('~~blocked~~\n内容', duplicate, 'author') == ('内容', ['blocked'])


@pytest.mark.parametrize('value', [None, {}, 'blocked', [123], ['user'], ['foreign'], ['author']])
def test_invalid_block_lists_cannot_refer_to_user_self_or_other_accounts(value):
    with pytest.raises(ValueError):
        normalize_blocked_ids(value, CHARACTERS, 'author')


@pytest.fixture
def case(accounts_client, tmp_path, monkeypatch):
    import blueprints.moments as moments
    monkeypatch.setattr(moments, 'USERS_ROOT', str(tmp_path / 'users'))
    config = tmp_path / 'characters.json'
    config.write_text(json.dumps(CHARACTERS), encoding='utf-8')
    monkeypatch.setattr(moments, '_get_characters_config_file', lambda **_: str(config))
    monkeypatch.setattr(moments, 'get_characters_config_for_current_user', lambda: copy.deepcopy(CHARACTERS))
    monkeypatch.setattr(moments, '_get_moments_id_display', lambda **_: ({}, {'user':'我', **{cid:info['name'] for cid,info in CHARACTERS.items()}}))
    path = tmp_path / 'users' / '1' / 'configs' / 'moments_data.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    post = {'char_id':'author', 'timestamp':STAMP, 'content':'秘密内容',
            'blocked_character_ids':['blocked'], 'likers':[],
            'comments':[{'commenter_id':'allowed', 'content':'旧评论', 'timestamp':STAMP}]}
    path.write_text(json.dumps([post]), encoding='utf-8')
    queue = []
    class QueuedThread:
        def __init__(self, target, args=(), **kwargs): self.target, self.args = target, args
        def start(self): queue.append(lambda: self.target(*self.args))
    monkeypatch.setattr(moments.threading, 'Thread', QueuedThread)
    return accounts_client, moments, path, post, queue


@pytest.mark.parametrize('route,extra', [
    ('post/ai_comment', {'commenter_id':'blocked'}),
    ('comment/ai_reply', {'replying_char_id':'blocked','comment_index':0}),
    ('comment/user_reply', {'content':'你怎么看','comment_index':1}),
    ('comment/regenerate', {'comment_index':1}),
    ('memory/regenerate', {'comment_index':1}),
])
def test_every_explicit_interaction_rejects_blocked_roles_before_generation(case, monkeypatch, route, extra):
    client, moments, path, post, _ = case
    # Simulate a stale historical comment; it must not become a bypass.
    post['comments'].append({'commenter_id':'blocked','content':'旧内容','timestamp':STAMP})
    path.write_text(json.dumps([post]), encoding='utf-8')
    before = path.read_bytes()
    mocks = []
    for name in ('_generate_moment_comment','_generate_moment_reply_to_user','_generate_ai_reply_to_any_comment','append_moment_event_to_short_memory'):
        mock = Mock(); monkeypatch.setattr(moments, name, mock); mocks.append(mock)
    response = client.post('/api/moments/'+route, json={'char_id':'author','timestamp':STAMP, **extra})
    assert response.status_code == 403, response.json
    assert response.json['code'] == 'moment_not_visible'
    for mock in mocks: mock.assert_not_called()
    assert path.read_bytes() == before


def test_list_resolves_renamed_roles_and_edit_preserves_visibility(case, monkeypatch):
    client, moments, path, post, _ = case
    monkeypatch.setattr(moments, '_get_moments_id_display', lambda **_: ({}, {'author':'发布者','blocked':'新名字'}))
    response = client.post('/api/moments/edit', json={'char_id':'author','timestamp':STAMP,'new_content':'改过的正文'})
    assert response.status_code == 200
    data = client.get('/api/moments').json[0]
    assert data['blocked_character_ids'] == ['blocked']
    assert data['blocked_characters'][0]['name'] == '新名字'
    assert data['content'] == '改过的正文'
    post.pop('blocked_character_ids')
    path.write_text(json.dumps([post]), encoding='utf-8')
    assert client.get('/api/moments').json[0]['blocked_character_ids'] == []


def test_user_publish_and_mentions_respect_saved_ids(case, monkeypatch):
    client, moments, path, _, queue = case
    generate = Mock(return_value='允许的评论')
    monkeypatch.setattr(moments, '_generate_moment_comment', generate)
    response = client.post('/api/moments/post', data={
        'content':'@角色甲 @角色乙 配文', 'blocked_character_ids':json.dumps(['blocked'])})
    assert response.status_code == 200, response.json
    post = json.loads(path.read_text())[-1]
    assert post['blocked_character_ids'] == ['blocked']
    assert [row['commenter_id'] for row in post['comments']] == ['allowed']
    assert [row['liker_id'] for row in post['likers']] == ['allowed']
    assert generate.call_args.args[0] == 'allowed'
    assert generate.call_count == 1
    # Comment @ triggers are separately guarded.
    monkeypatch.setattr(moments, '_generate_moment_reply_to_user', Mock(return_value='作者回复'))
    response = client.post('/api/moments/comment', json={'char_id':'author','timestamp':STAMP,'content':'@角色甲 @角色乙'})
    assert response.status_code == 200
    assert all(call.args[0] != 'blocked' for call in generate.call_args_list)
    assert queue  # Random interactions remain deferred.


@pytest.mark.parametrize('value', ['{}', '["foreign"]', '["user"]', '[12]', 'invalid'])
def test_invalid_user_selection_does_not_upload_or_publish(case, value):
    client, _, path, _, _ = case
    before = path.read_bytes()
    response = client.post('/api/moments/post', data={'content':'内容','blocked_character_ids':value})
    assert response.status_code == 400
    assert path.read_bytes() == before


def test_background_rechecks_before_rng_and_on_commit(case, monkeypatch):
    _, moments, path, post, queue = case
    monkeypatch.setattr(moments, '_get_moments_relationship_candidates', lambda *a,**k: [('blocked',100),('allowed',100)])
    monkeypatch.setattr(moments.random, 'random', lambda: 0)
    calls = []
    def generate(cid, *args, **kwargs):
        calls.append(cid)
        # Another regeneration blocks the selected role while the provider was busy.
        current = json.loads(path.read_text())
        current[0]['blocked_character_ids'].append('allowed')
        path.write_text(json.dumps(current), encoding='utf-8')
        return '过时结果'
    monkeypatch.setattr(moments, '_generate_moment_comment', generate)
    moments._background_generate_moment_reactions(1, 'author', STAMP, post['content'])
    queue.pop()()
    assert calls == ['allowed']
    saved = json.loads(path.read_text())[0]
    assert saved['likers'] == [] and saved['comments'] == []
    # Stale requests cannot clear privacy metadata or restore blocked reactions.
    snapshot = moments._load_moments_snapshot(str(path))
    snapshot[0]['comments'].append({'commenter_id':'blocked','content':'禁止回填','timestamp':STAMP})
    moments._save_moments(str(path), snapshot)
    saved = json.loads(path.read_text())[0]
    assert set(saved['blocked_character_ids']) == {'blocked','allowed'}
    assert saved['comments'] == []


def test_deleted_post_stops_background_work(case, monkeypatch):
    _, moments, path, post, queue = case
    model = Mock(); monkeypatch.setattr(moments, '_generate_moment_comment', model)
    moments._background_generate_moment_reactions(1, 'author', STAMP, post['content'])
    path.write_text('[]')
    queue.pop()()
    model.assert_not_called()
    assert json.loads(path.read_text()) == []


def setup_author_model(monkeypatch, moments, output):
    import app
    monkeypatch.setattr(moments, 'sync_memory_before_moments', lambda *a,**k: (True,''))
    monkeypatch.setattr(moments, 'process_moments_media_tags', lambda text,*a,**k:text)
    monkeypatch.setattr(moments, 'append_moment_event_to_short_memory', Mock())
    monkeypatch.setattr(app, 'should_use_prompt_v2', lambda *_: True)
    monkeypatch.setattr(app, 'build_system_prompt_v2', lambda *a,**k: 'Persona')
    monkeypatch.setattr(app, 'get_ai_language', lambda *a,**k:'zh')
    monkeypatch.setattr(app, 'get_model_config', lambda *a,**k:('relay','test'))
    monkeypatch.setattr(app, 'process_agent_actions', lambda cid,text,*a,**k:(text,None,None))
    model = Mock(return_value=output)
    monkeypatch.setattr(app, 'call_openrouter', model)
    return model


@pytest.mark.parametrize('directive_type', ['user', 'group'])
@pytest.mark.parametrize('entry', ['post', 'regenerate', 'comment', 'user_reply', 'comment_reply', 'legacy_comment'])
def test_all_moments_entry_points_block_redirects_by_sleeping_characters(case, monkeypatch, entry, directive_type):
    import app
    from core.context import set_background_user

    client, moments, _, post, queue = case
    model = setup_author_model(monkeypatch, moments, '朋友圈正文或评论')
    directive = {'type': directive_type, 'member_ids': ['allowed']}
    monkeypatch.setattr(app, 'process_agent_actions', lambda cid, text, *a, **k: (text, None, directive))
    monkeypatch.setattr(app, 'get_characters_config_for_current_user', lambda: {
        cid: {**info, 'deep_sleep': True, 'chat_mode': 'offline'} for cid, info in CHARACTERS.items()
    })
    monkeypatch.setattr(moments, '_background_generate_moment_reactions', Mock())
    execute = Mock(wraps=app._execute_directive)
    ensure_group = Mock()
    single_paths = Mock()
    monkeypatch.setattr(app, '_execute_directive', execute)
    monkeypatch.setattr(app, 'ensure_directive_chat', ensure_group)
    monkeypatch.setattr(app, 'get_paths', single_paths)

    context = nullcontext() if entry == 'regenerate' else client.application.test_request_context()
    with context:
        set_background_user(1)
        if entry == 'post':
            assert moments.trigger_active_moments('author', user_id=1)
        elif entry == 'regenerate':
            response = client.post('/api/moments/regenerate', json={'char_id': 'author', 'timestamp': STAMP})
            assert response.status_code == 200, response.json
        elif entry == 'comment':
            assert moments._generate_moment_comment('author', 'user', post['content'], user_id=1, moment=post)
        elif entry == 'user_reply':
            assert moments._generate_moment_reply_to_user('author', post['content'], '用户评论', user_id=1, moment=post)
        elif entry == 'comment_reply':
            assert moments._generate_ai_reply_to_any_comment('author', 'author', post['content'], post['comments'], 0, user_id=1, moment=post)
        else:
            assert app._generate_moment_comment('author', 'user', post['content'], user_id=1)
        assert len(queue) == 1
        queue.pop()()

    execute.assert_called_once()
    assert execute.call_args.args[0]['source_scene'] == 'moments'
    assert model.call_count == 1  # Only the original post/comment; no chat generation.
    ensure_group.assert_not_called()
    single_paths.assert_not_called()


def test_ai_posts_strip_controls_before_mentions_and_persist(case, monkeypatch):
    client, moments, path, _, _ = case
    model = setup_author_model(monkeypatch,moments,'秘密 @角色甲 @角色乙\n~~角色甲~~')
    comment = Mock(return_value='回复'); monkeypatch.setattr(moments,'_generate_moment_comment',comment)
    with client.application.test_request_context():
        from core.context import set_background_user
        set_background_user(1)
        assert moments.trigger_active_moments('author',user_id=1)
    post=json.loads(path.read_text())[-1]
    assert '~~' not in post['content']
    assert post['blocked_character_ids'] == ['blocked']
    assert [row['commenter_id'] for row in post['comments']] == ['allowed']
    assert comment.call_count == 1
    assert '~~character ID~~' in model.call_args.args[0][-1]['content']


def test_regeneration_keeps_old_blocks_and_merges_new_controls(case, monkeypatch):
    client, moments, path, _, _ = case
    setup_author_model(monkeypatch,moments,'新正文\n~~allowed~~')
    response=client.post('/api/moments/regenerate',json={'char_id':'author','timestamp':STAMP})
    assert response.status_code == 200, response.json
    post=json.loads(path.read_text())[0]
    assert set(post['blocked_character_ids']) == {'blocked','allowed'}
    assert post['content'] == '新正文' and post['comments'] == []


def test_generators_recheck_before_memory_and_actions(case, monkeypatch):
    _, moments, path, post, _ = case
    model=setup_author_model(monkeypatch,moments,'回复')
    import app
    actions=Mock(return_value=('回复',None,None))
    monkeypatch.setattr(app,'process_agent_actions',actions)
    def model_finishes(*args,**kwargs):
        changed=copy.deepcopy(post);changed['blocked_character_ids'].append('allowed')
        path.write_text(json.dumps([changed]),encoding='utf-8')
        return '回复'
    model.side_effect=model_finishes
    with app.app.test_request_context():
        from core.context import set_background_user
        set_background_user(1)
        assert moments._generate_moment_comment('allowed','author','秘密',user_id=1,moment=post) is None
    actions.assert_not_called()
    moments.append_moment_event_to_short_memory.assert_not_called()


def test_redirect_cannot_deliver_source_to_blocked_recipient(case, monkeypatch):
    _, moments, _, post, _ = case
    import app
    directive=moments._moment_directive({'type':'group','member_ids':['blocked']},post)
    ensure=Mock();monkeypatch.setattr(app,'ensure_directive_chat',ensure)
    monkeypatch.setattr(app,'get_char_name',lambda cid:cid)
    with app.app.test_request_context():
        from core.context import set_background_user
        set_background_user(1)
        app._execute_directive(directive,'author','秘密')
    ensure.assert_not_called()
    assert moments.moment_directive_allowed(directive,['author','allowed'],1)
    assert not moments.moment_directive_allowed(directive,['blocked'],1)
