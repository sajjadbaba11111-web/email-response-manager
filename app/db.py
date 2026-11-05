"""Persistence layer with PostgreSQL (primary) and SQLite (fallback) backends.

PostgreSQL is used when ``DATABASE_URL`` is configured (recommended for
Docker/production).  When ``DATABASE_URL`` is empty, the implementation falls
back to SQLite at ``DB_PATH`` so local development and the test suite can run
without a PostgreSQL server.

The public API is synchronous.  All existing callers continue to work; the
module also stores email snapshots and an activity log for the admin UI.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

SQLITE_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS seen_emails (
    email_id TEXT PRIMARY KEY,
    seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id          TEXT NOT NULL,
    thread_id         TEXT,
    sender            TEXT NOT NULL,
    subject           TEXT,
    message_id_header TEXT,
    draft_text        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'discarded')),
    created_at        TEXT NOT NULL,
    sent_at           TEXT,
    final_text        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_email_id ON drafts (email_id);

CREATE TABLE IF NOT EXISTS processed_wa_messages (
    wa_message_id TEXT PRIMARY KEY,
    processed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_snapshots (
    email_id          TEXT PRIMARY KEY,
    thread_id         TEXT,
    sender            TEXT,
    subject           TEXT,
    message_id_header TEXT,
    email_date        TEXT,
    body              TEXT,
    first_seen_at     TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    email_id   TEXT,
    draft_id   INTEGER,
    actor      TEXT,
    summary    TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_created_at ON activity_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_email_id ON activity_log (email_id);
"""

POSTGRES_SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS seen_emails (
        email_id TEXT PRIMARY KEY,
        seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drafts (
        id                BIGSERIAL PRIMARY KEY,
        email_id          TEXT NOT NULL,
        thread_id         TEXT,
        sender            TEXT NOT NULL,
        subject           TEXT,
        message_id_header TEXT,
        draft_text        TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'discarded')),
        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        sent_at           TIMESTAMPTZ,
        final_text        TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_email_id ON drafts (email_id)",
    """
    CREATE TABLE IF NOT EXISTS processed_wa_messages (
        wa_message_id TEXT PRIMARY KEY,
        processed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_snapshots (
        email_id          TEXT PRIMARY KEY,
        thread_id         TEXT,
        sender            TEXT,
        subject           TEXT,
        message_id_header TEXT,
        email_date        TEXT,
        body              TEXT,
        first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id         BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        kind       TEXT NOT NULL,
        email_id   TEXT,
        draft_id   BIGINT,
        actor      TEXT,
        summary    TEXT NOT NULL,
        detail     JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_created_at ON activity_log (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_activity_email_id ON activity_log (email_id)",
]


class DatabaseError(RuntimeError):
    """Raised when the database cannot be used."""


@dataclass(frozen=True, slots=True)
class Draft:
    """A reply draft awaiting (or having received) the owner's approval."""

    id: int
    email_id: str
    thread_id: str | None
    sender: str
    subject: str | None
    message_id_header: str | None
    draft_text: str
    status: str
    created_at: str
    sent_at: str | None
    final_text: str | None


@dataclass(frozen=True, slots=True)
class Activity:
    id: int
    created_at: str
    kind: str
    email_id: str | None
    draft_id: int | None
    actor: str | None
    summary: str
    detail: Any


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _backend() -> str:
    return "postgres" if get_settings().use_postgres else "sqlite"


def _q(sql: str) -> str:
    """Translate SQLite-style ``?`` placeholders to ``%s`` for PostgreSQL."""
    if _backend() == "postgres":
        return sql.replace("?", "%s")
    return sql


def _val(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return value


def _row_get(row: Any, key: str, index: int | None = None) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        if index is not None:
            return row[index]
        return None


# --------------------------------------------------------------------------- #
# connections
# --------------------------------------------------------------------------- #
_pg_pool: Any = None


def _get_pg_pool():  # pragma: no cover - exercised via postgres integration
    global _pg_pool
    if _pg_pool is None:
        try:
            from psycopg_pool import ConnectionPool
        except Exception as exc:
            raise DatabaseError(
                "psycopg is required for PostgreSQL. Install dependencies: pip install psycopg[binary] psycopg-pool"
            ) from exc
        _pg_pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=5, open=True)
    return _pg_pool


@contextmanager
def _connect_sqlite() -> Iterator[sqlite3.Connection]:
    path: Path = get_settings().db_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise DatabaseError(f"database error at {path}: {exc}") from exc


@contextmanager
def _connect_pg() -> Iterator[Any]:  # pragma: no cover - exercised via postgres integration
    try:
        from psycopg.rows import dict_row
    except Exception as exc:
        raise DatabaseError("psycopg is required for PostgreSQL") from exc
    pool = _get_pg_pool()
    try:
        with pool.connection() as conn:
            conn.row_factory = dict_row
            yield conn
    except Exception as exc:
        raise DatabaseError(f"PostgreSQL error: {exc}") from exc


@contextmanager
def _connect() -> Iterator[Any]:
    if _backend() == "postgres":
        with _connect_pg() as conn:
            yield conn
    else:
        with _connect_sqlite() as conn:
            yield conn


def close_db() -> None:
    global _pg_pool
    if _pg_pool is not None:
        with suppress(Exception):
            _pg_pool.close()
        _pg_pool = None


def init_db() -> None:
    """Create the schema if it does not exist yet."""
    if _backend() == "postgres":  # pragma: no cover - exercised via postgres integration
        with _connect_pg() as conn:
            for stmt in POSTGRES_SCHEMA:
                conn.execute(stmt)
            conn.commit()
    else:
        with _connect_sqlite() as conn:
            conn.executescript(SQLITE_SCHEMA)


def ping() -> bool:
    """Return ``True`` when the database answers a trivial query."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
            row = (
                conn.fetchone()
                if hasattr(conn, "fetchone")
                else conn.execute("SELECT 1").fetchone()
            )
            return row is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# seen_emails
# --------------------------------------------------------------------------- #
def is_seen(email_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(_q("SELECT 1 FROM seen_emails WHERE email_id = ?"), (email_id,))
        row = cur.fetchone()
    return row is not None


def mark_seen(email_id: str) -> None:
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            conn.execute(
                "INSERT INTO seen_emails (email_id, seen_at) VALUES (%s, NOW()) ON CONFLICT(email_id) DO NOTHING",
                (email_id,),
            )
            conn.commit()
    else:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO seen_emails (email_id, seen_at) VALUES (?, ?) ON CONFLICT(email_id) DO NOTHING",
                (email_id, _now_iso()),
            )


def mark_seen_many(email_ids: Iterable[str]) -> int:
    ids = list(email_ids)
    if not ids:
        return 0
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM seen_emails")
            before = cur.fetchone()["count"]
            conn.executemany(
                "INSERT INTO seen_emails (email_id, seen_at) VALUES (%s, NOW()) ON CONFLICT(email_id) DO NOTHING",
                [(email_id,) for email_id in ids],
            )
            cur = conn.execute("SELECT COUNT(*) FROM seen_emails")
            after = cur.fetchone()["count"]
            conn.commit()
        return int(after - before)
    with _connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM seen_emails").fetchone()[0]
        conn.executemany(
            "INSERT INTO seen_emails (email_id, seen_at) VALUES (?, ?) ON CONFLICT(email_id) DO NOTHING",
            [(email_id, _now_iso()) for email_id in ids],
        )
        after = conn.execute("SELECT COUNT(*) FROM seen_emails").fetchone()[0]
    return int(after - before)


def filter_unseen(email_ids: Sequence[str]) -> list[str]:
    if not email_ids:
        return []
    placeholders = ",".join("?" for _ in email_ids)
    sql = _q(f"SELECT email_id FROM seen_emails WHERE email_id IN ({placeholders})")
    with _connect() as conn:
        cur = conn.execute(sql, tuple(email_ids))
        rows = cur.fetchall()
    seen = {_row_get(row, "email_id", 0) for row in rows}
    return [email_id for email_id in email_ids if email_id not in seen]


# --------------------------------------------------------------------------- #
# drafts
# --------------------------------------------------------------------------- #
def _row_to_draft(row: Any) -> Draft:
    return Draft(
        id=int(_row_get(row, "id", 0)),
        email_id=_row_get(row, "email_id", 1),
        thread_id=_row_get(row, "thread_id", 2),
        sender=_row_get(row, "sender", 3),
        subject=_row_get(row, "subject", 4),
        message_id_header=_row_get(row, "message_id_header", 5),
        draft_text=_row_get(row, "draft_text", 6),
        status=_row_get(row, "status", 7),
        created_at=str(_row_get(row, "created_at", 8) or ""),
        sent_at=str(_row_get(row, "sent_at", 9)) if _row_get(row, "sent_at", 9) else None,
        final_text=_row_get(row, "final_text", 10),
    )


def create_draft(
    *,
    email_id: str,
    thread_id: str | None,
    sender: str,
    subject: str | None,
    message_id_header: str | None,
    draft_text: str,
) -> int:
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO drafts (email_id, thread_id, sender, subject, message_id_header, draft_text, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending') RETURNING id",
                (email_id, thread_id, sender, subject, message_id_header, draft_text),
            )
            row = cur.fetchone()
            conn.commit()
        return int(_row_get(row, "id", 0))
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO drafts (email_id, thread_id, sender, subject, message_id_header, "
            "draft_text, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (email_id, thread_id, sender, subject, message_id_header, draft_text, _now_iso()),
        )
        return int(cursor.lastrowid or 0)


def get_draft(draft_id: int) -> Draft | None:
    with _connect() as conn:
        cur = conn.execute(_q("SELECT * FROM drafts WHERE id = ?"), (draft_id,))
        row = cur.fetchone()
    return _row_to_draft(row) if row else None


def get_draft_for_email(email_id: str) -> Draft | None:
    with _connect() as conn:
        cur = conn.execute(
            _q("SELECT * FROM drafts WHERE email_id = ? ORDER BY id LIMIT 1"), (email_id,)
        )
        row = cur.fetchone()
    return _row_to_draft(row) if row else None


def list_pending_drafts() -> list[Draft]:
    with _connect() as conn:
        cur = conn.execute("SELECT * FROM drafts WHERE status = 'pending' ORDER BY id DESC")
        rows = cur.fetchall()
    return [_row_to_draft(row) for row in rows]


def list_all_drafts(limit: int = 200) -> list[Draft]:
    with _connect() as conn:
        cur = conn.execute(
            _q("SELECT * FROM drafts ORDER BY id DESC LIMIT ?"), (max(1, min(limit, 1000)),)
        )
        rows = cur.fetchall()
    return [_row_to_draft(row) for row in rows]


def mark_draft_sent(draft_id: int, final_text: str) -> bool:
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE drafts SET status = 'sent', sent_at = NOW(), final_text = %s "
                "WHERE id = %s AND status = 'pending'",
                (final_text, draft_id),
            )
            conn.commit()
            return cur.rowcount > 0
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE drafts SET status = 'sent', sent_at = ?, final_text = ? "
            "WHERE id = ? AND status = 'pending'",
            (_now_iso(), final_text, draft_id),
        )
        return cursor.rowcount > 0


def mark_draft_discarded(draft_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            _q("UPDATE drafts SET status = 'discarded' WHERE id = ? AND status = 'pending'"),
            (draft_id,),
        )
        if _backend() == "postgres":  # pragma: no cover
            conn.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# processed_wa_messages
# --------------------------------------------------------------------------- #
def mark_wa_message_processed(wa_message_id: str) -> bool:
    if not wa_message_id:
        return False
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO processed_wa_messages (wa_message_id, processed_at) VALUES (%s, NOW()) "
                "ON CONFLICT(wa_message_id) DO NOTHING",
                (wa_message_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO processed_wa_messages (wa_message_id, processed_at) VALUES (?, ?) "
            "ON CONFLICT(wa_message_id) DO NOTHING",
            (wa_message_id, _now_iso()),
        )
        return cursor.rowcount > 0


# --------------------------------------------------------------------------- #
# app_state
# --------------------------------------------------------------------------- #
def get_state(key: str) -> str | None:
    with _connect() as conn:
        cur = conn.execute(_q("SELECT value FROM app_state WHERE key = ?"), (key,))
        row = cur.fetchone()
    return _row_get(row, "value", 0) if row else None


def set_state(key: str, value: str) -> None:
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
    else:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


# --------------------------------------------------------------------------- #
# email snapshots and activity log (admin/audit)
# --------------------------------------------------------------------------- #
def save_email_snapshot(
    *,
    email_id: str,
    thread_id: str | None,
    sender: str | None,
    subject: str | None,
    message_id_header: str | None,
    email_date: str | None,
    body: str | None,
) -> None:
    """Store/update an email snapshot so responses can be viewed with context."""
    if _backend() == "postgres":  # pragma: no cover
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO email_snapshots
                    (email_id, thread_id, sender, subject, message_id_header, email_date, body, first_seen_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT(email_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    sender = excluded.sender,
                    subject = excluded.subject,
                    message_id_header = excluded.message_id_header,
                    email_date = excluded.email_date,
                    body = excluded.body,
                    updated_at = NOW()
                """,
                (email_id, thread_id, sender, subject, message_id_header, email_date, body),
            )
            conn.commit()
    else:
        now = _now_iso()
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO email_snapshots
                    (email_id, thread_id, sender, subject, message_id_header, email_date, body, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    sender = excluded.sender,
                    subject = excluded.subject,
                    message_id_header = excluded.message_id_header,
                    email_date = excluded.email_date,
                    body = excluded.body,
                    updated_at = excluded.updated_at
                """,
                (
                    email_id,
                    thread_id,
                    sender,
                    subject,
                    message_id_header,
                    email_date,
                    body,
                    now,
                    now,
                ),
            )


def log_activity(
    kind: str,
    summary: str,
    *,
    email_id: str | None = None,
    draft_id: int | None = None,
    actor: str | None = None,
    detail: Any = None,
) -> None:
    """Append an audit/activity record (best-effort; never breaks core flow)."""
    try:
        detail_value = (
            json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None
        )
        if _backend() == "postgres":  # pragma: no cover
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO activity_log (created_at, kind, email_id, draft_id, actor, summary, detail) "
                    "VALUES (NOW(), %s, %s, %s, %s, %s, %s::jsonb)",
                    (kind, email_id, draft_id, actor, summary, detail_value),
                )
                conn.commit()
        else:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO activity_log (created_at, kind, email_id, draft_id, actor, summary, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_now_iso(), kind, email_id, draft_id, actor, summary, detail_value),
                )
    except Exception:
        # Activity logging must not break the main email/WhatsApp flow.
        return


def _row_to_activity(row: Any) -> Activity:
    raw_detail = _row_get(row, "detail")
    detail = raw_detail
    if isinstance(raw_detail, str) and raw_detail:
        try:
            detail = json.loads(raw_detail)
        except Exception:
            detail = raw_detail
    return Activity(
        id=int(_row_get(row, "id", 0)),
        created_at=str(_row_get(row, "created_at", 1) or ""),
        kind=str(_row_get(row, "kind", 2) or ""),
        email_id=_row_get(row, "email_id", 3),
        draft_id=int(_row_get(row, "draft_id")) if _row_get(row, "draft_id") is not None else None,
        actor=_row_get(row, "actor", 5),
        summary=str(_row_get(row, "summary", 6) or ""),
        detail=detail,
    )


def list_activities(limit: int = 200, kind: str | None = None) -> list[Activity]:
    limit = max(1, min(limit, 1000))
    with _connect() as conn:
        if kind:
            cur = conn.execute(
                _q("SELECT * FROM activity_log WHERE kind = ? ORDER BY id DESC LIMIT ?"),
                (kind, limit),
            )
        else:
            cur = conn.execute(_q("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?"), (limit,))
        rows = cur.fetchall()
    return [_row_to_activity(row) for row in rows]


def list_email_snapshots(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with _connect() as conn:
        cur = conn.execute(
            _q("SELECT * FROM email_snapshots ORDER BY updated_at DESC LIMIT ?"), (limit,)
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "email_id": _row_get(row, "email_id"),
                "thread_id": _row_get(row, "thread_id"),
                "sender": _row_get(row, "sender"),
                "subject": _row_get(row, "subject"),
                "message_id_header": _row_get(row, "message_id_header"),
                "email_date": str(_row_get(row, "email_date") or ""),
                "body": _row_get(row, "body"),
                "first_seen_at": str(_row_get(row, "first_seen_at") or ""),
                "updated_at": str(_row_get(row, "updated_at") or ""),
            }
        )
    return out


def get_email_snapshot(email_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        cur = conn.execute(_q("SELECT * FROM email_snapshots WHERE email_id = ?"), (email_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {
        "email_id": _row_get(row, "email_id"),
        "thread_id": _row_get(row, "thread_id"),
        "sender": _row_get(row, "sender"),
        "subject": _row_get(row, "subject"),
        "message_id_header": _row_get(row, "message_id_header"),
        "email_date": str(_row_get(row, "email_date") or ""),
        "body": _row_get(row, "body"),
        "first_seen_at": str(_row_get(row, "first_seen_at") or ""),
        "updated_at": str(_row_get(row, "updated_at") or ""),
    }


def get_draft_with_email(draft_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        cur = conn.execute(_q("SELECT * FROM drafts WHERE id = ?"), (draft_id,))
        d = cur.fetchone()
        if not d:
            return None
        cur = conn.execute(
            _q("SELECT * FROM email_snapshots WHERE email_id = ?"), (_row_get(d, "email_id"),)
        )
        e = cur.fetchone()
    draft = _row_to_draft(d)
    email = None
    if e:
        email = {
            "email_id": _row_get(e, "email_id"),
            "sender": _row_get(e, "sender"),
            "subject": _row_get(e, "subject"),
            "body": _row_get(e, "body"),
            "email_date": str(_row_get(e, "email_date") or ""),
        }
    return {
        "draft": {
            "id": draft.id,
            "email_id": draft.email_id,
            "thread_id": draft.thread_id,
            "sender": draft.sender,
            "subject": draft.subject,
            "draft_text": draft.draft_text,
            "final_text": draft.final_text,
            "status": draft.status,
            "created_at": draft.created_at,
            "sent_at": draft.sent_at,
        },
        "email": email,
    }


def stats() -> dict[str, Any]:
    """Return small counters used by the health endpoint and admin dashboard."""
    with _connect() as conn:

        def count(sql: str) -> int:
            cur = conn.execute(sql)
            row = cur.fetchone()
            if row is None:
                return 0
            if isinstance(row, dict):
                return int(row.get("count") or next(iter(row.values())) or 0)
            return int(row[0])

        seen = count("SELECT COUNT(*) AS count FROM seen_emails")
        pending = count("SELECT COUNT(*) AS count FROM drafts WHERE status = 'pending'")
        sent = count("SELECT COUNT(*) AS count FROM drafts WHERE status = 'sent'")
        discarded = count("SELECT COUNT(*) AS count FROM drafts WHERE status = 'discarded'")
        activities = count("SELECT COUNT(*) AS count FROM activity_log")
        emails = count("SELECT COUNT(*) AS count FROM email_snapshots")
    return {
        "seen_emails": seen,
        "pending_drafts": pending,
        "sent_drafts": sent,
        "discarded_drafts": discarded,
        "activities": activities,
        "email_snapshots": emails,
    }
