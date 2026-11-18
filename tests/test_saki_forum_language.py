import json
import sqlite3
import pytest
from flask import Flask
import blueprints.forum as forum


@pytest.mark.parametrize('forum_type', ['novel_readers', 'world_bystanders'])
def test_forum_language_both_paths_and_continuation(tmp_path,monkeypatch,forum_type):
    db=tmp_path/'forums.db';monkeypatch.setattr(forum,'FORUMS_DB',str(db));forum.init_forums_db()
    monkeypatch.setattr(forum,'get_current_user_id',lambda:1)
    monkeypatch.setattr(forum,'user_language',lambda *args:'en')
    seen=[]
    def call(messages,**kwargs):
        seen.append(messages);return json.dumps({'title':'Generated title','posts':[{'name':'Author','content':'New body','floor':0}]})
    def stream(messages,**kwargs):
        seen.append(messages);yield '[FORUM:New title][POST:Author:npc:1]New reply[/POST][/FORUM]'
    class ImmediateThread:
        def __init__(self,target,args,**kwargs):self.target=target;self.args=args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(forum,'call_active_model',call);monkeypatch.setattr(forum,'stream_ai_call',stream);monkeypatch.setattr(forum.threading,'Thread',ImmediateThread)
    app=Flask(__name__);app.register_blueprint(forum.forum_bp);c=app.test_client()
    payload={'topic':'Topic','forum_type':forum_type,'chat_segments':[{
        'contact_name': '小凛', 'contact_type': 'chat', 'user_name': '桐奈', 'character_name': '糸師凛',
        'messages':[{'role':'user','content':'source','timestamp':'2026-10-07 20:15'},
                    {'role':'assistant','sender_name':'糸師凛','content':'reply'}],
    }]}
    response=c.post('/api/forums/generate',json=payload);assert response.status_code==200,response.json
    task=c.get('/api/forums/task/' + response.json['task_id']).json;assert task['status']=='done',task
    assert '"en"' in seen[-1][0]['content']
    old=task['result'];old_posts=old['posts']
    result=c.post('/api/forums/generate-stream',json=dict(payload,language='Français',historical_forum_id=old['id']))
    assert result.status_code==200
    body=result.get_data(as_text=True);assert '"done": true' in body,body
    assert '"Français"' in seen[-1][0]['content']
    history = seen[-1][0]['content'].split('<existing_posts>\n', 1)[1].split('\n</existing_posts>', 1)[0]
    assert json.loads(history) == old_posts
    with sqlite3.connect(db) as conn:
        title,posts=conn.execute('SELECT title,posts_json FROM forums WHERE id=?',(old['id'],)).fetchone()
    assert title==old['title'];assert json.loads(posts)[:len(old_posts)]==old_posts
    response=c.post('/api/forums/generate',json=dict(payload,language='Español',historical_forum_id=old['id']))
    task=c.get('/api/forums/task/' + response.json['task_id']).json;assert task['status']=='done',task
    assert '"Español"' in seen[-1][0]['content'];assert task['result']['title']==old['title']
    history = seen[-1][0]['content'].split('<existing_posts>\n', 1)[1].split('\n</existing_posts>', 1)[0]
    assert json.loads(history) == json.loads(posts)
    result=c.post('/api/forums/generate-stream',json=payload);result.get_data();assert '"en"' in seen[-1][0]['content']
    for messages in seen:
        prompt = messages[0]['content']
        assert '[原记录时间：2026-10-07 20:15] [桐奈（用户）] source' in prompt
        assert '[糸師凛（角色）] reply' in prompt
        assert '比例仅作节奏参考' in prompt
        assert '一个问号、一个词或一句话' in prompt
        assert '不会变成多份独立证据' in prompt
        if forum_type == 'novel_readers':
            assert '模式：小说读者论坛' in prompt
            assert '不可用未提供的新章来验证自己' in prompt
        else:
            assert '模式：世界路人论坛' in prompt
            assert '否则不能被网友直接引用、读心或据此确认真相' in prompt
    for path in ['generate','generate-stream']:
        assert c.post('/api/forums/'+path,json=dict(payload,language='x'*101)).status_code==400


@pytest.mark.parametrize('builder', [forum.build_forum_generation_prompt, forum.build_forum_streaming_prompt])
@pytest.mark.parametrize('old_posts, next_floor', [
    ([], 0),
    ([{'name': '旧ID', 'role': 'npc', 'floor': 0, 'content': '我先蹲'}], 1),
    ([{'name': '旧ID', 'role': 'npc', 'floor': 0, 'content': '我先蹲'},
      {'name': 'old_fan', 'role': 'npc', 'floor': 7, 'content': '这个猜测还没证实'},
      {'name': '旧ID', 'role': 'npc', 'floor': None, 'content': '旧格式回复'}], 8),
])
def test_forum_batch_uses_actual_floor_numbers_and_keeps_custom_context(builder, old_posts, next_floor):
    messages = builder(
        chat_segments=[{'contact_name': '测试联系人', 'messages': [{'role': 'user', 'content': '约在周五'}]}],
        forum_type='custom', custom_type_description='校园音乐社讨论区',
        floor_limit=1, supplementary_info='只公开排练日期，不公开名单', topic='排练安排',
        existing_posts=old_posts, output_language='ja',
    )
    prompt = messages[0]['content']
    assert f'本批只生成 1 个新楼层，楼层号从 {next_floor} 到 {next_floor}' in prompt
    assert '自定义论坛类型：校园音乐社讨论区' in prompt
    assert '只公开排练日期，不公开名单' in prompt
    assert '"ja"' in prompt
    assert '模式：小说读者论坛' not in prompt
    assert '模式：世界路人论坛' not in prompt
    if old_posts:
        history = prompt.split('<existing_posts>\n', 1)[1].split('\n</existing_posts>', 1)[0]
        assert json.loads(history) == old_posts
        assert '复用旧ID时保持原拼写' in prompt
    else:
        assert '<existing_posts>' not in prompt


@pytest.mark.parametrize('builder', [forum.build_forum_generation_prompt, forum.build_forum_streaming_prompt])
def test_forum_source_preserves_names_and_distinguishes_group_speakers(builder):
    segments = [
        {'contact_name': '小凛', 'contact_type': 'chat', 'user_name': '桐奈', 'character_name': '糸師凛', 'messages': [
            {'role': 'user', 'sender_name': '桐奈', 'content': '晚上好'},
            {'role': 'assistant', 'sender_name': '糸師凛', 'content': '晚安', 'timestamp': '2026-10-07T12:00:00Z'},
        ]},
        {'contact_name': '足球部', 'contact_type': 'group', 'user_name': '桐奈', 'messages': [
            {'role': 'rin', 'sender_name': '糸師凛', 'content': '这是凛说的'},
            {'role': 'sae', 'sender_name': '糸師冴', 'content': '这是冴说的'},
            {'role': 'system', 'content': '系统场景提示'},
            {'role': 'unknown_member', 'content': '未知姓名'},
        ]},
        {'contact_name': '另一段记录', 'contact_type': 'chat', 'user_name': '花子', 'character_name': '洁世一', 'messages': [
            {'role': 'user', 'content': '另一个用户'}, {'role': 'assistant', 'content': '另一个角色'},
        ]},
        {'messages': [{'role': 'user', 'content': '旧用户消息'}, {'role': 'assistant', 'content': '旧角色消息'}]},
    ]
    prompt = builder(segments, 'world_bystanders', '', 5, '', '群聊讨论', output_language='en')[0]['content']
    evidence = prompt.split('<chat_source>\n', 1)[1].split('\n</chat_source>', 1)[0]
    assert '用户姓名：桐奈' in evidence
    assert '角色姓名：糸師凛' in evidence
    assert '[桐奈（用户）] 晚上好' in evidence
    assert '[原记录时间：2026-10-07T12:00:00Z] [糸師凛（角色）] 晚安' in evidence
    assert '[糸師凛（角色）] 这是凛说的' in evidence
    assert '[糸師冴（角色）] 这是冴说的' in evidence
    assert '[system] 系统场景提示' in evidence
    assert '[unknown_member] 未知姓名' in evidence
    assert '角色姓名：足球部' not in evidence
    assert '[花子（用户）] 另一个用户' in evidence
    assert '[洁世一（角色）] 另一个角色' in evidence
    assert '[用户] 旧用户消息' in evidence
    assert '[对方] 旧角色消息' in evidence


@pytest.mark.parametrize('route', ['generate', 'generate-stream'])
def test_forum_continuation_inherits_saved_background_across_rounds(tmp_path, monkeypatch, route):
    db = tmp_path / 'forums.db'
    monkeypatch.setattr(forum, 'FORUMS_DB', str(db))
    forum.init_forums_db()
    monkeypatch.setattr(forum, 'get_current_user_id', lambda: 1)
    monkeypatch.setattr(forum, 'user_language', lambda *args: 'zh')
    prompts = []

    def call(messages, **kwargs):
        prompts.append(messages[0]['content'])
        return json.dumps({'title': '原帖标题', 'posts': [
            {'name': '常驻ID', 'role': 'npc', 'floor': len(prompts) - 1, 'content': '继续讨论'},
        ]})

    def stream(messages, **kwargs):
        result = json.loads(call(messages))
        floor = result['posts'][0]['floor']
        yield f'[FORUM:原帖标题][POST:常驻ID:npc:{floor}]继续讨论[/POST][/FORUM]'

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
    payload = {
        'topic': '排练安排', 'forum_type': 'custom', 'custom_type_description': '校园音乐社讨论区',
        'floor_limit': 1, 'chat_segments': [{'messages': [{'role': 'user', 'content': '约在周五'}]}],
    }

    def generate(**extra):
        response = client.post('/api/forums/' + route, json={**payload, **extra})
        assert response.status_code == 200
        if route == 'generate':
            task = client.get('/api/forums/task/' + response.json['task_id']).json
            assert task['status'] == 'done', task
            return task['result']
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith('data: ')]
        assert events[-1].get('done'), events
        return events[-1]['forum']

    original = '两人是同学，只有社团成员知道排练日期。'
    saved = generate(supplementary_info=original)
    forum_id = saved['id']
    payload.update(historical_forum_id=forum_id, topic='本次输入的临时标题', forum_type='novel_readers')

    for background in ({}, {'supplementary_info': ''}, {'supplementary_info': ' \n '}, {'supplementary_info': None}):
        saved = generate(**background)
        assert original in prompts[-1]
        assert saved['supplementary_info'] == original
        assert saved['topic'] == '排练安排'
        assert saved['custom_type_description'] == '校园音乐社讨论区'
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT supplementary_info FROM forums WHERE id=?', (forum_id,)).fetchone()[0] == original

    edited = original + '\n新增：演出安排尚未公开。'
    assert generate(supplementary_info=edited)['supplementary_info'] == edited
    assert generate()['supplementary_info'] == edited
    assert edited in prompts[-1]
    assert client.get(f'/api/forums/{forum_id}').json['supplementary_info'] == edited
    assert client.get('/api/forums/list').json[0]['supplementary_info'] == edited
