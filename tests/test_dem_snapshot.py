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



def test_production_live_status_never_enqueues_when_publisher_is_missing(simulator, monkeypatch):
    import gevent
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    monkeypatch.setenv('TRAFFICGEN_DEM_WORKER', '1')
    monkeypatch.setattr(controller, 'query_dem', lambda **kw: (_ for _ in ()).throw(AssertionError('status queued aggregation')))
    run = {'run_id': 'missing', 'status': 'running', 'media_mode': 'strict',
           'contract_epoch': 3, 'live_context': {'revision': 2, 'phase_id': 'phase-2',
               'window_seconds': 15, 'until': None}}
    # Concurrent API reads must return a valid pending payload, retaining run/phase.
    # A blocked report request or the legacy live lock cannot delay these reads.
    controller.dem_lock.acquire()
    try:
        started = time.monotonic()
        with gevent.Timeout(1):
            jobs = [gevent.spawn(controller._live_dem, run) for _ in range(5)]
            results = [job.get() for job in jobs]
        assert time.monotonic() - started < 1
        assert all(item['live_phase_id'] == 'phase-2' and item['experience_score'] is None for item in results)
        assert all(item['evidence_issues'] == ['live_snapshot_pending'] for item in results)
        controller.run = run
        token = controller.settings.api_key_path.read_text().strip()
        with gevent.Timeout(1):
            response = simulator.test_client().get('/api/v1/status', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200
        assert response.json['status'] == 'running'
        assert response.json['dem']['live_phase_id'] == 'phase-2'
        controller.run = None
        assert controller.status()['status'] == 'idle'
    finally:
        controller.dem_lock.release()
        controller.run = None
        monkeypatch.setenv('TRAFFICGEN_DEM_WORKER', '0')


def test_slow_report_does_not_block_live_publication(tmp_path, monkeypatch):
    from trafficgen import aggregation
    entered, release = threading.Event(), threading.Event()
    published = []
    stop = threading.Event()
    def slow_report(path):
        entered.set()
        release.wait(3)
        return False
    monkeypatch.setattr(aggregation, 'process_one', slow_report)
    monkeypatch.setattr(dem_worker, 'publish_once', lambda path: published.append(time.monotonic()))
    thread = threading.Thread(target=dem_worker.serve, args=(tmp_path / 'traffic.db', stop, .02), daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        deadline = time.monotonic() + 1
        while len(published) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(published) >= 3
        assert not release.is_set()
    finally:
        stop.set()
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_sampler_failure_does_not_poison_transaction_persistence(simulator, monkeypatch):
    from trafficgen import engine
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    class OnePass:
        calls = 0
        def wait(self, seconds):
            self.calls += 1
            return self.calls > 1
    original = controller.stop_event
    controller.stop_event = OnePass()
    controller.run = {'run_id': 'sample-read-error', 'status': 'running'}
    monkeypatch.setattr(engine, 'offloaded', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('snapshot read failed')))
    try:
        controller._dem_loop()
        assert controller.persistence_error is None
        assert controller.dropped_transactions == 0
        assert controller.dem_sampler_error == 'snapshot read failed'
    finally:
        controller.stop_event = original
        controller.run = None


def test_live_snapshot_read_has_a_short_database_lock_bound(tmp_path):
    import sqlite3
    path = make_database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
    blocker = sqlite3.connect(path)
    blocker.execute('BEGIN EXCLUSIVE')
    try:
        started = time.monotonic()
        assert database.read_dem_snapshot(path) is None
        assert time.monotonic() - started < .5
    finally:
        blocker.rollback()
        blocker.close()
