from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: Path):
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                ended_at REAL,
                status TEXT NOT NULL,
                profile TEXT NOT NULL,
                target TEXT NOT NULL,
                target_users INTEGER NOT NULL,
                spawn_rate REAL NOT NULL,
                activity TEXT NOT NULL,
                personas_json TEXT NOT NULL,
                applications_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                run_id TEXT,
                endpoint_id TEXT,
                persona TEXT,
                application TEXT,
                request_type TEXT NOT NULL,
                name TEXT NOT NULL,
                success INTEGER NOT NULL,
                response_time_ms REAL,
                response_length INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_time
                ON transactions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_transactions_run
                ON transactions(run_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_transactions_app
                ON transactions(application, timestamp);
            CREATE INDEX IF NOT EXISTS idx_transactions_persona
                ON transactions(persona, timestamp);

            CREATE TABLE IF NOT EXISTS dem_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                run_id TEXT,
                users INTEGER NOT NULL,
                requests_per_second REAL NOT NULL,
                failures_per_second REAL NOT NULL,
                availability_pct REAL,
                p50_ms REAL,
                p95_ms REAL,
                experience_score REAL
            );
            CREATE INDEX IF NOT EXISTS idx_dem_time
                ON dem_samples(timestamp);
            """
        )
        sample_columns = list(conn.execute("PRAGMA table_info(dem_samples)"))
        if any(row["name"] in ("availability_pct", "experience_score") and row["notnull"]
               for row in sample_columns):
            conn.executescript("""
                ALTER TABLE dem_samples RENAME TO old_dem_samples;
                CREATE TABLE dem_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                    run_id TEXT, users INTEGER NOT NULL, requests_per_second REAL NOT NULL,
                    failures_per_second REAL NOT NULL, availability_pct REAL,
                    p50_ms REAL, p95_ms REAL, experience_score REAL
                );
                INSERT INTO dem_samples SELECT * FROM old_dem_samples;
                DROP TABLE old_dem_samples;
                CREATE INDEX idx_dem_time ON dem_samples(timestamp);
            """)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        if "endpoint_id" not in columns:
            conn.execute("ALTER TABLE transactions ADD COLUMN endpoint_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_endpoint "
            "ON transactions(endpoint_id, timestamp)"
        )


def create_run(path: Path, run: dict):
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, started_at, ended_at, status, profile, target,
                target_users, spawn_rate, activity, personas_json, applications_json
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["run_id"],
                run["started_at"],
                run["status"],
                run["profile"],
                run["target"],
                run["target_users"],
                run["spawn_rate"],
                run["activity"],
                json.dumps(run["personas"], sort_keys=True),
                json.dumps(run["applications"], sort_keys=True),
            ),
        )


def finish_run(path: Path, run_id: str, status: str = "stopped"):
    with connect(path) as conn:
        conn.execute(
            "UPDATE runs SET ended_at = ?, status = ? WHERE run_id = ?",
            (time.time(), status, run_id),
        )


def record_transaction(path: Path, item: dict):
    record_transactions(path, [item])


def record_transactions(path: Path, items: list[dict]):
    values = [(
        item.get("timestamp", time.time()), item.get("run_id"), item.get("endpoint_id"),
        item.get("persona"), item.get("application"), item.get("request_type", "HTTP"),
        item.get("name", "request"), 1 if item.get("success") else 0,
        item.get("response_time_ms"), int(item.get("response_length") or 0), item.get("error"),
    ) for item in items]
    with connect(path) as conn:
        conn.executemany("""
            INSERT INTO transactions (timestamp, run_id, endpoint_id, persona,
                application, request_type, name, success, response_time_ms, response_length, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)


def record_dem_sample(path: Path, sample: dict):
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO dem_samples (
                timestamp, run_id, users, requests_per_second, failures_per_second,
                availability_pct, p50_ms, p95_ms, experience_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample["timestamp"],
                sample.get("run_id"),
                sample["users"],
                sample["requests_per_second"],
                sample["failures_per_second"],
                sample["availability_pct"],
                sample.get("p50_ms"),
                sample.get("p95_ms"),
                sample["experience_score"],
            ),
        )


def query_dem_timeseries(path: Path, since: float, limit: int = 2000):
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM (SELECT timestamp, run_id, users, requests_per_second,
                   failures_per_second, availability_pct, p50_ms, p95_ms,
                   experience_score
            FROM dem_samples
            WHERE timestamp >= ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?) ORDER BY timestamp ASC
            """,
            (since, max(1, min(5000, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]


def query_transactions(path: Path, since: float, run_id=None, limit: int = 10000):
    clauses = ["timestamp >= ?"]
    values = [since]
    if run_id:
        clauses.append("run_id = ?")
        values.append(run_id)
    values.append(max(1, min(50000, int(limit))))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (SELECT timestamp, run_id, endpoint_id, persona, application, request_type, name,
                   success, response_time_ms, response_length, error
            FROM transactions
            WHERE {' AND '.join(clauses)}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?) ORDER BY timestamp ASC
            """,
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def recent_runs(path: Path, limit: int = 20):
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM runs
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (max(1, min(100, int(limit))),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["personas"] = json.loads(item.pop("personas_json"))
        item["applications"] = json.loads(item.pop("applications_json"))
        result.append(item)
    return result


def recover_runs(path: Path):
    with connect(path) as conn:
        conn.execute("UPDATE runs SET status = 'interrupted', ended_at = ? "
                     "WHERE ended_at IS NULL", (time.time(),))


def prune_history(path: Path, retention_days: int = 7):
    cutoff = time.time() - retention_days * 86400
    with connect(path) as conn:
        conn.execute("DELETE FROM transactions WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM dem_samples WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM runs WHERE ended_at < ?", (cutoff,))


def update_run(path: Path, run: dict):
    with connect(path) as conn:
        conn.execute("UPDATE runs SET target_users=?, spawn_rate=?, activity=?, "
                     "personas_json=?, applications_json=?, status=? WHERE run_id=?",
                     (run["target_users"], run["spawn_rate"], run["activity"],
                      json.dumps(run["personas"]), json.dumps(run["applications"]),
                      run["status"], run["run_id"]))
