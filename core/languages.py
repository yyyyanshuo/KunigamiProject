"""Separate language policies for model output, owned data and user translation."""
import json
from pathlib import Path
from core.localization import read_settings, user_language
from core.runtime import DATA_ROOT

ALIASES={'zh-cn':'zh','zh-hans':'zh','chinese':'zh','中文':'zh','日本語':'ja','日语':'ja','japanese':'ja','english':'en','英语':'en'}


def normalize_language(value):
    text=str(value or '').strip()
    if len(text)>100 or any(ord(c)<32 for c in text):
        raise ValueError('Invalid language value')
    return ALIASES.get(text.lower(),text.lower() if text.lower() in ('en','ja','zh') else text)


def _user_id(user_id):
    if user_id is None:
        from core.context import get_current_user_id
        return get_current_user_id()
    return user_id


def _config(filename,user_id=None):
    uid=_user_id(user_id)
    folder=Path(DATA_ROOT)/'users'/str(uid)/'configs' if uid is not None else Path(DATA_ROOT)/'configs'
    try:
        data=json.loads((folder/filename).read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError): return {}


def default_chat_language(user_id=None):
    settings=read_settings(_user_id(user_id))
    # Old ai_language is a legacy conversation preference, never a UI preference.
    value=settings.get('default_chat_language') if 'default_chat_language' in settings else settings.get('ai_language')
    return normalize_language(value) or normalize_language(user_language(_user_id(user_id)))


def character_language(char_id=None,user_id=None):
    info=_config('characters.json',user_id).get(str(char_id),{})
    return normalize_language(info.get('language')) or default_chat_language(user_id)


def group_language(group_id=None,user_id=None):
    info=_config('groups.json',user_id).get(str(group_id),{})
    return normalize_language(info.get('language')) or default_chat_language(user_id)


def role_output_language(char_id=None,group_id=None,user_id=None):
    if group_id:
        value=_config('groups.json',user_id).get(str(group_id),{}).get('language')
        if normalize_language(value): return normalize_language(value)
    return character_language(char_id,user_id)


def outgoing_language(scene='single',char_id=None,group_id=None,user_id=None):
    if scene=='group': return group_language(group_id,user_id)
    if scene=='moments': return default_chat_language(user_id)
    if scene=='single': return character_language(char_id,user_id)
    raise ValueError('Invalid translation scene')


def language_instruction(language):
    return ('\n\n[Output language]\nWrite all newly generated natural-language content in '
            +json.dumps(normalize_language(language),ensure_ascii=False)
            +'. Keep JSON keys, IDs, names and protected verbatim blocks unchanged. '
             'The language of source material does not override this output language.\n')
