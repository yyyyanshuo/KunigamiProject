"""Exercise the real metadata save handler without starting background services."""
import ast
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import time_utils


class ManualSleepTimezoneTests(unittest.TestCase):
    def save(self, original, payload):
        tree = ast.parse((ROOT / 'blueprints/chat.py').read_text(encoding='utf-8-sig'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_update_char_meta_locked')
        namespace = dict(vars(time_utils))
        namespace.update(
            json=json, request=SimpleNamespace(json=payload), jsonify=lambda data: data,
            _load_user_settings=lambda: {'timezone': 'America/New_York'},
            atomic_write_json=lambda path, data: Path(path).write_text(json.dumps(data), encoding='utf-8'),
        )
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'blueprints/chat.py', 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'characters.json'
            path.write_text(json.dumps({'rin': original}), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                response = namespace[function.name]('rin', str(path))
            return response, json.loads(path.read_text())['rin']

    def original(self):
        return {
            'timezone': 'Asia/Tokyo', 'timezone_source': 'manual',
            'ds_start': '23:00', 'ds_end': '07:00',
            'ds_time_basis': 'user', 'ds_set_by': 'legacy',
            'ds_timezone_at_set': 'America/New_York',
            'sleep_last_event_key': 'old-event', 'sleep_manual_override': True,
        }

    def test_manual_edits_use_character_clock_and_preserve_entered_values(self):
        for payload in [
            {'ds_start': '22:30', 'ds_end': '08:00'},
            {'ds_start': '22:30'}, {'ds_end': '08:00'},
            {'ds_start': '23:00', 'ds_end': '07:00'},
            {'timezone': 'Europe/Paris', 'ds_start': '22:30'},
        ]:
            with self.subTest(payload=payload):
                response, saved = self.save(self.original(), payload)
                zone = payload.get('timezone', 'Asia/Tokyo')
                self.assertEqual(response['status'], 'success')
                self.assertEqual(saved['ds_time_basis'], 'character')
                self.assertEqual(saved['ds_set_by'], 'user')
                self.assertEqual(saved['ds_timezone_at_set'], zone)
                self.assertEqual(saved['ds_start'], payload.get('ds_start', '23:00'))
                self.assertEqual(saved['ds_end'], payload.get('ds_end', '07:00'))
                self.assertIsNone(saved['sleep_last_event_key'])
                self.assertFalse(saved['sleep_manual_override'])
                self.assertEqual(response['time_preview']['source_timezone'], zone)
                event = time_utils.sleep_event_datetime(saved, {'timezone': 'America/New_York'}, date(2026, 9, 10), 'sleep')
                self.assertEqual(event.tzinfo.key, zone)
                self.assertEqual(event.strftime('%H:%M'), saved['ds_start'])

    def test_invalid_times_do_not_write_config(self):
        for payload in [{'ds_start': '25:00'}, {'ds_end': '23:00'}]:
            response, saved = self.save(self.original(), payload)
            self.assertEqual(response[1], 400)
            self.assertEqual(saved, self.original())

    def test_unrelated_edit_preserves_existing_schedule(self):
        response, saved = self.save(self.original(), {'remark': 'Rin'})
        self.assertEqual(response['status'], 'success')
        for key in ('ds_time_basis', 'ds_start', 'ds_end', 'ds_timezone_at_set', 'sleep_last_event_key'):
            self.assertEqual(saved[key], self.original()[key])


if __name__ == '__main__':
    unittest.main()
