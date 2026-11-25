"""Persistencia local (SQLite): usuario, credenciales cifradas, vistos y recordatorios."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    chat_id        INTEGER PRIMARY KEY,
    email          TEXT,
    provider_apid  TEXT,
    credentials    BLOB,
    created_at     TEXT NOT NULL,
    session_ok     INTEGER NOT NULL DEFAULT 0,
    last_login_at  TEXT,
    last_check_at  TEXT
);

CREATE TABLE IF NOT EXISTS seen_items (
    chat_id    INTEGER NOT NULL,
    item_id    TEXT NOT NULL,
    due        TEXT,
    title      TEXT,
    course     TEXT,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (chat_id, item_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    chat_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, item_id, kind)
);
"""


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # ----------------------------------------------------------------- owner
    def owner_id(self) -> int | None:
        raw = self.get_meta("owner_chat_id")
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def set_owner(self, chat_id: int) -> int:
        """Registra el primer chat como dueño sin permitir que otro lo reemplace."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('owner_chat_id', ?)",
                (str(chat_id),),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'owner_chat_id'"
            ).fetchone()
        return int(row["value"]) if row else chat_id

    # ----------------------------------------------------------------- users
    def get_user(self, chat_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def save_user(self, chat_id: int, email: str, provider_apid: str) -> None:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(chat_id, email, provider_apid, created_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "email = excluded.email, provider_apid = excluded.provider_apid",
                (chat_id, email, provider_apid, now),
            )
            self._conn.commit()

    def set_credentials(self, chat_id: int, blob: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET credentials = ? WHERE chat_id = ?",
                (sqlite3.Binary(blob), chat_id),
            )
            self._conn.commit()

    def clear_credentials(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET credentials = NULL, session_ok = 0 WHERE chat_id = ?",
                (chat_id,),
            )
            self._conn.commit()

    def has_credentials(self, chat_id: int) -> bool:
        row = self.get_user(chat_id)
        return bool(row and row["credentials"])

    def mark_session(self, chat_id: int, ok: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET session_ok = ? WHERE chat_id = ?",
                (1 if ok else 0, chat_id),
            )
            self._conn.commit()

    def mark_login(self, chat_id: int) -> None:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE users SET session_ok = 1, last_login_at = ? WHERE chat_id = ?",
                (now, chat_id),
            )
            self._conn.commit()

    def touch_check(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET last_check_at = ? WHERE chat_id = ?",
                (_utcnow(), chat_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------ seen items
    @staticmethod
    def _rows(items) -> list[dict]:
        out = []
        for item in items:
            due = getattr(item, "due", None)
            out.append(
                {
                    "id": item.id,
                    "due": due.isoformat() if due else None,
                    "title": item.title,
                    "course": item.course_name,
                }
            )
        return out

    def seen_count(self, chat_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM seen_items WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def _insert_seen(self, chat_id: int, rows: list[dict]) -> None:
        now = _utcnow()
        for row in rows:
            self._conn.execute(
                "INSERT INTO seen_items(chat_id, item_id, due, title, course, first_seen) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, item_id) DO UPDATE SET "
                "due = excluded.due, title = excluded.title, course = excluded.course",
                (chat_id, row["id"], row["due"], row["title"], row["course"], now),
            )

    def mark_items_seen(self, chat_id: int, items) -> None:
        rows = self._rows(items)
        if not rows:
            return
        with self._lock:
            self._insert_seen(chat_id, rows)
            self._conn.commit()

    def diff_new_items(self, chat_id: int, items) -> list[dict]:
        rows = self._rows(items)
        if not rows:
            return []
        with self._lock:
            existing = {
                r["item_id"]
                for r in self._conn.execute(
                    "SELECT item_id FROM seen_items WHERE chat_id = ?", (chat_id,)
                )
            }
            new_rows = [r for r in rows if r["id"] not in existing]
            self._insert_seen(chat_id, rows)
            self._conn.commit()
        return new_rows

    def cleanup(self, chat_id: int, days: int = 45) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            self._conn.execute(
                "DELETE FROM seen_items WHERE chat_id = ? AND due IS NOT NULL AND due < ?",
                (chat_id, cutoff),
            )
            self._conn.execute(
                "DELETE FROM reminders WHERE chat_id = ? AND sent_at < ?",
                (chat_id, cutoff),
            )
            self._conn.commit()

    # ------------------------------------------------------------ reminders
    def claim_reminder(self, chat_id: int, item_id: str, kind: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO reminders(chat_id, item_id, kind, sent_at) "
                "VALUES(?, ?, ?, ?)",
                (chat_id, item_id, kind, _utcnow()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def reset_reminders(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,))
            self._conn.commit()
