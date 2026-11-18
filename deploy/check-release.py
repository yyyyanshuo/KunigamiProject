"""Run the offline regression and verify a release's exact production baseline."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HOST = 'ubuntu@43.160.218.82'
REMOTE_CHECK = """
import hashlib, json
from pathlib import Path

root = Path('/var/www/sakiglobal')
result = {}
for name in json.load(__import__('sys').stdin):
    path = root / name
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError('Unexpected production path')
    if path.exists() and not path.is_file():
        raise ValueError('Expected a regular production file')
    result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
print(json.dumps(result))
"""
RELEASE_DIRS = {'blueprints', 'core', 'services', 'static', 'templates', 'agent-test', 'docs'}
ROOT_FILES = {
    'app.py', 'agent_utils.py', 'admin_stats.py', 'cos_utils.py', 'memory_jobs.py',
    'music_api.py', 'music_manager.py', 'run_agent.py', 'run_scheduler.py', 'weather_api.py',
    'CHANGELOG.md', 'requirements.txt', 'package.json', 'package-lock.json',
}
EXCLUDED = {'node_modules', 'venv', '__pycache__'}
DIGEST = re.compile('[0-9a-f]{64}')


def read_manifest(path):
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or not data:
        raise ValueError('Expected a non-empty release manifest')
    for name, hashes in data.items():
        parts = PurePosixPath(name).parts
        if (not parts or PurePosixPath(name).as_posix() != name or name.startswith('/')
                or any(part.startswith('.') or part in EXCLUDED for part in parts)
                or not all(re.fullmatch(r'[\w.-]+', part) for part in parts)
                or (name not in ROOT_FILES and (len(parts) < 2 or parts[0] not in RELEASE_DIRS))):
            raise ValueError(f'Unsupported release path: {name}')
        if not isinstance(hashes, dict) or set(hashes) != {'before', 'after'}:
            raise ValueError(f'Expected before and after hashes: {name}')
        for side, value in hashes.items():
            if value is None and side == 'before':
                continue
            if not isinstance(value, str) or not DIGEST.fullmatch(value):
                raise ValueError(f'Invalid {side} SHA-256: {name}')
    return data


def check_payload(directory, manifest):
    directory = directory.resolve()
    files = set()
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Unexpected payload path: {path.relative_to(directory)}')
        if path.is_file():
            files.add(path.relative_to(directory).as_posix())
    if files != set(manifest):
        raise ValueError(f'Payload differs from manifest: {sorted(files ^ set(manifest))}')
    for name, hashes in manifest.items():
        with (directory / name).open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != hashes['after']:
            raise ValueError(f'Payload SHA-256 mismatch: {name}')


def production_hashes(manifest, key, hosts):
    with tempfile.TemporaryDirectory(prefix='saki-release-ssh-') as temp:
        private = Path(temp) / 'identity'
        private.touch(mode=0o600)
        shutil.copyfile(key, private)
        remote = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', REMOTE_CHECK])
        result = subprocess.run(
            ['ssh', '-i', str(private), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={hosts}',
             '-o', 'ConnectTimeout=8', HOST, remote],
            input=json.dumps(list(manifest)), text=True, stdout=subprocess.PIPE, check=True, timeout=60,
        )
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('payload', type=Path)
    parser.add_argument('--key', type=Path, default=Path('/mnt/c/itoshirin.pem'))
    parser.add_argument('--known-hosts', type=Path,
                        default=Path('/mnt/c/Users/96980/.ssh/known_hosts'))
    parser.add_argument('--installed', action='store_true',
                        help='verify the installed hashes instead of a pending release baseline')
    args = parser.parse_args()
    try:
        manifest = read_manifest(args.manifest)
        check_payload(args.payload, manifest)
        subprocess.run([sys.executable, str(ROOT/'scripts/check_core.py')], cwd=ROOT, check=True)
        expected = {name: hashes['after' if args.installed else 'before'] for name, hashes in manifest.items()}
        actual = production_hashes(manifest, args.key.resolve(), args.known_hosts.resolve())
        wrong = sorted(name for name in expected.keys() | actual.keys()
                       if name not in expected or name not in actual or actual[name] != expected[name])
        if wrong:
            raise ValueError('Production SHA-256 mismatch: ' + ', '.join(wrong))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Release preflight failed: {error}', file=sys.stderr)
        return 1
    print(f'PASS: offline core regression, payload and production hashes ({len(manifest)} files)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
