"""Workload-only HTTP dependency; root routing/DHCP helpers use stdlib."""
import ipaddress
from requests.adapters import HTTPAdapter


class SourceAdapter(HTTPAdapter):
    def __init__(self, source, **kwargs):
        self.source = str(ipaddress.IPv4Address(source))
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        super().init_poolmanager(connections, maxsize, block, source_address=(self.source, 0), **kwargs)
