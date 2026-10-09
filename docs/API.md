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
      "target": "http://198.18.0.1:8090",
      "media_mode": "strict"
    }

The start request may also override `personas` and `applications` with percentage/weight maps.

`label` (optional, at most 120 characters) describes what the run represents, for example the site NetEm modelled it on; it is shown on the dashboard and in recent runs.

Besides the generic applications, the catalog includes industry applications that controllers can weight in `applications`: `ot_telemetry` (realtime, any loss fails), `mes`, `erp`, `pos`, `wms_scan`, `emr` and `core_banking` (interactive), and `plm_cad`, `pacs_imaging`, `cctv_backhaul` and `guest_internet` (bulk). Matching personas (`shop_floor`, `engineer`, `ot_device`, `camera`, `store_associate`, `warehouse_operator`, `clinician`, `banker`, `guest`) can be weighted in `personas`. They use the controlled target's existing endpoints, so the target needs no update for them.

`media_mode` decides how much packet loss fails a voice/video burst. `strict` (default) fails a burst on any lost packet. `realistic` accepts small random loss that codecs usually conceal: voice up to 2% (1 of 50 packets) and video up to 1% (2 of 200 packets) per burst. Bursts that get no reply at all fail in both modes. Adjust accepts `media_mode` too; it applies from the next burst. `/api/v1/catalog` lists the modes and their tolerances under `media_modes`.

## Digital Experience Monitoring

The DEM API is designed for NetEm and other controllers that want to understand simulated endpoint experience, not just generated traffic volume.

### `GET /api/v1/dem/experience?window=60`

Returns experience score (0–100), experience rating, availability, P50/P95 response time, interactive P95 (web, collaboration and DNS only), requests/s, failures/s, active users, application summaries, persona summaries and `diagnosis`.

### Diagnosis

`diagnosis` (also in `/api/v1/status` under `dem.diagnosis`) explains degraded experience:

- `findings`: ordered worst first. Each has `id`, `severity` (`bad`, `warn`, `info`), `title`, `detail`, `applications`, `affected` and `by_egress`. Ids are `media_loss`, `media_no_reply`, `timeouts`, `connection_errors`, `http_errors`, `bandwidth_bound` (with `direction`), `slow_wait` and `target_outdated`. Media findings include `fail_by_mode`, the bursts that fail under each media mode.
- `causes`: failed transactions per cause: `connect_timeout`, `read_timeout`, `connection_error`, `http_status`, `redirect`, `media_loss`, `media_no_reply`, `other`. `cause_labels` names them.
- `egress`: experience per appliance egress address, the post-NAT source address the target observed. It identifies the WAN that carried each transaction. Failures that got no reply at all are grouped as `unknown`. `egress_recent` counts requests and failures per egress address and application over the last 10 seconds, to show where traffic goes right now (for example to time SD-WAN steering).
- `interactive_p95_ms` and `media_mode`.

Application summaries add `class` (`interactive`, `bulk`, `realtime`), `causes`, `top_cause`, `p95_wait_ms` (request sent until the response starts, including uploads), `p95_transfer_ms` (receiving the response), `median_down_mbps`/`median_up_mbps` per transfer of at least 64 KiB, `transfer_share` and, for voice/video, `media` (bursts, packets sent/lost, loss %, bursts with loss, bursts without reply, `fail_by_mode`).

Egress attribution needs a compatible target: it adds the `X-NetEm-Observed-Source` header and, for media probes that carry the `NTA1` marker after the 12-byte nonce/sequence header, appends the observed source address to the echo. Older probes get the plain 12-byte echo. The target's `/health` reports `version` and `capabilities`; the simulator checks it when a workload starts and every 30 seconds while running, so upgrades are detected without restarting a workload. A target missing capabilities produces `target_outdated` only when its reported version is demonstrably older, otherwise `target_incompatible`. Failed or invalid health checks do not establish that a target is outdated. Verify health from the simulator's routed path, not only on the target host.

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

## Validation and measurement semantics

Start/adjust bodies must be JSON objects. Invalid types, nonfinite numbers, unknown/all-zero/negative mixes, and non-lab targets return HTTP 400; lifecycle conflicts return HTTP 409. User count is 1–5000 and spawn rate is 0.1–1000 users/s. Adjustments are validated completely before modifying the active run. Application exclusions take effect on existing users; persona changes reassign their persona on the next transaction.

HTTP transactions have connect/read timeouts of 5/10 seconds and do not use environment proxies or follow redirects. Targets must be HTTP(S) base URLs without credentials, a path prefix, query or fragment. Voice/video use UDP acknowledgements on port 9000: the media mode decides how much loss fails a burst, every burst records packets sent and lost, and successful bursts record mean RTT. These remain synthetic transactions, rather than a MOS or RTP jitter measurement.

`/dem/experience` additionally returns `window_seconds`, `requests` and `truncated`. Scores use application-specific latency thresholds; rates count transactions over the full requested window. A window exceeding 50,000 transactions uses its latest records and sets `truncated: true`. Status includes `persistence_error` and `dropped_transactions`. Its DEM view includes `endpoint_count` and at most 200 worst endpoint records to keep polling bounded; dedicated DEM endpoints provide larger views. UDP response bytes count acknowledged headers received by the client. Historical no-data DEM samples use null scores/availability. Runs left open by a process interruption are marked `interrupted` at startup.

Browser POST forms require session CSRF tokens; the Bearer-authenticated API is independent of browser sessions. API rotation replaces the secret atomically, revokes the previous key immediately, and requires updating NetEm.
