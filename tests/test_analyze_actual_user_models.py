import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analyze_actual_user_models import build_report, count_successful_calls


def create_user_db(path, user_ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO users (id) VALUES (?)", [(uid,) for uid in user_ids])
    connection.commit()
    connection.close()


def write_user(project_root, user_id, usage, *, route="relay", model="gemini-current"):
    user_root = project_root / "users" / str(user_id)
    logs = user_root / "logs"
    configs = user_root / "configs"
    logs.mkdir(parents=True, exist_ok=True)
    configs.mkdir(parents=True, exist_ok=True)
    (logs / "usage_history.json").write_text(json.dumps(usage), encoding="utf-8")
    (configs / "api_settings.json").write_text(
        json.dumps(
            {
                "active_route": route,
                "routes": {
                    route: {
                        "relay_provider": "new",
                        "models": {"chat": model},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def write_user_without_relay_provider(project_root, user_id, usage, *, model):
    user_root = project_root / "users" / str(user_id)
    logs = user_root / "logs"
    configs = user_root / "configs"
    logs.mkdir(parents=True, exist_ok=True)
    configs.mkdir(parents=True, exist_ok=True)
    (logs / "usage_history.json").write_text(json.dumps(usage), encoding="utf-8")
    (configs / "api_settings.json").write_text(
        json.dumps(
            {
                "active_route": "relay",
                "routes": {"relay": {"models": {"chat": model}}},
            }
        ),
        encoding="utf-8",
    )


class ActualUserModelReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_count_successful_calls_requires_positive_input_and_output(self):
        usage_path = self.project_root / "usage_history.json"
        usage_path.write_text(
            json.dumps(
                [
                    {"input": 12, "output": 3},
                    {"input": "4", "output": "2"},
                    {"input": 9, "output": 0},
                    {"input": 0, "output": 9},
                    {"input": None, "output": 9},
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(count_successful_calls(usage_path), (2, 3))

    def test_report_uses_current_model_not_model_in_usage_log(self):
        db_path = self.project_root / "configs" / "users.db"
        create_user_db(db_path, [6, 7])
        write_user(
            self.project_root,
            6,
            [{"input": 10, "output": 5, "model": "historical-model"}],
            model="deepseek-v4-pro",
        )
        write_user(
            self.project_root,
            7,
            [{"input": 10, "output": 0, "model": "gemini-historical"}],
            model="gemini-2.5-pro",
        )

        report = build_report(self.project_root, db_path)

        self.assertEqual(report["summary"]["actual_users"], 1)
        self.assertEqual(
            report["by_model"],
            [
                {
                    "route": "relay",
                    "model_provider": "DeepSeek",
                    "model": "deepseek-v4-pro",
                    "actual_users": 1,
                    "actual_user_share_pct": 100.0,
                }
            ],
        )

    def test_test_accounts_are_excluded_by_default(self):
        db_path = self.project_root / "configs" / "users.db"
        create_user_db(db_path, [1, 6])
        usage = [{"input": 10, "output": 5}]
        write_user(self.project_root, 1, usage, model="gemini-test")
        write_user(self.project_root, 6, usage, model="gemini-real")

        default_report = build_report(self.project_root, db_path)
        all_report = build_report(self.project_root, db_path, include_test_users=True)

        self.assertEqual(default_report["summary"]["actual_users"], 1)
        self.assertEqual(all_report["summary"]["actual_users"], 2)

    def test_missing_relay_provider_uses_runtime_old_default(self):
        db_path = self.project_root / "configs" / "users.db"
        create_user_db(db_path, [6])
        write_user_without_relay_provider(
            self.project_root,
            6,
            [{"input": 10, "output": 5}],
            model="gemini-2.5-pro",
        )

        report = build_report(self.project_root, db_path)

        self.assertEqual(report["by_route"][0]["route_provider"], "old")


if __name__ == "__main__":
    unittest.main()
