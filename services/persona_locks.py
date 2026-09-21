"""Parse persona blocks that are protected from AI edits, not user edits."""

from __future__ import annotations

from dataclasses import dataclass
LOCK_OPEN = "[[LOCK]]"
LOCK_CLOSE = "[[/LOCK]]"


class PersonaLockError(ValueError):
    code = "invalid_persona_lock_markup"


@dataclass(frozen=True)
class PersonaSegment:
    locked: bool
    text: str
    raw: str


def parse_persona_segments(value: object) -> list[PersonaSegment]:
    """Parse standalone LOCK marker lines without changing the original text."""

    text = "" if value is None else str(value)
    segments: list[PersonaSegment] = []
    unlocked_lines: list[str] = []
    locked_lines: list[str] = []
    locked_raw_lines: list[str] = []
    inside_lock = False
    open_line = None

    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        marker = line.strip()
        if marker == LOCK_OPEN:
            if inside_lock:
                raise PersonaLockError(f"第 {line_number} 行不允许嵌套 [[LOCK]]")
            if unlocked_lines:
                raw = "".join(unlocked_lines)
                segments.append(PersonaSegment(False, raw, raw))
                unlocked_lines = []
            inside_lock = True
            open_line = line_number
            locked_lines = []
            locked_raw_lines = [line]
            continue

        if marker == LOCK_CLOSE:
            if not inside_lock:
                raise PersonaLockError(f"第 {line_number} 行出现了没有开始标签的 [[/LOCK]]")
            locked_raw_lines.append(line)
            content = "".join(locked_lines)
            if not content.strip():
                raise PersonaLockError(f"第 {open_line} 行的锁定区块不能为空")
            segments.append(
                PersonaSegment(True, content, "".join(locked_raw_lines))
            )
            inside_lock = False
            open_line = None
            locked_lines = []
            locked_raw_lines = []
            continue

        if inside_lock:
            locked_lines.append(line)
            locked_raw_lines.append(line)
        else:
            unlocked_lines.append(line)

    # splitlines() drops nothing except that an empty input produces no lines.
    if inside_lock:
        raise PersonaLockError(f"第 {open_line} 行的 [[LOCK]] 缺少 [[/LOCK]]")
    if unlocked_lines:
        raw = "".join(unlocked_lines)
        segments.append(PersonaSegment(False, raw, raw))
    return segments


def validate_persona_locks(value: object) -> str:
    text = "" if value is None else str(value)
    parse_persona_segments(text)
    return text


def locked_persona_blocks(value: object) -> list[str]:
    return [
        segment.text.replace("\r\n", "\n").replace("\r", "\n")
        for segment in parse_persona_segments(value)
        if segment.locked
    ]


def persona_locked_ranges(value: object) -> list[tuple[int, int]]:
    """Return character offsets for raw LOCK blocks, including both markers."""

    ranges: list[tuple[int, int]] = []
    cursor = 0
    for segment in parse_persona_segments(value):
        end = cursor + len(segment.raw)
        if segment.locked:
            ranges.append((cursor, end))
        cursor = end
    return ranges


def ensure_model_preserved_locks(old_value: object, generated_value: object) -> None:
    """Validate an AI edit without restricting direct user edits."""

    old_blocks = locked_persona_blocks(old_value)
    generated_blocks = locked_persona_blocks(generated_value)
    cursor = 0
    for old_block in old_blocks:
        while cursor < len(generated_blocks) and generated_blocks[cursor] != old_block:
            cursor += 1
        if cursor >= len(generated_blocks):
            raise PersonaLockError("AI 生成结果修改或遗漏了已有的 LOCK 区块")
        cursor += 1


PERSONA_LOCK_MODEL_INSTRUCTION = (
    "人设中的 [[LOCK]] 与 [[/LOCK]] 是模型编辑保护标记。你必须读取并遵守其中内容。"
    "日常对话不要向用户解释或输出这些标记；如果今后被要求编辑、重写或返回完整人设，"
    "必须逐字保留所有已有锁定区块及标签，不得删改、改写、概括或改变顺序，只能编辑标签外内容。"
    "用户本人仍可在设置页面自由修改或移除任何内容和标签。"
)
