"""Local display-only Japanese readings; stored source is never rewritten."""
import html
import re
from functools import lru_cache
from core.localization import read_settings, user_language

KANJI = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
KANA = re.compile(r'[\u3041-\u3096\u30a1-\u30fa]')
# Protect code, URLs, existing ruby, markup, functional tags and identifiers.
PROTECTED = re.compile(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|https?://[^\s<>]+|<ruby\b[\s\S]*?</ruby>|<[^>]*>|\[[^\]\n]*\](?:[^\s/]+)?|[A-Za-z_][A-Za-z0-9_./:@-]*|\S+@\S+)', re.I)


def enabled(user_id=None):
    return user_language(user_id) != 'ja' and read_settings(user_id).get('furigana_enabled', True) is not False


@lru_cache(maxsize=1)
def _converter():
    import pykakasi
    return pykakasi.kakasi()


@lru_cache(maxsize=2048)
def _annotate_chunk(text):
    # Kana is evidence of Japanese; isolated Han-only text is ambiguous.
    if not KANA.search(text) or not KANJI.search(text):
        return text
    output=[]
    for chunk in re.split(r'([\u3400-\u4dbf\u4e00-\u9fff\u3041-\u3096\u30a1-\u30faー々]+)',text):
        if not KANJI.search(chunk) or not KANA.search(chunk):
            output.append(chunk); continue
        converted=_converter().convert(chunk)
        if ''.join(item['orig'] for item in converted)!=chunk:
            output.append(chunk); continue
        for item in converted:
            original,reading=item['orig'],item['hira']
            if KANJI.search(original) and not KANJI.search(reading) and reading!=original:
                output.append('<ruby>'+html.escape(original)+'<rt>'+html.escape(reading)+'</rt></ruby>')
            else: output.append(original)
    return ''.join(output)


def annotate(text,user_id=None):
    if not text or not enabled(user_id): return text
    from core.system_messages import is_system_prompt_message
    from services.image_tags import protect_image_tags
    if is_system_prompt_message(text): return text
    source,tags=protect_image_tags(text,'__SAKI_IMAGE_')
    parts=PROTECTED.split(source)
    for i in range(0,len(parts),2):
        # Sentence boundaries prevent nearby Chinese sentences from being treated as Japanese.
        parts[i]=''.join(_annotate_chunk(piece) for piece in re.split(r'([。！？!?\n\r]+)',parts[i]))
    result=''.join(parts)
    for i,tag in enumerate(tags): result=result.replace(f'__SAKI_IMAGE_{i}__',tag)
    return result
