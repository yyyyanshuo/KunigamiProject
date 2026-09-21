"""Recognition helpers for the project's long-standing system-hint message format."""

from __future__ import annotations


SYSTEM_PROMPT_PREFIX = "（系统提示："
SYSTEM_PROMPT_SUFFIX = "）"


def is_system_prompt_message(message: object) -> bool:
    """Return True only when the entire message uses （系统提示：内容）."""

    text = str(message or "").strip()
    if not text.startswith(SYSTEM_PROMPT_PREFIX) or not text.endswith(SYSTEM_PROMPT_SUFFIX):
        return False
    content = text[len(SYSTEM_PROMPT_PREFIX):-len(SYSTEM_PROMPT_SUFFIX)]
    return bool(content.strip())


def should_suppress_reply_for_deep_sleep(deep_sleep: object, message: object) -> bool:
    """A system prompt bypasses deep sleep for this message without changing sleep state."""

    return bool(deep_sleep) and not is_system_prompt_message(message)
