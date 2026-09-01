"""Durable state store: checkpoints + idempotency, backed by SQLite.

This is the piece that makes the pipeline resumable instead of restart-from-
zero: every step result is written here BEFORE the orchestrator advances, so
a crash (or an exhausted retry budget) never loses more than the in-flight
step. `has_succeeded` is what the engine checks before *every* step -
including publish - so a retried run can't double-execute something that
already went through.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from orchestrator.models import RunState, StepResult, StepStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    seed_topic TEXT NOT NULL,
    platforms TEXT NOT NULL,       -- json list
    manifest_path TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    output_ref TEXT,
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (run_id, step_name)
);
"""


class StateStore:
    def __init__(self, db_path: str = "pipeline_state.db"):
        self.db_path = db_path
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create_run(self, run: RunState) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, seed_topic, platforms, manifest_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run.run_id, run.seed_topic, json.dumps(run.platforms), run.manifest_path, run.created_at),
            )

    def save_step_result(self, result: StepResult) -> None:
        """Upsert - safe to call repeatedly for the same (run_id, step_name)."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO steps (run_id, step_name, status, attempt, output_ref, error, started_at, finished_at)
                VALUES (:run_id, :step_name, :status, :attempt, :output_ref, :error, :started_at, :finished_at)
                ON CONFLICT(run_id, step_name) DO UPDATE SET
                    status=excluded.status, attempt=excluded.attempt, output_ref=excluded.output_ref,
                    error=excluded.error, started_at=excluded.started_at, finished_at=excluded.finished_at
                """,
                {
                    "run_id": result.run_id,
                    "step_name": result.step_name,
                    "status": result.status.value,
                    "attempt": result.attempt,
                    "output_ref": result.output_ref,
                    "error": result.error,
                    "started_at": result.started_at,
                    "finished_at": result.finished_at,
                },
            )

    def has_succeeded(self, run_id: str, step_name: str) -> bool:
        row = self._fetch_step(run_id, step_name)
        return row is not None and row["status"] == StepStatus.SUCCEEDED.value

    def get_step(self, run_id: str, step_name: str) -> Optional[StepResult]:
        row = self._fetch_step(run_id, step_name)
        if row is None:
            return None
        return _row_to_step_result(row)

    def _fetch_step(self, run_id: str, step_name: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_name = ?", (run_id, step_name)
            )
            return cur.fetchone()

    def get_run(self, run_id: str) -> Optional[RunState]:
        with self._conn() as conn:
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                return None
            step_rows = conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY rowid ASC", (run_id,)
            ).fetchall()

        run = RunState(
            run_id=run_row["run_id"],
            seed_topic=run_row["seed_topic"],
            platforms=json.loads(run_row["platforms"]),
            manifest_path=run_row["manifest_path"],
            created_at=run_row["created_at"],
        )
        for r in step_rows:
            run.steps[r["step_name"]] = _row_to_step_result(r)
        return run

    def list_runs(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC").fetchall()
        return [r["run_id"] for r in rows]


def _row_to_step_result(row: sqlite3.Row) -> StepResult:
    return StepResult(
        run_id=row["run_id"],
        step_name=row["step_name"],
        status=StepStatus(row["status"]),
        attempt=row["attempt"],
        output_ref=row["output_ref"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
