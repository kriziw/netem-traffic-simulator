# NetEm Traffic Simulator

Corporate endpoint traffic simulation and Digital Experience Monitoring (DEM) companion for **NetEm WAN Lab**.

The simulator uses [Locust](https://github.com/locustio/locust) as the proven workload execution engine and adds the parts that are specific to the NetEm resilience-lab use case:

- corporate user personas;
- application traffic profiles;
- user-count and activity patterns;
- a NetEm-facing HTTPS REST API;
- Bearer API-key authentication;
- network discovery;
- per-endpoint Digital Experience Monitoring;
- persistent run/DEM history;
- a modern standalone management UI;
- a controlled upstream traffic target for the lab.

Current version: **0.1.0**

## Why this exists

NetEm can inject controlled WAN conditions, but a useful resilience test also needs realistic endpoint demand and a way to answer:

> What did the simulated employees actually experience while the network failed over, degraded or recovered?

This project provides that layer.

The intended workflow is:

    Corporate Traffic Simulator
            |
            | simulated endpoints
            v
        FortiGate LAN
            |
          SD-WAN
        /         \
      WAN1       WAN2
        \         /
         NetEm WAN Lab
              |
       controlled target

NetEm controls the simulator over a separate management/API path.

## Recommended LXC topology

Use an **unprivileged Debian 12/13 LXC** with two NICs:

    eth0  management
      |
      +---- NetEm API/control

    eth1  corporate LAN
      |
      v
    FortiGate / SD-WAN
      |
    NetEm impaired WANs
      |
    upstream target

Suggested starting resources:

- 2–4 vCPU
- 2–4 GB RAM
- 8–16 GB disk
- management NIC on the same management network as NetEm
- data NIC on the FortiGate corporate LAN

For larger populations Locust can later be distributed across additional workers.

## Locust foundation

The platform does **not** automate Locust's web interface.

Locust is embedded as a Python library behind the stable simulator API:

    NetEm
      |
      | /api/v1
      v
    Traffic Simulator API
      |
      v
    WorkloadController
      |
      v
    Locust Environment / Runner

This keeps NetEm independent of Locust's internal web API and gives the simulator freedom to add other traffic engines later without changing the NetEm integration contract.

## Corporate personas

Built-in personas include:

- **Knowledge worker**
- **Collaboration user**
- **Developer**
- **Heavy cloud user**
- **Background / services**

Each persona has its own application tendencies.

## Synthetic applications

Current application classes:

- Web / SaaS
- Collaboration
- Voice
- Video meeting
- Cloud file sync
- Developer / packages
- Software updates
- Backup
- DNS-like application transactions

Voice and video use controlled UDP media bursts. Other application classes use HTTP transactions against the controlled target service.

These are traffic-behaviour models, not claims to reproduce proprietary application protocols exactly.

## Workload profiles

Built-in workload profiles include:

- Office workload
- Video-heavy office
- Developer office
- Cloud-heavy branch
- Backup window

Traffic patterns include:

- Steady
- Office day · 8 hours
- Office day · compressed 20 minutes
- Meeting peak

NetEm can override users, spawn rate, activity, persona distribution and application mix through the API.

## Digital Experience Monitoring

DEM is a first-class API and UI feature.

Each Locust virtual user receives an endpoint identity such as:

    ep-a13c782a19

Every synthetic request is tagged with:

- run ID
- endpoint ID
- persona
- application

The simulator records:

- success / failure
- response time
- response bytes
- application
- persona
- endpoint

It then calculates:

- transaction availability
- P50 response time
- P95 response time
- request/failure rate
- application-level experience
- persona-level experience
- **individual endpoint experience**
- aggregate 0–100 experience score

The experience score currently weights:

- availability: 65%
- application-aware P95 response-time score: 35%

This is intentionally transparent synthetic DEM, not a proprietary vendor score.

### DEM API

The endpoint designed for NetEm is:

    GET /api/v1/dem/experience?window=60

It returns aggregate endpoint experience plus application, persona and worst-endpoint views.

Additional endpoints:

    GET /api/v1/dem/applications
    GET /api/v1/dem/personas
    GET /api/v1/dem/endpoints
    GET /api/v1/dem/timeseries

This allows NetEm scenarios and future session reports to correlate WAN events with endpoint experience.

## API authentication

The integration API uses:

    Authorization: Bearer <API_KEY>

The API key is generated during installation and stored outside the repository:

    /etc/netem-traffic-simulator/api.key

The standalone UI contains **API & Integration**, where an administrator can:

- view the current key;
- reveal/copy it;
- rotate it.

Rotation invalidates the old key immediately.

The API key is never placed in source control and is never included in network-discovery responses.

The management UI uses a separate generated administrator password.

## Automatic NetEm discovery

The simulator listens on:

    UDP/47890

NetEm sends:

    {
      "protocol": "NETEM_TRAFFIC_SIMULATOR_DISCOVERY_V1",
      "nonce": "..."
    }

The simulator responds with:

- instance name;
- hostname;
- candidate management addresses;
- HTTPS API port;
- application version;
- capabilities;
- TLS SHA-256 certificate fingerprint;
- caller nonce.

No secret is included.

If broadcast discovery is unavailable, NetEm can be configured with the simulator IP/hostname manually.

## HTTPS

The installer generates a local self-signed certificate and exposes the management/API service on:

    https://<traffic-generator>:8443

For lab deployments NetEm can explicitly allow the discovered self-signed certificate. The discovery payload also includes its SHA-256 fingerprint so pinning/trust-on-first-use can be added cleanly.

## Controlled traffic target

The repository also includes a deliberately bounded target service intended to run on the **upstream / ISP-router side** of the lab.

It listens on:

    HTTP/8090
    UDP/9000

It provides:

- synthetic pages and API responses;
- bounded file downloads;
- upload sinks;
- developer/artifact downloads;
- update downloads;
- backup uploads;
- collaboration endpoints;
- UDP media sink.

This keeps load inside your own lab rather than generating high-volume traffic against public Internet services.

## Installation — simulator LXC

Clone the repository in the new LXC:

    git clone https://github.com/kriziw/netem-traffic-simulator.git
    cd netem-traffic-simulator

Run:

    bash scripts/install-lxc.sh

The installer:

- installs Python/venv/OpenSSL requirements;
- creates the `trafficgen` service account;
- installs the application under `/opt/netem-traffic-simulator`;
- creates a virtual environment;
- generates administrator/API/session secrets;
- generates a TLS certificate;
- installs and starts the systemd service.

At the end it prints the administrator password and API key.

Check:

    systemctl status netem-traffic-simulator --no-pager

Then open:

    https://<LXC-management-IP>:8443

## Installation — controlled target

On the upstream/ISP-router Debian VM, clone this repository and run:

    bash scripts/install-target.sh

Check:

    systemctl status netem-traffic-target --no-pager

Test:

    curl http://<target-IP>:8090/health

Configure the simulator's target URL as:

    http://<target-IP>:8090

## API examples

Health does not require authentication:

    curl -k https://TRAFFIC-GENERATOR:8443/api/v1/health

Authenticated status:

    curl -k \
      -H "Authorization: Bearer <API_KEY>" \
      https://TRAFFIC-GENERATOR:8443/api/v1/status

Start 100 office users:

    curl -k \
      -H "Authorization: Bearer <API_KEY>" \
      -H "Content-Type: application/json" \
      -d '{
        "profile": "office",
        "users": 100,
        "spawn_rate": 5,
        "activity": "normal",
        "pattern": "office_day_compressed",
        "target": "http://192.168.0.191:8090"
      }' \
      https://TRAFFIC-GENERATOR:8443/api/v1/workloads/start

Read endpoint experience:

    curl -k \
      -H "Authorization: Bearer <API_KEY>" \
      https://TRAFFIC-GENERATOR:8443/api/v1/dem/experience

Stop:

    curl -k \
      -X POST \
      -H "Authorization: Bearer <API_KEY>" \
      https://TRAFFIC-GENERATOR:8443/api/v1/workloads/stop

See [docs/API.md](docs/API.md) for the API contract and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design model.

## Persistent data

Default runtime database:

    /var/lib/netem-traffic-simulator/traffic-generator.db

It stores:

- workload runs;
- synthetic transactions;
- endpoint/persona/application metadata;
- DEM samples.

Secrets and TLS material:

    /etc/netem-traffic-simulator/

## Security model

This is a lab traffic generator, but the control plane is treated as privileged:

- separate admin UI password;
- separate API Bearer key;
- HTTPS;
- immediate API-key rotation;
- no secret in discovery;
- API key stored outside Git;
- systemd hardening;
- bounded workload configuration;
- controlled default target.

The target service should only be exposed inside the lab.

## Development

Install:

    python3 -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    pip install pytest

Run tests:

    pytest -q

Compile:

    python -m py_compile trafficgen/*.py

## Releases

The repository uses **Release Please**.

Version state is held in:

- `version.txt`
- `.release-please-manifest.json`
- `release-please-config.json`
- `CHANGELOG.md`

Conventional commits such as `feat:` and `fix:` are used to drive semantic release PRs.

## License

MIT.
