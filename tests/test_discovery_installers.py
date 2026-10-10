"""Discovery resilience and sandboxed installer upgrade regressions."""
import dataclasses
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import gevent
import pytest

from trafficgen.discovery import DISCOVERY_MAGIC, discovery_server


def test_discovery_survives_non_object_datagrams(simulator):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    settings = dataclasses.replace(simulator.config["TRAFFICGEN_SETTINGS"], discovery_port=port)
    stop = threading.Event()
    listener = gevent.spawn(discovery_server, settings, stop)
    gevent.sleep(0.02)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(1)
    try:
        for payload in ([], None, 42, {"protocol": DISCOVERY_MAGIC, "nonce": []}):
            probe.sendto(json.dumps(payload).encode(), ("127.0.0.1", port))
        probe.sendto(json.dumps({"protocol": DISCOVERY_MAGIC, "nonce": "test"}).encode(), ("127.0.0.1", port))
        payload = json.loads(probe.recv(8192))
        assert payload["nonce"] == "test"
        assert payload["service"] == "netem-traffic-simulator"
        assert "api_key" not in payload
        assert not listener.dead
    finally:
        stop.set()
        probe.close()
        listener.kill()


@pytest.mark.parametrize("installer", ["install-lxc.sh", "install-target.sh"])
@pytest.mark.parametrize("source_location", ["in-place", "private-source"])
@pytest.mark.parametrize("target_management", [False, True])
def test_in_place_reinstall_preserves_source_and_restarts_service(tmp_path, installer, source_location, target_management):
    # Execute actual scripts twice with system operations replaced by stubs.
    # All absolute write paths are redirected under tmp_path; no host services change.
    source = Path(__file__).resolve().parents[1]
    application = tmp_path / "application"
    installation_source = application if source_location == "in-place" else tmp_path / "private-source"
    installation_source.mkdir(mode=0o700)
    (installation_source / "scripts").mkdir()
    shutil.copytree(source / "deploy", installation_source / "deploy")
    (installation_source / "requirements.txt").write_text("# test\n")
    (installation_source / "source-marker.py").write_text("# must survive in-place reinstall\n")
    config = tmp_path / "config"
    config.mkdir()
    for name in ("api.key", "admin.password", "session.secret", "tls.crt", "tls.key"):
        (config / name).write_text("preserved-" + name)
    units = tmp_path / "units"
    units.mkdir()
    manager_config = tmp_path / "manager-config"
    if target_management and installer == "install-target.sh":
        manager_config.mkdir()
        (manager_config / "manager.env").write_text("NETEM_TARGET_MANAGER_HOST=192.168.10.20\n")
        (manager_config / "api.key").write_text("ntt_" + "a" * 64)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", str(manager_config / "tls.key"),
                        "-out", str(manager_config / "tls.crt")], check=True, capture_output=True)
        old_key = (manager_config / "tls.key").read_bytes()
    script = (source / "scripts" / installer).read_text().replace('/opt/netem-traffic-simulator', str(application)).replace('/etc/netem-traffic-simulator', str(config)).replace('/var/lib/netem-traffic-simulator', str(tmp_path / 'runtime')).replace('/etc/systemd/system', str(units)).replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
    script = script.replace('/etc/netem-traffic-target-manager', str(manager_config)).replace('/var/lib/netem-traffic-target-manager', str(tmp_path / 'manager-runtime')).replace('/var/lib/netem-traffic-target-admin', str(tmp_path / 'target-admin'))
    (installation_source / "scripts" / installer).write_text(script)
    helper = (source / "scripts/configure-lxc-service.sh").read_text().replace('/etc/systemd/system', str(units))
    (installation_source / "scripts/configure-lxc-service.sh").write_text(helper)
    shutil.copy(source / "scripts/ensure-time-sync.sh", installation_source / "scripts/ensure-time-sync.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    logfile = tmp_path / "calls"
    for name in ("apt-get", "id", "chown", "useradd", "systemctl"):
        path = bin_dir / name
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$INSTALL_LOG"\nexit 0\n')
        path.chmod(0o755)
    path = bin_dir / "systemd-detect-virt"
    path.write_text('#!/bin/sh\necho lxc\n')
    path.chmod(0o755)
    path = bin_dir / "python3"
    path.write_text('#!/bin/sh\n[ "$1" = "-m" ] || exit 0\nmkdir -p "$3/bin"\nprintf "#!/bin/sh\\nexit 0\\n" > "$3/bin/pip"\nchmod +x "$3/bin/pip"\n')
    path.chmod(0o755)
    environment = dict(os.environ, PATH=str(bin_dir) + ":" + os.environ["PATH"], INSTALL_LOG=str(logfile))
    for _ in range(2):
        subprocess.run(["bash", str(installation_source / "scripts" / installer)], env=environment, check=True, capture_output=True)
    assert application.stat().st_mode & 0o777 == 0o755
    assert (application / "source-marker.py").exists()
    assert (config / "api.key").read_text() == "preserved-api.key"
    assert (config / "tls.key").read_text() == "preserved-tls.key"
    service = "netem-traffic-simulator" if installer == "install-lxc.sh" else "netem-traffic-target"
    assert logfile.read_text().splitlines().count(str(bin_dir / 'systemctl') + " restart " + service) == 2
    assert "ProtectSystem=false" in (units / (service + ".service.d") / "10-lxc.conf").read_text()
    # In a container the clock is the host's: the installer must not try to change time sync.
    assert "systemd-timesyncd" not in logfile.read_text()
    if target_management and installer == "install-target.sh":
        assert (manager_config / "api.key").read_text() == "ntt_" + "a" * 64
        assert (manager_config / "tls.key").read_bytes() == old_key
        assert (manager_config / "manager.env").read_text() == "NETEM_TARGET_MANAGER_HOST=192.168.10.20\n"
        assert (units / "netem-traffic-target-manager.service").exists()
        assert "enable --now netem-traffic-target-admin.path" in logfile.read_text()
