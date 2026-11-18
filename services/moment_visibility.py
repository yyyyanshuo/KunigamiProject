"""Per-post Moments visibility; names are resolved once and stored as character IDs."""
import re

BLOCK_LINE = re.compile(r'^[ \t]*~~([^~\r\n]+)~~[ \t]*$')


def blocked_ids(post):
    value = post.get('blocked_character_ids', [])
    return set(item for item in value if isinstance(item, str)) if isinstance(value, list) else set()


def normalize_blocked_ids(value, characters, author_id='user'):
    if not isinstance(value, list) or any(not isinstance(cid, str) for cid in value):
        raise ValueError('屏蔽名单格式无效')
    if any(cid not in characters or cid in ('user', author_id) for cid in value):
        raise ValueError('屏蔽名单包含无效角色')
    return sorted(set(value))


def extract_block_lines(content, characters, author_id):
    aliases = {}
    for cid, info in characters.items():
        for name in (cid, info.get('name'), info.get('remark')):
            if isinstance(name, str) and name.strip():
                aliases.setdefault(name.strip().casefold(), set()).add(cid)
    visible, blocked = [], set()
    for line in content.splitlines():
        match = BLOCK_LINE.fullmatch(line)
        if not match:
            visible.append(line)
            continue
        name = match[1].strip()
        matches = {name} if name in characters else aliases.get(name.casefold(), set())
        if len(matches) != 1:
            raise ValueError('屏蔽角色无法唯一匹配，请使用角色 ID')
        cid = next(iter(matches))
        if cid not in ('user', author_id):
            blocked.add(cid)
    return '\n'.join(visible).strip(), sorted(blocked)


def remove_blocked_reactions(post):
    blocked = blocked_ids(post)
    if not blocked:
        return post
    if 'likers' in post:
        post['likers'] = [row for row in post['likers'] if row.get('liker_id') not in blocked]
    if 'liker_ids' in post:
        post['liker_ids'] = [cid for cid in post['liker_ids'] if cid not in blocked]
    if 'comments' in post:
        post['comments'] = [row for row in post['comments']
                            if row.get('commenter_id') not in blocked and row.get('reply_to') not in blocked]
    return post


def visibility_prompt(characters, author_id):
    candidates = [{'id': cid, 'name': info.get('name') or cid, 'remark': info.get('remark') or ''}
                  for cid, info in characters.items() if cid not in ('user', author_id)]
    if not candidates:
        return ''
    import json
    return ('\n\nPost visibility: You MAY hide this post from specific other characters if it fits your intentions. '
            'Default: everyone can see it. To hide it, append ~~character ID~~ on a line by itself, one character per line. '
            'These control lines are removed from the visible body. Use only IDs from the list below; never invent an ID '
            'or block yourself or the user. Blocking takes precedence over @mentions. Do not explain these control lines.\n'
            + json.dumps(candidates, ensure_ascii=False))
