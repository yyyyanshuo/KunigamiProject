import base64
import json
import pytest
from cryptography.fernet import Fernet
from core import model_settings as settings
from core import credentials
from services import ai_client
from services import model_provider as provider


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_ROOT', str(tmp_path))
    store = credentials.CredentialStore(tmp_path / 'users', Fernet.generate_key().decode())
    monkeypatch.setattr(credentials, 'default_store', store)
    return store


def test_defaults_and_all_task_routing(isolated):
    assert settings.load_config(1)['active_route'] == 'gemini'
    for route in settings.PROVIDERS:
        cfg = settings.defaults(); cfg['active_route'] = route
        cfg['routes']['custom']['base_url'] = 'https://relay.example/v1'
        settings.save_config(cfg, 1)
        for task in settings.TASKS:
            assert ai_client.get_model_config(task, 1) == (route, settings.DEFAULT_MODELS[route][task])


def test_migration_preserves_model_ids_and_custom_key(isolated):
    isolated.set(1, 'openrouter', 'synthetic-old-key')
    cfg = {'active_route': 'relay', 'enable_system_prompt_v2': False, 'routes': {'relay': {'relay_provider': 'custom', 'relay_custom_url': 'https://relay.example/v1', 'models': {'chat': 'private-model', 'image': ''}}}}
    result = settings.normalize_config(cfg)
    assert result['active_route'] == 'custom'
    assert result['routes']['custom']['models']['chat'] == 'private-model'
    assert result['routes']['custom']['models']['image'] == ''
    assert result['routes']['custom']['base_url'] == 'https://relay.example/v1'
    assert result['enable_system_prompt_v2'] is False
    assert isolated.get(1, 'openrouter') == 'synthetic-old-key'
    assert not settings.config_path(1).exists()


@pytest.mark.parametrize('route', settings.PROVIDERS)
def test_keys_encrypted_and_separate(isolated, route):
    cfg = settings.defaults(); cred = settings.PROVIDERS[route]['credential']
    cfg['credential_updates'] = {cred: 'synthetic-'+route+'-secret'}
    settings.save_config(cfg, 1)
    assert isolated.get(1, cred) == 'synthetic-'+route+'-secret'
    for path in [settings.config_path(1), isolated.credential_path(1)]:
        assert 'synthetic-'+route+'-secret' not in path.read_text()
    assert 'credential_updates' not in settings.public_config(1)
    assert isolated.get(2, cred) == ''


def test_failed_save_rolls_back_keys_and_models(isolated, monkeypatch):
    cfg = settings.defaults(); settings.save_config(cfg, 1)
    isolated.set(1, 'gemini', 'original-secret')
    original = settings.config_path(1).read_bytes()
    write = isolated._atomic_write_json
    def fail_config(path, data):
        if path == settings.config_path(1) and data['active_route'] == 'openai':
            raise OSError('synthetic disk failure')
        return write(path, data)
    monkeypatch.setattr(isolated, '_atomic_write_json', fail_config)
    cfg['active_route'] = 'openai';cfg['credential_updates'] = {'gemini': 'changed-secret'}
    with pytest.raises(OSError): settings.save_config(cfg, 1)
    assert isolated.get(1, 'gemini') == 'original-secret'
    assert settings.config_path(1).read_bytes() == original


@pytest.mark.parametrize('url',['http://relay.example','https://localhost/v1','https://127.0.0.1/v1','https://user:secret@relay.example/v1','https://relay.example/?key=secret'])
def test_invalid_custom_urls_rejected(isolated,url):
    cfg=settings.defaults();cfg['active_route']='custom';cfg['routes']['custom']['base_url']=url
    with pytest.raises(ValueError):settings.save_config(cfg,1)
    assert not settings.config_path(1).exists()


def test_claude_image_empty_and_invalid_protocol(isolated):
    cfg=settings.defaults();cfg['active_route']='anthropic';cfg['routes']['anthropic']['models']['image']='invented-image'
    settings.save_config(cfg,1)
    assert ai_client.get_model_config('image',1)==('anthropic','')
    cfg['routes']['custom']['protocol']='invalid'
    with pytest.raises(ValueError):settings.save_config(cfg,1)


@pytest.mark.parametrize('protocol',settings.PROTOCOLS)
def test_protocol_payload_preserves_system_and_vision(protocol):
    messages=[{'role':'system','content':'Persona 原文'}, {'role':'user','content':[{'type':'text','text':'Describe'},{'type':'image_url','image_url':{'url':'data:image/png;base64,YQ=='}}]}]
    url,headers,payload=provider.build_request(protocol,'https://service.example/v1','synthetic-key','model-1',messages,700)
    assert 'Persona 原文' in json.dumps(payload,ensure_ascii=False)
    assert 'YQ==' in json.dumps(payload)
    if protocol=='anthropic':
        assert url.endswith('/messages') and payload['system']=='Persona 原文'
        assert 'temperature' not in payload
        assert payload['messages'][0]['content'][1]['source']['type']=='base64'
    elif protocol=='gemini':
        assert url.endswith('/models/model-1:generateContent') and headers['x-goog-api-key']=='synthetic-key'
    elif protocol=='openai_responses':
        assert url.endswith('/responses') and payload['store'] is False
        assert payload['input'][0]['content'][1]['type']=='input_image'
    else: assert url.endswith('/chat/completions')


class Response:
    def __init__(self,data,status=200): self.data=data;self.status_code=status
    def __enter__(self):return self
    def __exit__(self,*args):pass
    headers = {}

    def iter_content(self, chunk_size):
        if isinstance(self.data, list):
            yield b'\n'.join(self.data)
        else:
            yield json.dumps(self.data).encode()



@pytest.mark.parametrize('route,protocol,data',[
    ('openai','openai_responses',{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'reply'}]}]}),
    ('anthropic','anthropic',{'stop_reason':'end_turn','content':[{'type':'text','text':'reply'}]}),
    ('xai','openai_responses',{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'reply'}]}]}),
    ('custom','openai_chat',{'choices':[{'message':{'content':'reply'},'finish_reason':'stop'}]}),
])
def test_calls_use_selected_credentials_and_no_real_network(isolated,monkeypatch,route,protocol,data):
    cfg=settings.defaults();cfg['active_route']=route;cfg['routes']['custom']['base_url']='https://relay.example/v1'
    cred=settings.PROVIDERS[route]['credential'];cfg['credential_updates']={cred:'synthetic-'+route}
    settings.save_config(cfg,1)
    monkeypatch.setattr(provider,'LOCAL_TEST',False);monkeypatch.setattr(ai_client,'LOCAL_TEST',False)
    monkeypatch.setattr(provider,'_usage',lambda *a:None)
    monkeypatch.setattr(provider,'check_circuit_breaker',lambda *a:None)
    monkeypatch.setattr(provider,'reset_route_success',lambda *a:None)
    calls=[]
    def post(url,**kw):
        calls.append(url)
        assert kw['headers'].get('Authorization',kw['headers'].get('x-api-key')).endswith('synthetic-'+route)
        if route != 'custom':
            assert kw['allow_redirects'] is False
        return Response(data)
    monkeypatch.setattr(provider.requests,'post',post)
    monkeypatch.setattr(provider.public_https, 'open_response', lambda method, url, **kw: post(url, **kw))
    # Both old entry points must delegate to the selected provider.
    for call in (ai_client.call_gemini,ai_client.call_openrouter):
        result=call([{'role':'user','content':'hello'}],model_name='chosen-model',user_id=1)
        assert str(result)=='reply' and result.complete
    assert len(calls)==2


def test_provider_errors_are_not_saved_as_success(monkeypatch):
    monkeypatch.setattr(provider,'LOCAL_TEST',False)
    monkeypatch.setattr(provider,'connection',lambda *a:('openai','openai_responses','https://api.example/v1','synthetic'))
    monkeypatch.setattr(provider.requests,'post',lambda *a,**k:Response({'error':'secret-url-and-key'},401))
    result=provider.call_provider([],model_name='model')
    assert result.error_code=='authentication_failed' and result.status_code==401
    assert 'secret-url-and-key' not in str(result)


@pytest.mark.parametrize('protocol,event,text',[
    ('anthropic',{'type':'content_block_delta','delta':{'type':'text_delta','text':'hello'}},'hello'),
    ('openai_responses',{'type':'response.output_text.delta','delta':'hello'},'hello'),
    ('openai_chat',{'choices':[{'delta':{'content':'hello'}}]},'hello'),
    ('gemini',{'candidates':[{'content':{'parts':[{'text':'hidden','thought':True},{'text':'hello'}]}}]},'hello'),
])
def test_stream_protocols(monkeypatch,protocol,event,text):
    monkeypatch.setattr(provider,'LOCAL_TEST',False)
    monkeypatch.setattr(provider,'connection',lambda *a:('custom',protocol,'https://api.example/v1','synthetic'))
    monkeypatch.setattr(provider.public_https, 'open_response', lambda *a, **k: Response([b'data: '+json.dumps(event).encode(), b'data: [DONE]']))
    assert ''.join(provider.stream_provider([], 'model'))==text


def test_image_empty_never_calls_any_provider(monkeypatch):
    monkeypatch.setattr(provider,'LOCAL_TEST',False)
    monkeypatch.setattr(provider,'connection',lambda *a:pytest.fail('No provider should be called'))
    assert provider.generate_image_bytes('test','') is None


@pytest.mark.parametrize('protocol', ['openai_responses','openai_chat','gemini'])
def test_images_use_same_route_and_decode_bytes(monkeypatch,protocol):
    monkeypatch.setattr(provider,'LOCAL_TEST',False)
    monkeypatch.setattr(provider,'connection',lambda *a:('custom',protocol,'https://api.example/v1','synthetic'))
    image=base64.b64encode(b'synthetic-image').decode()
    data={'data':[{'b64_json':image}]} if protocol!='gemini' else {'candidates':[{'content':{'parts':[{'inlineData':{'data':image}}]}}]}
    monkeypatch.setattr(provider.public_https, 'open_response', lambda *a, **k: Response(data))
    assert provider.generate_image_bytes('test','image-model')==b'synthetic-image'


def test_interrupted_stream_is_not_treated_as_complete(monkeypatch):
    monkeypatch.setattr(provider, 'LOCAL_TEST', False)
    monkeypatch.setattr(provider, 'connection', lambda *a: ('openai', 'openai_responses', 'https://api.example/v1', 'synthetic'))
    event = {'type': 'response.output_text.delta', 'delta': 'partial'}
    monkeypatch.setattr(provider.requests, 'post', lambda *a, **k: Response([b'data: ' + json.dumps(event).encode()]))
    stream = provider.stream_provider([], 'model')
    assert next(stream) == 'partial'
    with pytest.raises(ValueError, match='interrupted'):
        next(stream)


def test_circuit_breaker_blocks_provider_request(monkeypatch):
    monkeypatch.setattr(provider, 'LOCAL_TEST', False)
    monkeypatch.setattr(provider, 'connection', lambda *a: ('openai', 'openai_responses', 'https://api.example/v1', 'synthetic'))
    monkeypatch.setattr(provider, 'check_circuit_breaker', lambda *a: {'type': 'cooldown'})
    monkeypatch.setattr(provider.requests, 'post', lambda *a, **k: pytest.fail('Must not call a cooling-down provider'))
    reply = provider.call_provider([], model_name='model', user_id=1)
    assert reply.error_code == 'circuit_breaker'


def test_official_hosts_cannot_be_replaced_by_config(isolated):
    cfg = settings.defaults()
    for route in ('gemini', 'openai', 'anthropic', 'xai'):
        cfg['routes'][route]['base_url'] = 'https://unexpected.example'
        cfg['routes'][route]['credential'] = 'openrouter'
    settings.save_config(cfg, 1)
    stored = settings.load_config(1)
    for route in ('gemini', 'openai', 'anthropic', 'xai'):
        assert stored['routes'][route]['base_url'] == settings.PROVIDERS[route]['base_url']
        assert stored['routes'][route]['credential'] == route


def test_bad_route_type_fails_validation_without_writing(isolated):
    cfg = settings.defaults(); cfg['active_route'] = {'unexpected': 'object'}
    with pytest.raises(ValueError): settings.save_config(cfg, 1)
    assert not settings.config_path(1).exists()


@pytest.mark.parametrize('field', ['openai_api_key', 'anthropic_api_key', 'xai_api_key'])
def test_new_keys_cannot_be_written_through_profile(monkeypatch, field):
    import app as app_module
    monkeypatch.setattr(app_module, 'get_current_user_id', lambda: 1)
    monkeypatch.setattr(app_module, '_load_user_settings', lambda: {})
    monkeypatch.setattr(app_module, '_save_user_settings', lambda *a: pytest.fail('Plaintext key write'))
    with app_module.app.test_request_context(method='POST', json={field: 'synthetic-secret'}):
        response, status = app_module.user_profile_settings()
    assert status == 400
    assert 'synthetic-secret' not in response.get_data(as_text=True)


def test_existing_imagen_models_keep_native_image_protocol(monkeypatch):
    monkeypatch.setattr(provider, 'LOCAL_TEST', False)
    monkeypatch.setattr(provider, 'connection', lambda *a: ('gemini', 'gemini', 'https://generativelanguage.googleapis.com', 'synthetic'))
    def post(url, **kwargs):
        assert url.endswith('/models/imagen-test:predict')
        assert kwargs['json']['instances'] == [{'prompt': 'test'}]
        return Response({'predictions': [{'bytesBase64Encoded': base64.b64encode(b'image').decode()}]})
    monkeypatch.setattr(provider.requests, 'post', post)
    assert provider.generate_image_bytes('test', 'imagen-test') == b'image'


def test_catalogue_expansion_preserves_saved_choices(isolated):
    # Simulate a saved config from before the expanded catalogue was introduced.
    cfg = {'active_route': 'openai', 'routes': {
        'openai': {'models': {'chat': 'gpt-5.4', 'translation': 'private-translator', 'image': ''}},
        'xai': {'models': {'translation': 'grok-4.7'}},
    }, 'model_options': {'openai': ['private-translator']},
        'model_catalog': {'openai': {'forged-model': {'group': 'forged'}}}}
    settings.save_config(cfg, 1)
    public = settings.public_config(1)
    assert public['routes']['openai']['models']['chat'] == 'gpt-5.4'
    assert public['routes']['openai']['models']['translation'] == 'private-translator'
    assert public['routes']['openai']['models']['image'] == ''
    assert public['routes']['xai']['models']['translation'] == 'grok-4.7'
    assert 'gpt-6-astra' in public['model_options']['openai']
    assert 'private-translator' in public['model_options']['openai']
    assert 'forged-model' not in public['model_catalog']['openai']
    assert 'model_catalog' not in json.loads(settings.config_path(1).read_text())
    for route, catalogue in public['model_catalog'].items():
        for task, model in public['recommendations'][route].items():
            if model:
                capability = 'image_generation' if task == 'image' else 'vision' if task == 'vision' else 'text'
                assert capability in catalogue[model]['capabilities']


def test_catalogue_labels_translated_and_retired_models_not_added():
    from pathlib import Path
    from core.model_catalog import MODEL_CATALOG, PROVIDER_ADVICE
    strings = json.loads((Path(__file__).resolve().parents[1] / 'static/locales/ui.json').read_text())
    labels = [item[field] for models in MODEL_CATALOG.values() for item in models.values() for field in ('group', 'description')]
    labels += [info['text'] for info in PROVIDER_ADVICE.values()]
    for label in labels:
        assert all(strings[label].get(lang) for lang in ('zh-CN', 'en', 'ja'))
    assert 'grok-4-1-fast-reasoning' not in MODEL_CATALOG['xai']
    assert 'claude-opus-4-1-20250805' not in MODEL_CATALOG['anthropic']


@pytest.mark.parametrize('model', ['gpt-6-astra','gpt-6-sol','gpt-6-luna','gpt-5.4-mini','gpt-5.4-nano'])
def test_new_openai_models_use_supported_reasoning_parameters(model):
    _, _, payload = provider.build_request('openai_responses', 'https://api.openai.com/v1', 'synthetic', model, [{'role':'user','content':'hello'}])
    assert payload['reasoning']['effort'] == 'low'
    assert 'temperature' not in payload


def test_gemini_refresh_defaults_and_saved_choices(isolated):
    config = settings.public_config(1)
    models = config['routes']['gemini']['models']
    assert models['chat'] == models['call'] == 'gemini-3.8-flash'
    assert models['summary'] == 'gemini-3-flash-preview'
    assert models['image'] == 'gemini-nano-banana-2.1'
    assert models['image'] in config['image_options']['gemini']
    catalogue = config['model_catalog']['gemini']
    assert catalogue['gemini-4-hydrogen']['group'] == 'Flash · 均衡'
    for model in ('gemini-4-carbon', 'gemini-4-argon'):
        assert catalogue[model]['group'] == 'Pro · 能力优先'
        assert model in config['model_options']['gemini']
    config['routes']['gemini']['models'].update(chat='gemini-2.5-pro', summary='gemini-2.5-flash', image='')
    settings.save_config(config, 1)
    saved = settings.public_config(1)
    assert saved['routes']['gemini']['models']['chat'] == 'gemini-2.5-pro'
    assert saved['routes']['gemini']['models']['summary'] == 'gemini-2.5-flash'
    assert saved['routes']['gemini']['models']['image'] == ''
    assert saved['recommendations']['gemini']['image'] == 'gemini-nano-banana-2.1'
