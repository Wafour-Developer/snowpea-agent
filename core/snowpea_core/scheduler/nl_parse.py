"""Schedule-spec parsing: cron, relative delays, intervals, ko/en language.

One function does the work.  :func:`parse_spec` turns whatever the user typed
into a :class:`Spec`, and :func:`next_run` turns a :class:`Spec` plus "now"
into the next firing time.  Everything else here is a private helper for one
of the four spellings a spec may take (M5 contract §2):

======================  ==========================================
kind                    examples
======================  ==========================================
``cron``                ``0 9 * * *``, ``매일 09:00``, ``every day at 9am``
``once``                ``in 60s``, ``10분 뒤``, ``tomorrow 10:00``
``interval``            ``every 10m``, ``매 30분``, ``10분마다``
======================  ==========================================

Clock times are read in the daemon's local timezone — "매일 09:00" means nine
in the morning where the user is — and every datetime that leaves this module
is timezone-aware UTC, which is what the store and the wire want.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from croniter import croniter

Kind = Literal["cron", "once", "interval"]

#: Seconds in one unit, for every spelling we accept on either side.
UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "초": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "분": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "시간": 3600,
    "d": 86_400,
    "day": 86_400,
    "days": 86_400,
    "일": 86_400,
    "w": 604_800,
    "week": 604_800,
    "weeks": 604_800,
    "주": 604_800,
}

#: Words that pin a bare hour to morning or afternoon.
MERIDIEM: dict[str, str] = {
    "am": "am",
    "a.m.": "am",
    "오전": "am",
    "아침": "am",
    "새벽": "am",
    "morning": "am",
    "pm": "pm",
    "p.m.": "pm",
    "오후": "pm",
    "저녁": "pm",
    "밤": "pm",
    "evening": "pm",
    "night": "pm",
    "afternoon": "pm",
}

#: cron day-of-week numbers; croniter reads 0 as Sunday.
WEEKDAYS: dict[str, int] = {
    "sunday": 0,
    "sun": 0,
    "일요일": 0,
    "monday": 1,
    "mon": 1,
    "월요일": 1,
    "tuesday": 2,
    "tue": 2,
    "화요일": 2,
    "wednesday": 3,
    "wed": 3,
    "수요일": 3,
    "thursday": 4,
    "thu": 4,
    "목요일": 4,
    "friday": 5,
    "fri": 5,
    "금요일": 5,
    "saturday": 6,
    "sat": 6,
    "토요일": 6,
}

_DURATION_TERM = re.compile(r"(\d+)\s*([a-z]+|[가-힣]+)")
_CRON_FIELD = re.compile(r"^[0-9*/,\-]+$|^(?:[a-z]{3}(?:-[a-z]{3})?)(?:,[a-z]{3})*$")
_HH_MM = re.compile(r"\b(\d{1,2})\s*:\s*(\d{2})\b")
_KO_CLOCK = re.compile(r"(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?")
_EN_CLOCK = re.compile(r"\b(\d{1,2})(?:\s*:\s*(\d{2}))?\s*(am|pm)\b")
_BARE_HOUR = re.compile(r"(?:^|\bat\s+)(\d{1,2})\b(?!\s*[:\d])")
_ISO_AT = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})\b")
_EVERY_N = re.compile(r"(?:every|each|매)\s*(\d+\s*(?:[a-z]+|[가-힣]+))")
_N_MADA = re.compile(r"(\d+\s*[가-힣]+)\s*마다")


@dataclass(frozen=True)
class Spec:
    """A parsed schedule: what kind it is and the one field that kind uses."""

    kind: Kind
    display: str
    cron: str | None = None
    interval_sec: int | None = None
    #: Firing time of a ``once`` spec, timezone-aware UTC.
    at: datetime | None = None

    def describe(self) -> str:
        if self.kind == "cron":
            return f"cron {self.cron}"
        if self.kind == "interval":
            return f"every {self.interval_sec}s"
        return f"once at {self.at.isoformat() if self.at else '?'}"


def _now() -> datetime:
    """Current time, timezone-aware, in the daemon's local zone."""
    return datetime.now().astimezone()


def _local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.astimezone()
    return moment.astimezone()


def parse_duration(text: str) -> int | None:
    """``"1h 30m"`` -> ``5400``; ``None`` when nothing in ``text`` is a duration."""
    total = 0
    found = False
    for amount, unit in _DURATION_TERM.findall(text.lower()):
        seconds = UNIT_SECONDS.get(unit)
        if seconds is None:
            continue
        total += int(amount) * seconds
        found = True
    return total if found and total > 0 else None


def _meridiem(text: str) -> str | None:
    for word, value in MERIDIEM.items():
        if word in text:
            return value
    return None


def _apply_meridiem(hour: int, text: str) -> int:
    marker = _meridiem(text)
    if marker == "pm" and hour < 12:
        return hour + 12
    if marker == "am" and hour == 12:
        return 0
    return hour


def parse_clock(text: str) -> tuple[int, int] | None:
    """``"아침 9시"`` -> ``(9, 0)``, ``"9:30pm"`` -> ``(21, 30)``; else ``None``."""
    lowered = text.lower()
    if "정오" in lowered or "noon" in lowered:
        return 12, 0
    if "자정" in lowered or "midnight" in lowered:
        return 0, 0
    match = _EN_CLOCK.search(lowered)
    if match:
        hour, minute, marker = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if marker == "pm" and hour < 12:
            hour += 12
        if marker == "am" and hour == 12:
            hour = 0
        return _valid_clock(hour, minute)
    match = _HH_MM.search(lowered)
    if match:
        return _valid_clock(_apply_meridiem(int(match.group(1)), lowered), int(match.group(2)))
    match = _KO_CLOCK.search(lowered)
    if match:
        hour = _apply_meridiem(int(match.group(1)), lowered)
        return _valid_clock(hour, int(match.group(2) or 0))
    match = _BARE_HOUR.search(lowered)
    if match:
        return _valid_clock(_apply_meridiem(int(match.group(1)), lowered), 0)
    return None


def _valid_clock(hour: int, minute: int) -> tuple[int, int] | None:
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def is_cron(text: str) -> bool:
    """True when ``text`` looks like a 5- or 6-field cron expression."""
    fields = text.lower().split()
    if len(fields) not in (5, 6):
        return False
    return all(_CRON_FIELD.match(field) for field in fields)


def _relative_seconds(text: str) -> int | None:
    """``"in 60s"``, ``"after 2h"``, ``"10분 뒤"`` -> seconds."""
    match = re.match(r"^(?:in|after)\s+(.+)$", text)
    if match:
        return parse_duration(match.group(1))
    match = re.match(r"^(.+?)\s*(?:뒤|후|이따|있다가)\s*(?:에)?$", text)
    if match:
        return parse_duration(match.group(1))
    return None


def _interval_seconds(text: str) -> int | None:
    """``"every 10m"``, ``"매 30분"``, ``"10분마다"`` -> seconds."""
    match = _N_MADA.search(text)
    if match:
        return parse_duration(match.group(1))
    match = _EVERY_N.search(text)
    if match:
        return parse_duration(match.group(1))
    return None


def _weekday(text: str) -> int | None:
    for word, number in WEEKDAYS.items():
        if word in text:
            return number
    return None


def _recurring_cron(text: str) -> str | None:
    """Daily / weekly / weekday phrases with a clock, as a cron expression."""
    daily = bool(re.search(r"매일|every\s+day|each\s+day|daily", text))
    weekly = bool(re.search(r"매주|every\s+week|weekly", text))
    weekdays = bool(re.search(r"평일|weekdays?|every\s+weekday", text))
    day = _weekday(text)
    if not (daily or weekly or weekdays or day is not None):
        return None
    clock = parse_clock(text)
    if clock is None:
        return None
    hour, minute = clock
    if weekdays:
        return f"{minute} {hour} * * 1-5"
    if day is not None:
        return f"{minute} {hour} * * {day}"
    if daily:
        return f"{minute} {hour} * * *"
    return None


def _one_shot(text: str, now: datetime) -> datetime | None:
    """``"tomorrow 10:00"``, ``"내일 10시"``, ``"at 9am"`` -> an absolute time."""
    match = _ISO_AT.search(text)
    if match:
        year, month, day, hour, minute = (int(part) for part in match.groups())
        return _local(now).replace(
            year=year,
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
    clock = parse_clock(text)
    if clock is None:
        return None
    hour, minute = clock
    base = _local(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if re.search(r"내일|tomorrow", text):
        return base + timedelta(days=1)
    if re.search(r"오늘|today", text):
        return base
    return base if base > _local(now) else base + timedelta(days=1)


def parse_spec(text: str, now: datetime | None = None) -> Spec:
    """Parse ``text`` into a :class:`Spec`; raises ``ValueError`` if it cannot.

    The order matters: an explicit cron expression wins over everything, then
    relative delays ("in 60s"), then recurring clock phrases ("매일 09:00"),
    then plain intervals ("every 10m"), then one-shot clock times.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("a schedule spec cannot be empty")
    moment = _local(now or _now())
    lowered = raw.lower()

    if is_cron(raw):
        try:
            croniter(raw)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid cron expression {raw!r}: {exc}") from exc
        return Spec(kind="cron", display=raw, cron=raw)

    seconds = _relative_seconds(lowered)
    if seconds is not None:
        return Spec(kind="once", display=raw, at=_utc(moment + timedelta(seconds=seconds)))

    cron = _recurring_cron(lowered)
    if cron is not None:
        return Spec(kind="cron", display=raw, cron=cron)

    seconds = _interval_seconds(lowered)
    if seconds is not None:
        return Spec(kind="interval", display=raw, interval_sec=seconds)

    at = _one_shot(lowered, moment)
    if at is not None:
        return Spec(kind="once", display=raw, at=_utc(at))

    raise ValueError(
        f"could not understand the schedule {text!r};"
        ' try a cron expression, "in 10m", "every 30m" or "매일 09:00"'
    )


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def next_run(spec: Spec, after: datetime | None = None) -> datetime | None:
    """The first firing strictly after ``after``, or ``None`` when there is none."""
    moment = _local(after or _now())
    if spec.kind == "cron" and spec.cron:
        return _utc(croniter(spec.cron, moment).get_next(datetime))
    if spec.kind == "interval" and spec.interval_sec:
        return _utc(moment + timedelta(seconds=spec.interval_sec))
    if spec.kind == "once" and spec.at is not None:
        return spec.at if spec.at > _utc(moment) else None
    return None


def first_run(spec: Spec, now: datetime | None = None) -> datetime | None:
    """When a freshly created job should first fire."""
    if spec.kind == "once":
        return spec.at
    return next_run(spec, now)


__all__ = [
    "MERIDIEM",
    "UNIT_SECONDS",
    "WEEKDAYS",
    "Kind",
    "Spec",
    "first_run",
    "is_cron",
    "next_run",
    "parse_clock",
    "parse_duration",
    "parse_spec",
]
