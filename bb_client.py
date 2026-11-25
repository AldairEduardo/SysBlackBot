"""Cliente de Blackboard Learn (Ultra) para SENATI.

- Login por SAML contra Microsoft Entra ID usando un navegador real (Playwright).
- Persistencia de cookies de sesion cifradas con la clave local.
- Consultas a los endpoints internos de Ultra con esas cookies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx

import crypto_store
from config import PROVIDER_BY_DOMAIN, Settings

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ProgressCallback = Callable[[str], Awaitable[None]]


class BlackboardError(RuntimeError):
    """Error generico hablando con Blackboard."""


class SessionExpired(BlackboardError):
    """La sesion guardada ya no es valida."""


class LoginError(BlackboardError):
    def __init__(
        self, message: str, screenshot: Path | None = None, stage: str = "unknown"
    ):
        super().__init__(message)
        self.screenshot = screenshot
        self.stage = stage


class MFARequired(LoginError):
    """Microsoft o Blackboard pidieron un segundo factor."""


@dataclass(frozen=True)
class LoginResult:
    cookies: list[dict]
    provider_apid: str


# --------------------------------------------------------------------------
#  Parseo (funciones puras, testables sin red)
# --------------------------------------------------------------------------


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    role: str = ""
    display_id: str = ""
    term_id: str = ""
    term_name: str = ""
    start_date: datetime | None = None
    end_date: datetime | None = None
    term_start_date: datetime | None = None
    term_end_date: datetime | None = None
    is_closed: bool = False


def course_is_active(course: Course, now: datetime | None = None, grace_days: int = 0) -> bool:
    """Curso no cerrado y cuya fecha final aún no pasó (con margen opcional)."""
    if course.is_closed:
        return False
    if course.end_date is None:
        return True
    current = now or datetime.now(timezone.utc)
    return course.end_date >= current - timedelta(days=grace_days)


def current_term_id(courses: list[Course]) -> str:
    """Elige el término más reciente de las inscripciones."""
    terms: dict[str, tuple[datetime, int]] = {}
    for course in courses:
        if not course.term_id:
            continue
        end = course.term_end_date or course.end_date or datetime.min.replace(tzinfo=timezone.utc)
        previous = terms.get(course.term_id)
        if previous is None or end > previous[0]:
            terms[course.term_id] = (end, (previous[1] if previous else 0) + 1)
        else:
            terms[course.term_id] = (previous[0], previous[1] + 1)
    if not terms:
        return ""
    return max(terms.items(), key=lambda item: (item[1][0], item[1][1]))[0]


def current_term_name(courses: list[Course]) -> str:
    term_id = current_term_id(courses)
    for course in courses:
        if course.term_id == term_id and course.term_name:
            return course.term_name
    return ""


@dataclass(frozen=True)
class GradeEntry:
    course_id: str
    column_id: str
    status: str = ""
    grade: str = ""
    score: float | None = None
    possible: float | None = None
    effective_score: float | None = None
    is_exempt: bool = False
    reviewed: bool = False
    grade_id: str = ""
    has_attempts: bool = False
    column_name: str = ""
    column_due: datetime | None = None
    column_scorable: bool = True
    column_external: bool = False

    @property
    def has_grade(self) -> bool:
        return bool(self.grade or self.score is not None)

    @property
    def submitted(self) -> bool:
        status = self.status.upper()
        return self.has_attempts or status in {"GRADED", "NEEDS_GRADING", "IN_PROGRESS"}

    @property
    def signature(self) -> str:
        return "|".join(
            [
                self.status,
                self.grade,
                f"{self.score}" if self.score is not None else "",
                f"{self.effective_score}" if self.effective_score is not None else "",
                "1" if self.is_exempt else "0",
                "1" if self.reviewed else "0",
                self.grade_id,
            ]
        )


@dataclass(frozen=True)
class GradeColumn:
    id: str
    course_id: str
    name: str
    possible: float | None = None
    due: datetime | None = None
    scorable: bool = True
    external: bool = False
    visible: bool = True
    attempt_based: bool = False
    grades_released: bool = True
    position: int = 0


@dataclass(frozen=True)
class Announcement:
    id: str
    title: str
    course_id: str = ""
    course_name: str = ""
    body: str = ""
    created: datetime | None = None
    url: str = ""
    kind: str = ""


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")


def html_to_text(value) -> str:
    """Convierte el HTML de Blackboard en texto plano compacto."""
    raw = str(value or "")
    if not raw:
        return ""
    parser = _HTMLText()
    try:
        parser.feed(raw)
        text = unescape(" ".join(parser.parts))
    except Exception:
        text = unescape(raw)
    return " ".join(text.replace("\xa0", " ").split())


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CalendarItem:
    id: str
    type: str
    title: str
    course_id: str
    course_name: str
    start: datetime
    end: datetime
    event_type: str = ""
    description: str = ""
    location: str = ""
    attemptable: bool = False
    gradable: bool = False
    handle: str = ""

    @property
    def due(self) -> datetime:
        return self.end or self.start

    @property
    def is_grade_item(self) -> bool:
        """True para columnas del libro de calificaciones (tareas/examenes)."""
        source = self.type.lower()
        return "gradableitem" in source or "gradebookcolumn" in source

    @property
    def is_task(self) -> bool:
        if not self.is_grade_item:
            return False
        if self.attemptable or self.handle:
            return True
        # Servidores antiguos no mandan attemptable/handle: usar eventType.
        event = self.event_type.strip().lower()
        return bool(event) and "manual" not in event and "column" not in event

    @property
    def is_event(self) -> bool:
        """Clases, eventos institucionales y personales (no columnas de notas)."""
        return not self.is_grade_item


def parse_memberships(payload) -> list[Course]:
    results = _results(payload)
    courses: list[Course] = []
    seen: set[str] = set()
    for membership in results:
        if not isinstance(membership, dict):
            continue
        course = membership.get("course") or {}
        course_id = str(
            membership.get("courseId") or course.get("id") or ""
        ).strip()
        if not course_id or course_id in seen:
            continue
        name = (
            course.get("name")
            or course.get("displayName")
            or membership.get("courseName")
            or course_id
        )
        role = membership.get("courseRoleId") or membership.get("role") or ""
        term = course.get("term") or {}
        courses.append(
            Course(
                id=course_id,
                name=str(name).strip(),
                role=str(role),
                display_id=str(course.get("displayId") or "").strip(),
                term_id=str(course.get("termId") or term.get("id") or "").strip(),
                term_name=str(term.get("name") or "").strip(),
                start_date=parse_dt(course.get("startDate")),
                end_date=parse_dt(course.get("endDate")),
                term_start_date=parse_dt(term.get("startDate")),
                term_end_date=parse_dt(term.get("endDate")),
                is_closed=bool(course.get("isClosed")),
            )
        )
        seen.add(course_id)
    return courses


def _results(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "calendarItems"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def parse_my_courses(payload) -> list[Course]:
    results = _results(payload)
    courses: list[Course] = []
    seen: set[str] = set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        course_id = str(entry.get("id") or entry.get("courseId") or "").strip()
        if not course_id or course_id in seen:
            continue
        name = entry.get("name") or entry.get("displayName") or course_id
        courses.append(Course(id=course_id, name=str(name).strip()))
        seen.add(course_id)
    return courses


def parse_grades(payload, course_id: str = "") -> list[GradeEntry]:
    entries: list[GradeEntry] = []
    for raw in _results(payload):
        if not isinstance(raw, dict):
            continue
        column_id = str(raw.get("columnId") or raw.get("id") or "").strip()
        if not column_id:
            continue
        display = raw.get("displayGrade") or {}
        if not isinstance(display, dict):
            display = {}
        grade = str(display.get("grade") or raw.get("manualGrade") or "").strip()
        score = _float(display.get("score"))
        if score is None:
            score = _float(raw.get("effectiveScore"))
        possible = _float(raw.get("pointsPossible"))
        effective = _float(raw.get("effectiveScore"))
        attempts = any(
            raw.get(key)
            for key in (
                "firstAttemptId",
                "lastAttemptId",
                "highestAttemptId",
                "lowestAttemptId",
            )
        )
        entries.append(
            GradeEntry(
                course_id=str(raw.get("courseId") or course_id),
                column_id=column_id,
                status=str(raw.get("status") or "").strip(),
                grade=grade,
                score=score,
                possible=possible,
                effective_score=effective,
                is_exempt=bool(raw.get("isExempt")),
                reviewed=bool(raw.get("studentReviewedTimestamp")),
                grade_id=str(raw.get("id") or ""),
                has_attempts=attempts,
            )
        )
    return entries


def parse_grade_columns(payload, course_id: str = "") -> list[GradeColumn]:
    columns: list[GradeColumn] = []
    for raw in _results(payload):
        if not isinstance(raw, dict):
            continue
        column_id = str(raw.get("id") or "").strip()
        if not column_id:
            continue
        columns.append(
            GradeColumn(
                id=column_id,
                course_id=str(raw.get("courseId") or course_id),
                name=str(raw.get("columnName") or raw.get("effectiveColumnName") or column_id),
                possible=_float(raw.get("possible")),
                due=parse_dt(raw.get("dueDate")),
                scorable=bool(raw.get("scorable", True)),
                external=bool(raw.get("externalGrade")),
                visible=bool(raw.get("visible", True)),
                attempt_based=bool(raw.get("isAttemptBased")),
                grades_released=bool(raw.get("gradesReleased", True)),
                position=int(raw.get("position") or 0),
            )
        )
    return columns


def parse_announcements(
    payload, course_names: dict[str, str] | None = None
) -> list[Announcement]:
    names = course_names or {}
    announcements: list[Announcement] = []
    seen: set[str] = set()
    for raw in _results(payload):
        if not isinstance(raw, dict):
            continue
        announcement_id = str(raw.get("id") or "").strip()
        if not announcement_id or announcement_id in seen:
            continue
        body = raw.get("body") or {}
        if isinstance(body, dict):
            body_text = html_to_text(body.get("displayText") or body.get("rawText"))
        else:
            body_text = html_to_text(body)
        course_id = str(raw.get("courseId") or "").strip()
        announcements.append(
            Announcement(
                id=announcement_id,
                title=str(raw.get("title") or "(sin título)").strip(),
                course_id=course_id,
                course_name=names.get(course_id, "Institución" if course_id in {"", "_1_1"} else ""),
                body=body_text,
                created=parse_dt(raw.get("modifiedDate") or raw.get("startDateRestriction")),
                url=str(raw.get("url") or "").strip(),
                kind=str(raw.get("type") or "").strip(),
            )
        )
        seen.add(announcement_id)
    announcements.sort(key=lambda item: item.created or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return announcements


def _localized_name(raw: dict) -> str:
    """Extrae el nombre visible de calendarNameLocalizable / calendarName."""
    localizable = raw.get("calendarNameLocalizable")
    if isinstance(localizable, dict):
        for key in ("rawValue", "value", "name"):
            value = localizable.get(key)
            if value:
                return str(value).strip()
    elif isinstance(localizable, str) and localizable.strip():
        return localizable.strip()
    return str(raw.get("calendarName") or "").strip()


def _calendar_fallback(calendar_id: str) -> str:
    return {
        "INSTITUTION": "Institución",
        "PERSONAL": "Personal",
    }.get(calendar_id, "Sin curso")


def parse_calendar(payload, course_names: dict[str, str] | None = None) -> list[CalendarItem]:
    names = course_names or {}
    items: list[CalendarItem] = []
    seen: set[str] = set()
    for raw in _results(payload):
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or raw.get("itemSourceId") or "").strip()
        if not item_id or item_id in seen:
            continue
        dynamic = raw.get("dynamicCalendarItemProps") or {}
        start = parse_dt(raw.get("startDate") or raw.get("start"))
        end = parse_dt(raw.get("endDate") or raw.get("end")) or start
        if not start:
            start = end
        if not start:
            continue
        item_type = str(raw.get("itemSourceType") or raw.get("type") or "")
        calendar_id = str(
            raw.get("calendarId") or raw.get("courseId") or ""
        ).strip()
        calendar_name = _localized_name(raw)
        course_name = (
            names.get(calendar_id)
            or calendar_name
            or _calendar_fallback(calendar_id)
        )
        items.append(
            CalendarItem(
                id=item_id,
                type=item_type,
                title=str(raw.get("title") or "(sin título)").strip(),
                course_id=calendar_id,
                course_name=course_name,
                start=start,
                end=end or start,
                event_type=str(dynamic.get("eventType") or ""),
                description=str(raw.get("description") or ""),
                location=str(raw.get("location") or ""),
                attemptable=bool(dynamic.get("attemptable")),
                gradable=bool(dynamic.get("gradable")),
                handle=str(dynamic.get("handle") or raw.get("handle") or ""),
            )
        )
        seen.add(item_id)
    items.sort(key=lambda item: item.due)
    return items


# --------------------------------------------------------------------------
#  Sesion HTTP con cookies de Blackboard
# --------------------------------------------------------------------------


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


class BlackboardSession:
    def __init__(self, base_url: str, session_path: Path, key_path: Path):
        self.base_url = base_url.rstrip("/")
        self.session_path = Path(session_path)
        self.key_path = Path(key_path)
        self._cookies: list[dict] = self._load_cookies()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------- cookies
    def _load_cookies(self) -> list[dict]:
        if not self.session_path.exists():
            return []
        try:
            blob = self.session_path.read_bytes()
            doc = crypto_store.decrypt_json(self.key_path, blob)
            cookies = doc.get("cookies") or []
            return [c for c in cookies if isinstance(c, dict) and c.get("name")]
        except Exception as exc:
            log.warning("No pude leer la sesion guardada: %s", exc)
            return []

    def has_saved_session(self) -> bool:
        return bool(self._cookies)

    def save_cookies(self, cookies: list[dict]) -> None:
        keep = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
        cleaned = [
            {k: c.get(k) for k in keep if c.get(k) is not None}
            for c in cookies
            if c.get("name")
        ]
        doc = {
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_url": self.base_url,
            "cookies": cleaned,
        }
        blob = crypto_store.encrypt_json(self.key_path, doc)
        _atomic_write(self.session_path, blob)
        self._cookies = cleaned
        self._reset_client()

    async def reset(self) -> None:
        self._cookies = []
        await self._reset_client()
        try:
            self.session_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------- cliente
    def _cookie_jar(self) -> httpx.Cookies:
        jar = httpx.Cookies()
        default_domain = urlparse(self.base_url).hostname or ""
        for cookie in self._cookies:
            domain = cookie.get("domain") or default_domain
            path = cookie.get("path") or "/"
            try:
                jar.set(cookie["name"], cookie["value"], domain=domain, path=path)
            except Exception:
                try:
                    jar.set(cookie["name"], cookie["value"])
                except Exception:
                    pass
        return jar

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
                    "Referer": self.base_url + "/ultra",
                    "X-Requested-With": "XMLHttpRequest",
                },
                cookies=self._cookie_jar(),
                timeout=httpx.Timeout(30.0, connect=15.0),
                follow_redirects=False,
            )
        return self._client

    async def _reset_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def close(self) -> None:
        await self._reset_client()

    # ------------------------------------------------------------- requests
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BlackboardError(f"No pude conectar con Blackboard: {exc}") from exc

        if response.status_code in (301, 302, 303, 307, 308):
            raise SessionExpired("La sesion de Blackboard expiro (redireccion al login).")
        if response.status_code == 401:
            raise SessionExpired("La sesion de Blackboard expiro (HTTP 401).")
        if response.status_code == 403:
            body = response.text[:400].lower()
            if ("sesión" in body or "sesion" in body) and (
                "no está asociada" in body
                or "no esta asociada" in body
                or "not associated" in body
                or "invalid" in body
            ):
                raise SessionExpired(
                    "La sesion de Blackboard expiro (HTTP 403)."
                )
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            raise BlackboardError(f"Blackboard respondio HTTP {response.status_code}: {body}")
        if "text/html" in response.headers.get("content-type", ""):
            raise SessionExpired("Blackboard devolvio una pagina de login en vez de datos.")
        return response

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        response = await self._request("GET", path, params=params)
        try:
            data = response.json()
        except ValueError as exc:
            raise BlackboardError("Blackboard devolvio una respuesta que no es JSON.") from exc
        if not isinstance(data, dict):
            raise BlackboardError("Respuesta inesperada de Blackboard.")
        return data

    # ------------------------------------------------------------- API Ultra
    async def fetch_courses(self) -> list[Course]:
        base_params = {
            "expand": "course.effectiveAvailability,course.permissions,courseRole",
            "includeCount": "true",
            "limit": 100,
            "offset": 0,
        }
        raw_results: list[dict] = []
        try:
            for _ in range(5):
                base_params["offset"] = len(raw_results)
                payload = await self._get_json(
                    "/learn/api/v1/users/me/memberships", base_params
                )
                page = payload.get("results") or []
                raw_results.extend(page)
                if len(page) < 100:
                    break
        except SessionExpired:
            raise
        except BlackboardError as exc:
            log.warning("memberships con expand fallo (%s); reintento simple", exc)
            raw_results = []
            simple = {"limit": 100, "offset": 0}
            for _ in range(5):
                simple["offset"] = len(raw_results)
                payload = await self._get_json(
                    "/learn/api/v1/users/me/memberships", simple
                )
                page = payload.get("results") or []
                raw_results.extend(page)
                if len(page) < 100:
                    break

        courses = parse_memberships({"results": raw_results})
        if courses:
            return courses

        payload = await self._get_json(
            "/learn/api/v1/users/me/courses", {"limit": 100, "offset": 0}
        )
        return parse_my_courses(payload)

    async def fetch_calendar_items(
        self,
        since: datetime,
        until: datetime,
        courses: list[Course] | None = None,
    ) -> list[CalendarItem]:
        params = {"since": iso_z(since), "until": iso_z(until)}
        names = {course.id: course.name for course in (courses or [])}
        payload = await self._get_json(
            "/learn/api/v1/calendars/calendarItems", params
        )
        items = parse_calendar(payload, names)
        raw_count = len(payload.get("results") or [])
        if raw_count and not items:
            raise BlackboardError(
                "Blackboard devolvió datos del calendario en un formato "
                "desconocido. Usa /debug para revisar la respuesta."
            )
        log.info(
            "Calendario: %s elementos entre %s y %s",
            len(items),
            params["since"],
            params["until"],
        )
        return items

    async def fetch_me(self, force: bool = False) -> dict:
        if not force and self._me is not None:
            return self._me
        payload = await self._get_json("/learn/api/v1/users/me")
        self._me = payload
        return payload

    async def fetch_course_grades(
        self, course_id: str, user_id: str | None = None
    ) -> list[GradeEntry]:
        uid = user_id or str((await self.fetch_me()).get("id") or "")
        if not uid:
            return []
        try:
            payload = await self._get_json(
                f"/learn/api/v1/courses/{quote(course_id, safe='')}/gradebook/grades",
                {"userId": uid, "limit": 200},
            )
        except SessionExpired:
            raise
        except BlackboardError as exc:
            log.warning("No pude leer notas de %s: %s", course_id, exc)
            return []
        return parse_grades(payload, course_id)

    async def fetch_grade_columns(self, course_id: str) -> list[GradeColumn]:
        try:
            payload = await self._get_json(
                f"/learn/api/v1/courses/{quote(course_id, safe='')}/gradebook/columns",
                {"limit": 200},
            )
        except SessionExpired:
            raise
        except BlackboardError as exc:
            log.warning("No pude leer columnas de %s: %s", course_id, exc)
            return []
        return parse_grade_columns(payload, course_id)

    async def fetch_all_grades(
        self, courses: list[Course], concurrency: int = 5
    ) -> dict[str, list[GradeEntry]]:
        uid = str((await self.fetch_me()).get("id") or "")
        if not uid or not courses:
            return {}
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(course: Course):
            async with semaphore:
                return course.id, await self.fetch_course_grades(course.id, uid)

        results = await asyncio.gather(*(one(course) for course in courses))
        return {course_id: entries for course_id, entries in results}

    async def fetch_all_grade_columns(
        self, courses: list[Course], concurrency: int = 5
    ) -> dict[str, list[GradeColumn]]:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(course: Course):
            async with semaphore:
                return course.id, await self.fetch_grade_columns(course.id)

        results = await asyncio.gather(*(one(course) for course in courses))
        return {course_id: columns for course_id, columns in results}

    async def fetch_announcements(
        self, course_names: dict[str, str] | None = None, limit: int = 200
    ) -> list[Announcement]:
        collected: list[dict] = []
        offset = 0
        while len(collected) < limit:
            page_size = min(100, limit - len(collected))
            payload = await self._get_json(
                "/learn/api/v1/announcements",
                {"limit": page_size, "offset": offset},
            )
            page = payload.get("results") or []
            if not isinstance(page, list):
                break
            collected.extend(item for item in page if isinstance(item, dict))
            if len(page) < page_size:
                break
            offset += len(page)
        return parse_announcements({"results": collected}, course_names)

    async def probe(self, path: str, params: dict | None = None) -> tuple[str, str]:
        """Diagnostico: devuelve (estado, resumen) sin lanzar excepciones."""
        try:
            client = await self._get_client()
            response = await client.get(path, params=params)
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    data = response.json()
                except ValueError:
                    return str(response.status_code), "JSON invalido"
                if isinstance(data, dict):
                    results = data.get("results")
                    if isinstance(results, list):
                        return str(response.status_code), f"JSON, {len(results)} resultados"
                    keys = ", ".join(sorted(data)[:5]) or "vacio"
                    return str(response.status_code), f"JSON, claves: {keys}"
                if isinstance(data, list):
                    return str(response.status_code), f"JSON, {len(data)} elementos"
                return str(response.status_code), "JSON"
            return str(response.status_code), content_type or "sin content-type"
        except Exception as exc:
            return "ERR", str(exc)[:120]


# --------------------------------------------------------------------------
#  Login con Microsoft (SAML) usando Playwright
# --------------------------------------------------------------------------

_PASSWORD_SELECTOR = 'input[type="password"], input[name="passwd"]'
_EMAIL_SELECTOR = 'input[name="loginfmt"], input[type="email"]'
_SUBMIT_SELECTORS = ("#idSIButton9", "button[type='submit']", "input[type='submit']")
_ERROR_SELECTORS = (
    "#usernameError",
    "#passwordError",
    "#idErrorText",
    "#errorText",
    "div[role='alert']",
)
_MFA_SELECTORS = (
    "#idDiv_SAOTCAS_Description",
    "#idDiv_SAOTCS_Description",
    "#idRichContext_DisplaySign",
    "input[name='otc']",
    "#idTxtBx_SAOTCC_OTC",
)
_ACCOUNT_ERRORS = (
    "aadsts50020",
    "aadsts700016",
    "aadsts50034",
    "doesn't exist in the directory",
    "does not exist in the directory",
    "no existe en el directorio",
    "no encontramos una cuenta",
    "couldn't find an account",
    "could not find an account",
)
_MFA_HINTS = (
    "verificacion en dos pasos",
    "verificación en dos pasos",
    "autenticador",
    "authenticator",
    "aprobar la solicitud",
    "two-step verification",
    "verify your identity",
    "ayudanos a proteger",
    "ayúdanos a proteger",
    "protege tu cuenta",
    "we need to verify",
)


async def _progress(callback: ProgressCallback | None, text_value: str) -> None:
    if callback is None:
        return
    try:
        await callback(text_value)
    except Exception:
        log.debug("No se pudo actualizar el progreso", exc_info=True)


async def _launch_browser(playwright, settings: Settings):
    args = ["--disable-blink-features=AutomationControlled"]
    kwargs = {"headless": settings.headless, "args": args}
    if settings.browser_channel:
        try:
            return await playwright.chromium.launch(
                channel=settings.browser_channel, **kwargs
            )
        except Exception as exc:
            log.warning(
                "No pude abrir el navegador %s (%s). Probando Chromium de Playwright.",
                settings.browser_channel,
                exc,
            )
    try:
        return await playwright.chromium.launch(**kwargs)
    except Exception as exc:
        raise LoginError(
            "No pude abrir ningun navegador. Ejecuta: "
            ".venv\\Scripts\\playwright install chromium"
        ) from exc


async def _click_any(page, selectors) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


async def _element_visible(locator) -> bool:
    """Visibilidad real: Microsoft deja campos ocultos con opacity: 0."""
    try:
        if not await locator.count() or not await locator.is_visible():
            return False
        box = await locator.bounding_box()
        if not box or box["width"] < 4 or box["height"] < 4:
            return False
        opacity = await locator.evaluate(
            "el => parseFloat(getComputedStyle(el).opacity)"
        )
        return opacity >= 0.1
    except Exception:
        return False


async def _visible(page, selector: str) -> bool:
    return await _element_visible(page.locator(selector).first)


async def _collect_error(page) -> str:
    texts: list[str] = []
    for selector in _ERROR_SELECTORS:
        locator = page.locator(selector)
        try:
            for index in range(await locator.count()):
                element = locator.nth(index)
                if not await _element_visible(element):
                    continue
                text = " ".join((await element.inner_text()).split())
                if text and text not in texts:
                    texts.append(text)
        except Exception:
            continue
    if not texts:
        return ""
    joined = " | ".join(texts)
    low = joined.lower()
    keywords = (
        "incorrect",
        "no es correct",
        "no son correct",
        "no encontramos",
        "aadsts",
        "bloqueada",
        "bloqueado",
        "locked",
        "denegado",
        "denied",
        "expired",
        "caducado",
        "error",
        "usuario o la contrasena",
        "usuario o la contraseña",
    )
    return joined[:400] if any(word in low for word in keywords) else ""


async def _click_kmsi_no(page) -> bool:
    """Pulsa 'No' en 'mantener la sesion iniciada' solo si es esa pagina."""
    if not await _visible(page, "#idBtn_Back"):
        return False
    try:
        text = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return False
    hints = (
        "mantener la sesión iniciada",
        "mantener la sesion iniciada",
        "stay signed in",
        "kmsi",
    )
    if not any(hint in text for hint in hints):
        return False
    try:
        await page.locator("#idBtn_Back").first.click(timeout=5000)
        await page.wait_for_timeout(800)
        return True
    except Exception:
        return False


async def _needs_mfa(page) -> bool:
    url = page.url.lower()
    if any(part in url for part in ("/sas/", "convergence", "fido", "/mfa", "proofup")):
        return True
    for selector in _MFA_SELECTORS:
        if await _visible(page, selector):
            return True
    try:
        body = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return False
    return any(hint in body for hint in _MFA_HINTS)


async def _screenshot_and_html(page, settings: Settings) -> Path | None:
    screenshot = settings.debug_dir / "login-error.png"
    try:
        await page.screenshot(path=str(screenshot), full_page=True)
        (settings.debug_dir / "login-error.html").write_text(
            await page.content(), encoding="utf-8", errors="replace"
        )
        return screenshot
    except Exception:
        log.debug("No pude guardar la captura del error", exc_info=True)
        return None


async def _login_once(
    settings: Settings,
    email: str,
    password: str,
    provider_apid: str,
    progress: ProgressCallback | None,
) -> list[dict]:
    from playwright.async_api import async_playwright

    base = settings.base_url
    redirect = quote(base + "/ultra", safe="")
    start_url = (
        f"{base}/auth-saml/saml/login?apId={provider_apid}&redirectUrl={redirect}"
    )

    await _progress(progress, "🌐 Abriendo el navegador...")

    async with async_playwright() as playwright:
        browser = None
        context = None
        page = None
        try:
            browser = await _launch_browser(playwright, settings)
            context = await browser.new_context(
                locale="es-PE",
                timezone_id=settings.timezone,
                viewport={"width": 1280, "height": 900},
                user_agent=USER_AGENT,
            )
            page = await context.new_page()
            page.set_default_timeout(20_000)

            await _progress(progress, "🔑 Abriendo el login de Microsoft...")
            await page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)

            # --- correo / usuario ---
            try:
                await page.wait_for_selector(_EMAIL_SELECTOR, timeout=45_000, state="visible")
            except Exception as exc:
                mfa = await _needs_mfa(page)
                shot = await _screenshot_and_html(page, settings)
                if mfa:
                    raise MFARequired(
                        "Microsoft pidio verificacion adicional antes del correo.",
                        screenshot=shot,
                    ) from exc
                raise LoginError(
                    "No aparecio el formulario de correo de Microsoft. "
                    "Puede que el proveedor SAML haya cambiado.",
                    screenshot=shot,
                    stage="start",
                ) from exc

            await page.locator(_EMAIL_SELECTOR).first.fill(email)
            await _click_any(page, _SUBMIT_SELECTORS)

            # --- contrasena ---
            await _progress(progress, "🔒 Enviando credenciales a Microsoft...")
            deadline = time.monotonic() + 90
            password_seen = False
            while time.monotonic() < deadline:
                error = await _collect_error(page)
                if error:
                    shot = await _screenshot_and_html(page, settings)
                    raise LoginError(error, screenshot=shot, stage="email")
                if await _visible(page, _PASSWORD_SELECTOR):
                    password_seen = True
                    break
                if await _needs_mfa(page):
                    shot = await _screenshot_and_html(page, settings)
                    raise MFARequired(
                        "Microsoft pidio verificacion en dos pasos (MFA).",
                        screenshot=shot,
                    )
                await page.wait_for_timeout(400)

            if not password_seen:
                shot = await _screenshot_and_html(page, settings)
                raise LoginError(
                    "Microsoft no mostro el campo de contrasena.",
                    screenshot=shot,
                    stage="password",
                )

            await page.locator(_PASSWORD_SELECTOR).first.fill(password)
            await _click_any(page, _SUBMIT_SELECTORS)
            password = ""  # no mantener la contrasena en memoria

            # --- "mantener sesion iniciada", MFA, errores y vuelta a Blackboard ---
            await _progress(progress, "✅ Verificando la sesion...")
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                url = page.url
                if base in url and "/auth-saml/" not in url:
                    break
                if await _click_kmsi_no(page):  # "No" a mantener sesion
                    continue
                error = await _collect_error(page)
                if error:
                    shot = await _screenshot_and_html(page, settings)
                    raise LoginError(error, screenshot=shot, stage="password")
                if await _needs_mfa(page):
                    shot = await _screenshot_and_html(page, settings)
                    raise MFARequired(
                        "Microsoft pidio verificacion en dos pasos (MFA).",
                        screenshot=shot,
                    )
                if await _visible(page, "#idDiv_SAOTCC_Title, #idDiv_SAOTCAS_Title"):
                    shot = await _screenshot_and_html(page, settings)
                    raise MFARequired(
                        "Microsoft pidio un codigo o aprobacion en el movil (MFA).",
                        screenshot=shot,
                    )
                await page.wait_for_timeout(500)
            else:
                shot = await _screenshot_and_html(page, settings)
                raise LoginError(
                    "El login no termino a tiempo. Revisa la captura en "
                    f"{settings.debug_dir}.",
                    screenshot=shot,
                    stage="final",
                )

            # --- verificar que Blackboard acepto la sesion ---
            for attempt in range(6):
                try:
                    response = await context.request.get(
                        f"{base}/learn/api/v1/users/me/memberships",
                        params={"limit": 1, "offset": 0},
                    )
                    if response.status == 200:
                        break
                    status = response.status
                except Exception:
                    status = "sin respuesta"
                if attempt < 5:
                    await page.wait_for_timeout(1500)
            else:
                shot = await _screenshot_and_html(page, settings)
                raise LoginError(
                    f"Microsoft acepto el login, pero Blackboard no abrio la sesion "
                    f"(HTTP {status}). Puede que falte un paso de MFA institucional.",
                    screenshot=shot,
                    stage="verify",
                )

            cookies = [
                cookie
                for cookie in await context.cookies()
                if "blackboard.com" in (cookie.get("domain") or "")
            ]
            if not cookies:
                raise LoginError(
                    "No se obtuvieron cookies de Blackboard despues del login."
                )
            return cookies

        except LoginError:
            if page is not None:
                await _screenshot_and_html(page, settings)
            raise
        except Exception as exc:
            shot = None
            if page is not None:
                shot = await _screenshot_and_html(page, settings)
            raise LoginError(f"Error inesperado durante el login: {exc}", screenshot=shot) from exc
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass


async def login_with_microsoft(
    settings: Settings,
    email: str,
    password: str,
    provider_apid: str,
    progress: ProgressCallback | None = None,
) -> LoginResult:
    """Inicia sesion y devuelve las cookies y el proveedor que funciono.

    Si el correo existe en el otro dominio de SENATI, reintenta una vez con
    el proveedor alternativo (solo cuando no se forzo BB_SAML_APID).
    """
    try:
        cookies = await _login_once(settings, email, password, provider_apid, progress)
        return LoginResult(cookies=cookies, provider_apid=provider_apid)
    except MFARequired:
        raise
    except LoginError as exc:
        message = str(exc).lower()
        alternative = [
            apid
            for apid in set(PROVIDER_BY_DOMAIN.values())
            if apid != provider_apid
        ]
        retry_stages = {"start", "email"}
        if (
            settings.saml_apid_override
            or not alternative
            or (
                exc.stage not in retry_stages
                and not any(token in message for token in _ACCOUNT_ERRORS)
            )
        ):
            raise
        log.info("La cuenta no pertenece al proveedor %s; probando %s", provider_apid, alternative[0])
        await _progress(progress, "🔁 Probando el otro portal de SENATI...")
        cookies = await _login_once(
            settings, email, password, alternative[0], progress
        )
        return LoginResult(cookies=cookies, provider_apid=alternative[0])
