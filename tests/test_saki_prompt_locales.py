import pytest
from flask import Flask
import app as app_module
import blueprints.chat as chat
from core.generation_prompts import relationship_prompt

@pytest.mark.parametrize('locale,opening,user_label',[
    ('zh-CN','你是一位资深的角色设定师','角色名：'),
    ('en','You are an expert character setting writer.','Character name:'),
    ('ja','あなたは熟練したキャラクター設定作家です。','キャラクター名：'),
])
def test_copied_persona_language_and_protected_source(monkeypatch,locale,opening,user_label):
    monkeypatch.setattr(app_module,'get_current_user_id',lambda:1)
    monkeypatch.setattr(app_module,'user_language',lambda *args:locale)
    monkeypatch.setattr(app_module,'character_language',lambda *args,**kwargs:'Español')
    def forbidden(*args,**kwargs):raise AssertionError('Copy must not call a model')
    monkeypatch.setattr(app_module,'call_openrouter',forbidden);monkeypatch.setattr(app_module,'call_gemini',forbidden)
    locked='[[LOCK]]\n原始内容 English {target}\n[[/LOCK]]'
    with app_module.app.test_request_context(json={'char_name':'Test name','source_ip':'Test series','prompt_only':True,'web_search':False,'existing_persona':locked}):
        result=app_module.generate_persona().json
    assert opening in result['prompt'];assert user_label in result['prompt']
    assert 'Español' in result['prompt'] and locked in result['prompt']
    assert '1800' in result['prompt'] or '1,800' in result['prompt']
    assert 'Language: English' not in result['prompt']
    assert '言語：日本語' not in result['prompt']
    assert '语言：中文' not in result['prompt']

@pytest.mark.parametrize('locale,start',[('zh-CN','根据角色设定'),('en','Using the character settings'),('ja','キャラクター設定に基づき')])
def test_relationship_copy_forward_reverse_languages(locale,start):
    records=[{'id':'b','name':'Beta','output_language':'en'},{'id':'c','name':'Gamma','output_language':'Français'}]
    raw={'原名':{'role':'旧角色 {name}','score':3,'description':'原始描述 English'}}
    normal=relationship_prompt('Alpha',records,raw,'normal',locale,'Español')
    reverse=relationship_prompt('Alpha',records,raw,'reverse',locale,'Español')
    assert normal.startswith(start) and 'Español' in normal
    assert 'output_language' in reverse and 'Français' in reverse
    assert '旧角色 {name}' in normal and '原始描述 English' in reverse
