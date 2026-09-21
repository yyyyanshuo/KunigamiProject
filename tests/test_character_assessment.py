import json
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.character_assessment import (
    DIMENSIONS,
    QuestionnaireError,
    load_questionnaire,
    parse_choice_reply,
    score_answers,
    build_assessment_messages,
)
from services.assessment_translation import (
    AssessmentTranslationError,
    build_translation_messages,
    parse_translation_reply,
    translate_assessment_monologues,
    translation_quality_issues,
)


class CharacterAssessmentQuestionnaireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questionnaire = load_questionnaire()
        cls.questions = {item["id"]: item for item in cls.questionnaire["questions"]}

    def option(self, question_id, choice):
        return next(
            item for item in self.questions[question_id]["options"] if item["key"] == choice
        )

    def test_questionnaire_shape_and_dimensions(self):
        self.assertEqual(6, len(self.questionnaire["modules"]))
        self.assertEqual(36, len(self.questionnaire["questions"]))
        self.assertEqual(list(DIMENSIONS), self.questionnaire["dimensions"])
        self.assertTrue(all(len(item["options"]) == 5 for item in self.questionnaire["questions"]))

    def test_nonstandard_labels_are_normalized(self):
        self.assertEqual(
            {"掌控": 2, "预判": 1, "灵活": -2},
            self.option("Q11", "C")["scores"],
        )
        self.assertEqual(
            {
                "灵活": 2,
                "坚韧": 1,
                "预判": -1,
                "掌控": -1,
                "愉悦": -1,
                "突破": -1,
                "锻造": -1,
                "定力": -1,
            },
            self.option("Q36", "A")["scores"],
        )

    def test_none_options_have_no_score(self):
        for question in self.questionnaire["questions"]:
            option = next(item for item in question["options"] if item["key"] == "E")
            self.assertTrue(option["is_none"])
            self.assertEqual({}, option["scores"])

    def test_choice_parser_accepts_one_tag_and_monologue(self):
        parsed = parse_choice_reply("[CHOICE:Q01:C]\n最难的先来才有意思。", "Q01")
        self.assertEqual("C", parsed["choice"])
        self.assertEqual("最难的先来才有意思。", parsed["monologue"])

    def test_choice_parser_rejects_ambiguous_or_action_tagged_answers(self):
        invalid = [
            "[CHOICE:Q01:A][CHOICE:Q01:B]\n随便。",
            "[CHOICE:Q02:A]\n随便。",
            "[CHOICE:Q01:A]",
            "[CHOICE:Q01:A]\n随便。\n[NONE]",
        ]
        for answer in invalid:
            with self.subTest(answer=answer):
                with self.assertRaises(QuestionnaireError):
                    parse_choice_reply(answer, "Q01")

    def test_score_totals_and_hidden_ending(self):
        answers = [
            {"question_id": "Q01", "choice": "C"},
            {"question_id": "Q02", "choice": "E"},
            {"question_id": "Q36", "choice": "A"},
        ]
        scored = score_answers(self.questionnaire, answers)
        self.assertEqual(
            {
                "灵活": 2,
                "锻造": -1,
                "突破": 1,
                "预判": -1,
                "掌控": -1,
                "愉悦": 1,
                "定力": -3,
                "坚韧": 1,
            },
            scored["dimensions"],
        )
        self.assertEqual(1, scored["none_count"])
        self.assertIsNone(scored["hidden_ending"])

        hidden_answers = [
            {"question_id": f"Q{number:02d}", "choice": "E"}
            for number in range(1, 12)
        ]
        hidden = score_answers(self.questionnaire, hidden_answers)
        self.assertEqual(11, hidden["none_count"])
        self.assertEqual("绘心甚八／旁观者", hidden["hidden_ending"]["title"])

    def test_previous_choices_are_injected_without_scores(self):
        fake_prompt_builder = types.ModuleType("services.prompt_builder")
        fake_prompt_builder.build_system_prompt_v2 = lambda *args, **kwargs: "PERSONA"
        with patch.dict("sys.modules", {"services.prompt_builder": fake_prompt_builder}):
            messages = build_assessment_messages(
                1,
                "kunigami",
                self.questions["Q03"],
                previous_answers=[
                    {"question_id": "Q01", "choice": "C"},
                    {"question_id": "Q02", "choice": "A"},
                ],
            )
        contract = messages[1]["content"]
        self.assertIn("Q01：C", contract)
        self.assertIn("Q02：A", contract)
        self.assertNotIn("突破 +2", contract)

    def test_translation_reply_parser_requires_exact_ordered_json(self):
        parsed = parse_translation_reply(
            '```json\n[{"id":"kunigami:Q01","zh":"先从基础开始。"}]\n```',
            ["kunigami:Q01"],
        )
        self.assertEqual({"kunigami:Q01": "先从基础开始。"}, parsed)
        with self.assertRaises(AssessmentTranslationError):
            parse_translation_reply(
                '[{"id":"kunigami:Q02","zh":"错误题号"}]',
                ["kunigami:Q01"],
            )

    def test_translation_quality_detects_untranslated_japanese(self):
        source = "まずは全体を把握して、核心を見極める。"
        self.assertIn("copied_source", translation_quality_issues(source, source))
        self.assertTrue(
            any(
                issue.startswith("kana_remaining:")
                for issue in translation_quality_issues(
                    source, "先掌握全局，然后核心を見極める。"
                )
            )
        )
        self.assertEqual([], translation_quality_issues(source, "先掌握全局，再找出核心。"))

    def test_translation_prompt_treats_any_kana_as_japanese(self):
        messages = build_translation_messages(
            [{"id": "test:Q01", "source": "これは日本語です。"}],
            repair_attempt=1,
        )
        self.assertIn("只要 source 中出现平假名或片假名", messages[0]["content"])
        self.assertIn("第 1 次纠错重试", messages[0]["content"])

    def test_translation_retries_bad_batch_item_individually(self):
        replies = [
            '[{"id":"test:Q01","zh":"これは日本語です。"}]',
            '[{"id":"test:Q01","zh":"这是日语。"}]',
        ]
        fake_ai_client = types.ModuleType("services.ai_client")
        mocked = Mock(side_effect=replies)
        fake_ai_client.call_gemini = mocked
        with patch.dict("sys.modules", {"services.ai_client": fake_ai_client}):
            result = translate_assessment_monologues(
                1,
                [{"id": "test:Q01", "source": "これは日本語です。"}],
            )
        self.assertEqual({"test:Q01": "这是日语。"}, result["translations"])
        self.assertEqual(2, mocked.call_count)


class CharacterAssessmentReportTests(unittest.TestCase):
    def test_translation_jobs_resume_by_source_hash(self):
        from scripts.translate_ego_monologues import (
            collect_translation_jobs,
            source_digest,
        )

        questionnaire = load_questionnaire()
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            character_dir = run_dir / "characters"
            character_dir.mkdir()
            source = "基礎からやる。"
            result_path = character_dir / "kunigami.json"
            result_path.write_text(
                json.dumps(
                    {
                        "char_id": "kunigami",
                        "char_name": "國神錬介",
                        "answers": [
                            {
                                "question_id": "Q01",
                                "choice": "D",
                                "monologue": source,
                                "monologue_zh": "从基础开始。",
                                "monologue_zh_source_sha256": source_digest(source),
                            },
                            {
                                "question_id": "Q02",
                                "choice": "D",
                                "monologue": "俺がやる。",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            jobs, _ = collect_translation_jobs(run_dir, questionnaire)
            forced_jobs, _ = collect_translation_jobs(run_dir, questionnaire, force=True)

        self.assertEqual(["kunigami:Q02"], [item["id"] for item in jobs])
        self.assertEqual(["kunigami:Q01", "kunigami:Q02"], [item["id"] for item in forced_jobs])

    def test_batch_character_filter_excludes_requested_ids(self):
        from scripts.run_character_ego_test import (
            filter_character_ids,
            parse_character_ids,
            pending_character_ids,
        )

        configured = {
            "bachira": {},
            "wataru": {},
            "itsuki": {},
            "study_assistant": {},
            "ryouko": {},
            "rei": {},
            "reo": {},
        }
        excluded = parse_character_ids(
            "wataru, itsuki, study_assistant, ryouko, rei"
        )
        progress = {
            char_id: {"state": "completed" if char_id == "bachira" else "not_started"}
            for char_id in configured
        }
        pending = pending_character_ids(configured, progress)
        selected = filter_character_ids(pending, excluded, configured)

        self.assertEqual(["reo"], selected)

    def test_manifest_completion_requires_every_character_result(self):
        from scripts.run_character_ego_test import all_manifest_characters_completed

        manifest = {"characters": ["bachira", "reo"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            character_dir = run_dir / "characters"
            character_dir.mkdir()
            (character_dir / "bachira.json").write_text(
                json.dumps(
                    {"answers": [{"question_id": f"Q{number:02d}"} for number in range(1, 37)]}
                ),
                encoding="utf-8",
            )
            self.assertFalse(all_manifest_characters_completed(run_dir, manifest))

            (character_dir / "reo.json").write_text(
                json.dumps(
                    {"answers": [{"question_id": f"Q{number:02d}"} for number in range(1, 37)]}
                ),
                encoding="utf-8",
            )
            self.assertTrue(all_manifest_characters_completed(run_dir, manifest))

    def test_user_email_can_be_loaded_read_only_for_interactive_login(self):
        from scripts.run_character_ego_test import lookup_user_email

        with tempfile.TemporaryDirectory() as temp_dir:
            users_db = Path(temp_dir) / "users.db"
            connection = sqlite3.connect(users_db)
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
            connection.execute("INSERT INTO users (id, email) VALUES (1, 'user@example.com')")
            connection.commit()
            connection.close()
            email = lookup_user_email(1, users_db=users_db)
        self.assertEqual("user@example.com", email)

    def test_progress_menu_lists_unfinished_and_unstarted_characters(self):
        from scripts.run_character_ego_test import (
            choose_pending_character,
            discover_character_progress,
        )

        configured = {
            "kunigami": {"name": "國神錬介"},
            "isagi": {"name": "潔世一"},
            "rin": {"name": "糸師凛"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            partial_run = root / "partial-run"
            complete_run = root / "complete-run"
            for run_dir in (partial_run, complete_run):
                (run_dir / "characters").mkdir(parents=True)
                (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
            (partial_run / "characters" / "kunigami.json").write_text(
                json.dumps(
                    {"answers": [{"question_id": "Q01"}, {"question_id": "Q02"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (complete_run / "characters" / "isagi.json").write_text(
                json.dumps(
                    {"answers": [{"question_id": f"Q{number:02d}"} for number in range(1, 37)]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            progress = discover_character_progress(1, configured, assessments_root=root)
            output = []
            selected, run_dir = choose_pending_character(
                configured,
                progress,
                input_fn=lambda _: "1",
                output_fn=output.append,
            )

        self.assertEqual("incomplete", progress["kunigami"]["state"])
        self.assertEqual(2, progress["kunigami"]["answered"])
        self.assertEqual("completed", progress["isagi"]["state"])
        self.assertEqual("not_started", progress["rin"]["state"])
        self.assertEqual("kunigami", selected)
        self.assertEqual("partial-run", run_dir.name)
        rendered_menu = "\n".join(output)
        self.assertIn("未完成 2/36", rendered_menu)
        self.assertIn("未开始", rendered_menu)
        self.assertNotIn("潔世一 (isagi)", rendered_menu)

    def test_shared_run_imports_existing_character_result(self):
        from scripts.run_character_ego_test import (
            discover_character_progress,
            ensure_shared_character_run,
        )

        configured = {"kunigami": {"name": "國神錬介"}, "isagi": {"name": "潔世一"}}
        questionnaire = load_questionnaire()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_run = root / "old-run"
            (old_run / "characters").mkdir(parents=True)
            (old_run / "manifest.json").write_text("{}", encoding="utf-8")
            old_result = {
                "run_id": "old-run",
                "char_id": "kunigami",
                "answers": [{"question_id": "Q01", "choice": "D"}],
            }
            (old_run / "characters" / "kunigami.json").write_text(
                json.dumps(old_result, ensure_ascii=False), encoding="utf-8"
            )
            progress = discover_character_progress(1, configured, assessments_root=root)
            shared_run = ensure_shared_character_run(
                1,
                configured,
                questionnaire,
                progress,
                run_dir=root / "all-characters",
            )
            imported = json.loads(
                (shared_run / "characters" / "kunigami.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((shared_run / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual("all-characters", imported["run_id"])
        self.assertEqual("old-run", imported["source_run_id"])
        self.assertEqual(["kunigami", "isagi"], manifest["characters"])

    def test_loopback_secure_cookie_is_reenabled_for_each_request(self):
        from scripts.run_character_ego_test import allow_loopback_session_cookie

        cookie = types.SimpleNamespace(name="session", secure=True)
        session = types.SimpleNamespace(cookies=[cookie])
        allow_loopback_session_cookie(session, "http://127.0.0.1:8000")
        self.assertFalse(cookie.secure)

        cookie.secure = True
        allow_loopback_session_cookie(session, "https://example.com")
        self.assertTrue(cookie.secure)

    def test_report_is_self_contained_and_embeds_character_data(self):
        from scripts.run_character_ego_test import render_report

        questionnaire = load_questionnaire()
        manifest = {
            "run_id": "test-run",
            "created_at": "2026-08-13T00:00:00+08:00",
            "opening_message": "（系统提示：测试开始。）",
            "characters": ["kunigami"],
        }
        result = {
            "char_id": "kunigami",
            "char_name": "國神錬介",
            "status": "partial",
            "answers": [
                {
                    "question_id": "Q01",
                    "choice": "D",
                    "monologue": "基礎からやる。",
                    "monologue_zh": "从基础开始。",
                    "score_delta": {"锻造": 2},
                    "attempts": 1,
                }
            ],
            "dimensions": {name: 0 for name in DIMENSIONS},
            "none_count": 0,
            "hidden_ending": None,
            "opening_message": manifest["opening_message"],
            "route": None,
            "model": None,
            "completed_at": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            character_dir = run_dir / "characters"
            character_dir.mkdir()
            (character_dir / "kunigami.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8"
            )
            render_report(run_dir, manifest, questionnaire)
            report = (run_dir / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("__ASSESSMENT_DATA_JSON__", report)
        self.assertIn("國神錬介", report)
        self.assertIn("从基础开始。", report)
        self.assertIn("中文翻译", report)
        self.assertIn("const DATA =", report)


if __name__ == "__main__":
    unittest.main()
