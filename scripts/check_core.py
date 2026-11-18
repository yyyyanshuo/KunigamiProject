"""Run Saki's Linux/WSL release smoke tests on disposable, offline accounts."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_saki_local import child
from test_security_boundaries import SecurityBoundaryTests

CORE = (
    "test_routes.py", "test_production_scheduler.py", "test_public_https.py",
    "test_account_deletion_recovery.py", "test_account_delete.py",
    "test_credential_commit.py", "test_saki_model_settings.py",
    "test_memory_generation_conflicts.py", "test_manual_memory_save.py",
    "test_state_batch_recovery.py", "test_maintenance_recovery.py",
    "test_forum_concurrent_append.py", "test_forum_task_recovery.py",
    "test_shared_save_concurrency.py", "test_chat_user_furigana.py",
)


def main():
    paths = [str(ROOT / "tests" / name) for name in CORE]
    with TemporaryDirectory(prefix="saki-core-", dir="/tmp") as data:
        code = """
from core.runtime import DATA_ROOT
import pytest
raise SystemExit(pytest.main([
    '-q', '-p', 'no:cacheprovider', '--basetemp', DATA_ROOT + '/tmp/pytest',
    '--log-file', DATA_ROOT + '/tmp/pytest.log',
] + %r))
""" % paths
        result = child(data, code, timeout=300, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            return result.returncode
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SecurityBoundaryTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
