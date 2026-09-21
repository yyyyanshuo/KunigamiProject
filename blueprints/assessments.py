"""Authenticated, read-only character assessment endpoints."""

import os
import re

from flask import Blueprint, jsonify, request, send_file

from core.config import USERS_ROOT
from core.context import get_current_user_id
from services.character_assessment import (
    DEFAULT_OPENING,
    QuestionnaireError,
    call_assessment_model,
    get_character_info,
    get_question,
)
from services.assessment_translation import (
    AssessmentTranslationError,
    translate_assessment_monologues,
    validate_translation_items,
)


assessment_bp = Blueprint("assessment", __name__)
_REPORT_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _assessment_report_path(user_id: int, run_id: str) -> str | None:
    if not _REPORT_RUN_ID_RE.fullmatch(str(run_id or "")) or run_id in {".", ".."}:
        return None
    report_root = os.path.realpath(
        os.path.join(USERS_ROOT, str(user_id), "assessments", "ego_8d")
    )
    report_path = os.path.realpath(os.path.join(report_root, run_id, "report.html"))
    try:
        if os.path.commonpath((report_root, report_path)) != report_root:
            return None
    except ValueError:
        return None
    return report_path


@assessment_bp.route("/assessments/ego_8d/<run_id>/report", methods=["GET"])
def assessment_report(run_id):
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "authentication_required"}), 401
    report_path = _assessment_report_path(user_id, run_id)
    if report_path is None:
        return jsonify({"error": "invalid_run_id"}), 400
    if not os.path.isfile(report_path):
        return jsonify({"error": "report_not_found"}), 404
    response = send_file(report_path, mimetype="text/html", conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@assessment_bp.route("/api/<char_id>/assessment_chat", methods=["POST"])
def assessment_chat(char_id):
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "authentication_required"}), 401

    character = get_character_info(user_id, char_id)
    if character is None:
        return jsonify({"error": "character_not_found"}), 404

    data = request.get_json(silent=True) or {}
    question_id = str(data.get("question_id") or "").strip().upper()
    opening_message = str(data.get("opening_message") or DEFAULT_OPENING).strip()
    retry_instruction = str(data.get("retry_instruction") or "").strip()
    raw_previous_answers = data.get("previous_answers") or []
    if not question_id:
        return jsonify({"error": "question_id_required"}), 400
    if not opening_message:
        opening_message = DEFAULT_OPENING
    if len(opening_message) > 2000:
        return jsonify({"error": "opening_message_too_long"}), 400
    if len(retry_instruction) > 500:
        return jsonify({"error": "retry_instruction_too_long"}), 400

    try:
        question = get_question(question_id)
        if not isinstance(raw_previous_answers, list) or len(raw_previous_answers) > 35:
            raise QuestionnaireError("previous_answers 必须是最多 35 项的数组")
        previous_answers = []
        seen_question_ids = set()
        for item in raw_previous_answers:
            if not isinstance(item, dict):
                raise QuestionnaireError("previous_answers 中的项目必须是对象")
            previous_question = get_question(str(item.get("question_id") or ""))
            choice = str(item.get("choice") or "").strip().upper()
            if choice not in "ABCDE":
                raise QuestionnaireError(f"{previous_question['id']} 的历史选项无效")
            if previous_question["number"] >= question["number"]:
                raise QuestionnaireError("只能带入当前题之前的选择")
            if previous_question["id"] in seen_question_ids:
                raise QuestionnaireError("previous_answers 中存在重复题号")
            seen_question_ids.add(previous_question["id"])
            previous_answers.append({"question_id": previous_question["id"], "choice": choice})
        previous_answers.sort(key=lambda item: int(item["question_id"][1:]))
        generated = call_assessment_model(
            user_id,
            char_id,
            question,
            opening_message,
            retry_instruction=retry_instruction,
            previous_answers=previous_answers,
        )
    except QuestionnaireError as exc:
        return jsonify({"error": "invalid_question", "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": "assessment_generation_failed", "message": str(exc)}), 500

    response = jsonify(
        {
            "user_id": user_id,
            "char_id": char_id,
            "char_name": character.get("remark") or character.get("name") or char_id,
            "question_id": question["id"],
            "raw_reply": generated["raw_reply"],
            "route": generated["route"],
            "model": generated["model"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@assessment_bp.route("/api/assessments/ego_8d/translate_monologues", methods=["POST"])
def translate_monologues():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "authentication_required"}), 401
    data = request.get_json(silent=True) or {}
    try:
        items = validate_translation_items(data.get("items"))
    except AssessmentTranslationError as exc:
        return jsonify({"error": "invalid_translation_request", "message": str(exc)}), 400
    try:
        generated = translate_assessment_monologues(user_id, items)
    except AssessmentTranslationError as exc:
        return jsonify({"error": "translation_failed", "message": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": "translation_generation_failed", "message": str(exc)}), 500

    response = jsonify(
        {
            "user_id": user_id,
            "model": generated["model"],
            "translations": generated["translations"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response
