import json
import multiprocessing
import os
import shutil

import pytest
from cryptography.fernet import Fernet
from core import credentials, model_settings


def test_worker_death_preserves_route_key_pair(tmp_path, monkeypatch):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux worker test')
    monkeypatch.setattr(model_settings, 'DATA_ROOT', str(tmp_path))
    store = credentials.CredentialStore(tmp_path/'users', Fernet.generate_key().decode())
    monkeypatch.setattr(credentials, 'default_store', store)
    original = model_settings.defaults()
    original['credential_updates'] = {'gemini': 'synthetic-original'}
    model_settings.save_config(original, 1)
    def interrupt():
        write = store._atomic_write_json
        def stop(path, data):
            if path == model_settings.config_path(1):
                os._exit(31)
            write(path, data)
        store._atomic_write_json = stop
        new = model_settings.defaults()
        new.update(active_route='openai', credential_updates={'gemini': 'synthetic-changed', 'openai': 'synthetic-new'})
        model_settings.save_config(new, 1)
    child = multiprocessing.get_context('fork').Process(target=interrupt)
    child.start()
    child.join(8)
    assert child.exitcode == 31
    assert model_settings.load_config(1)['active_route'] == 'gemini'
    assert store.get(1, 'gemini') == 'synthetic-original'
    assert store.get(1, 'openai') == ''


def test_restore_separate_master_key_and_rotate(tmp_path):
    old, new = (Fernet.generate_key().decode() for _ in range(2))
    store = credentials.CredentialStore(tmp_path/'original', old)
    store.set(1,'gemini','synthetic-restore')
    shutil.copytree(store.users_root,tmp_path/'restored')
    restored = credentials.CredentialStore(tmp_path/'restored', new)
    with pytest.raises(credentials.CredentialDecryptError):
        restored.get(1,'gemini')
    restored = credentials.CredentialStore(tmp_path/'restored', new+','+old)
    assert restored.get(1,'gemini') == 'synthetic-restore'
    restored.set(1,'gemini','synthetic-rotated')
    assert credentials.CredentialStore(tmp_path/'restored',new).get(1,'gemini') == 'synthetic-rotated'


def test_live_store_rejects_plaintext_even_if_requested(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials.default_store, 'users_root', tmp_path)
    path = credentials.default_store.legacy_settings_path(1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'gemini_api_key':'synthetic-only'}))
    assert credentials.get_user_credential(1,'gemini',allow_legacy=True) == ''
    assert not credentials.default_store.status(1,'gemini')['configured']


def test_changed_key_invalidates_open_settings_page(tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings,'DATA_ROOT',str(tmp_path))
    store = credentials.CredentialStore(tmp_path/'users', Fernet.generate_key().decode())
    monkeypatch.setattr(credentials,'default_store',store)
    draft = model_settings.public_config(1)
    store.set(1,'openrouter','synthetic-other-session')
    with pytest.raises(model_settings.SettingsConflict):
        model_settings.save_config(draft,1)
    assert not model_settings.config_path(1).exists()


def test_crash_after_settings_commit_keeps_new_pair(tmp_path, monkeypatch):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux worker test')
    store = credentials.CredentialStore(tmp_path/'users', Fernet.generate_key().decode())
    monkeypatch.setattr(credentials, 'default_store', store)
    monkeypatch.setattr(model_settings,'DATA_ROOT',str(tmp_path))
    def interrupt():
        write = store._atomic_write_json
        def stop(path, data):
            write(path, data)
            if path == model_settings.config_path(1):
                os._exit(32)
        store._atomic_write_json = stop
        cfg = model_settings.defaults()
        cfg.update(active_route='openai',credential_updates={'openai':'synthetic-new-key'})
        model_settings.save_config(cfg,1)
    child=multiprocessing.get_context('fork').Process(target=interrupt)
    child.start()
    child.join(8)
    assert child.exitcode == 32
    assert store.get(1,'openai') == 'synthetic-new-key'
    assert model_settings.load_config(1)['active_route'] == 'openai'


def test_processes_save_different_providers_without_lost_update(tmp_path):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux worker test')
    ctx = multiprocessing.get_context('fork')
    gate = ctx.Event()
    store = credentials.CredentialStore(tmp_path/'users', Fernet.generate_key().decode())
    def writer(name):
        assert gate.wait(4)
        for index in range(8):
            store.set(1,name,f'synthetic-{name}-{index}')
    jobs=[ctx.Process(target=writer,args=(name,)) for name in ('openai','gemini','xai')]
    for job in jobs:
        job.start()
    gate.set()
    for job in jobs:
        job.join(8)
        assert job.exitcode == 0
    for name in ('openai','gemini','xai'):
        assert store.get(1,name) == f'synthetic-{name}-7'
