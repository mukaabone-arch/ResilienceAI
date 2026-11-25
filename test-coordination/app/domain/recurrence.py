"""Recurring test schedules (RA-015).

Occurrences keep the same wall-clock time in the test's own time zone (so a
02:00 IST drill stays at 02:00 IST), and the same duration. Monthly steps that
land on a missing day (31 Jan + 1 month) fall back to the month's last day.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MONTH_STEPS = {"monthly": 1, "quarterly": 3, "half_yearly": 6, "yearly": 12}
FREQUENCIES = ("weekly", *MONTH_STEPS)
LABEL = {"weekly": "weekly", "monthly": "monthly", "quarterly": "quarterly", "half_yearly": "every 6 months", "yearly": "yearly"}
MAX_OCCURRENCES = 52


class RecurrenceError(ValueError):
    pass


def _add_months(d: datetime, months: int) -> datetime:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def occurrences(start: datetime, end: datetime, tz: str, frequency: str, count: int) -> list[tuple[datetime, datetime]]:
    if frequency not in FREQUENCIES:
        raise RecurrenceError(f"Unknown frequency '{frequency}'")
    if not 2 <= count <= MAX_OCCURRENCES:
        raise RecurrenceError(f"A series has between 2 and {MAX_OCCURRENCES} occurrences")
    if end <= start:
        raise RecurrenceError("The end must be after the start")
    duration = end - start
    shortest_gap = timedelta(days=7) if frequency == "weekly" else timedelta(days=28 * MONTH_STEPS[frequency])
    if duration >= shortest_gap:
        raise RecurrenceError("Each occurrence must end before the next one starts")
    zone = ZoneInfo(tz)
    local = start.astimezone(zone).replace(tzinfo=None)   # wall-clock time in the test's zone
    out = []
    for i in range(count):
        wall = local + timedelta(weeks=i) if frequency == "weekly" else _add_months(local, MONTH_STEPS[frequency] * i)
        s = wall.replace(tzinfo=zone).astimezone(timezone.utc)
        out.append((s, s + duration))
    return out


def occurrence_title(title: str, start: datetime, tz: str, frequency: str) -> str:
    local = start.astimezone(ZoneInfo(tz))
    stamp = f"{local.day} {local:%b %Y}" if frequency == "weekly" else f"{local:%b %Y}"
    return f"{title} ({stamp})"
