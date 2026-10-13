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

SNAPSHOT_INTERVAL_S = 2.0     # how often the active run's DEM is recomputed
SNAPSHOT_MAX_AGE_S = 6.0      # older than this, the workload computes the DEM itself rather than showing stale figures


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
        dem = dem_summary(Path(database_path), run_id=run_id, window_seconds=60, media_mode=media_mode or "strict")
    snapshot = {"created": time.time() if now is None else now, "run_id": run_id, "dem": dem,
                "compute_ms": round((time.perf_counter() - started) * 1000.0, 1)}
    write_dem_snapshot(Path(database_path), snapshot)
    return snapshot


def fresh_snapshot(snapshot, run_id, now=None):
    """The published snapshot when it is for this run and recent enough to show; otherwise None."""
    if not snapshot or snapshot.get("run_id") != run_id or not snapshot.get("dem"):
        return None
    if (time.time() if now is None else now) - snapshot["created"] > SNAPSHOT_MAX_AGE_S:
        return None
    return snapshot


def serve(database_path, stop, interval_s=SNAPSHOT_INTERVAL_S):
    """Publishes a snapshot every interval until stop is set. A failed pass is logged and the next one still runs."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            publish_once(database_path)
        except Exception:
            logging.exception("Cannot publish the DEM snapshot")
        stop.wait(max(0.0, interval_s - (time.monotonic() - started)))


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
