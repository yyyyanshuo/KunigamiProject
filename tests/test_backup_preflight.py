import importlib.util
import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

spec = importlib.util.spec_from_file_location('backup_preflight', Path(__file__).parents[1] / 'scripts/check_user1_backup.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture
def snapshot(tmp_path):
    backup = tmp_path / 'backup'
    root = tmp_path / 'data'
    backup.mkdir()
    (root / 'configs').mkdir(parents=True)
    for path, populated in ((backup / 'users.db', True), (root / 'configs/users.db', False)):
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, password_hash TEXT)')
            if populated:
                conn.execute("INSERT INTO users VALUES(1, 'synthetic-hash')")
                conn.execute("INSERT INTO users VALUES(2, 'other-account')")
    key = Fernet.generate_key()
    env = tmp_path / '.env'
    env.write_text('KUNIGAMI_ENV=production\nSAKI_LOCAL_TEST=false\nSAKI_DATA_ROOT=' + str(root) + '\nCREDENTIAL_ENCRYPTION_KEYS=' + key.decode() + '\n')
    payload = json.dumps({'version': 1, 'providers': {'gemini': {'ciphertext': Fernet(key).encrypt(b'synthetic-private-secret').decode()}}}).encode()
    with tarfile.open(backup / 'user1.tar', 'w') as tar:
        info = tarfile.TarInfo('users/1/configs/credentials.json')
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    (backup / 'COMPLETE').touch()
    # Deterministic microseconds: the good and bad start times share this second.
    marker_ns = 1_700_000_000_343656000
    os.utime(backup / 'user1.tar', ns=(marker_ns - 1000, marker_ns - 1000))
    os.utime(backup / 'COMPLETE', ns=(marker_ns, marker_ns))
    return backup, env, root, marker_ns // 1000


def run_check(snapshot, **kwargs):
    backup, env, root, completed = snapshot
    check.check_backup(backup, env, started=kwargs.get('started', lambda unit: completed + 500), expected_data_root=root)


def test_readonly_success_and_no_secrets(snapshot, capsys):
    base = snapshot[0].parent
    before = {p.relative_to(base): p.read_bytes() for p in base.rglob('*') if p.is_file()}
    run_check(snapshot)
    after = {p.relative_to(base): p.read_bytes() for p in base.rglob('*') if p.is_file()}
    assert before == after
    output = capsys.readouterr().out
    assert '检查通过' in output
    assert '500 微秒' in output
    assert 'synthetic-private-secret' not in output
    assert 'synthetic-hash' not in output


def test_start_before_completion_in_same_second_is_rejected(snapshot):
    with pytest.raises(check.CheckFailed, match='启动早于'):
        run_check(snapshot, started=lambda unit: snapshot[3] - 500)


def test_occupied_target_is_rejected(snapshot):
    with sqlite3.connect(snapshot[2] / 'configs/users.db') as conn:
        conn.execute("INSERT INTO users VALUES(1, 'existing')")
    with pytest.raises(check.CheckFailed, match='不为空'):
        run_check(snapshot)


def test_existing_user_directory_is_rejected(snapshot):
    (snapshot[2] / 'users/1').mkdir(parents=True)
    with pytest.raises(check.CheckFailed, match='目录已存在'):
        run_check(snapshot)


@pytest.mark.parametrize('name,kind', [('users/2/private', 'file'), ('users/1/../../outside', 'file'), ('users/1/link', 'link')])
def test_unsafe_archive_is_rejected(snapshot, name, kind):
    archive = snapshot[0] / 'user1.tar'
    with tarfile.open(archive, 'w') as tar:
        info = tarfile.TarInfo(name)
        if kind == 'link':
            info.type = tarfile.SYMTYPE
            info.linkname = '/etc/passwd'
        tar.addfile(info)
    stamp = (snapshot[3] - 1) * 1000
    os.utime(archive, ns=(stamp, stamp))
    with pytest.raises(check.CheckFailed, match='归档'):
        run_check(snapshot)


def test_wrong_key_is_rejected(snapshot):
    path = snapshot[1]
    lines = path.read_text().splitlines()
    path.write_text('\n'.join(line if not line.startswith('CREDENTIAL_ENCRYPTION_KEYS=') else 'CREDENTIAL_ENCRYPTION_KEYS=' + Fernet.generate_key().decode() for line in lines))
    with pytest.raises(check.CheckFailed, match='无法解密'):
        run_check(snapshot)


def test_precise_systemd_property(monkeypatch):
    def run(command, **kwargs):
        assert command[3] == '/org/freedesktop/systemd1/unit/kunigami_2dtask_2eservice'
        assert command[-1] == 'ActiveEnterTimestamp'
        return SimpleNamespace(returncode=0, stdout='t 1700000000344000\n')
    monkeypatch.setattr(check.subprocess, 'run', run)
    assert check.service_started_us('kunigami-task.service') == 1700000000344000
