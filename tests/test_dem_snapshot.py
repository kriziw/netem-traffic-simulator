"""The live DEM is computed by the worker process and read by the workload: fresh snapshots are used, stale ones are not,
and the worker exits with the simulator that started it."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from trafficgen import database, dem_worker


def make_database(directory):
    path = Path(directory) / "traffic.db"
    database.init_db(path)
    return path


def make_run(path, run_id, status="running"):
    database.create_run(path, {
        "run_id": run_id, "started_at": time.time(), "status": status, "profile": "office", "target": "http://t:8090",
        "target_users": 10, "spawn_rate": 10, "activity": "normal", "personas": {}, "applications": {},
        "media_mode": "strict",
    })


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = make_database(self.directory)

    def test_with_no_active_run_the_snapshot_says_so_and_computes_nothing(self):
        snapshot = dem_worker.publish_once(self.path)
        self.assertIsNone(snapshot["run_id"])
        self.assertIsNone(snapshot["dem"])
        self.assertEqual(database.read_dem_snapshot(self.path)["run_id"], None)

    def test_an_active_run_is_computed_and_published(self):
        make_run(self.path, "run-live")
        snapshot = dem_worker.publish_once(self.path)
        self.assertEqual(snapshot["run_id"], "run-live")
        self.assertIn("requests", snapshot["dem"])
        published = database.read_dem_snapshot(self.path)
        self.assertEqual(published["run_id"], "run-live")
        self.assertEqual(published["dem"]["requests"], 0)
        self.assertGreaterEqual(published["compute_ms"], 0)

    def test_a_finished_run_is_not_treated_as_active(self):
        make_run(self.path, "run-done", status="stopped")
        self.assertIsNone(dem_worker.publish_once(self.path)["run_id"])


class FreshnessTests(unittest.TestCase):
    def snapshot(self, created, run_id="run-live", dem=None):
        return {"created": created, "run_id": run_id, "dem": {"requests": 3} if dem is None else dem}

    def test_a_fresh_snapshot_for_this_run_is_used(self):
        now = 1000.0
        self.assertIsNotNone(dem_worker.fresh_snapshot(self.snapshot(now - 1), "run-live", now=now))

    def test_a_stale_snapshot_is_not_used(self):
        now = 1000.0
        stale = now - dem_worker.SNAPSHOT_MAX_AGE_S - 1
        self.assertIsNone(dem_worker.fresh_snapshot(self.snapshot(stale), "run-live", now=now))

    def test_a_snapshot_for_another_run_is_not_used(self):
        self.assertIsNone(dem_worker.fresh_snapshot(self.snapshot(time.time()), "run-other"))

    def test_an_idle_snapshot_has_no_figures_to_show(self):
        idle = {"created": time.time(), "run_id": None, "dem": None}
        self.assertIsNone(dem_worker.fresh_snapshot(idle, None))


class ServeTests(unittest.TestCase):
    def test_serve_returns_at_once_when_asked_to_stop(self):
        path = make_database(tempfile.mkdtemp())
        stop = threading.Event()
        stop.set()
        dem_worker.serve(path, stop)
        self.assertIsNone(database.read_dem_snapshot(path))

    def test_serve_publishes_until_stopped(self):
        path = make_database(tempfile.mkdtemp())
        stop = threading.Event()
        thread = threading.Thread(target=dem_worker.serve, args=(path, stop, 0.05), daemon=True)
        thread.start()
        time.sleep(0.2)
        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(database.read_dem_snapshot(path))


class WorkerProcessTests(unittest.TestCase):
    def test_a_worker_process_publishes_and_exits_when_its_parent_closes_its_input(self):
        path = make_database(tempfile.mkdtemp())
        make_run(path, "run-worker")
        env = dict(os.environ)
        process = subprocess.Popen([sys.executable, "-m", "trafficgen.dem_worker", "--database", str(path)],
                                   stdin=subprocess.PIPE, env=env, cwd=str(Path(__file__).resolve().parents[1]))
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and database.read_dem_snapshot(path) is None:
                time.sleep(0.2)
            self.assertEqual(database.read_dem_snapshot(path)["run_id"], "run-worker")
        finally:
            process.stdin.close()
            self.assertEqual(process.wait(timeout=30), 0)


def test_the_workload_reads_a_fresh_snapshot_instead_of_computing(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    run = {"run_id": "run-x", "status": "running", "media_mode": "strict"}
    database.write_dem_snapshot(controller.settings.database_path,
                                {"created": time.time(), "run_id": "run-x", "dem": {"requests": 7}, "compute_ms": 1.0})
    assert controller._live_dem(run)["requests"] == 7


def test_a_stale_snapshot_is_recomputed_instead_of_shown(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    run = {"run_id": "run-x", "status": "running", "media_mode": "strict"}
    database.write_dem_snapshot(controller.settings.database_path,
                                {"created": time.time() - 60, "run_id": "run-x", "dem": {"requests": 7}, "compute_ms": 1.0})
    assert controller._live_dem(run)["requests"] == 0


if __name__ == "__main__":
    unittest.main()
