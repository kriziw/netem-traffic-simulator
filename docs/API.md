# API

The simulator exposes a versioned HTTPS REST API on TCP/8443 by default.

## Authentication

Except for `/api/v1/health`, all API endpoints require:

    Authorization: Bearer <API_KEY>

The key can be viewed and rotated from **API & Integration** in the simulator UI. Rotation invalidates the previous key immediately.

Network discovery uses UDP/47890 and never includes the credential.

## Service health

`GET /api/v1/health`

Unauthenticated identity/health response.

## Workload control

- `GET /api/v1/status`
- `GET /api/v1/catalog`
- `GET /api/v1/profiles`
- `GET /api/v1/applications`
- `POST /api/v1/workloads/start`
- `POST /api/v1/workloads/adjust`
- `POST /api/v1/workloads/stop`

Example start request:

    {
      "profile": "office",
      "users": 100,
      "spawn_rate": 5,
      "activity": "normal",
      "pattern": "office_day_compressed",
      "target": "http://192.168.0.191:8090"
    }

The start request may also override `personas` and `applications` with percentage/weight maps.

## Digital Experience Monitoring

The DEM API is designed for NetEm and other controllers that want to understand simulated endpoint experience, not just generated traffic volume.

### `GET /api/v1/dem/experience?window=60`

Returns experience score (0–100), experience rating, availability, P50/P95 response time, requests/s, failures/s, active users, application summaries, and persona summaries.

### `GET /api/v1/dem/applications?window=60`

Per-application request count, availability, P50/P95 and experience score.

### `GET /api/v1/dem/personas?window=60`

Per-persona experience.

### `GET /api/v1/dem/timeseries?minutes=15`

Persistent DEM samples for charting and session correlation.

### `GET /api/v1/runs`

Recent workload runs.

## Discovery

Send a UDP JSON datagram to port 47890:

    {"protocol":"NETEM_TRAFFIC_SIMULATOR_DISCOVERY_V1","nonce":"<caller nonce>"}

A simulator on the same broadcast domain responds with instance name, version, API port, management IP candidates and capabilities.
