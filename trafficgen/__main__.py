from __future__ import annotations

import signal
import threading

from gevent.pywsgi import WSGIServer

from .app import app
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

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(
        f"NetEm Traffic Simulator listening on https://{settings.bind_host}:{settings.api_port}"
    )
    print(f"Admin password file: {settings.admin_password_path}")
    print(f"API key file: {settings.api_key_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
