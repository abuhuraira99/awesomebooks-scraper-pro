"""SQLite persistence: one small database file per module.

``data/catalog.db``   – catalog extraction jobs and the books they found
``data/inventory.db`` – stock-check jobs and the per-ISBN answers

SQLite is the single source of truth: workers claim rows, write results and
move on, so a crash, reboot or "Stop" never loses finished work, and a paused
job resumes exactly where it stopped. WAL mode lets the UI read while workers
write.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

JOB_ACTIVE_STATES = ("queued", "discovering", "running")
JOB_RESUMABLE_STATES = ("paused", "interrupted", "failed")

_console = logging.getLogger("awesomebooks_pro.jobs")
_LEVELS = {"info": logging.INFO, "success": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


class JobStore:
    """Generic job/items/events store. Subclasses provide the item schema."""

    ITEM_SCHEMA: str = ""
    JOB_EXTRA_COLUMNS: str = ""
    SEARCH_COLUMNS: Sequence[str] = ()
    INDEXES: Sequence[str] = ()
    COLUMN_MIGRATIONS: Sequence[tuple[str, str, str]] = (
        ("jobs", "traffic", "TEXT"),
        ("jobs", "heartbeat_at", "REAL"),
        ("jobs", "export_path", "TEXT"),
        ("jobs", "exported_at", "REAL"),
    )
    LABEL = "Job"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._claim_lock = threading.Lock()
        self._migrate()

    # -- connection ---------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                sessions INTEGER NOT NULL,
                pace TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                active_seconds REAL NOT NULL DEFAULT 0,
                traffic TEXT,
                heartbeat_at REAL,
                export_path TEXT,
                exported_at REAL,
                error TEXT
                {("," + self.JOB_EXTRA_COLUMNS) if self.JOB_EXTRA_COLUMNS else ""}
            );
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                updated_at REAL,
                {self.ITEM_SCHEMA}
            );
            CREATE INDEX IF NOT EXISTS ix_items_job_state ON items(job_id, state);
            CREATE INDEX IF NOT EXISTS ix_items_job_updated ON items(job_id, updated_at);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                ts REAL NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id, id);
            """
        )
        # Additive migrations: columns introduced after a database was first created.
        for table, column, decl in self.COLUMN_MIGRATIONS:
            existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        for index in self.INDEXES:
            self.conn.execute(index)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- jobs -----------------------------------------------------------------
    def create_job(self, **fields: Any) -> str:
        job_id = fields.pop("id", None) or uuid.uuid4().hex[:12]
        fields.setdefault("status", "queued")
        fields.setdefault("created_at", time.time())
        cols = ", ".join(["id", *fields])
        marks = ", ".join("?" for _ in range(len(fields) + 1))
        self.conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", (job_id, *fields.values()))
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def mark_interrupted_jobs(self) -> list[str]:
        """Called at start-up: a job that was running when the process died becomes ``interrupted``.

        Its in-flight rows go back to the queue, and the time it actually ran
        (up to its last heartbeat) is credited, so nothing is lost or double counted.
        """
        marks = ",".join("?" for _ in JOB_ACTIVE_STATES)
        ids = [r[0] for r in self.conn.execute(f"SELECT id FROM jobs WHERE status IN ({marks})", JOB_ACTIVE_STATES)]
        self.conn.execute(
            f"""UPDATE jobs SET active_seconds = active_seconds + MAX(0, heartbeat_at - started_at)
                WHERE status IN ({marks}) AND heartbeat_at IS NOT NULL AND started_at IS NOT NULL
                  AND heartbeat_at >= started_at""",
            JOB_ACTIVE_STATES,
        )
        self.conn.execute(f"UPDATE jobs SET status = 'interrupted' WHERE status IN ({marks})", JOB_ACTIVE_STATES)
        # A Stop that was still finishing when the app closed stays a Stop: paused, never auto-resumed.
        self.conn.execute("UPDATE jobs SET status = 'paused' WHERE status = 'stopping'")
        # Back to the queue. The attempt stays counted, so an item that keeps crashing the app still ends as 'error'.
        self.conn.execute("UPDATE items SET state = 'pending' WHERE state = 'processing'")
        return ids

    def purge_except(self, keep_id: str) -> int:
        """Delete every other run (items and events cascade) and reclaim the disk space.

        A module only ever needs its current run: finished runs were already
        saved to Excel, so old rows are dead weight.
        """
        cur = self.conn.execute("DELETE FROM jobs WHERE id != ?", (keep_id,))
        removed = cur.rowcount
        if removed:
            try:
                self.conn.execute("VACUUM")  # best effort: reclaiming disk space must never fail a request
            except sqlite3.Error:
                pass
        return removed

    def mark_stopping(self, job_id: str) -> bool:
        """Record a Stop request, but only for a job that is still actively running."""
        marks = ",".join("?" for _ in JOB_ACTIVE_STATES)
        cur = self.conn.execute(
            f"UPDATE jobs SET status = 'stopping' WHERE id = ? AND status IN ({marks})", (job_id, *JOB_ACTIVE_STATES)
        )
        return cur.rowcount == 1

    def requeue_processing(self, job_id: str) -> int:
        cur = self.conn.execute(
            "UPDATE items SET state = 'pending', attempts = MAX(0, attempts - 1) WHERE job_id = ? AND state = 'processing'",
            (job_id,),
        )
        return cur.rowcount

    # -- events ---------------------------------------------------------------
    def add_event(self, job_id: str, level: str, message: str) -> None:
        _console.log(_LEVELS.get(level, logging.INFO), "[%s] %s", self.LABEL, message)
        self.conn.execute(
            "INSERT INTO events (job_id, ts, level, message) VALUES (?, ?, ?, ?)", (job_id, time.time(), level, message)
        )

    def events(self, job_id: str, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, ts, level, message FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- items ----------------------------------------------------------------
    def insert_items(self, job_id: str, records: Iterable[dict[str, Any]], *, state: str = "pending") -> int:
        records = list(records)
        if not records:
            return 0
        columns = list(records[0].keys())
        cols = ", ".join(["job_id", "state", *columns])
        marks = ", ".join("?" for _ in range(len(columns) + 2))
        before = self.conn.total_changes
        with self._claim_lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(
                    f"INSERT OR IGNORE INTO items ({cols}) VALUES ({marks})",
                    [(job_id, r.get("state", state), *[r.get(c) for c in columns]) for r in records],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.conn.total_changes - before

    def claim_next(self, job_id: str) -> dict[str, Any] | None:
        with self._claim_lock:
            row = self.conn.execute(
                "SELECT * FROM items WHERE job_id = ? AND state = 'pending' ORDER BY id LIMIT 1", (job_id,)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE items SET state = 'processing', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
            item = dict(row)
            item["attempts"] += 1
            return item

    def complete_item(self, item_id: int, fields: dict[str, Any], *, state: str = "done") -> None:
        fields = {**fields, "state": state, "error": None, "updated_at": time.time()}
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE items SET {sets} WHERE id = ?", (*fields.values(), item_id))

    def fail_item(self, item: dict[str, Any], error: str, max_attempts: int) -> str:
        state = "pending" if item["attempts"] < max_attempts else "error"
        self.conn.execute(
            "UPDATE items SET state = ?, error = ?, updated_at = ? WHERE id = ?",
            (state, error[:500], time.time(), item["id"]),
        )
        return state

    def release_item(self, item: dict[str, Any]) -> None:
        """Put an item back untouched (job was stopped mid-request)."""
        self.conn.execute("UPDATE items SET state = 'pending', attempts = MAX(0, attempts - 1) WHERE id = ?", (item["id"],))

    def retry_errors(self, job_id: str) -> int:
        cur = self.conn.execute(
            "UPDATE items SET state = 'pending', attempts = 0, error = NULL WHERE job_id = ? AND state = 'error'",
            (job_id,),
        )
        return cur.rowcount

    def state_counts(self, job_id: str) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) FROM items WHERE job_id = ? GROUP BY state", (job_id,))
        counts = {"pending": 0, "processing": 0, "done": 0, "error": 0}
        for state, count in rows:
            counts[state] = count
        return counts

    def completed_since(self, job_id: str, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE job_id = ? AND state NOT IN ('pending', 'processing') AND updated_at >= ?",
            (job_id, since),
        ).fetchone()
        return int(row[0])

    def _filter_sql(self, status: str | None) -> tuple[str, list[Any]]:  # pragma: no cover - overridden
        return "", []

    def page(
        self, job_id: str, *, offset: int = 0, limit: int = 25, query: str | None = None, status: str | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        where = ["job_id = ?"]
        params: list[Any] = [job_id]
        if query and self.SEARCH_COLUMNS:
            like = f"%{query.strip()}%"
            where.append("(" + " OR ".join(f"{c} LIKE ?" for c in self.SEARCH_COLUMNS) + ")")
            params.extend([like] * len(self.SEARCH_COLUMNS))
        clause, extra = self._filter_sql(status)
        if clause:
            where.append(clause)
            params.extend(extra)
        sql_where = " AND ".join(where)
        total = self.conn.execute(f"SELECT COUNT(*) FROM items WHERE {sql_where}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT * FROM items WHERE {sql_where} ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset]
        ).fetchall()
        return int(total), [dict(r) for r in rows]

    def iter_items(self, job_id: str, batch: int = 2000) -> Iterable[dict[str, Any]]:
        last = 0
        while True:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?", (job_id, last, batch)
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield dict(row)
            last = rows[-1]["id"]


# Columns shared by both modules: one book, with its New and Used offer for a US shopper.
OFFER_COLUMNS = """
        new_available INTEGER,
        new_price REAL,
        new_qty INTEGER,
        used_available INTEGER,
        used_price REAL,
        used_qty INTEGER,
        rrp REAL
"""
BOOK_COLUMNS = """
        title TEXT,
        author TEXT,
        format TEXT,
        publisher TEXT,
        publication_date TEXT,
        page_count INTEGER,
        dimensions TEXT,
        weight TEXT,
        category TEXT,
        image_url TEXT,
        product_url TEXT
"""
IN_STOCK_SQL = "(new_available = 1 OR used_available = 1)"


class CatalogStore(JobStore):
    LABEL = "Catalog"
    JOB_EXTRA_COLUMNS = """
        url TEXT NOT NULL,
        section TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'listing',
        with_details INTEGER NOT NULL DEFAULT 1,
        max_products INTEGER,
        pages_scanned INTEGER NOT NULL DEFAULT 0,
        listed_total INTEGER,
        discovery_done INTEGER NOT NULL DEFAULT 0,
        scan_state TEXT
    """
    # scan_state: JSON progress of the listing scan (slices, page cursor), so discovery resumes mid-way.
    ITEM_SCHEMA = f"""
        isbn TEXT NOT NULL,
        listed_condition TEXT,
        listed_price REAL,
        found INTEGER,
        {BOOK_COLUMNS},
        {OFFER_COLUMNS},
        note TEXT,
        checked_at REAL,
        UNIQUE(job_id, isbn)
    """
    SEARCH_COLUMNS = ("title", "isbn", "author", "publisher")

    def known_isbns(self, job_id: str, isbns: Iterable[str]) -> set[str]:
        """Which of these books are already in the run."""
        ids = list(dict.fromkeys(isbns))
        if not ids:
            return set()
        marks = ", ".join("?" for _ in ids)
        rows = self.conn.execute(f"SELECT isbn FROM items WHERE job_id = ? AND isbn IN ({marks})", (job_id, *ids)).fetchall()
        return {row[0] for row in rows}

    def item_count(self, job_id: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM items WHERE job_id = ?", (job_id,)).fetchone()[0])

    def _filter_sql(self, status: str | None) -> tuple[str, list[Any]]:
        return {
            "in_stock": (f"state = 'done' AND {IN_STOCK_SQL}", []),
            "out_of_stock": (f"state = 'done' AND NOT {IN_STOCK_SQL}", []),
            "used": ("state = 'done' AND used_available = 1", []),
            "pending": ("state IN ('pending', 'processing')", []),
            "error": ("state = 'error'", []),
        }.get(status or "", ("", []))

    def stock_counts(self, job_id: str) -> dict[str, int]:
        row = self.conn.execute(
            f"""SELECT
                 SUM(CASE WHEN state = 'done' AND {IN_STOCK_SQL} THEN 1 ELSE 0 END),
                 SUM(CASE WHEN state = 'done' AND NOT {IN_STOCK_SQL} THEN 1 ELSE 0 END),
                 SUM(CASE WHEN state = 'done' AND used_available = 1 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN state = 'done' AND new_available = 1 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN state = 'done' THEN COALESCE(new_qty, 0) + COALESCE(used_qty, 0) ELSE 0 END)
               FROM items WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        return {
            "in_stock": int(row[0] or 0),
            "out_of_stock": int(row[1] or 0),
            "used": int(row[2] or 0),
            "new": int(row[3] or 0),
            "units": int(row[4] or 0),
        }


class InventoryStore(JobStore):
    LABEL = "Stock check"
    JOB_EXTRA_COLUMNS = """
        filename TEXT NOT NULL,
        valid_rows INTEGER NOT NULL DEFAULT 0,
        invalid_rows INTEGER NOT NULL DEFAULT 0,
        duplicate_rows INTEGER NOT NULL DEFAULT 0
    """
    ITEM_SCHEMA = f"""
        row_no INTEGER,
        raw_sku TEXT,
        isbn TEXT,
        stock_status TEXT,
        found INTEGER,
        quantity INTEGER,
        {BOOK_COLUMNS},
        {OFFER_COLUMNS},
        note TEXT,
        checked_at REAL
    """
    SEARCH_COLUMNS = ("raw_sku", "isbn", "title", "author")
    INDEXES = ("CREATE INDEX IF NOT EXISTS ix_inv_isbn ON items(job_id, isbn)",)
    ANSWER_COLUMNS = (
        "stock_status", "found", "quantity", "title", "author", "format", "publisher", "publication_date",
        "page_count", "dimensions", "weight", "category", "image_url", "product_url", "new_available",
        "new_price", "new_qty", "used_available", "used_price", "used_qty", "rrp", "note", "checked_at",
    )  # fmt: skip

    def _filter_sql(self, status: str | None) -> tuple[str, list[Any]]:
        return {
            "in_stock": ("stock_status = 'in_stock'", []),
            "out_of_stock": ("stock_status = 'out_of_stock'", []),
            "used": ("state = 'done' AND used_available = 1", []),
            "not_found": ("state = 'done' AND found = 0", []),
            "pending": ("state IN ('pending', 'processing')", []),
            "error": ("state = 'error'", []),
            "invalid": ("state = 'invalid'", []),
        }.get(status or "", ("", []))

    def stock_counts(self, job_id: str) -> dict[str, int]:
        row = self.conn.execute(
            """SELECT
                 SUM(CASE WHEN stock_status = 'in_stock' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN stock_status = 'out_of_stock' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN state = 'done' AND found = 0 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN state = 'invalid' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN stock_status = 'in_stock' THEN COALESCE(quantity, 0) ELSE 0 END),
                 SUM(CASE WHEN state = 'done' AND used_available = 1 THEN 1 ELSE 0 END)
               FROM items WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        return {
            "in_stock": int(row[0] or 0),
            "out_of_stock": int(row[1] or 0),
            "not_found": int(row[2] or 0),
            "invalid": int(row[3] or 0),
            "units": int(row[4] or 0),
            "used": int(row[5] or 0),
        }

    def cached_answer(self, job_id: str, isbn: str) -> dict[str, Any] | None:
        """Reuse the answer for a duplicate ISBN within the same job instead of re-requesting."""
        row = self.conn.execute(
            "SELECT * FROM items WHERE job_id = ? AND isbn = ? AND state = 'done' LIMIT 1", (job_id, isbn)
        ).fetchone()
        return dict(row) if row else None
