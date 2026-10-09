from pathlib import Path
import time

from trafficgen.database import init_db, record_transaction
from trafficgen.dem import dem_summary


def test_dem_summary_tracks_endpoint_persona_and_application(tmp_path: Path):
    db = tmp_path / "test.db"
    init_db(db)
    now = time.time()
    rows = [
        {
            "timestamp": now,
            "run_id": "run-1",
            "endpoint_id": "ep-1",
            "persona": "knowledge_worker",
            "application": "web_saas",
            "request_type": "HTTP",
            "name": "web/page",
            "success": True,
            "response_time_ms": 100,
            "response_length": 1000,
        },
        {
            "timestamp": now - 1,
            "run_id": "run-1",
            "endpoint_id": "ep-1",
            "persona": "knowledge_worker",
            "application": "web_saas",
            "request_type": "HTTP",
            "name": "web/page",
            "success": False,
            "response_time_ms": 800,
            "response_length": 0,
            "error": "timeout",
        },
    ]
    for row in rows:
        record_transaction(db, row)

    summary = dem_summary(db, run_id="run-1", window_seconds=60)
    assert summary["requests"] == 2
    assert "web_saas" in summary["applications"]
    assert "knowledge_worker" in summary["personas"]
    assert "ep-1" in summary["endpoints"]
    assert summary["endpoints"]["ep-1"]["persona"] == "knowledge_worker"
