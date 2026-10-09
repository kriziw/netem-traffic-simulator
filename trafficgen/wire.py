"""Wire details shared by the simulator and its controlled target."""
from __future__ import annotations

import ipaddress

# The target reports the source address it observed, i.e. the appliance's
# post-NAT WAN address, so experience can be attributed to the WAN that carried it.
OBSERVED_SOURCE_HEADER = "X-NetEm-Observed-Source"

# Media probes carrying this marker right after the 12-byte nonce/sequence header
# ask the target to append the observed source address to its echo. Probes without
# it get the plain 12-byte echo, so older simulators and targets keep working.
ADDRESS_REQUEST = b"NTA1"
ECHO_HEADER_BYTES = 12


def normalize_address(value):
    if value is None:
        return None
    try:
        address = ipaddress.ip_address(value if isinstance(value, bytes) else str(value).strip().split("%", 1)[0])
    except ValueError:
        return None
    return str(getattr(address, "ipv4_mapped", None) or address)


def pack_address(value) -> bytes:
    address = normalize_address(value)
    if address is None:
        raise ValueError("Not an IP address.")
    return ipaddress.ip_address(address).packed


def unpack_address(data: bytes):
    return normalize_address(bytes(data)) if len(data) in (4, 16) else None
