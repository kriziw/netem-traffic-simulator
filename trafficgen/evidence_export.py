"""Bounded, cursor-paged evidence reads; never export the appliance database itself."""
import json
import math

from .database import connect


def evidence_page(path, run_id, since, until, kind="transactions", cursor=0, ceiling=None, limit=1000):
    tables = {"transactions": "transactions", "operations": "operation_events", "contracts": "run_contracts"}
    if kind not in tables or not run_id or not all(math.isfinite(x) for x in (since, until)) or since >= until or until - since > 86400:
        raise ValueError("Evidence requires a run, known kind and a finite interval of at most 24 hours.")
    if cursor < 0 or (ceiling is not None and ceiling < 0) or not 1 <= limit <= 2000:
        raise ValueError("Invalid evidence cursor or page size.")
    table = tables[kind]
    column = "effective_at" if kind == "contracts" else "timestamp"
    # Include the preceding contract so even a borrowed run has its scoring inputs.
    left = 0 if kind == "contracts" else since
    with connect(path) as conn:
        if not conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            raise LookupError("Run not found.")
        if ceiling is None:
            ceiling = conn.execute(f"SELECT coalesce(max(id),0) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM {table} WHERE run_id=? AND {column}>=? AND {column}<? AND id>? AND id<=? ORDER BY id LIMIT ?",
                            (run_id, left, until, cursor, ceiling, limit + 1)).fetchall()
    more = len(rows) > limit
    items = [dict(row) for row in rows[:limit]]
    for item in items:
        if "payload" in item:
            item["payload"] = json.loads(item["payload"])
    return {"schema_version": 1, "run_id": run_id, "since": since, "until": until, "kind": kind,
            "items": items, "ceiling": ceiling, "next_cursor": items[-1]["id"] if more else None,
            "complete": not more, "timestamp_basis": "simulator Unix seconds; transactions use completion time"}
