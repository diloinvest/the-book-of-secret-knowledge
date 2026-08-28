"""Persistent memory and audit log, in one SQLite file.

Two tables. `facts` is what Atlas chooses to remember about you and your work;
it is injected into the system prompt at the start of every session. `events`
is the append-only record of every tool call it made, so you can always go back
and see what it actually did.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    session   TEXT,
    kind      TEXT NOT NULL,
    tool      TEXT,
    detail    TEXT,
    decision  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS facts_updated ON facts (updated_at DESC);
"""


@dataclass
class Fact:
    key: str
    value: str
    tags: str
    updated_at: float


class Memory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- facts ---------------------------------------------------------
    def remember(self, key: str, value: str, tags: str = "") -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO facts (key, value, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   tags = excluded.tags,
                   updated_at = excluded.updated_at""",
            (key, value, tags, now, now),
        )
        self.db.commit()

    def forget(self, key: str) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE key = ?", (key,))
        self.db.commit()
        return cur.rowcount > 0

    def recall(self, query: str = "", limit: int = 20) -> list[Fact]:
        if query:
            like = f"%{query}%"
            rows = self.db.execute(
                """SELECT key, value, tags, updated_at FROM facts
                   WHERE key LIKE ? OR value LIKE ? OR tags LIKE ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (like, like, like, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT key, value, tags, updated_at FROM facts ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Fact(r["key"], r["value"], r["tags"], r["updated_at"]) for r in rows]

    def context_block(self, limit: int = 25) -> str:
        """The remembered facts, formatted for the system prompt."""
        facts = self.recall(limit=limit)
        if not facts:
            return ""
        lines = [f"- {f.key}: {f.value}" + (f"  [{f.tags}]" if f.tags else "") for f in facts]
        return "## What you remember about this user\n" + "\n".join(lines)

    # --- audit ---------------------------------------------------------
    def log(
        self,
        kind: str,
        tool: str | None = None,
        detail: Any = None,
        decision: str | None = None,
        session: str | None = None,
    ) -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)[:8000]
        self.db.execute(
            "INSERT INTO events (ts, session, kind, tool, detail, decision) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), session, kind, tool, detail, decision),
        )
        self.db.commit()

    def events(self, limit: int = 50, tool: str | None = None) -> list[sqlite3.Row]:
        if tool:
            return self.db.execute(
                "SELECT * FROM events WHERE tool LIKE ? ORDER BY ts DESC LIMIT ?",
                (f"%{tool}%", limit),
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self.db.close()
