"""Publishes the live DEM of the active run from its own process, so the workload's event loop never computes it.

The workload process serves every simulated user on one event loop. Computing the DEM there held that loop for hundreds
of milliseconds at a time on a busy run, stalling every user at once. This process has its own interpreter and CPU: it
computes the figures on another core and publishes them as one database row that the workload reads.

The process exits when its standard input closes, which happens when the simulator that started it exits, on any
platform.
"""

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from .database import connect, write_dem_snapshot
from .dem import dem_summary
from . import live_window

SNAPSHOT_INTERVAL_S = 1.0     # how often the active run's DEM is recomputed
SNAPSHOT_MAX_AGE_S = 6.0      # older than this, status reports pending evidence instead of queuing more work


def active_run(database_path):
    with connect(Path(database_path)) as conn:
        row = conn.execute(
            "SELECT run_id, media_mode FROM runs WHERE status IN ('starting', 'running') "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return (row["run_id"], row["media_mode"]) if row else (None, None)


def publish_once(database_path, now=None):
    """Computes the active run's DEM, or records that no run is active, and publishes the snapshot."""
    started = time.perf_counter()
    run_id, media_mode = active_run(database_path)
    dem = None
    if run_id is not None:
        context = live_window.read_context(database_path, run_id)
        dem = (live_window.measure(database_path, run_id, context, media_mode or 'strict') if context else
               dem_summary(Path(database_path), run_id=run_id, media_mode=media_mode or 'strict', window_seconds=15))
    snapshot = {"created": time.time() if now is None else now, "run_id": run_id, "dem": dem,
                "created_monotonic": time.monotonic(),
                "compute_ms": round((time.perf_counter() - started) * 1000.0, 1)}
    write_dem_snapshot(Path(database_path), snapshot)
    return snapshot


def fresh_snapshot(snapshot, run_id, now=None, contract_epoch=None, live_revision=None):
    """The published snapshot when it is for this run and recent enough to show; otherwise None."""
    if not snapshot or snapshot.get("run_id") != run_id or not snapshot.get("dem"):
        return None
    if live_revision is not None and snapshot['dem'].get('live_context_revision') != live_revision:
        return None
    if contract_epoch is not None and snapshot["dem"].get("contract_epoch") != contract_epoch:
        return None
    if not 0 <= (time.time() if now is None else now) - snapshot["created"] <= SNAPSHOT_MAX_AGE_S:
        return None
    if now is None and snapshot.get("created_monotonic") is not None:
        age = time.monotonic() - snapshot["created_monotonic"]
        if not 0 <= age <= SNAPSHOT_MAX_AGE_S or abs((time.time() - snapshot["created"]) - age) > 1:
            return None
    return snapshot


def serve(database_path, stop, interval_s=SNAPSHOT_INTERVAL_S):
    """Live publication never waits for an explicit report aggregation."""
    from .aggregation import process_one

    def reports():
        while not stop.is_set():
            try:
                processed = process_one(database_path)
            except Exception:
                logging.exception("Cannot aggregate a report")
                processed = False
            if not processed:
                stop.wait(.05)

    if stop.is_set():
        return
    # Both tasks already run outside the workload interpreter. A separate report
    # thread also keeps slow queries/database waits off the live publisher loop.
    worker = threading.Thread(target=reports, name='dem-report-aggregation', daemon=True)
    worker.start()
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                publish_once(database_path)
            except Exception:
                logging.exception("Cannot publish the DEM snapshot")
            stop.wait(max(.01, interval_s - (time.monotonic() - started)))
    finally:
        worker.join(timeout=1)


def _stop_when_stdin_closes(stop):
    try:
        sys.stdin.read()
    finally:
        stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publishes the live DEM snapshot for the Traffic Simulator.")
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    threading.Thread(target=_stop_when_stdin_closes, args=(stop,), name="dem-worker-parent-watch", daemon=True).start()
    serve(args.database, stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

