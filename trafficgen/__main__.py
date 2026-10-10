from __future__ import annotations

from gevent import monkey, signal_handler

# Patch before importing SSL, Flask, threading or the workload engine.
monkey.patch_all()

import signal
import threading

from gevent.pywsgi import WSGIServer

from .app import app, path_watchdog
from .config import ensure_admin_password, ensure_api_key, load_settings
from .discovery import discovery_server


def main():
    settings = load_settings()
    api_key = ensure_api_key(settings)
    admin_password = ensure_admin_password(settings)

    stop_event = threading.Event()
    discovery = threading.Thread(
        target=discovery_server,
        args=(settings, stop_event),
        name="trafficgen-discovery",
        daemon=True,
    )
    discovery.start()
    watchdog = threading.Thread(target=path_watchdog, args=(app, stop_event), name="trafficgen-path-watchdog", daemon=True)
    watchdog.start()

    server = WSGIServer(
        (settings.bind_host, settings.api_port),
        app,
        certfile=str(settings.tls_cert),
        keyfile=str(settings.tls_key),
        log=None,
    )

    def shutdown(*_args):
        stop_event.set()
        server.stop(timeout=2)

    handlers = [signal_handler(signal.SIGTERM, shutdown), signal_handler(signal.SIGINT, shutdown)]

    print(
        f"NetEm Traffic Simulator listening on https://{settings.bind_host}:{settings.api_port}"
    )
    print(f"Admin password file: {settings.admin_password_path}")
    print(f"API key file: {settings.api_key_path}")
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        app.config["TRAFFICGEN_CONTROLLER"].shutdown()
        discovery.join(timeout=2)


if __name__ == "__main__":
    main()
