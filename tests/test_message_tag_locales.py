import sqlite3
import pytest
from core.message_tags import ALIASES, canonicalize_message_tags
from core.system_messages import is_system_prompt_message
from services.image_tags import build_image_tag, split_message_bubbles
from blueprints.chat import apply_transfer_action, apply_assistant_transfer_decision, parse_transfer_tag

@pytest.mark.parametrize('old,new', ALIASES.items())
def test_aliases_produce_canonical_tags(old,new):
    assert canonicalize_message_tags(f'[{old}]') == f'[{new}]'
    assert canonicalize_message_tags(f'[{old}:123円|description]') == f'[{new}:123円|description]'


def test_opaque_image_fields_and_mixed_history():
    old='[图片](a/b.png)(description mentions [表情] and f(x/y))'
    new=canonicalize_message_tags(old)
    assert new=='[IMAGE](a/b.png)(description mentions [表情] and f(x/y))'
    assert split_message_bubbles(old+' / '+new)==[old,new]
    assert build_image_tag('a.png','日本語 description').startswith('[IMAGE]')

@pytest.mark.parametrize('source',['[转账:100円|旧记录]','[TRANSFER:100円|New]'])
@pytest.mark.parametrize('decision',['[领取转账:100円]','[TRANSFER_ACCEPT:100円]'])
def test_canonical_and_legacy_transfer_interoperate(source,decision):
    db=sqlite3.connect(':memory:');db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,role TEXT,content TEXT)')
    db.execute('INSERT INTO messages VALUES(1,?,?)',('assistant',source))
    db.execute('INSERT INTO messages VALUES(2,?,?)',('assistant','[转账:200円|untouched]'))
    result,event=apply_transfer_action(db.cursor(),{'transfer_action':'accept','transfer_source_id':1},decision)
    assert result=='[TRANSFER_ACCEPT:100円]'
    assert event['content'].startswith('[TRANSFER_ACCEPTED:100円|')
    assert db.execute('SELECT content FROM messages WHERE id=2').fetchone()[0]=='[转账:200円|untouched]'

@pytest.mark.parametrize('text',['（系统提示：测试）','(System: test)','(System Hint: test)','(system notice: test)','（システム：テスト）','（システム通知：テスト）'])
def test_three_language_notices(text):
    assert is_system_prompt_message(text)

@pytest.mark.parametrize('text',['(System: )','（システム： ）','Someone said (System: x)','系统提示：普通正文','[系统: x]'])
def test_regular_text_is_not_a_system_hint(text):
    assert not is_system_prompt_message(text)
