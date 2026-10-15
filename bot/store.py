"""Хранилище в SQLite: что уже видели, что в очереди, что опубликовано."""
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT UNIQUE NOT NULL,
    source        TEXT,
    title         TEXT,
    summary       TEXT,
    published_at  REAL,
    fetched_at    REAL,
    status        TEXT,   -- new | filtered | low | duplicate | queued | review | posted | skipped | error
    score         INTEGER,
    category      TEXT,
    title_ru      TEXT,
    post_html     TEXT,
    image         TEXT,
    note          TEXT,
    posted_at     REAL,
    tg_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON items(status);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # новые колонки для старых баз
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        if "card_kind" not in cols:
            self.db.execute("ALTER TABLE items ADD COLUMN card_kind TEXT")
            self.db.commit()

    @contextmanager
    def tx(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def add_item(self, url, source, title, summary, published_at) -> bool:
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO items(url, source, title, summary, published_at, fetched_at, status)"
                " VALUES (?,?,?,?,?,?, 'new')",
                (url, source, title, summary, published_at, time.time()),
            )
            return cur.rowcount == 1

    def by_status(self, status: str, limit: int = 100, order: str = "published_at DESC"):
        return self.db.execute(
            f"SELECT * FROM items WHERE status=? ORDER BY {order} LIMIT ?", (status, limit)
        ).fetchall()

    def get(self, item_id: int):
        return self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def update(self, item_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))

    def recent_posted(self, hours: int = 72, limit: int = 40):
        since = time.time() - hours * 3600
        return self.db.execute(
            "SELECT id, title_ru FROM items WHERE status IN ('posted','queued','review','sent','published','scored')"
            " AND fetched_at>? ORDER BY fetched_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()

    def posted_since(self, ts: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE status='posted' AND posted_at>?", (ts,)
        ).fetchone()[0]

    def last_post_time(self) -> float:
        row = self.db.execute("SELECT MAX(posted_at) FROM items WHERE status='posted'").fetchone()
        return row[0] or 0.0

    def prune(self, days: int = 14):
        """Чистим старое, чтобы база оставалась маленькой (её хранит GitHub)."""
        cutoff = time.time() - days * 86400
        with self.tx() as db:
            db.execute("DELETE FROM items WHERE fetched_at < ? AND status != 'review'", (cutoff,))
            # тексты отсеянных новостей не нужны — оставляем только адрес, чтобы не брать повторно
            db.execute("UPDATE items SET summary=NULL WHERE status IN ('filtered','low','duplicate','error')"
                       " AND fetched_at < ? AND summary IS NOT NULL", (time.time() - 2 * 86400,))
        self.db.execute("VACUUM")

    def kv_get(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def kv_set(self, k, v):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?,?)", (k, str(v)))
