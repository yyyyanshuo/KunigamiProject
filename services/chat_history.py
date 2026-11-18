"""Date navigation for chats stored with Beijing wall-clock timestamps."""

import re
from datetime import date, time, timedelta

from core.time_utils import format_beijing_timestamp, resolve_local_datetime


def history_date_bounds(value, timezone):
    """Convert a strict calendar date in the display zone to storage bounds."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    day = date.fromisoformat(value)
    try:
        return tuple(
            format_beijing_timestamp(resolve_local_datetime(d, time(), timezone))
            for d in (day, day + timedelta(days=1))
        )
    except (OverflowError, OSError) as exc:
        raise ValueError("Date is out of range") from exc


def find_date_message_id(cursor, bounds):
    """Locate the day's first message without loading the full conversation."""
    cursor.execute(
        "SELECT id FROM messages WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp ASC, id ASC LIMIT 1", bounds,
    )
    row = cursor.fetchone()
    return row[0] if row else None
