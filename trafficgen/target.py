from __future__ import annotations

from gevent import monkey, signal_handler

monkey.patch_all()

import argparse
import signal
from gevent.pywsgi import WSGIServer
import json
import ipaddress
import socket
import struct
import sys
import threading
import time
from flask import Flask, Response, jsonify, request

from . import __version__
from .wire import ADDRESS_REQUEST, ECHO_HEADER_BYTES, OBSERVED_SOURCE_HEADER, normalize_address, pack_address

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


@app.after_request
def observed_source(response):
    address = normalize_address(request.remote_addr)
    if address:
        response.headers[OBSERVED_SOURCE_HEADER] = address
    return response


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
    return {"status": "ok", "service": "netem-traffic-target", "version": __version__,
            "capabilities": ["media-echo", "observed-source"], "time": time.time()}


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


def udp_packet_info(sock):
    if not ipaddress.ip_address(sock.getsockname()[0]).is_unspecified:
        return None
    if not sys.platform.startswith("linux") or not hasattr(sock, "recvmsg") or not hasattr(sock, "sendmsg"):
        raise RuntimeError("Wildcard UDP listening requires Linux packet info; use --host with a specific local IP")
    if sock.family == socket.AF_INET:
        option = getattr(socket, "IP_PKTINFO", 8)
        sock.setsockopt(socket.IPPROTO_IP, option, 1)
        return socket.IPPROTO_IP, option, 12
    option = getattr(socket, "IPV6_PKTINFO", 50)
    sock.setsockopt(socket.IPPROTO_IPV6, getattr(socket, "IPV6_RECVPKTINFO", 49), 1)
    return socket.IPPROTO_IPV6, option, 20


def reply_packet_info(ancillary, packet_info):
    level, option, size = packet_info
    for message_level, message_option, value in ancillary:
        if (message_level, message_option) != (level, option) or len(value) != size:
            continue
        if level == socket.IPPROTO_IP:
            _, _, destination = struct.unpack("=I4s4s", value)
            value = struct.pack("=I4s4s", 0, destination, b"\x00" * 4)
        else:
            destination, interface = struct.unpack("=16sI", value)
            value = struct.pack("=16sI", destination,
                                interface if ipaddress.ip_address(destination).is_link_local else 0)
        return [(level, option, value)]
    return []


def udp_socket(host: str, port: int):
    family, kind, protocol, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM, flags=socket.AI_PASSIVE)[0]
    sock = socket.socket(family, kind, protocol)
    try:
        sock.bind(address)
        udp_packet_info(sock)
        sock.settimeout(1.0)
    except (OSError, RuntimeError):
        sock.close()
        raise
    return sock


def udp_sink(host: str, port: int, stop_event: threading.Event, sock=None):
    sock = sock if sock is not None else udp_socket(host, port)
    packet_info = udp_packet_info(sock)
    while not stop_event.is_set():
        try:
            ancillary = []
            if packet_info:
                data, received_info, flags, peer = sock.recvmsg(65535, socket.CMSG_SPACE(packet_info[2]))
                if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                    continue
                ancillary = reply_packet_info(received_info, packet_info)
                if not ancillary:
                    continue
            else:
                data, peer = sock.recvfrom(65535)
            if len(data) >= ECHO_HEADER_BYTES:
                reply = data[:ECHO_HEADER_BYTES]
                if data[ECHO_HEADER_BYTES:ECHO_HEADER_BYTES + len(ADDRESS_REQUEST)] == ADDRESS_REQUEST:
                    try:
                        reply += pack_address(peer[0])
                    except ValueError:
                        pass
                if packet_info:
                    sock.sendmsg([reply], ancillary, 0, peer)
                else:
                    sock.sendto(reply, peer)
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
