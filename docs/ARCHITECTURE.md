# Architecture

## Control plane

NetEm controls the simulator only through the versioned REST API:

    NetEm WAN Lab
          |
          | HTTPS + Bearer API key
          v
    NetEm Traffic Simulator
          |
          +-- management UI
          +-- API/discovery
          +-- Locust workload engine
          +-- DEM collector
          +-- SQLite history

Locust is the execution foundation. NetEm never automates the Locust web interface and does not depend on Locust-specific API details.

## Data plane

Recommended two-NIC LXC:

    eth0  management network
      |
      +---- NetEm control/API

    eth1  corporate LAN
      |
      v
    FortiGate LAN
      |
    SD-WAN
      |
    NetEm transparent WAN paths
      |
    controlled traffic target

The LXC's data-plane default route should point through the firewall/SD-WAN appliance. Management traffic should use the management interface without becoming the workload path.

## Controlled target

The repository includes `trafficgen.target`, a deliberately simple HTTP/UDP responder designed to run on the upstream/ISP-router side of the lab.

It provides bounded synthetic objects, upload sinks, API-style endpoints and a UDP sink. This keeps generated workload traffic inside the controlled lab instead of placing load on arbitrary Internet services.

## DEM

Every Locust request carries context for run ID, persona and application.

The simulator records success/failure, response time and response bytes to SQLite.

Experience score combines:

- synthetic transaction availability (65%)
- application-aware P95 latency score (35%)

The score is intentionally transparent and should be treated as a synthetic endpoint-experience indicator, not a vendor-specific commercial DEM score.

## Security

- management UI requires a separate administrator password
- integration API uses Bearer API keys
- API key is stored outside the repository
- API key is viewable/rotatable only after administrator sign-in
- discovery never reveals credentials
- HTTPS is enabled with a generated certificate
- workload target defaults to a controlled lab responder
