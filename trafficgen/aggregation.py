"""Bounded, deduplicated requests handled by the supervised aggregation process."""
import hashlib
import json
import time

from .database import connect


def enqueue(path, arguments):
    encoded = json.dumps(arguments, sort_keys=True)
    key = hashlib.sha256(encoded.encode()).hexdigest()
    now = time.monotonic()
    with connect(path) as conn:
        conn.execute("DELETE FROM dem_queries WHERE created < ? OR created > ?", (now - 30, now + 1))
        row = conn.execute("SELECT created FROM dem_queries WHERE key=?", (key,)).fetchone()
        if row and now - row["created"] > 2:
            conn.execute("DELETE FROM dem_queries WHERE key=? AND result IS NOT NULL", (key,))
        count = conn.execute("SELECT count(*) FROM dem_queries WHERE result IS NULL").fetchone()[0]
        if count >= 8 and not row:
            raise ValueError("Aggregation queue is full; retry after current measurements complete.")
        conn.execute("INSERT OR IGNORE INTO dem_queries(key, created, arguments) VALUES (?, ?, ?)", (key, now, encoded))
    return key


def read_result(path, key):
    with connect(path) as conn:
        row = conn.execute("SELECT result FROM dem_queries WHERE key=?", (key,)).fetchone()
    return json.loads(row["result"]) if row and row["result"] is not None else None


def process_one(path):
    from .dem import dem_summary
    with connect(path) as conn:
        row = conn.execute("SELECT key, arguments FROM dem_queries WHERE result IS NULL ORDER BY created LIMIT 1").fetchone()
    if not row:
        return False
    try:
        result = {"dem": dem_summary(path, **json.loads(row["arguments"]))}
    except Exception as exc:
        result = {"error": str(exc)[:240]}
    with connect(path) as conn:
        conn.execute("UPDATE dem_queries SET result=? WHERE key=?", (json.dumps(result), row["key"]))
    return True
