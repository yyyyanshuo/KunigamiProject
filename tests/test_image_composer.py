"""Image instructions, without calling real model/storage services."""
import io

from flask import Flask
from PIL import Image
import pytest


@pytest.fixture
def media_client(tmp_path, monkeypatch):
    import blueprints.media as media
    monkeypatch.setattr(media.core.config, 'USERS_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(media, 'get_current_user_id', lambda: 1)
    monkeypatch.setattr(media, 'upload_to_cos', lambda *_: None)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(media.media_bp)
    return app.test_client(), media


def upload(client, **fields):
    image = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')
    image.seek(0)
    return client.post('/api/vision/upload', data={'file': (image, 'sample.png'), **fields})


def test_custom_prompt_reaches_model_but_not_description(media_client, monkeypatch):
    client, media = media_client
    import services.ai_client as ai
    import services.model_provider as provider
    monkeypatch.setattr(ai, 'get_model_config', lambda _: (None, 'test-vision'))
    calls = []
    def call(messages, **kwargs):
        calls.append((messages, kwargs))
        return '蓝色的画面，标注 f(x/y)。'
    monkeypatch.setattr(provider, 'call_provider', call)
    response = upload(client, vision_prompt='  重点描述服装。  ')
    assert response.status_code == 200
    message = calls[0][0][0]['content']
    assert message[0]['text'].startswith('重点描述服装。\n')
    assert '最多500字' in message[0]['text']
    assert message[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert calls[0][1]['model_name'] == 'test-vision'
    assert response.json['description'] == '蓝色的画面，标注 f(x/y)。'
    assert 'vision_prompt' not in response.json


def test_empty_prompt_keeps_default_and_manual_skips_model(media_client, monkeypatch):
    client, media = media_client
    import services.ai_client as ai
    import services.model_provider as provider
    monkeypatch.setattr(ai, 'get_model_config', lambda _: (None, 'test-vision'))
    calls = []
    def call(messages, **kwargs):
        calls.append(messages)
        return '蓝色的画面。'
    monkeypatch.setattr(provider, 'call_provider', call)
    assert upload(client, vision_prompt=' ').status_code == 200
    assert calls[0][0]['content'][0]['text'].startswith(media.VISION_DESCRIPTION_PROMPT)
    manual = upload(client, description_mode='manual', description='手写描述', vision_prompt='字' * 3000)
    assert manual.status_code == 200
    assert manual.json['description'] == '手写描述'
    assert len(calls) == 1


def test_prompt_limit_checked_before_upload_or_model(media_client, monkeypatch, tmp_path):
    client, media = media_client
    monkeypatch.setattr(media, '_generate_image_description', lambda *_a, **_k: pytest.fail('Must not call model'))
    response = upload(client, vision_prompt='字' * 2001)
    assert response.status_code == 400
    assert not (tmp_path / 'users').exists()


def test_ai_failure_still_allows_manual_fallback(media_client, monkeypatch):
    client, media = media_client
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic unavailable')
    monkeypatch.setattr(media, '_generate_image_description', fail)
    response = upload(client, vision_prompt='自定义')
    assert response.status_code == 502
    assert response.json['code'] == 'vision_failed'
    assert upload(client, description_mode='manual', description='手动内容').status_code == 200


def test_multiline_manual_description_at_limit(media_client):
    client, _ = media_client
    detail = "第一行（说明）\n" + "🎨" * 3992
    assert len(detail) == 4000
    response = upload(client, description_mode="manual", description=detail.replace("\n", "\r\n"))
    assert response.status_code == 200
    assert response.json["description"] == detail
    too_long = upload(client, description_mode="manual", description=detail + "字")
    assert too_long.status_code == 400
    assert too_long.json["error"] == "图片描述不得超过4000字"


def test_saved_vision_prompts_are_persistent_isolated_deduplicated_and_removable(media_client, monkeypatch):
    client, media = media_client
    uid = [1]
    monkeypatch.setattr(media, 'get_current_user_id', lambda: uid[0])
    assert client.get('/api/vision/prompts').json == {'prompts': []}
    for text in ['First', 'Second', ' First ']:
        assert client.post('/api/vision/prompts', json={'action':'add', 'prompt':text}).status_code == 200
    assert client.get('/api/vision/prompts').json['prompts'] == ['First', 'Second']
    uid[0] = 2
    assert client.get('/api/vision/prompts').json['prompts'] == []
    uid[0] = 1
    assert client.post('/api/vision/prompts', json={'action':'delete', 'prompt':'First'}).json['prompts'] == ['Second']
    for text in ['', '字'*2001, {'bad':True}]:
        assert client.post('/api/vision/prompts', json={'action':'add', 'prompt':text}).status_code == 400
    assert client.get('/api/vision/prompts').json['prompts'] == ['Second']
    for action in [[], {}, None, 'replace']:
        assert client.post('/api/vision/prompts', json={'action':action, 'prompt':'valid'}).status_code == 400
    uid[0] = None
    assert client.get('/api/vision/prompts').status_code == 401
    assert client.post('/api/vision/prompts', json={'action':'add','prompt':'private'}).status_code == 401


def test_saved_prompt_limit_and_write_failure_preserve_previous_prompts(media_client, monkeypatch):
    client, media = media_client
    for i in range(30):
        assert client.post('/api/vision/prompts', json={'action':'add','prompt':str(i)}).status_code == 200
    assert client.post('/api/vision/prompts', json={'action':'add','prompt':'full'}).status_code == 400
    import core.json_storage as storage
    def fail(*args): raise OSError('synthetic write failure')
    monkeypatch.setattr(storage, 'atomic_write_json', fail)
    assert client.post('/api/vision/prompts', json={'action':'delete','prompt':'0'}).status_code == 500
    assert client.get('/api/vision/prompts').json['prompts'] == [str(i) for i in range(30)]


def test_saved_prompts_concurrent_adds_do_not_overwrite(media_client):
    from concurrent.futures import ThreadPoolExecutor
    client, _ = media_client
    app = client.application
    def add(i):
        with app.test_client() as local:
            return local.post('/api/vision/prompts', json={'action':'add','prompt':str(i)}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(add, range(12))) == [200]*12
    assert set(client.get('/api/vision/prompts').json['prompts']) == {str(i) for i in range(12)}


@pytest.fixture
def moments_upload_client(tmp_path, monkeypatch):
    import blueprints.moments as moments
    import blueprints.media as media
    import cos_utils
    app = Flask(__name__); app.config['TESTING'] = True; app.register_blueprint(moments.moments_bp)
    monkeypatch.setattr(moments, 'get_current_user_id', lambda: 1)
    monkeypatch.setattr(moments, 'USERS_ROOT', str(tmp_path/'users'))
    monkeypatch.setattr(moments, 'get_characters_config_for_current_user', lambda: {})
    monkeypatch.setattr(moments, '_get_moments_name_to_id', lambda **kw: {})
    monkeypatch.setattr(moments, 'get_moments_paths', lambda **kw: (str(tmp_path/'posts.json'), 'unused'))
    monkeypatch.setattr(moments, '_load_moments_snapshot', lambda *_: [])
    monkeypatch.setattr(moments, '_save_moments', lambda *_: None)
    monkeypatch.setattr(moments, '_generate_likes_comments_for_user_moment', lambda *a,**kw: ([],[]))
    monkeypatch.setattr(moments, '_append_moment_reactions', lambda *a,**kw: None)
    monkeypatch.setattr(moments, '_read_moment', lambda *a: None)
    monkeypatch.setattr(moments, '_background_generate_moment_reactions', lambda *a,**kw: None)
    monkeypatch.setattr(cos_utils, 'upload_to_cos', lambda *a: 'https://media.invalid/test.jpg')
    calls = []
    def vision(source, vision_prompt=''):
        calls.append(vision_prompt); return 'Recognized picture'
    monkeypatch.setattr(media, '_generate_image_description', vision)
    return app.test_client(), calls


def post_images(client, modes, prompt):
    import json
    images = []
    for i in range(len(modes)):
        image = io.BytesIO(); Image.new('RGB', (8,8), 'blue').save(image,format='PNG'); image.seek(0)
        images.append((image, f'{i}.png'))
    return client.post('/api/moments/post', data={'content':'My pictures', 'images':images,
        'vision_prompt':prompt, 'image_metadata':json.dumps([{'mode':mode,'description':'Manual picture'} for mode in modes])})


def test_moments_shared_prompt_applies_only_to_ai_images(moments_upload_client):
    client, calls = moments_upload_client
    response = post_images(client, ['ai','manual','ai'], ' Focus on clothes ')
    assert response.status_code == 200, response.json
    assert calls == ['Focus on clothes', 'Focus on clothes']
    text = response.json['post']['content']
    assert 'Focus on clothes' not in text
    assert 'Recognized picture' in text and 'Manual picture' in text


def test_moments_prompt_validation_precedes_processing_and_manual_ignores_it(moments_upload_client):
    client, calls = moments_upload_client
    assert post_images(client, ['ai'], 'x'*2001).status_code == 400
    assert calls == []
    assert post_images(client, ['manual'], 'x'*2001).status_code == 200
    assert calls == []
    assert post_images(client, ['ai'], '').status_code == 200
    assert calls == ['']
