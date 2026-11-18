"""Offline checks of model prompts, language inheritance and display-only readings."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
class ContentLanguageTests(unittest.TestCase):
    def test_content_language_policies(self):
        with tempfile.TemporaryDirectory(prefix='saki-content-') as temp:
            env=os.environ.copy();env.update(SAKI_LOCAL_TEST='true',SAKI_DATA_ROOT=temp,KUNIGAMI_ENV='test',PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE='1',FLASK_SECRET_KEY='synthetic-language-test-secret-not-a-real-key',SAKI_TEST_EMAIL='test@saki.invalid',SAKI_TEST_PASSWORD='synthetic-language-password')
            code=r'''
import json
from pathlib import Path
from unittest.mock import patch
import app as module
from app import app
from saki_local import seed
from core.runtime import DATA_ROOT
from core.languages import *
from core.context import set_background_user, clear_background_user
from core.furigana import annotate
from core.time_utils import default_character_timezone,ensure_character_time_defaults
from services import memory
seed();c=app.test_client()
assert c.post('/api/login',json={'username':'test@saki.invalid','password':'synthetic-language-password'}).status_code==200
c.post('/api/ui/language',json={'user_language':'zh-CN'})
c.post('/api/user/profile_settings',json={'default_chat_language':'ja'})
folder=Path(DATA_ROOT)/'users'/'1'/'configs'
roles={'a':{'name':'Alpha','language':'en','timezone':'Europe/Paris'},'b':{'name':'Beta','language':'Español'},'c':{'name':'Gamma','language':''}}
groups={'g':{'language':'','members':['a','b']},'h':{'language':'Français','members':['a','b']}}
(folder/'characters.json').write_text(json.dumps(roles));(folder/'groups.json').write_text(json.dumps(groups))
set_background_user(1)
assert character_language('a')=='en' and character_language('c')=='ja'
assert role_output_language('a','g')=='en' and role_output_language('a','h')=='Français'
assert outgoing_language('group',group_id='g')=='ja'
assert outgoing_language('moments',char_id='a')=='ja'
assert group_language('h')=='Français'
assert '"en"' in memory.get_memory_summary_prompt('short','a')
assert '"Español"' in memory.get_memory_summary_prompt('short','b')
captured=[]
def fake(messages,**kwargs):captured.append(messages);return 'translated draft'
with patch.object(module,'get_model_config',return_value=('relay','offline')),patch.object(module,'call_openrouter',side_effect=fake):
 for payload,target in [({'purpose':'read','char_id':'a'},'zh-CN'),({'purpose':'send','char_id':'a'},'en'),({'purpose':'send','scene':'group','group_id':'g'},'ja'),({'purpose':'send','scene':'group','group_id':'h'},'Français'),({'purpose':'send','scene':'moments','char_id':'a'},'ja')]:
  result=c.post('/api/translate',json=dict(text='Original draft / (content)',**payload))
  assert result.status_code==200,result.json
  assert result.json['target_language']==target,result.json
  assert json.dumps(target,ensure_ascii=False) in captured[-1][0]['content']
 # Moments posts and comments read into the UI language; send drafts always
 # use the default conversation language, irrespective of the post author.
 for ui in ['zh-CN','en','ja']:
  c.post('/api/ui/language',json={'user_language':ui})
  for text in ['今日の投稿。','This is a comment.']:
   for purpose,target in [('read',ui),('send','ja')]:
    result=c.post('/api/translate',json={'text':text,'context':'Reference only','purpose':purpose,'scene':'moments','char_id':'a'})
    assert result.status_code==200 and result.json['target_language']==target,result.json
    assert json.loads(captured[-1][1]['content'])['source_text']==text
    assert json.dumps(target,ensure_ascii=False) in captured[-1][0]['content']
 c.post('/api/ui/language',json={'user_language':'zh-CN'})
 tokens=['__SAKI_CAPTION_0__','__SAKI_CAPTION_1__']
 protected='Look'+tokens[0]+'at this'+tokens[1]+'What do you think?'
 for payload,target in [({'char_id':'a','scene':'single'},'en'),({'group_id':'h','scene':'group'},'Français')]:
  result=c.post('/api/translate',json=dict(text=protected,purpose='send',protected_tokens=tokens,**payload))
  assert result.status_code==200 and result.json['target_language']==target,result.json
  user_payload=json.loads(captured[-1][1]['content'])
  assert user_payload['source_text']==protected and user_payload['protected_tokens']==tokens
  assert 'Copy every listed token exactly' in captured[-1][0]['content']
 calls=len(captured)
 for invalid in [None,'not a list',['instruction'],[42],tokens[::-1],tokens*2,['__SAKI_CAPTION_9__']]:
  result=c.post('/api/translate',json=dict(text=protected,purpose='send',protected_tokens=invalid))
  assert result.status_code==400,result.json
 assert len(captured)==calls
# Prompt-only persona generation must use the owner, even when default and UI differ.
r=c.post('/api/generate_persona',json={'char_name':'Alpha','source_ip':'Fiction','char_id':'a','existing_persona':'','prompt_only':True,'web_search':False})
assert r.status_code==200,r.json
assert '必须使用 en' in r.json['prompt']
# The square editor uses the selected local source character's language too.
import blueprints.square as square
from flask import jsonify
with patch.object(square,'_call_llm_for_graph',side_effect=lambda prompt: jsonify(status='success',prompt=prompt)):
 r=c.post('/api/square/ai_complete_graph',json={'name':'Beta','source_character_id':'b'})
 assert r.status_code==200 and '\"Español\"' in r.json['prompt']
# The actual summary call receives the role language, not just the settings helper.
with patch.object(memory,'get_model_config',return_value=('relay','offline')),patch.object(memory,'call_openrouter',side_effect=fake):
 memory.call_ai_to_summarize('日本語の素材です','medium','a',user_id=1)
 assert '"en"' in captured[-1][0]['content']
 memory.call_ai_to_summarize('素材','group_log','system',user_id=1,language=group_language('h'))
 assert '"Français"' in captured[-1][0]['content']
raw='今日は学校へ行きます。中文内容。 `学校です` https://example.com/学校です ID_123'
display=annotate(raw)
assert '<ruby>' in display,display
assert '中文内容。' in display and '`学校です`' in display and 'https://example.com/学校です' in display
import re
assert re.sub(r'<rt>.*?</rt>|</?ruby>','',display)==raw
assert annotate('中文内容')=='中文内容'
assert default_character_timezone('ja')==default_character_timezone('en')
info={'language':'ja','timezone':'Europe/Paris'};ensure_character_time_defaults(info);assert info['timezone']=='Europe/Paris'
c.post('/api/user/profile_settings',json={'furigana_enabled':False});assert annotate(raw)==raw
c.post('/api/user/profile_settings',json={'furigana_enabled':True})
c.post('/api/ui/language',json={'user_language':'ja'});assert annotate(raw)==raw
c.post('/api/ui/language',json={'user_language':'en'})
c.post('/api/user/profile_settings',json={'default_chat_language':''})
assert character_language('c')=='en' and character_language('b')=='Español'
assert json.loads((folder/'characters.json').read_text())==roles
clear_background_user()
print('PASS owner and group policies, five translation targets, actual persona/memory prompts, inherited settings, timezone independence, protected readings and original text')
'''
            result=subprocess.run([sys.executable,'-c',code],env=env,cwd=temp,text=True,capture_output=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
