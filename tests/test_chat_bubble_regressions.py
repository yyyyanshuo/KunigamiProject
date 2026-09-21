"""Preserve special characters while annotating and loading chat messages."""
import ast
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_function(relative_path, name, namespace):
    tree = ast.parse((ROOT / relative_path).read_text(encoding='utf-8-sig'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), relative_path, 'exec'), namespace)
    return namespace[name]


@pytest.mark.parametrize('implementation', ['core/utils.py', 'app.py'])
@pytest.mark.parametrize('text', [
    '今日は楽しいヽ(｡>﹏< <｡)ﾉ/明日も遊ぶ',
    '今日\t楽しい/明日も遊ぶ',
    '今日は楽しい🥹/明日も遊ぶ',
    '今日\n明日',
    '今日/[图片](/static/a.png)(猫/犬)/明日',
])
def test_annotation_preserves_original_and_survives_symbols(implementation, text):
    from core.utils import kks, EMOJI_SPLIT_RE
    from services.image_tags import protect_image_tags
    annotate = load_function(implementation, '_add_furigana_to_japanese', {
        're': re, 'kks': kks, 'EMOJI_SPLIT_RE': EMOJI_SPLIT_RE,
        'protect_image_tags': protect_image_tags,
    })
    result = annotate(text)
    assert '<ruby>今日<rt>' in result
    assert '<ruby>明日<rt>' in result
    restored = re.sub(r'<rt>.*?</rt>', '', result)
    restored = restored.replace('<ruby>', '').replace('</ruby>', '')
    assert restored == text


@pytest.mark.parametrize('language', ['ja', 'zh'])
@pytest.mark.parametrize('query', ['', '?after_id=1', '?before_id=3', '?target_id=1'])
def test_group_history_annotates_user_and_keeps_database_raw(tmp_path, monkeypatch, language, query):
    import os
    from flask import Flask, request, jsonify
    from core.utils import _add_furigana_to_japanese
    db_path = tmp_path / 'chat.db'
    original = '今日\t楽しい'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)')
        db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?)', [
            (1, 'user', original, '2026-09-06 12:00:00'),
            (2, 'member', original, '2026-09-06 12:00:01'),
        ])
    targets = []
    def get_language(target_id=None, group_id=None):
        targets.append((target_id, group_id))
        return language
    monkeypatch.setitem(sys.modules, 'app', SimpleNamespace(
        _sticker_content_from_ai=lambda text: text, get_ai_language=get_language))
    history = load_function('blueprints/group.py', 'get_group_history', {
        'os': os, 'sqlite3': sqlite3, 'request': request, 'jsonify': jsonify,
        'get_group_dir': lambda _: str(tmp_path),
        '_add_furigana_to_japanese': _add_furigana_to_japanese,
    })
    with Flask(__name__).test_request_context('/history' + query):
        messages = history('group').get_json()['messages']
    assert len(messages) == (1 if query == '?after_id=1' else 2)
    assert all(('<ruby>' in message['content']) == (language == 'ja') for message in messages)
    assert targets == ([('member', 'group')] if query == '?after_id=1' else [(None, 'group'), ('member', 'group')])
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT content FROM messages').fetchall() == [(original,), (original,)]
