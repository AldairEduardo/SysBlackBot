"""Pruebas locales sin red ni credenciales: python selftest.py"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import crypto_store
from bb_client import parse_calendar, parse_memberships
from formatting import chunk_lines, render_event_list, render_task_list
from storage import Storage


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    memberships = {
        "results": [
            {
                "courseId": "_1_1",
                "course": {"id": "_1_1", "name": "Programación de Software"},
                "courseRoleId": "Student",
            }
        ]
    }
    courses = parse_memberships(memberships)
    assert len(courses) == 1 and courses[0].name == "Programación de Software"

    # Forma real que devuelve senati.blackboard.com (Ultra 2026).
    payload = {
        "results": [
            {
                "itemSourceId": "_2_1",
                "itemSourceType": "blackboard.platform.gradebook2.GradableItem",
                "calendarId": "_1_1",
                "calendarNameLocalizable": {
                    "rawValue": "202620-PIAD-221-TEC-NRC_19983: PROGRAMACIÓN DE SOFTWARE"
                },
                "title": "Entrega 1",
                "startDate": "2026-11-20T04:59:00.000Z",
                "endDate": "2026-11-20T04:59:00.000Z",
                "dynamicCalendarItemProps": {
                    "eventType": "Examen",
                    "gradable": False,
                    "attemptable": True,
                    "handle": "resource/x-bb-assessment",
                },
            },
            {
                "itemSourceId": "_4_1",
                "itemSourceType": "blackboard.platform.gradebook2.GradableItem",
                "calendarId": "_1_1",
                "calendarNameLocalizable": {"rawValue": "Programación de Software"},
                "title": "Actitudes",
                "startDate": "2026-12-01T04:59:00.000Z",
                "endDate": "2026-12-01T04:59:00.000Z",
                "dynamicCalendarItemProps": {
                    "eventType": "Columna de calificación manual",
                    "attemptable": False,
                },
            },
            {
                "itemSourceId": "_3_1",
                "itemSourceType": "blackboard.platform.calendar.CalendarItem",
                "calendarId": "INSTITUTION",
                "calendarNameLocalizable": {"rawValue": "Institución"},
                "title": "Clase virtual",
                "startDate": "2026-11-19T14:00:00.000Z",
                "endDate": "2026-11-19T16:00:00.000Z",
            },
        ]
    }
    items = parse_calendar(payload, {c.id: c.name for c in courses})
    assert len(items) == 3
    task = next(item for item in items if item.id == "_2_1")
    manual = next(item for item in items if item.id == "_4_1")
    event = next(item for item in items if item.id == "_3_1")
    assert task.is_task and not task.is_event
    assert task.course_name == "Programación de Software"
    assert task.event_type == "Examen" and task.handle.endswith("assessment")
    assert not manual.is_task and not manual.is_event
    assert event.is_event and not event.is_task
    assert event.course_name == "Institución"

    # Compatibilidad con la forma antigua (type/id/start/end).
    legacy = parse_calendar(
        {
            "results": [
                {
                    "id": "_5_1",
                    "type": "GradebookColumn",
                    "calendarId": "_1_1",
                    "calendarName": "Programación de Software",
                    "title": "Tarea antigua",
                    "start": "2026-11-21T04:00:00.000Z",
                    "end": "2026-11-21T04:00:00.000Z",
                    "dynamicCalendarItemProps": {"eventType": "Assignment"},
                }
            ]
        },
        {c.id: c.name for c in courses},
    )
    assert len(legacy) == 1 and legacy[0].is_task

    from formatting import item_icon

    assert item_icon(task) == "🧪"
    assert item_icon(manual) == "📊"

    tz = ZoneInfo("America/Lima")
    now = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    chunks = render_task_list(
        [task], tz=tz, now=now, range_label="14 días", range_phrase="los próximos 14 días"
    )
    text = "\n".join(chunks)
    assert "Entrega 1" in text and "Programación" in text

    events = render_event_list([event], tz=tz, now=now)
    assert "Clase virtual" in "\n".join(events)
    assert "para hoy" in render_task_list(
        [], tz=tz, now=now, range_phrase="hoy"
    )[0]
    assert "para hoy" in render_event_list(
        [], tz=tz, now=now, range_phrase="hoy"
    )[0]

    big = chunk_lines(["x" * 200] * 40)
    assert len(big) > 1 and all(len(chunk) <= 3300 for chunk in big)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        key_path = tmp_path / "secret.key"
        blob = crypto_store.encrypt_json(key_path, {"password": "secreto", "n": 1})
        data = crypto_store.decrypt_json(key_path, blob)
        assert data["password"] == "secreto" and data["n"] == 1

        store = Storage(tmp_path / "bot.db")
        try:
            assert store.set_owner(1) == 1
            assert store.set_owner(2) == 1  # no debe reemplazar al dueno
            assert store.owner_id() == 1
            store.save_user(1, "alumno@senati.pe", "_5702_1")
            store.set_credentials(1, blob)
            assert store.has_credentials(1)
            store.mark_items_seen(1, [task])
            assert store.seen_count(1) == 1
            assert store.diff_new_items(1, [task]) == []
            new_items = store.diff_new_items(1, items)
            assert len(new_items) == 2
            assert {row["id"] for row in new_items} == {"_3_1", "_4_1"}
            assert store.claim_reminder(1, "_2_1", "24h") is True
            assert store.claim_reminder(1, "_2_1", "24h") is False
            store.set_meta("first_check_done:1", "1")
            assert store.get_meta("first_check_done:1") == "1"
            store.clear_credentials(1)
            assert not store.has_credentials(1)
        finally:
            store.close()

    print("OK: parsing, formato, cifrado, base de datos y recordatorios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
