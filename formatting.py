"""Formato de mensajes para Telegram."""

from __future__ import annotations

import html
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Sequence

from bb_client import CalendarItem, Course

WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

EVENT_ICONS = {
    "assignment": "📝",
    "tarea": "📝",
    "entregable": "📝",
    "asignacion": "📝",
    "test": "🧪",
    "quiz": "🧪",
    "examen": "🧪",
    "evaluacion": "🧪",
    "parcial": "🧪",
    "discussion": "💬",
    "foro": "💬",
    "discusion": "💬",
    "journal": "📓",
    "diario": "📓",
    "survey": "📋",
    "encuesta": "📋",
    "scorm": "📦",
    "clase": "📅",
    "evento": "📅",
    "reunion": "📅",
    "horario": "📅",
    "office hours": "🕘",
    "hora de oficina": "🕘",
    "columna de calificacion manual": "📊",
}
TYPE_ICONS = {
    "Institution": "🏛️",
    "Course": "📅",
    "OfficeHours": "🕘",
    "Personal": "👤",
}
DEFAULT_ICON = "📌"


def _norm(text) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.lower().split())


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def item_icon(item: CalendarItem) -> str:
    event = _norm(item.event_type)
    if event:
        if event in EVENT_ICONS:
            return EVENT_ICONS[event]
        for hint, icon in EVENT_ICONS.items():
            if hint in event:
                return icon
    return TYPE_ICONS.get(item.type, DEFAULT_ICON)


def day_label(day: date, today: date) -> str:
    base = f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
    if day == today:
        return f"HOY · {base}"
    if day == today + timedelta(days=1):
        return f"MAÑANA · {base}"
    if day == today - timedelta(days=1):
        return f"AYER · {base}"
    return base


def relative_due(due: datetime, now: datetime) -> str:
    seconds = (due - now).total_seconds()
    if seconds < 0:
        seconds = -seconds
        if seconds < 3600:
            return f"vencida hace {max(1, int(seconds // 60))} min"
        if seconds < 86400:
            return f"vencida hace {int(seconds // 3600)} h"
        return f"vencida hace {int(seconds // 86400)} d"
    if seconds < 60:
        return "ahora"
    if seconds < 3600:
        return f"en {int(seconds // 60)} min"
    if seconds < 86400:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"en {hours} h" if minutes < 5 else f"en {hours} h {minutes} min"
    days = int(seconds // 86400)
    if days == 1:
        return "mañana"
    if days < 30:
        return f"en {days} días"
    months = days // 30
    return f"en {months} mes" + ("es" if months > 1 else "")


def _group_by_day(items: Iterable[CalendarItem], tz) -> list[tuple[date, list[CalendarItem]]]:
    groups: dict[date, list[CalendarItem]] = {}
    for item in sorted(items, key=lambda entry: entry.due):
        local_day = item.due.astimezone(tz).date()
        groups.setdefault(local_day, []).append(item)
    return sorted(groups.items())


def chunk_lines(lines: Sequence[str], limit: int = 3300) -> list[str]:
    if not lines:
        return []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = line[:limit] if len(line) > limit else line
        addition = len(line) + 1
        if current and size + addition > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def _range_suffix(phrase: str) -> str:
    """'hoy' -> 'para hoy'; el resto -> 'en <frase>'."""
    if phrase.strip().lower() == "hoy":
        return "para hoy"
    return f"en {esc(phrase)}"


def render_task_list(
    items: Sequence[CalendarItem],
    *,
    tz,
    now: datetime,
    range_label: str = "14 días",
    range_phrase: str | None = None,
) -> list[str]:
    phrase = range_phrase or range_label
    if not items:
        return [
            "✅ <b>Sin tareas</b>\n\n"
            f"No hay entregas con fecha {_range_suffix(phrase)}.\n"
            "Puedes ampliar con <code>/tareas 30</code>."
        ]

    today = now.astimezone(tz).date()
    lines: list[str] = [
        "📚 <b>Próximas tareas</b>",
        f"<i>{esc(phrase)} · {len(items)} pendiente{'s' if len(items) != 1 else ''}</i>",
    ]
    for day, day_items in _group_by_day(items, tz):
        lines.append("")
        lines.append(f"<b>{day_label(day, today)}</b>")
        for item in day_items:
            due_local = item.due.astimezone(tz)
            lines.append(
                f"• <b>{due_local:%H:%M}</b> {item_icon(item)} <b>{esc(item.title)}</b>"
            )
            details = [esc(item.course_name or "Sin curso")]
            details.append(f"<i>{relative_due(item.due, now)}</i>")
            lines.append("   " + " · ".join(details))
    lines.append("")
    lines.append("<i>💡 /tareas 30 · /hoy · /semana · /eventos</i>")
    return chunk_lines(lines)


def render_event_list(
    items: Sequence[CalendarItem],
    *,
    tz,
    now: datetime,
    range_label: str = "14 días",
    range_phrase: str | None = None,
) -> list[str]:
    phrase = range_phrase or range_label
    if not items:
        return [
            "✅ <b>Sin eventos</b>\n\n"
            f"No hay eventos ni clases {_range_suffix(phrase)}."
        ]

    today = now.astimezone(tz).date()
    lines: list[str] = [
        "📅 <b>Eventos y clases</b>",
        f"<i>{esc(phrase)} · {len(items)} evento{'s' if len(items) != 1 else ''}</i>",
    ]
    for day, day_items in _group_by_day(items, tz):
        lines.append("")
        lines.append(f"<b>{day_label(day, today)}</b>")
        for item in day_items:
            start_local = item.start.astimezone(tz)
            end_local = item.end.astimezone(tz)
            if end_local > start_local:
                when = f"{start_local:%H:%M}–{end_local:%H:%M}"
            else:
                when = f"{start_local:%H:%M}"
            lines.append(f"• <b>{when}</b> {item_icon(item)} <b>{esc(item.title)}</b>")
            extra = [esc(item.course_name or "Sin curso")]
            if item.location:
                extra.append(esc(item.location))
            lines.append("   " + " · ".join(extra))
    return chunk_lines(lines)


def render_courses(courses: Sequence[Course]) -> list[str]:
    if not courses:
        return ["📚 No encontré cursos en tu cuenta."]
    lines = [f"📚 <b>Tus cursos</b> · {len(courses)}", ""]
    for course in courses[:80]:
        lines.append(f"• {esc(course.name)}")
    if len(courses) > 80:
        lines.append(f"… y {len(courses) - 80} más")
    return chunk_lines(lines)


def local_day_bounds(now: datetime, tz) -> tuple[datetime, datetime]:
    local_now = now.astimezone(tz)
    start = datetime.combine(local_now.date(), time.min, tzinfo=tz).astimezone(
        timezone.utc
    )
    return start, start + timedelta(days=1)


def format_dt_short(value: str | None, tz) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).strftime("%d/%m %H:%M")
