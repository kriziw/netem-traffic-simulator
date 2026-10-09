import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def test_cli_serves_https_and_stops_cleanly(tmp_path):
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-out", str(cert), "-keyout", str(key)], check=True, capture_output=True)
    ports = []
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    environment = dict(os.environ, TRAFFICGEN_CONFIG_DIR=str(tmp_path / "config"), TRAFFICGEN_RUNTIME_DIR=str(tmp_path / "runtime"), TRAFFICGEN_DATABASE=str(tmp_path / "runtime/db.sqlite"), TRAFFICGEN_TLS_CERT=str(cert), TRAFFICGEN_TLS_KEY=str(key), TRAFFICGEN_API_PORT=str(ports[0]), TRAFFICGEN_DISCOVERY_PORT=str(ports[1]))
    with open(tmp_path / "cli.log", "w+") as output:
        process = subprocess.Popen([sys.executable, "-m", "trafficgen"], cwd=Path(__file__).resolve().parents[1], env=environment, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with urlopen(f"https://127.0.0.1:{ports[0]}/api/v1/health", context=ssl._create_unverified_context(), timeout=1) as response:
                        assert response.status == 200
                        break
                except OSError:
                    assert process.poll() is None
                    time.sleep(0.05)
            else:
                raise AssertionError("CLI did not start")
            process.terminate()
            assert process.wait(timeout=8) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        output.seek(0)
        assert "Traceback" not in output.read()
