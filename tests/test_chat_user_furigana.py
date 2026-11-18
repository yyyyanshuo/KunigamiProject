"""User readings must survive every single/group history loading path."""
import ast
import html
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[1]


def load_history(filename, name, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), filename, 'exec'), namespace)
    return namespace[name]


class ChatUserFuriganaTests(unittest.TestCase):
    def test_history_readings_follow_display_settings_and_keep_database_raw(self):
        preferences = {'language': 'en', 'enabled': True}
        localization = SimpleNamespace(
            user_language=lambda user_id=None: preferences['language'],
            read_settings=lambda user_id=None: {'furigana_enabled': preferences['enabled']},
        )
        spec = importlib.util.spec_from_file_location('chat_test_furigana', ROOT / 'core/furigana.py')
        furigana = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'core.localization': localization}):
            spec.loader.exec_module(furigana)

        japanese = '今日は学校へ行きます。😀'
        originals = [
            (1, 'user', japanese), (2, 'assistant', japanese), (3, 'user', japanese),
            (4, 'user', '中文内容。'), (5, 'user', '（システム：今日は学校へ行きます。）'),
        ]
        with tempfile.TemporaryDirectory(prefix='saki-chat-furigana-') as folder:
            db_path = str(Path(folder) / 'chat.db')
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)')
                db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?)', [
                    (*row, '2026-09-29 12:00:00') for row in originals
                ])
            namespace = {
                'os': os, 'sqlite3': sqlite3, 'request': request, 'jsonify': jsonify,
                'get_current_user_id': lambda: 1,
                'get_paths': lambda *args, **kwargs: (db_path, folder),
                'get_group_dir': lambda _: folder,
                '_add_furigana_to_japanese': furigana.annotate,
            }
            app_helpers = SimpleNamespace(init_char_db=lambda _: None,
                _sticker_content_from_ai=lambda text: text, get_ai_language=lambda *args: 'en')
            for filename, name in [('blueprints/chat.py', 'get_history'), ('blueprints/group.py', 'get_group_history')]:
                history = load_history(filename, name, dict(namespace))
                for language, enabled in [('en', True), ('zh-CN', True), ('ja', True), ('en', False)]:
                    preferences.update(language=language, enabled=enabled)
                    for query in ['', '?after_id=1', '?before_id=6', '?target_id=3']:
                        with self.subTest(route=name, language=language, enabled=enabled, query=query):
                            with patch.dict(sys.modules, {'app': app_helpers}), Flask(__name__).test_request_context('/history' + query):
                                messages = history('demo').get_json()['messages']
                            self.assertTrue(any(message['role'] == 'user' and message['content'].startswith(('今日', '<ruby>')) for message in messages))
                            for message in messages:
                                original = originals[message['id'] - 1][2]
                                self.assertEqual(message['raw_content'], original)
                                should_annotate = original == japanese and language != 'ja' and enabled
                                self.assertEqual('<ruby>' in message['content'], should_annotate)
                                plain = re.sub(r'<rt>.*?</rt>|</?ruby>', '', message['content'])
                                self.assertEqual(html.unescape(plain), original)
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute('SELECT id, role, content FROM messages ORDER BY id').fetchall(), originals)


if __name__ == '__main__':
    unittest.main()
