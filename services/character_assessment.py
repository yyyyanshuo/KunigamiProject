"""Read-only character questionnaire support.

This module deliberately reuses the normal persona/model stack while avoiding
chat database writes and Agent Action processing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS_ROOT = os.path.join(BASE_DIR, "users")
DIMENSIONS = ("灵活", "锻造", "突破", "预判", "掌控", "愉悦", "定力", "坚韧")
DEFAULT_OPENING = (
    "（系统提示：现在将进行一项共36题的角色倾向测试。请完全按照你平时的性格作答。"
    "每题只能选择一个选项，先用指定标签明确选择，再说一句自然的碎碎念。）"
)
HIDDEN_ENDING_THRESHOLD = 11
QUESTIONNAIRE_PATH = os.path.join(BASE_DIR, "data", "assessments", "ego_8d.v1.md")

_MODULE_RE = re.compile(r"^###\s+(第.+?模块)：(.+)$")
_QUESTION_RE = re.compile(r"^####\s+Q(\d+)（([^）]+)）：(.+)$")
_OPTION_RE = re.compile(r"^\*\s+\*\*([A-E])\.【([^】]+)】\*\*\s*(.+)$")
_SCORE_RE = re.compile(r"\[([^\]]+?)\s+([+-]\d+)\]")
_CHOICE_RE = re.compile(r"\[CHOICE:Q(\d{2}):([A-E])\]", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"\[[^\]\n]+\]")


class QuestionnaireError(ValueError):
    """Raised when the canonical questionnaire or a model answer is invalid."""


def _normalize_score_label(label: str, value: int) -> dict[str, int]:
    label = label.strip()
    if label == "所有维度":
        if value != 0:
            raise QuestionnaireError("“所有维度”只允许记 0 分")
        return {}
    if label == "宽恕/灵活":
        return {"灵活": value}
    if label == "全蓝锁攻击维度":
        return {name: value for name in ("预判", "掌控", "愉悦", "突破", "锻造", "定力")}
    if label not in DIMENSIONS:
        raise QuestionnaireError(f"未知计分维度：{label}")
    return {label: value}


def load_questionnaire(path: str = QUESTIONNAIRE_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        source = handle.read()

    modules: list[dict[str, str]] = []
    questions: list[dict[str, Any]] = []
    current_module: dict[str, str] | None = None
    current_question: dict[str, Any] | None = None
    current_option: dict[str, Any] | None = None

    for line_no, raw_line in enumerate(source.splitlines(), 1):
        line = raw_line.strip()
        module_match = _MODULE_RE.match(line)
        if module_match:
            current_module = {
                "id": f"M{len(modules) + 1}",
                "label": module_match.group(1),
                "title": module_match.group(2).strip(),
            }
            modules.append(current_module)
            continue

        question_match = _QUESTION_RE.match(line)
        if question_match:
            if current_module is None:
                raise QuestionnaireError(f"第 {line_no} 行的题目没有所属模块")
            number = int(question_match.group(1))
            current_question = {
                "id": f"Q{number:02d}",
                "number": number,
                "short_title": question_match.group(2).strip(),
                "text": question_match.group(3).strip(),
                "module_id": current_module["id"],
                "module": current_module["title"],
                "options": [],
            }
            questions.append(current_question)
            current_option = None
            continue

        option_match = _OPTION_RE.match(line)
        if option_match:
            if current_question is None:
                raise QuestionnaireError(f"第 {line_no} 行的选项没有所属题目")
            current_option = {
                "key": option_match.group(1),
                "label": option_match.group(2).strip(),
                "text": option_match.group(3).strip(),
                "scores": {},
                "is_none": option_match.group(1) == "E",
            }
            current_question["options"].append(current_option)
            continue

        if "**得分：**" in line:
            if current_option is None:
                raise QuestionnaireError(f"第 {line_no} 行的得分没有所属选项")
            scores: dict[str, int] = {}
            for label, raw_value in _SCORE_RE.findall(line):
                for dimension, value in _normalize_score_label(label, int(raw_value)).items():
                    scores[dimension] = scores.get(dimension, 0) + value
            current_option["scores"] = scores

    if len(modules) != 6:
        raise QuestionnaireError(f"题库应有 6 个模块，实际为 {len(modules)}")
    if len(questions) != 36:
        raise QuestionnaireError(f"题库应有 36 道题，实际为 {len(questions)}")
    if [q["number"] for q in questions] != list(range(1, 37)):
        raise QuestionnaireError("题号必须从 Q01 连续到 Q36")
    for question in questions:
        if [option["key"] for option in question["options"]] != list("ABCDE"):
            raise QuestionnaireError(f"{question['id']} 必须恰好包含 A-E 五个选项")

    version = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return {
        "schema_version": 1,
        "questionnaire": "ego_8d",
        "questionnaire_version": version,
        "dimensions": list(DIMENSIONS),
        "hidden_ending_threshold": HIDDEN_ENDING_THRESHOLD,
        "modules": modules,
        "questions": questions,
    }


def get_character_info(user_id: int, char_id: str) -> dict[str, Any] | None:
    config_path = os.path.join(USERS_ROOT, str(user_id), "configs", "characters.json")
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            characters = json.load(handle) or {}
    except (OSError, ValueError):
        return None
    info = characters.get(char_id)
    return dict(info) if isinstance(info, dict) else None


def get_question(question_id: str) -> dict[str, Any]:
    normalized = str(question_id or "").strip().upper()
    for question in load_questionnaire()["questions"]:
        if question["id"] == normalized:
            return question
    raise QuestionnaireError(f"不存在的题号：{question_id}")


def format_question_prompt(question: dict[str, Any], opening_message: str) -> str:
    option_lines = [
        f"{option['key']}.【{option['label']}】{option['text']}"
        for option in question["options"]
    ]
    return "\n".join(
        [
            opening_message.strip() or DEFAULT_OPENING,
            "",
            f"【{question['id']} / 36｜{question['short_title']}】",
            question["text"],
            *option_lines,
        ]
    )


def build_assessment_messages(
    user_id: int,
    char_id: str,
    question: dict[str, Any],
    opening_message: str = DEFAULT_OPENING,
    retry_instruction: str = "",
    previous_answers: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    from services.prompt_builder import build_system_prompt_v2

    persona_prompt = build_system_prompt_v2(
        char_id,
        include_global_format=True,
        recent_messages=[],
        user_latest_input=None,
        include_long_memory=True,
        include_recent_messages=False,
        user_id=user_id,
        include_general_agent_rules=False,
        exclude_bedtime_diaries_from_timeline=True,
        read_only=True,
    )
    output_contract = (
        "【角色测评输出规则——优先级高于普通聊天格式】\n"
        f"你正在回答 {question['id']}。必须且只能选择 A、B、C、D、E 中的一项。\n"
        f"第一行必须严格输出 `[CHOICE:{question['id']}:选项字母]`，例如 `[CHOICE:{question['id']}:A]`。\n"
        "第二行起说一至三句符合角色性格的自然碎碎念，解释你对题目的直观反应。\n"
        "不要输出其他方括号标签，不要改写题号，不要报告分数，不要声称自己是AI。"
    )
    if retry_instruction:
        output_contract += f"\n【上次输出未通过格式校验】{retry_instruction.strip()}"
    if previous_answers:
        history = "\n".join(
            f"- {item['question_id']}：{item['choice']}"
            for item in previous_answers
        )
        output_contract += (
            "\n【本次测评中你此前已经做出的选择】\n"
            f"{history}\n"
            "请把它们当作你刚才亲自做出的选择，保持人物立场连续；不要复述清单，也不要据此计算分数。"
        )
    return [
        {"role": "system", "content": persona_prompt},
        {"role": "system", "content": output_contract},
        {"role": "user", "content": format_question_prompt(question, opening_message)},
    ]


def call_assessment_model(
    user_id: int,
    char_id: str,
    question: dict[str, Any],
    opening_message: str = DEFAULT_OPENING,
    retry_instruction: str = "",
    previous_answers: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    from services.ai_client import call_gemini, call_openrouter, get_model_config

    messages = build_assessment_messages(
        user_id,
        char_id,
        question,
        opening_message,
        retry_instruction=retry_instruction,
        previous_answers=previous_answers,
    )
    route, model = get_model_config("chat", user_id=user_id)
    if route == "relay":
        reply = call_openrouter(messages, char_id=char_id, model_name=model, user_id=user_id)
    else:
        reply = call_gemini(messages, char_id=char_id, model_name=model, user_id=user_id)
    cleaned_reply = re.sub(
        r"\[(?:(?:\d{2}-\d{2}\s+)?\d{1,2}:\d{2})\]\s*",
        "",
        str(reply or ""),
    ).strip()
    return {"raw_reply": cleaned_reply, "route": route, "model": model}


def parse_choice_reply(raw_reply: str, expected_question_id: str) -> dict[str, str]:
    text = str(raw_reply or "").strip()
    matches = list(_CHOICE_RE.finditer(text))
    if len(matches) != 1:
        raise QuestionnaireError("回答必须恰好包含一个 [CHOICE:Qxx:X] 标签")
    match = matches[0]
    actual_question_id = f"Q{int(match.group(1)):02d}"
    expected = expected_question_id.strip().upper()
    if actual_question_id != expected:
        raise QuestionnaireError(f"回答题号为 {actual_question_id}，预期为 {expected}")
    remaining = (text[: match.start()] + text[match.end() :]).strip()
    if _ANY_TAG_RE.search(remaining):
        raise QuestionnaireError("回答包含 CHOICE 之外的方括号标签")
    monologue = re.sub(r"^[\s/]+|[\s/]+$", "", remaining).strip()
    if not monologue:
        raise QuestionnaireError("回答缺少角色碎碎念")
    return {
        "choice": match.group(2).upper(),
        "choice_tag": match.group(0).upper(),
        "monologue": monologue,
    }


def score_answers(questionnaire: dict[str, Any], answers: list[dict[str, Any]]) -> dict[str, Any]:
    questions = {question["id"]: question for question in questionnaire["questions"]}
    totals = {dimension: 0 for dimension in DIMENSIONS}
    none_count = 0
    for answer in answers:
        question = questions[answer["question_id"]]
        option = next(item for item in question["options"] if item["key"] == answer["choice"])
        for dimension, value in option["scores"].items():
            totals[dimension] += value
        if option["is_none"]:
            none_count += 1
    hidden_ending = None
    if none_count >= questionnaire["hidden_ending_threshold"]:
        hidden_ending = {
            "title": "绘心甚八／旁观者",
            "message": "你的自我（Ego）被厚厚的防御机制包裹，你拒绝被定义。",
        }
    return {"dimensions": totals, "none_count": none_count, "hidden_ending": hidden_ending}
