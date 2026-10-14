from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path


# Diagnosis columns added after the first release; created by migration so new and
# upgraded databases share one definition.
DIAGNOSIS_COLUMNS = (
    ("cause", "TEXT"), ("wait_ms", "REAL"), ("transfer_ms", "REAL"), ("bytes_up", "INTEGER"),
    ("status_code", "INTEGER"), ("packets_sent", "INTEGER"), ("packets_lost", "INTEGER"), ("egress", "TEXT"),
    ("udp_jitter_ms", "REAL"), ("started_at", "REAL"), ("operation_id", "TEXT"), ("cancelled", "INTEGER"), ("transfer_direction", "TEXT"), ("expected_bytes", "INTEGER"),
    ("packet_rtt_histogram", "TEXT"), ("packet_variation_histogram", "TEXT"),
    ("packets_late", "INTEGER"), ("packet_deadline_ms", "REAL"), ("collection_expired", "INTEGER"), ("packet_paths", "TEXT"), ("packet_transitions", "TEXT"),
)
TRANSACTION_FIELDS = (
    "timestamp", "run_id", "endpoint_id", "persona", "application", "request_type", "name",
    "success", "response_time_ms", "response_length", "error", *(name for name, _ in DIAGNOSIS_COLUMNS),
)


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
            CREATE TABLE IF NOT EXISTS dem_snapshots (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                created REAL NOT NULL,
                run_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                effective_at REAL NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_contract_run ON run_contracts(run_id, effective_at);
            CREATE TABLE IF NOT EXISTS run_health (run_id TEXT PRIMARY KEY, dropped INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS dem_queries (key TEXT PRIMARY KEY, created REAL NOT NULL,
                arguments TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS operation_events (
                id INTEGER PRIMARY KEY, run_id TEXT, operation_id TEXT, timestamp REAL,
                kind TEXT, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_operation_run_time ON operation_events(run_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_operation_id ON operation_events(run_id, operation_id, kind);
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
        if not any(row["name"] == "score_model" for row in conn.execute("PRAGMA table_info(dem_samples)")):
            conn.execute("ALTER TABLE dem_samples ADD COLUMN score_model TEXT NOT NULL DEFAULT 'legacy-v1'")
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        if "endpoint_id" not in columns:
            conn.execute("ALTER TABLE transactions ADD COLUMN endpoint_id TEXT")
        for name, kind in DIAGNOSIS_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {kind}")
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "media_mode" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN media_mode TEXT NOT NULL DEFAULT 'strict'")
        if "label" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN label TEXT")
        if "traffic_path_json" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN traffic_path_json TEXT")
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
                target_users, spawn_rate, activity, personas_json, applications_json, media_mode, label, traffic_path_json
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                run.get("media_mode", "strict"),
                run.get("label"),
                json.dumps(run["traffic_path"], sort_keys=True) if run.get("traffic_path") else None,
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


def record_operation_events(path, items):
    with connect(path) as conn:
        conn.executemany("INSERT INTO operation_events(run_id, operation_id, timestamp, kind, payload) VALUES (?, ?, ?, ?, ?)",
            [(item.get("run_id"), item["operation_id"], item["timestamp"], item["kind"], json.dumps(item)) for item in items])


def operation_evidence(path, run_id, since, until):
    with connect(path) as conn:
        rows = conn.execute("SELECT kind, count(*) AS n FROM operation_events WHERE run_id=? AND timestamp>=? AND timestamp<? GROUP BY kind",
                            (run_id, since, until)).fetchall()
        clocks = conn.execute("SELECT count(*) FROM operation_events WHERE run_id=? AND json_extract(payload, '$.clock_discontinuity')=1",
                              (run_id,)).fetchone()[0]
        outstanding = conn.execute("""SELECT count(*) FROM operation_events s
            WHERE s.run_id=? AND s.kind='started' AND s.timestamp<? AND NOT EXISTS
            (SELECT 1 FROM operation_events e WHERE e.run_id=s.run_id AND e.operation_id=s.operation_id
             AND e.kind IN ('completed','cancelled') AND e.timestamp<?)""", (run_id, until, until)).fetchone()[0]
        overdue = conn.execute("""SELECT count(*) FROM operation_events s WHERE s.run_id=? AND s.kind='deadline_expired'
            AND s.timestamp<? AND NOT EXISTS (SELECT 1 FROM operation_events e WHERE e.run_id=s.run_id
            AND e.operation_id=s.operation_id AND e.kind IN ('completed','cancelled') AND e.timestamp<?)""",
            (run_id, until, until)).fetchone()[0]
    return {"overdue_at_end": overdue, "events": {row["kind"]: row["n"] for row in rows}, "outstanding_at_end": outstanding,
            "clock_discontinuities": clocks}


def record_transactions(path: Path, items: list[dict]):
    values = [(
        item.get("timestamp", time.time()), item.get("run_id"), item.get("endpoint_id"),
        item.get("persona"), item.get("application"), item.get("request_type", "HTTP"),
        item.get("name", "request"), 1 if item.get("success") else 0,
        item.get("response_time_ms"), int(item.get("response_length") or 0), item.get("error"),
        *(json.dumps(item[name]) if (name.endswith("histogram") or name in ("packet_paths", "packet_transitions")) and item.get(name) is not None else item.get(name) for name, _ in DIAGNOSIS_COLUMNS),
    ) for item in items]
    with connect(path) as conn:
        conn.executemany(f"""
            INSERT INTO transactions ({', '.join(TRANSACTION_FIELDS)})
            VALUES ({', '.join('?' for _ in TRANSACTION_FIELDS)})
        """, values)


def record_dem_sample(path: Path, sample: dict):
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO dem_samples (
                timestamp, run_id, users, requests_per_second, failures_per_second,
                availability_pct, p50_ms, p95_ms, experience_score, score_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                sample.get("score_model", "legacy-v1"),
            ),
        )


def write_dem_snapshot(path: Path, snapshot: dict):
    """Replaces the single published DEM snapshot."""
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dem_snapshots (id, created, run_id, payload) VALUES (1, ?, ?, ?)",
            (snapshot["created"], snapshot.get("run_id"), json.dumps(snapshot, sort_keys=True, default=str)),
        )


def read_dem_snapshot(path: Path):
    """The published DEM snapshot, or None when none has been published or it cannot be read."""
    with connect(path) as conn:
        row = conn.execute("SELECT payload FROM dem_snapshots WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except ValueError:
        return None


def query_dem_timeseries(path: Path, since: float, limit: int = 2000):
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM (SELECT timestamp, run_id, users, requests_per_second, score_model,
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


def query_transactions(path: Path, since: float, run_id=None, limit: int = 10000, until=None, intervals=None):
    clauses = ["timestamp >= ?", "timestamp < ?"]
    values = [since, time.time() if until is None else until]
    if run_id:
        clauses.append("run_id = ?")
        values.append(run_id)
    if intervals:
        clauses.append("(" + " OR ".join("(timestamp >= ? AND timestamp < ?)" for _ in intervals) + ")")
        values.extend(value for interval in intervals for value in interval)
    values.append(max(1, min(50000, int(limit))))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (SELECT {', '.join(TRANSACTION_FIELDS)}
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
        item["traffic_path"] = json.loads(item.pop("traffic_path_json") or "null")
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
        conn.execute("DELETE FROM operation_events WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM dem_samples WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM runs WHERE ended_at < ?", (cutoff,))
        conn.execute("DELETE FROM run_contracts WHERE run_id NOT IN (SELECT run_id FROM runs)")
        conn.execute("DELETE FROM run_health WHERE run_id NOT IN (SELECT run_id FROM runs)")


def update_run(path: Path, run: dict):
    with connect(path) as conn:
        conn.execute("UPDATE runs SET target_users=?, spawn_rate=?, activity=?, "
                     "personas_json=?, applications_json=?, media_mode=?, status=? WHERE run_id=?",
                     (run["target_users"], run["spawn_rate"], run["activity"],
                      json.dumps(run["personas"]), json.dumps(run["applications"]),
                      run.get("media_mode", "strict"), run["status"], run["run_id"]))


def save_contract(path, run, effective_at=None):
    """Append immutable configuration epochs; never overwrite historical scoring inputs."""
    effective_at = time.time() if effective_at is None else effective_at
    run["contract_epoch"] = effective_at
    payload = {key: run.get(key) for key in ("contract_epoch", "policy", "manifest", "score_model", "media_mode", "seed", "generation_mode", "media_pattern", "media_session_s", "rate_down_mbps", "rate_up_mbps", "traffic_path")}
    payload["dropped_transactions"] = run.get("dropped_transactions", 0)
    with connect(path) as conn:
        conn.execute("INSERT INTO run_contracts(run_id, effective_at, payload) VALUES (?, ?, ?)",
                     (run["run_id"], effective_at, json.dumps(payload)))


def record_data_loss(path, run_id, count):
    if not run_id:
        return
    with connect(path) as conn:
        conn.execute("INSERT INTO run_health(run_id, dropped) VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET dropped=dropped+excluded.dropped",
                     (run_id, count))


def scoring_contract(path, run_id, since, until):
    if not run_id:
        return None, False
    with connect(path) as conn:
        rows = conn.execute("SELECT effective_at, payload FROM run_contracts WHERE run_id=? AND effective_at < ? ORDER BY effective_at, id",
                            (run_id, until)).fetchall()
    if not rows:
        return None, False
    epochs = [row for row in rows if row["effective_at"] > since]
    # The first epoch is the run start, not an adjustment inside the requested window.
    mixed = any(row is not rows[0] for row in epochs)
    payload = json.loads(rows[-1]["payload"])
    with connect(path) as conn:
        health = conn.execute("SELECT dropped FROM run_health WHERE run_id=?", (run_id,)).fetchone()
    payload["dropped_transactions"] = health["dropped"] if health else 0
    return payload, mixed
