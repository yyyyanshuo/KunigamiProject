"""Author supplied names for literal, Unicode-normalized square search."""
import json
import re
import unicodedata

MAX_ALIASES = 10
MAX_ALIAS_LENGTH = 80


def search_fold(value):
    return unicodedata.normalize('NFKC', str(value or '')).casefold().strip()


def parse_aliases(value):
    if value is None or value == '': return []
    if isinstance(value,str):
        if len(value)>10000: raise ValueError('别名内容过长')
        value=re.split(r'[\n\r,，]+',value)
    if not isinstance(value,list): raise ValueError('别名格式无效')
    result=[];seen=set()
    for item in value:
        if not isinstance(item,str): raise ValueError('别名格式无效')
        alias=' '.join(item.split())
        if not alias: continue
        if len(alias)>MAX_ALIAS_LENGTH: raise ValueError('每个别名最多80个字符')
        key=search_fold(alias)
        if key in seen: continue
        seen.add(key);result.append(alias)
        if len(result)>MAX_ALIASES: raise ValueError('最多填写10个别名')
    return result


def stored_aliases(value):
    try:
        parsed=json.loads(value) if isinstance(value,str) else value
        return parse_aliases(parsed)
    except (ValueError,TypeError): return []
