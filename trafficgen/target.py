from __future__ import annotations

import argparse
import json
import socket
import threading
from flask import Flask, Response, jsonify, request

app = Flask(__name__)


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
    length = min(int(request.content_length or 0), 16 * 1024 * 1024)
    request.stream.read(length)
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
    length = min(int(request.content_length or 0), 32 * 1024 * 1024)
    request.stream.read(length)
    return jsonify({"stored_bytes": length})


@app.get("/dns/query")
def dns_query():
    return jsonify({"name": request.args.get("name", "corp.example"), "address": "198.51.100.42"})


def udp_sink(host: str, port: int, stop_event: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            sock.recvfrom(65535)
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
    thread = threading.Thread(
        target=udp_sink,
        args=(args.host, args.udp_port, stop),
        daemon=True,
    )
    thread.start()
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
