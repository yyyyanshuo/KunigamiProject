"""Checks are pure/static or use the existing isolated subprocess fixture."""
import ast
import os
from pathlib import Path
import re
import tempfile
import unittest

from core.environment_check import configuration_errors, SERVICE_VARIABLES
from test_saki_local import child

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentConfigurationTests(unittest.TestCase):
    def test_template_covers_runtime_and_dynamic_names(self):
        template = (ROOT / '.env.example').read_text()
        names = set(re.findall(r'^(?:# )?([A-Z][A-Z0-9_]*)=', template, re.M))
        reads = set()
        files = [*ROOT.glob('*.py'), *ROOT.joinpath('core').glob('*.py'),
                 *ROOT.joinpath('services').glob('*.py'), *ROOT.joinpath('blueprints').glob('*.py')]
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if isinstance(node, ast.Call) and ast.unparse(node.func) in {'os.getenv', 'os.environ.get', '_positive_timeout_from_env'}:
                    if node.args and isinstance(node.args[0], ast.Constant):
                        reads.add(node.args[0].value)
        for keys in SERVICE_VARIABLES.values():
            reads.update(keys)
        self.assertFalse(reads - names, reads - names)
        self.assertIn('KUNIGAMI_ASSESSMENT_PASSWORD', names)
        self.assertNotIn('USE_OPENROUTER', names)

    def test_missing_and_malformed_values_are_redacted(self):
        base = {'SAKI_LOCAL_TEST':'true','SAKI_DATA_ROOT':'.local-data/test'}
        self.assertTrue(any('FLASK_SECRET_KEY' in msg for msg in configuration_errors(base)))
        secret = 'PRIVATE-SENTINEL-DO-NOT-DISPLAY'
        base.update(FLASK_SECRET_KEY='x'*40, MAIL_PORT=secret,
                    CREDENTIAL_ENCRYPTION_KEYS=secret, GEMINI_READ_TIMEOUT_SECONDS='nan')
        messages = '\n'.join(configuration_errors(base))
        for key in ('MAIL_PORT','CREDENTIAL_ENCRYPTION_KEYS','GEMINI_READ_TIMEOUT_SECONDS'):
            self.assertIn(key, messages)
        self.assertNotIn(secret, messages)
        base.update(KUNIGAMI_ENV='production')
        self.assertTrue(any('KUNIGAMI_ENV' in msg for msg in configuration_errors(base)))

    def test_admin_list_requires_explicit_positive_ids(self):
        base = {'SAKI_LOCAL_TEST':'false', 'KUNIGAMI_ENV':'development'}
        for valid in ('', '1', '7, 12'):
            self.assertFalse(configuration_errors({**base, 'SAKI_ADMIN_USER_IDS':valid}))
        for invalid in ('0', '-1', '1,', '1.5', 'everyone', '1;7'):
            self.assertTrue(any('SAKI_ADMIN_USER_IDS' in msg for msg in configuration_errors({**base, 'SAKI_ADMIN_USER_IDS':invalid})))

    def test_missing_key_with_encrypted_data_never_regenerates(self):
        with tempfile.TemporaryDirectory(prefix='saki-config-') as tmp:
            record = Path(tmp) / 'users/1/configs/credentials.json'
            record.parent.mkdir(parents=True)
            record.write_text('{"synthetic":"opaque-test-ciphertext"}')
            before = record.read_bytes()
            errors = configuration_errors({'SAKI_LOCAL_TEST':'true','SAKI_DATA_ROOT':tmp,'FLASK_SECRET_KEY':'x'*40})
            self.assertTrue(any('CREDENTIAL_ENCRYPTION_KEYS' in msg for msg in errors))
            self.assertEqual(before, record.read_bytes())

    def test_disabled_features_explain_without_external_calls(self):
        with tempfile.TemporaryDirectory(prefix='saki-features-') as tmp:
            result = child(tmp, '''
from app import app
client = app.test_client()
for path in ('/api/forgot_password/send_code','/api/subscribe','/api/voice-messages','/api/music/search','/api/forums/generate-stream'):
    response = client.post(path, json={})
    assert response.status_code == 503, (path, response.status_code)
    assert response.json['code'] == 'local_feature_unavailable'
    assert 'configuration_variables' in response.json
assert client.get('/api/local/config-status').status_code == 401
''')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_model_keys_do_not_attempt_requests(self):
        with tempfile.TemporaryDirectory(prefix='saki-empty-models-') as tmp:
            result = child(tmp, """
import requests
from services.ai_client import call_openrouter, call_gemini

def forbidden(*args, **kwargs):
    raise AssertionError('External request attempted without configuration')
requests.post = forbidden
for call in (call_openrouter, call_gemini):
    response = call([])
    assert response.error_code == 'service_not_configured'
    assert response.status_code == 503
""", SAKI_LOCAL_TEST='false', GEMINI_API_KEY='', OPENROUTER_KEY='',
                OPENROUTER_BASE_URL='', OPENROUTER_BASE_URL_OLD='')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_implicit_legacy_service_hosts(self):
        for name in ('app.py', 'core/config.py', 'blueprints/auth.py'):
            source = (ROOT / name).read_text()
            for host in ('https://oa.api2d.net/v1', 'https://vg.v1api.cc/v1', '"smtp.qq.com"'):
                self.assertNotIn(host, source)


if __name__ == '__main__':
    unittest.main()
