"""Explicit project configuration and optional isolated, offline development mode."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def load_environment():
    # Never search cwd or parent directories. Explicit environment wins.
    load_dotenv(SOURCE_ROOT / '.env', override=False)


load_environment()
from core.environment_check import configuration_errors
_errors = configuration_errors()
if _errors:
    raise RuntimeError("配置检查未通过（不显示值）：\n" + "\n".join(_errors))
LOCAL_TEST = os.getenv('SAKI_LOCAL_TEST', 'true').lower() == 'true'
APP_ENV = (os.getenv('KUNIGAMI_ENV') or os.getenv('FLASK_ENV') or 'development').lower()
if LOCAL_TEST and APP_ENV not in {'development', 'test', 'testing'}:
    raise RuntimeError('SAKI_LOCAL_TEST is only allowed in development/test')
raw_root = os.getenv('SAKI_DATA_ROOT')
DATA_ROOT = str((SOURCE_ROOT / raw_root).resolve()) if raw_root else str(SOURCE_ROOT)
if LOCAL_TEST:
    data_path = Path(DATA_ROOT)
    local_allowed = data_path.is_relative_to(SOURCE_ROOT / '.local-data')
    temporary_allowed = (
        data_path.is_relative_to(Path('/tmp'))
        and len(data_path.parts) > 2
        and data_path.parts[2].startswith('saki-')
    )
    if not raw_root or not (local_allowed or temporary_allowed):
        raise RuntimeError('Local data must be inside this project .local-data or /tmp/saki-*')


def _inside_data(path):
    return Path(os.fsdecode(path)).resolve().is_relative_to(Path(DATA_ROOT))


if LOCAL_TEST:
    # Guard before importing any app modules. No network clients (including SMTP,
    # COS and model SDKs) can open outbound connections in this process.
    sys.dont_write_bytecode = True
    for name in ('configs', 'users', 'characters', 'groups', 'logs', 'tmp', 'instance'):
        (Path(DATA_ROOT) / name).mkdir(parents=True, exist_ok=True)
    import tempfile
    tempfile.tempdir = str(Path(DATA_ROOT) / 'tmp')

    def _audit(event, args):
        if event in {'socket.connect', 'socket.sendto', 'subprocess.Popen', 'os.system'}:
            raise PermissionError('Saki offline mode: external connections/processes disabled')
        if event == 'socket.getaddrinfo' and args[0] != '127.0.0.1':
            raise PermissionError('Saki offline mode: external DNS disabled')
        if event == 'socket.bind' and isinstance(args[1], tuple) and args[1][0] != '127.0.0.1':
            raise PermissionError('Saki local mode only allows 127.0.0.1')
        paths = []
        if event == 'open' and not isinstance(args[0], int):
            flags = args[2] or 0
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                paths = [args[0]]
        elif event in {'os.remove', 'os.rmdir'}:
            # shutil.rmtree uses paths relative to a directory descriptor on Linux.
            # Validate that descriptor's real directory rather than process cwd.
            path, dir_fd = args[:2]
            if dir_fd not in (None, -1) and not os.path.isabs(path):
                path = os.path.join(os.readlink(f'/proc/self/fd/{dir_fd}'), os.fsdecode(path))
            paths = [path]
        elif event in {'os.mkdir', 'os.chmod', 'os.truncate'}:
            paths = [args[0]]
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            paths = args[:2]
        elif event == 'sqlite3.connect' and str(args[0]) != ':memory:':
            paths = [args[0]]
        for path in paths:
            if not _inside_data(path):
                raise PermissionError('Saki local mode: write outside SAKI_DATA_ROOT blocked')

    sys.addaudithook(_audit)
