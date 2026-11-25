"""Preferencias del usuario y utilidades de fechas/duraciones.

Todo se guarda como texto en SQLite para que sea fácil de migrar.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from typing import Iterable

WEEKDAY_NAMES = {
    "lun": 0,
    "lunes": 0,
    "mon": 0,
    "monday": 0,
    "mar": 1,
    "martes": 1,
    "tue": 1,
    "tuesday": 1,
    "mie": 2,
    "mié": 2,
    "miercoles": 2,
    "miércoles": 2,
    "wed": 2,
    "wednesday": 2,
    "jue": 3,
    "jueves": 3,
    "thu": 3,
    "thursday": 3,
    "vie": 4,
    "viernes": 4,
    "fri": 4,
    "friday": 4,
    "sab": 5,
    "sáb": 5,
    "sabado": 5,
    "sábado": 5,
    "sat": 5,
    "saturday": 5,
    "dom": 6,
    "domingo": 6,
    "sun": 6,
    "sunday": 6,
}

WEEKDAY_LABELS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
WEEKDAY_SHORT = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

DEFAULT_SETTINGS: dict[str, str] = {
    "reminders": "48h,24h,6h,2h,30m",
    "exam_mode": "on",
    "exam_reminders": "7d,3d,24h,6h,2h,30m",
    "digest": "08:00",
    "weekly": "domingo 18:00",
    "quiet": "00:00-06:00",
    "quiet_urgent": "on",
    "courses": "activos",
    "grades": "on",
    "announcements": "on",
    "week_load": "2",
    "class_reminder": "15m",
    "mute_until": "",
}

_DURATION_RE = re.compile(r"^(\d+)\s*(m|min|mins|minuto|minutos|h|hr|hrs|hora|horas|d|dia|dias|día|días)$", re.I)
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "si", "sí", "on", "yes", "y"}


def off(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"", "0", "false", "no", "off", "nunca", "none"}


def parse_duration(text: str) -> int | None:
    """Devuelve segundos o None si no es una duración válida."""
    match = _DURATION_RE.match(str(text or "").strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("m"):
        seconds = amount * 60
    elif unit.startswith("h"):
        seconds = amount * 3600
    else:
        seconds = amount * 86400
    if seconds < 60 or seconds > 30 * 86400:
        return None
    return seconds


def parse_duration_list(text: str) -> list[int] | None:
    """Convierte '48h, 24h, 2h' en segundos ordenados de mayor a menor."""
    raw = str(text or "").replace(";", ",")
    parts: list[str] = []
    for chunk in raw.split(","):
        parts.extend(piece for piece in chunk.split() if piece.strip())
    if not parts:
        return None
    values: set[int] = set()
    for part in parts:
        seconds = parse_duration(part)
        if seconds is None:
            return None
        values.add(seconds)
    if not values:
        return None
    return sorted(values, reverse=True)


def format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} día" + ("s" if days != 1 else "")
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} h"
    minutes = seconds // 60
    return f"{minutes} min"


def format_duration_list(values: Iterable[int]) -> str:
    ordered = sorted(set(values), reverse=True)
    return ",".join(_canonical_duration(value) for value in ordered)


def _canonical_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def parse_clock(text: str) -> time | None:
    match = _TIME_RE.match(str(text or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_quiet(text: str) -> tuple[time, time] | None:
    raw = str(text or "").strip()
    if off(raw):
        return None
    parts = re.split(r"\s*-\s*", raw, maxsplit=1)
    if len(parts) != 2:
        return None
    start, end = parse_clock(parts[0]), parse_clock(parts[1])
    if start is None or end is None or start == end:
        return None
    return start, end


def format_quiet(value: tuple[time, time] | None) -> str:
    if value is None:
        return "desactivado"
    start, end = value
    return f"{start:%H:%M}–{end:%H:%M}"


def parse_weekly(text: str) -> tuple[int, time] | None:
    raw = " ".join(str(text or "").strip().split())
    if off(raw):
        return None
    pieces = raw.split()
    if len(pieces) == 1 and parse_clock(pieces[0]):
        return 6, parse_clock(pieces[0])  # type: ignore[return-value]
    if len(pieces) != 2:
        return None
    weekday = WEEKDAY_NAMES.get(pieces[0].lower())
    clock = parse_clock(pieces[1])
    if weekday is None or clock is None:
        return None
    return weekday, clock


def format_weekly(value: tuple[int, time] | None) -> str:
    if value is None:
        return "desactivado"
    weekday, clock = value
    return f"{WEEKDAY_LABELS[weekday]} {clock:%H:%M}"


def parse_schedule_json(text: str | None) -> list[dict]:
    try:
        data = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            weekday = int(entry.get("weekday"))
        except (TypeError, ValueError):
            continue
        start = parse_clock(str(entry.get("start") or ""))
        end = parse_clock(str(entry.get("end") or ""))
        course = " ".join(str(entry.get("course") or "").split())
        if not (0 <= weekday <= 6) or start is None or end is None or not course:
            continue
        if end <= start:
            continue
        out.append(
            {
                "weekday": weekday,
                "start": f"{start:%H:%M}",
                "end": f"{end:%H:%M}",
                "course": course,
                "room": " ".join(str(entry.get("room") or "").split()),
            }
        )
    out.sort(key=lambda row: (row["weekday"], row["start"], row["course"].lower()))
    return out


def schedule_to_json(entries: Iterable[dict]) -> str:
    return json.dumps(list(entries), ensure_ascii=False, separators=(",", ":"))


def minutes_since_midnight(value: time | datetime) -> int:
    return value.hour * 60 + value.minute


def is_quiet_at(value: tuple[time, time] | None, now_local: datetime) -> bool:
    if value is None:
        return False
    start, end = value
    now_min = minutes_since_midnight(now_local)
    start_min = minutes_since_midnight(start)
    end_min = minutes_since_midnight(end)
    if start_min < end_min:
        return start_min <= now_min < end_min
    # Ventana que cruza medianoche (p. ej. 23:00–07:00).
    return now_min >= start_min or now_min < end_min


def is_within_window(start: time, now_local: datetime, hours: int) -> bool:
    """True si now_local está entre start y start + hours (sin cruzar medianoche)."""
    now_min = minutes_since_midnight(now_local)
    start_min = minutes_since_midnight(start)
    return start_min <= now_min < start_min + hours * 60


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def format_grade_value(entry) -> str:
    """Texto corto de una nota: 20/20, 5.1/20, —."""
    grade = str(getattr(entry, "grade", "") or "").strip()
    possible = getattr(entry, "possible", None)
    if grade:
        return grade
    score = getattr(entry, "score", None)
    if score is not None:
        if possible:
            return f"{score:g}/{possible:g}"
        return f"{score:g}"
    return "—"
