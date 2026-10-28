"""SQLite-backed task storage for the Arix Agent API.

Each operation opens its own short-lived connection, so the store is safe to
use from FastAPI's threadpool without sharing a connection between threads.
"""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


class TaskStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        return data

    def insert(self, task_id, goal, status, created_at, updated_at) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO tasks (id, goal, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, goal, status, created_at.isoformat(), updated_at.isoformat()),
            )

    def get(self, task_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_newest_first(self) -> list[dict]:
        # rowid grows with insertion, so it is creation order.
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY rowid DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_status(self, task_id, status, updated_at) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at.isoformat(), task_id),
            )
