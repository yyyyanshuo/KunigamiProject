"""Helpers for Kunigami's ``[图片](path)(description)`` message tags.

Both fields use balanced parentheses.  A slash inside a complete image tag is
data, not a chat-bubble separator.
"""

from dataclasses import dataclass
from typing import Iterator, Optional


IMAGE_TAG_PREFIX = "[图片]"


@dataclass(frozen=True)
class ImageTag:
    start: int
    end: int
    path: str
    description: str
    raw: str


def _parse_parenthesized(text: str, start: int):
    if start >= len(text) or text[start] != "(":
        return None
    depth = 1
    value_start = start + 1
    index = value_start
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[value_start:index], index + 1
        index += 1
    return None


def parse_image_tag_at(text: str, start: int = 0) -> Optional[ImageTag]:
    """Parse one complete image tag beginning exactly at *start*."""
    if not text.startswith(IMAGE_TAG_PREFIX, start):
        return None
    cursor = start + len(IMAGE_TAG_PREFIX)
    path_group = _parse_parenthesized(text, cursor)
    if not path_group:
        return None
    path, cursor = path_group
    description_group = _parse_parenthesized(text, cursor)
    if not description_group:
        return None
    description, end = description_group
    return ImageTag(start, end, path, description, text[start:end])


def iter_image_tags(text: str) -> Iterator[ImageTag]:
    """Yield complete image tags while leaving malformed text untouched."""
    source = str(text or "")
    cursor = 0
    while cursor < len(source):
        start = source.find(IMAGE_TAG_PREFIX, cursor)
        if start < 0:
            return
        tag = parse_image_tag_at(source, start)
        if tag:
            yield tag
            cursor = tag.end
        else:
            cursor = start + len(IMAGE_TAG_PREFIX)


def split_message_bubbles(text: str) -> list[str]:
    """Split on slashes outside complete image tags."""
    source = str(text or "")
    parts = []
    current = []
    cursor = 0
    while cursor < len(source):
        tag = parse_image_tag_at(source, cursor)
        if tag:
            current.append(tag.raw)
            cursor = tag.end
            continue
        if source[cursor] == "/":
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(source[cursor])
        cursor += 1
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def strip_image_tags(text: str) -> str:
    """Remove complete image tags using balanced-parenthesis parsing."""
    source = str(text or "")
    chunks = []
    cursor = 0
    for tag in iter_image_tags(source):
        chunks.append(source[cursor:tag.start])
        cursor = tag.end
    chunks.append(source[cursor:])
    return "".join(chunks)


def normalize_image_description(value: str, max_length: Optional[int] = 500) -> str:
    """Keep paired parentheses and neutralize only unmatched delimiters."""
    description = str(value or "").strip()
    if not description:
        raise ValueError("图片描述不能为空")
    if max_length is not None and len(description) > max_length:
        raise ValueError(f"图片描述不得超过{max_length}字")

    stack = []
    paired = set()
    for index, char in enumerate(description):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            opening = stack.pop()
            paired.add(opening)
            paired.add(index)

    chars = list(description)
    for index, char in enumerate(chars):
        if index in paired:
            continue
        if char == "(":
            chars[index] = "（"
        elif char == ")":
            chars[index] = "）"
    return "".join(chars)


def build_image_tag(path: str, description: str) -> str:
    """Build a parseable image tag without rewriting balanced parentheses."""
    return f"{IMAGE_TAG_PREFIX}({str(path or '').strip()})({normalize_image_description(description, max_length=None)})"


def protect_image_tags(text: str, placeholder_prefix: str = "__IMG_"):
    """Replace tags with placeholders and return ``(text, raw_tags)``."""
    source = str(text or "")
    tags = list(iter_image_tags(source))
    if not tags:
        return source, []
    chunks = []
    cursor = 0
    raw_tags = []
    for index, tag in enumerate(tags):
        chunks.append(source[cursor:tag.start])
        chunks.append(f"{placeholder_prefix}{index}__")
        raw_tags.append(tag.raw)
        cursor = tag.end
    chunks.append(source[cursor:])
    return "".join(chunks), raw_tags
