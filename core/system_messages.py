"""Recognition helpers for the project's long-standing system-hint message format."""

from __future__ import annotations


SYSTEM_PROMPT_PREFIX = "（系统提示："
SYSTEM_PROMPT_SUFFIX = "）"


def is_system_prompt_message(message: object) -> bool:
    """Recognize complete Chinese, Japanese and English system notices only."""
    import re
    text = str(message or "").strip()
    patterns = (
        r"^（系统提示：([\s\S]*)）$",
        r"^\(System(?:\s+(?:prompt|notice|tip|hint|message))?:\s*([\s\S]*)\)$",
        r"^（システム(?:提示|通知)?：\s*([\s\S]*)）$",
    )
    return any(match and match.group(1).strip()
               for pattern in patterns if (match := re.fullmatch(pattern, text, re.IGNORECASE)))


def should_suppress_reply_for_deep_sleep(deep_sleep: object, message: object) -> bool:
    """A system prompt bypasses deep sleep for this message without changing sleep state."""

    return bool(deep_sleep) and not is_system_prompt_message(message)
