"""Prompt and output contract for bedtime diary maintenance turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from services.content_actions import (
    PERSONA_ACTIONS,
    PLAN_ACTIONS,
    RELATION_ACTIONS,
    ParsedContentAction,
    extract_content_actions,
)


class BedtimeDiaryOutputError(ValueError):
    """Raised when a bedtime response violates its machine-readable contract."""


@dataclass(frozen=True)
class ParsedBedtimeDiaryOutput:
    thoughts: str
    review: dict[str, str]
    actions: tuple[ParsedContentAction, ...]


_THOUGHTS_RE = re.compile(r"\[THOUGHTS\](.*?)\[/THOUGHTS\]", re.IGNORECASE | re.DOTALL)
_REVIEW_RE = re.compile(
    r"\[MAINTENANCE_REVIEW\s*:\s*(\{.*?\})\s*\]",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_THOUGHT_ACTION_RE = re.compile(
    r"\[\s*(?:[A-Z][A-Z0-9_]*|voice|tickle|tickle_user|recall)\b",
    re.IGNORECASE,
)
_REVIEW_DOMAINS = {
    "persona": PERSONA_ACTIONS,
    "relation": RELATION_ACTIONS,
    "plan": PLAN_ACTIONS,
}


def build_bedtime_diary_trigger(date_str: str, time_str: str, lang: str) -> str:
    """Build only the final user turn; the regular character system prompt stays intact."""

    if lang == "ja":
        return f"""[Task: 就寝前の日記と夜間整理]

日付は {date_str}、現在時刻は {time_str} です。まもなく深い眠りにつきます。

一度の返答で次を行ってください：
1. 今日の出来事、感情、考え、人間関係、未完了のこと、これからへの思いを振り返り、一人称の私的な就寝前日記を書く。ユーザーに話しかけず、チェックリスト調にしない。
2. 現在のペルソナ、完全な関係グラフ、すべての予定を個別に確認する。変更を強制せず、明確で持続的な根拠がある場合だけ、System Prompt にある編集アクションを使う。

判断基準：持続的な自己変化はペルソナ、特定の相手に関する変化は関係、今後することは予定に属する。一時的な感情は日記だけに書く。[[LOCK]] 内は変更せず、記憶に根拠のない内容を作らない。

必ず次の順序だけで出力してください：
[THOUGHTS]
就寝前の日記
[/THOUGHTS]

[MAINTENANCE_REVIEW: {{"persona":"keep","relation":"keep","plan":"keep"}}]

各値は keep または change のどちらかにする。change にした項目には、その後に対応する編集タグを必ず出力する。keep の項目にはタグを出力しない。タグは一行に一つ。説明、画像、音声、スタンプ、会話切替など、上記以外は出力しない。"""

    if lang == "en":
        return f"""[Task: Bedtime Diary and Nightly Review]

The diary date is {date_str}, and the current time is {time_str}. You are about to fall into a deep sleep.

Complete both tasks in one response:
1. Write a private first-person bedtime diary reflecting naturally on today's experiences, emotions, thoughts, relationships, unfinished matters, and the future. Do not address the user or write a checklist.
2. Review your current persona, complete relationship graph, and all plans separately. Do not force changes. Only when there is clear, lasting evidence, use the editing actions already defined in the System Prompt.

Use these boundaries: lasting changes in who you are belong to persona; changes concerning a specific person belong to that relationship; things you intend to do belong to plans; temporary emotions belong only in the diary. Never alter [[LOCK]] content or invent unsupported changes.

Output only this structure, in this order:
[THOUGHTS]
Your bedtime diary
[/THOUGHTS]

[MAINTENANCE_REVIEW: {{"persona":"keep","relation":"keep","plan":"keep"}}]

Each value must be either keep or change. A change value requires at least one matching editing tag afterward; a keep value permits none. Put each tag on its own line. Output no explanation, image, voice, sticker, redirection, or other action."""

    return f"""[任务：睡前日记与夜间整理]

日记日期是 {date_str}，现在是 {time_str}。你即将进入深睡眠。

请在一次回复中完成两件事：
1. 用第一人称写一篇只给自己看的睡前日记，自然整理今天的经历、情绪、想法、关系、未完成事项和对未来的想法。不要对用户说话，不要写成检查清单。
2. 分别检查当前人设、完整关系图谱和全部计划。不要强行修改；只有存在明确、持续的依据时，才使用 System Prompt 中已有的编辑指令。

判断边界：持续的自我变化属于人设；针对具体对象的变化属于关系；以后准备做的事属于计划；临时情绪只写入日记。不得修改 [[LOCK]] 内容，不得编造记忆没有支持的变化。

只能严格按以下顺序输出：
[THOUGHTS]
睡前日记
[/THOUGHTS]

[MAINTENANCE_REVIEW: {{"persona":"keep","relation":"keep","plan":"keep"}}]

每个值只能是 keep 或 change。标记 change 时，后面必须输出至少一条对应的编辑标签；标记 keep 时不得输出对应标签。每条标签独占一行。不要输出解释、图片、语音、表情、对话转向或其他动作。"""


def parse_bedtime_diary_output(raw_text: object) -> ParsedBedtimeDiaryOutput:
    """Validate diary/review/action ordering without executing any model action."""

    source = "" if raw_text is None else str(raw_text).strip()
    thoughts_matches = list(_THOUGHTS_RE.finditer(source))
    if len(thoughts_matches) != 1:
        raise BedtimeDiaryOutputError("输出必须且只能包含一个完整的 THOUGHTS 区块")

    thoughts_match = thoughts_matches[0]
    if source[:thoughts_match.start()].strip():
        raise BedtimeDiaryOutputError("THOUGHTS 之前不得输出其他内容")
    thoughts_body = thoughts_match.group(1).strip()
    if not thoughts_body:
        raise BedtimeDiaryOutputError("睡前日记不能为空")
    if _FORBIDDEN_THOUGHT_ACTION_RE.search(thoughts_body):
        raise BedtimeDiaryOutputError("THOUGHTS 内不得包含动作标签")

    remainder = source[thoughts_match.end():]
    review_matches = list(_REVIEW_RE.finditer(remainder))
    if len(review_matches) != 1:
        raise BedtimeDiaryOutputError("输出必须且只能包含一个 MAINTENANCE_REVIEW")
    review_match = review_matches[0]
    if remainder[:review_match.start()].strip():
        raise BedtimeDiaryOutputError("THOUGHTS 后必须紧接 MAINTENANCE_REVIEW")

    try:
        review_raw = json.loads(review_match.group(1))
    except json.JSONDecodeError as exc:
        raise BedtimeDiaryOutputError("MAINTENANCE_REVIEW 必须是有效 JSON") from exc
    if not isinstance(review_raw, dict) or set(review_raw) != set(_REVIEW_DOMAINS):
        raise BedtimeDiaryOutputError("MAINTENANCE_REVIEW 必须只包含 persona、relation、plan")
    review = {key: str(value).strip().lower() for key, value in review_raw.items()}
    if any(value not in {"keep", "change"} for value in review.values()):
        raise BedtimeDiaryOutputError("MAINTENANCE_REVIEW 的值只能是 keep 或 change")

    action_text = remainder[review_match.end():]
    actions, residue = extract_content_actions(action_text)
    if residue.strip():
        raise BedtimeDiaryOutputError("MAINTENANCE_REVIEW 后只能输出有效的内容编辑标签")

    action_names = {action.name for action in actions}
    for domain, allowed_names in _REVIEW_DOMAINS.items():
        has_action = bool(action_names & allowed_names)
        if review[domain] == "change" and not has_action:
            raise BedtimeDiaryOutputError(f"{domain}=change 但没有对应编辑标签")
        if review[domain] == "keep" and has_action:
            raise BedtimeDiaryOutputError(f"{domain}=keep 但输出了对应编辑标签")

    thoughts = f"[THOUGHTS]\n{thoughts_body}\n[/THOUGHTS]"
    return ParsedBedtimeDiaryOutput(thoughts, review, tuple(actions))
