"""Canonical protocol names. Legacy aliases are read without rewriting history."""
import re

ALIASES = {'图片': 'IMAGE', '画像': 'IMAGE', 'Image': 'IMAGE', '表情': 'STICKER', '絵文字': 'STICKER', 'Sticker': 'STICKER', '转账': 'TRANSFER', '领取转账': 'TRANSFER_ACCEPT', '退回转账': 'TRANSFER_RETURN', '已领取转账': 'TRANSFER_ACCEPTED', '已退回转账': 'TRANSFER_RETURNED', '视频通话': 'VIDEO_CALL', 'ビデオ通話': 'VIDEO_CALL', 'Video Call': 'VIDEO_CALL', '文件': 'FILE', 'ファイル': 'FILE', 'File': 'FILE', '音乐': 'MUSIC', '音楽': 'MUSIC', 'Music': 'MUSIC', '链接': 'LINK', 'リンク': 'LINK', 'Link': 'LINK', '视频': 'VIDEO', 'ビデオ': 'VIDEO', 'Video': 'VIDEO', '已读不回': 'READ_RECEIPT', '既読スルー': 'READ_RECEIPT', 'Read Receipt': 'READ_RECEIPT', '未回复': 'UNANSWERED', '未返信': 'UNANSWERED', 'Unanswered': 'UNANSWERED'}
_PATTERN = re.compile(r"\[(" + "|".join(re.escape(k) for k in sorted(ALIASES, key=len, reverse=True)) + r")(?=\]|[:：（(])")

def canonicalize_message_tags(content):
    if not isinstance(content, str):
        return content
    # Complete image fields are opaque, so tags inside a description are data.
    from services.image_tags import iter_image_tags
    parts, cursor = [], 0
    for tag in iter_image_tags(content):
        parts.append(_PATTERN.sub(lambda m: "[" + ALIASES[m[1]], content[cursor:tag.start]))
        parts.append("[IMAGE]" + tag.raw[tag.raw.index("]") + 1:])
        cursor = tag.end
    parts.append(_PATTERN.sub(lambda m: "[" + ALIASES[m[1]], content[cursor:]))
    return "".join(parts)
