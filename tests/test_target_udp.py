import socket
import struct
import sys
import threading

import gevent
import pytest

from trafficgen.target import reply_packet_info, udp_packet_info, udp_sink, udp_socket
from trafficgen.wire import ADDRESS_REQUEST, unpack_address


def test_ipv4_reply_uses_contacted_address_without_forcing_incoming_interface():
    destination = socket.inet_aton("198.18.0.1")
    packet_info = (socket.IPPROTO_IP, 8, 12)
    received = [(socket.IPPROTO_IP, 8, struct.pack("=I4s4s", 12, destination, destination))]
    assert reply_packet_info(received, packet_info) == [
        (socket.IPPROTO_IP, 8, struct.pack("=I4s4s", 0, destination, b"\x00" * 4))]


@pytest.mark.parametrize("address, expected_interface", [("2001:db8::1", 0), ("fe80::1", 12)])
def test_ipv6_reply_preserves_destination_and_link_local_scope(address, expected_interface):
    destination = socket.inet_pton(socket.AF_INET6, address)
    packet_info = (socket.IPPROTO_IPV6, 50, 20)
    received = [(socket.IPPROTO_IPV6, 50, struct.pack("=16sI", destination, 12))]
    assert reply_packet_info(received, packet_info) == [
        (socket.IPPROTO_IPV6, 50, struct.pack("=16sI", destination, expected_interface))]


@pytest.mark.parametrize("ancillary", [[], [(socket.IPPROTO_IP, 9, b"x" * 12)],
                                        [(socket.IPPROTO_IP, 8, b"x" * 11)]])
def test_missing_or_malformed_packet_info_cannot_select_a_reply_source(ancillary):
    assert reply_packet_info(ancillary, (socket.IPPROTO_IP, 8, 12)) == []


def test_explicit_bind_does_not_require_packet_info():
    sock = udp_socket("127.0.0.1", 0)
    try:
        assert udp_packet_info(sock) is None
    finally:
        sock.close()


def test_unsupported_wildcard_bind_fails_at_startup(monkeypatch):
    monkeypatch.setattr("trafficgen.target.sys.platform", "unsupported")
    with pytest.raises(RuntimeError, match="specific local IP"):
        udp_socket("0.0.0.0", 0)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux packet-info regression")
@pytest.mark.parametrize("request_address", [False, True])
def test_wildcard_ipv4_target_replies_from_each_contacted_address(request_address):
    media_socket = udp_socket("0.0.0.0", 0)
    port = media_socket.getsockname()[1]
    stop = threading.Event()
    worker = gevent.spawn(udp_sink, "0.0.0.0", port, stop, media_socket)
    try:
        for destination in ("127.0.0.2", "127.0.0.3"):
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                client.settimeout(2)
                client.connect((destination, port))
                header = b"n" * 8 + struct.pack("!I", 7)
                client.send(header + (ADDRESS_REQUEST if request_address else b"") + b"m" * 20)
                response, peer = client.recvfrom(64)
                assert peer == (destination, port)
                assert response[:12] == header
                if request_address:
                    assert unpack_address(response[12:]) == client.getsockname()[0]
                else:
                    assert len(response) == 12
            finally:
                client.close()
    finally:
        stop.set()
        worker.kill()
        media_socket.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux packet-info regression")
@pytest.mark.parametrize("family, destination", [(socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.2")])
def test_wildcard_ipv6_target_replies_from_contacted_address(family, destination):
    media_socket = udp_socket("::", 0)
    port = media_socket.getsockname()[1]
    stop = threading.Event()
    worker = gevent.spawn(udp_sink, "::", port, stop, media_socket)
    client = socket.socket(family, socket.SOCK_DGRAM)
    try:
        client.settimeout(2)
        client.connect((destination, port))
        header = b"n" * 8 + struct.pack("!I", 7)
        client.send(header + ADDRESS_REQUEST + b"m" * 20)
        response, peer = client.recvfrom(64)
        assert peer[:2] == (destination, port)
        assert response[:12] == header
        assert unpack_address(response[12:]) == client.getsockname()[0]
    finally:
        client.close()
        stop.set()
        worker.kill()
        media_socket.close()
