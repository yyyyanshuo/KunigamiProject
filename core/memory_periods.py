"""Shared date helpers for medium/long memory rollovers."""

from datetime import date, timedelta


def week_key_for_end_date(end_date: date) -> str:
    """Return the WeekN key for a Sunday, grouped by that Sunday's month."""
    if end_date.weekday() != 6:
        raise ValueError("long-memory period must end on Sunday")

    first_day = end_date.replace(day=1)
    first_sunday = first_day + timedelta(days=(6 - first_day.weekday()) % 7)
    week_number = ((end_date - first_sunday).days // 7) + 1
    return f"{end_date:%Y-%m}-Week{week_number}"


def completed_week_before(reference_date: date) -> tuple[date, date]:
    """Return the last fully completed Monday-Sunday period."""
    end_date = reference_date - timedelta(days=reference_date.weekday() + 1)
    return end_date - timedelta(days=6), end_date


def parse_week_key_to_dates(week_key: str) -> tuple[date, date] | None:
    """Resolve YYYY-MM-WeekN to its complete Monday-Sunday date range.

    The month belongs to the ending Sunday. A week may therefore begin in the
    previous month. Legacy YYYY-MM month keys remain supported.
    """
    try:
        if "-Week" in week_key:
            year_month, raw_week_number = week_key.split("-Week", 1)
            year, month = map(int, year_month.split("-"))
            week_number = int(raw_week_number)
            if week_number < 1:
                return None

            first_day = date(year, month, 1)
            first_sunday = first_day + timedelta(days=(6 - first_day.weekday()) % 7)
            end_date = first_sunday + timedelta(weeks=week_number - 1)
            if end_date.month != month:
                return None
            return end_date - timedelta(days=6), end_date

        year, month = map(int, week_key.split("-"))
        next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        return date(year, month, 1), next_month - timedelta(days=1)
    except (TypeError, ValueError):
        return None
