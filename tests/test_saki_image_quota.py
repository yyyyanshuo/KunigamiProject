import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from core import image_quota as quota
from core import model_settings as settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, 'DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(settings, 'DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(quota, '_today', lambda: '2026-09-23')
    return tmp_path


def set_limit(uid, limit):
    path = settings.config_path(uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'image_daily_limit': limit}))


def test_default_ten_and_accounts_are_independent(isolated):
    for uid in (1, 7):  # User 1 has no special allowance.
        for _ in range(10):
            token = quota.reserve_image(uid)
            assert token
            quota.finish_image(token, success=True)
        assert quota.reserve_image(uid) is None


def test_concurrent_workers_cannot_exceed_limit(isolated):
    set_limit(7, 3)
    with ThreadPoolExecutor(max_workers=10) as pool:
        tokens = list(pool.map(quota.reserve_image, [7] * 25))
    tokens = [token for token in tokens if token]
    assert len(tokens) == 3
    assert len(set(tokens)) == 3
    quota.finish_image(tokens[0], success=False)
    replacement = quota.reserve_image(7)
    assert replacement
    assert quota.reserve_image(7) is None
    for token in [*tokens[1:], replacement]:
        quota.finish_image(token, success=True)
        quota.finish_image(token, success=True)  # Idempotent finalization.
    with quota._connect() as conn:
        assert conn.execute('SELECT used FROM image_days WHERE user_id=7').fetchone()[0] == 3
    assert quota.reserve_image(7) is None


def test_legacy_usage_migrates_once_and_limit_changes_keep_usage(isolated):
    legacy = isolated / 'users/7/logs/daily_gen_status.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({'date': '2026-09-23', 'count': 1}))
    set_limit(7, 2)
    token = quota.reserve_image(7)
    assert token
    quota.finish_image(token, success=True)
    assert quota.reserve_image(7) is None
    set_limit(7, 1)
    assert quota.reserve_image(7) is None
    set_limit(7, 3)
    assert quota.reserve_image(7)
    assert quota.reserve_image(7) is None
    assert json.loads(legacy.read_text())['count'] == 1


def test_midnight_keeps_old_requests_on_original_day(isolated, monkeypatch):
    set_limit(7, 1)
    old = quota.reserve_image(7)
    monkeypatch.setattr(quota, '_today', lambda: '2026-09-24')
    new = quota.reserve_image(7)
    assert new
    quota.finish_image(old, success=True)
    quota.finish_image(new, success=False)
    assert quota.reserve_image(7)


@pytest.mark.parametrize('limit', [0, -1, 1.5, True, '10', None, 2147483648])
def test_invalid_settings_rejected_before_save(isolated, limit):
    cfg = settings.defaults()
    cfg['image_daily_limit'] = limit
    with pytest.raises(ValueError, match='每日生图上限'):
        settings.validate_config(cfg)
    assert not settings.config_path(7).exists()


@pytest.mark.parametrize('outcome', ['success', 'empty', 'exception', 'watermark', 'upload'])
def test_real_image_entry_point_finalizes_quota(isolated, monkeypatch, outcome):
    import io
    import app as app_module
    from PIL import Image
    from services import model_provider
    monkeypatch.setattr(app_module, 'USERS_ROOT', str(isolated / 'users'))
    monkeypatch.setattr(app_module, 'get_model_config', lambda *a, **kw: ('openai', 'image-test'))
    set_limit(7, 1)
    buffer = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buffer, 'PNG')
    def generate(*args):
        if outcome == 'exception':
            raise RuntimeError('Synthetic failure')
        return None if outcome == 'empty' else buffer.getvalue()
    monkeypatch.setattr(model_provider, 'generate_image_bytes', generate)
    monkeypatch.setattr(app_module, 'apply_ai_watermark', lambda *a: outcome != 'watermark')
    monkeypatch.setattr(app_module, 'upload_to_cos', lambda *a: None if outcome == 'upload' else 'https://images.invalid/test.png')
    result = app_module._call_route_image_gen('synthetic prompt', 'test-character', 7)
    assert bool(result) == (outcome == 'success')
    assert bool(quota.reserve_image(7)) == (outcome != 'success')
