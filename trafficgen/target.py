from __future__ import annotations

from gevent import monkey, signal_handler

monkey.patch_all()

import argparse
import signal
from gevent.pywsgi import WSGIServer
import json
import socket
import threading
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


def bounded_size(raw, minimum=1, maximum=8192):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = minimum
    return max(minimum, min(maximum, value))


def byte_stream(size_bytes, chunk=65536):
    remaining = size_bytes
    payload = b"x" * min(chunk, size_bytes)
    while remaining > 0:
        part = payload[: min(len(payload), remaining)]
        yield part
        remaining -= len(part)


@app.get("/health")
def health():
    return {"status": "ok", "service": "netem-traffic-target"}


@app.get("/web/page")
def web_page():
    kb = bounded_size(request.args.get("kb"), 1, 1024)
    return Response(byte_stream(kb * 1024), mimetype="application/octet-stream")


@app.post("/api/action")
def api_action():
    return jsonify({"ok": True, "received": request.get_json(silent=True) or {}})


@app.get("/collaboration/poll")
def collaboration_poll():
    return jsonify(
        {
            "presence": "available",
            "messages": [],
            "sequence": 1,
        }
    )


@app.post("/collaboration/message")
def collaboration_message():
    return jsonify({"accepted": True})


@app.get("/files/download")
def file_download():
    kb = bounded_size(request.args.get("kb"), 1, 8192)
    return Response(byte_stream(kb * 1024), mimetype="application/octet-stream")


@app.post("/files/upload")
def file_upload():
    request.max_content_length = 16 * 1024 * 1024
    length = len(request.get_data(cache=False))
    return jsonify({"accepted_bytes": length})


@app.get("/developer/artifact")
def developer_artifact():
    kb = bounded_size(request.args.get("kb"), 1, 8192)
    return Response(byte_stream(kb * 1024), mimetype="application/octet-stream")


@app.get("/developer/api")
def developer_api():
    return jsonify({"repository": "synthetic", "status": "ok", "objects": 42})


@app.get("/updates/package")
def updates_package():
    kb = bounded_size(request.args.get("kb"), 1, 16384)
    return Response(byte_stream(kb * 1024), mimetype="application/octet-stream")


@app.post("/backup/upload")
def backup_upload():
    length = len(request.get_data(cache=False))
    return jsonify({"stored_bytes": length})


@app.get("/dns/query")
def dns_query():
    return jsonify({"name": request.args.get("name", "corp.example"), "address": "198.51.100.42"})


def udp_socket(host: str, port: int):
    family, kind, protocol, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM, flags=socket.AI_PASSIVE)[0]
    sock = socket.socket(family, kind, protocol)
    try:
        sock.bind(address)
        sock.settimeout(1.0)
    except OSError:
        sock.close()
        raise
    return sock


def udp_sink(host: str, port: int, stop_event: threading.Event, sock=None):
    sock = sock if sock is not None else udp_socket(host, port)
    while not stop_event.is_set():
        try:
            data, peer = sock.recvfrom(65535)
            if len(data) >= 12:
                sock.sendto(data[:12], peer)
        except socket.timeout:
            continue
        except OSError:
            break
    sock.close()


def main():
    parser = argparse.ArgumentParser(description="Controlled target for NetEm Traffic Simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--udp-port", type=int, default=9000)
    args = parser.parse_args()

    stop = threading.Event()
    # Binding in the main thread makes a port conflict fail service startup.
    media_socket = udp_socket(args.host, args.udp_port)
    thread = threading.Thread(
        target=udp_sink,
        args=(args.host, args.udp_port, stop, media_socket),
        daemon=True,
    )
    thread.start()
    server = WSGIServer((args.host, args.port), app, log=None)

    def shutdown(*_args):
        stop.set()
        server.stop(timeout=2)

    handlers = [signal_handler(signal.SIGTERM, shutdown), signal_handler(signal.SIGINT, shutdown)]
    try:
        server.serve_forever()
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
