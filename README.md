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

Current version: **0.2.0**

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
- management NIC on the same management network as NetEm, **without a default gateway**
- data NIC on the FortiGate corporate LAN, with the **default route via the FortiGate**

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

Voice and video use paced UDP media bursts with nonce/sequence acknowledgements from the controlled target. Delivery failures affect availability; media latency is measured round-trip time, excluding intentional pacing. Upgrade the simulator and controlled target together. Other application classes use HTTP transactions against the controlled target service.

The DEM page explains degraded experience live. It classifies each failure (timeouts, resets, HTTP errors, media loss, no media reply) and splits HTTP time into waiting for the response and transferring it. It also groups experience by the appliance address the target observed, which identifies the WAN that carried each transaction. Choose **Strict** or **Realistic** voice/video judging per workload: strict fails a burst on any lost packet, while realistic accepts the small random loss codecs usually conceal (voice up to 2%, video up to 1% per burst). Per-WAN attribution needs the appliance to SNAT to its WAN addresses, as described under deployment.

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

NetEm can explicitly allow the self-signed lab certificate. The fixed integration checks its SHA-256 fingerprint before sending the API key. Discovery supplies a candidate fingerprint; verify it against the simulator certificate. Manual entry with a blank fingerprint pins the first certificate seen. Later changes require verifying and updating the trusted fingerprint.

## Controlled traffic target

The repository also includes a deliberately bounded target service intended to run on the **upstream / ISP-router side** of the lab.

The target installer also adds a persistent RFC 2544 benchmarking loopback:

    198.18.0.1/32

It listens on:

    http://198.18.0.1:8090
    UDP 198.18.0.1:9000

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

At the end it prints the administrator password and API key. Re-running the installer preserves secrets, certificates and runtime history and restarts the service with the updated code. It can be run from the installed `/opt/netem-traffic-simulator` checkout without deleting its source.

On an LXC, the installer creates a service drop-in that disables mount-namespace hardening unsupported by common unprivileged Proxmox containers. The dedicated account, file permissions and `NoNewPrivileges` remain in effect. VMs retain full hardening. Environment overrides may be added through a separate systemd drop-in (`Environment=TRAFFICGEN_BIND_HOST=<management-IP>`, for example).

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

The installer creates the benchmark address automatically. Configure the simulator target as:

    http://198.18.0.1:8090

Before running a workload:

1. On the simulator, confirm `ip route get 198.18.0.1` selects the corporate LAN NIC and the FortiGate gateway. Keep the management subnet directly connected, with no management default route.
2. Ensure the FortiGate routes `198.18.0.1` through its SD-WAN zone and allows LAN-to-SD-WAN HTTP/TCP 8090 and UDP 9000 traffic. Apply SNAT to the WAN address, or provide explicit return routes to the simulator LAN on the upstream router.
3. Ensure both WAN paths reach the upstream router that owns `198.18.0.1/32`. A separate target host needs upstream routes to that address; the loopback alone does not advertise a route.
4. Test `curl --connect-timeout 5 http://198.18.0.1:8090/health` **from the simulator**, then verify the HTTP/UDP workload increments the intended WAN counters. A management-side curl verifies service health only.

Using a dedicated 198.18.0.0/15 RFC 2544 benchmarking address is important: do not point the workload at the ISP Router's 192.168.0.x management address, because a dual-homed simulator on the same management subnet could bypass the FortiGate/NetEm datapath.

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
        "target": "http://198.18.0.1:8090"
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

Raw transaction and DEM history is retained for seven days and pruned hourly during workloads. The database reuses freed pages. Live DEM returns the newest 50,000 transactions within its window and marks capped summaries with `truncated: true`; NetEm will not pass a DEM assertion on a truncated window. Rates use the complete requested window. No-data samples retain null availability/score values. Status also exposes persistence errors and dropped transaction counts.

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
- systemd hardening on VMs; LXC-specific namespace overrides where required;
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

    python -m gevent.monkey --module pytest -q

Compile:

    python -m py_compile trafficgen/*.py

## Releases

The repository uses **Release Please**, with tags and release titles such as `v0.3.1`, matching NetEm. The update worker also accepts the historical `netem-traffic-simulator-vX.Y.Z` tags and displays them as `vX.Y.Z`.

Version state is held in:

- `trafficgen/version.txt` (the single version file; Release Please rewrites it as the `simple` strategy `version-file`)
- `.release-please-manifest.json`
- `release-please-config.json`
- `CHANGELOG.md`

Conventional commits and squash-merge titles such as `feat:` and `fix:` drive semantic release PRs. Optionally configure `RELEASE_PLEASE_TOKEN` with repository contents, pull request and issue permissions so generated release PRs trigger CI; the default GitHub token can create releases but does not trigger follow-on workflows. Release Please owns version/changelog updates. The update worker refuses a release whose `trafficgen/version.txt` differs from its tag, so do not add a second version file or list it under `extra-files`: plain-text extra files are only rewritten on lines carrying an `x-release-please-version` marker.

## License

MIT.

## Updates, smooth controls, and appliance routing

The **Updates & Appliance Routing** page adds stable-release checks and installation from this repository, LAN candidate discovery, saved appliance profiles, and verified target routing. UI form controls update in place, and DEM chart transitions animate without changing measured values or filling missing-data gaps.

For an existing installation, run the new `scripts/install-lxc.sh` **once as root** after installing this release. It installs `git`, `iproute2`, `ping`, `curl` and the systemd administration units. The web service remains the unprivileged `trafficgen` account. A root-owned worker accepts fixed update, discovery, inventory, interface and benchmark-route operations. It accepts a Proxmox IPv4 address only on the directly connected management LAN and uses fixed read-only API paths; it never accepts shell commands, custom repository URLs, or arbitrary filesystem paths.

### Multiple SD-WAN appliances

1. Keep `eth0` for management. Add an addressed LXC data NIC on each separate appliance LAN bridge/VLAN in Proxmox. Appliances may also share a LAN if each has a distinct gateway address. The simulator does not create Proxmox NICs or provision vendor appliances.
2. Open **Updates & Appliance Routing**. Existing neighbors and configured gateways appear as candidates. **Scan data LANs** sends bounded ICMP probes only on directly connected, non-management IPv4 subnets of /24 or smaller (at most 1024 addresses). Larger LANs use existing neighbors or manual entry. The identity dropdown uses read-only Proxmox inventory and exposed network identity as product hints; confirm its vendor/gateway or edit the fields yourself.
3. Save a name, vendor label (Fortinet, VeloCloud, Cisco or Other), data interface, gateway and controlled target. The default target is `198.18.0.1`.
4. Stop the workload and click **Verify & select**. The helper adds a `/32` route to a target within `198.18.0.0/15`, checks the kernel's selected gateway/interface and calls the target's HTTP health endpoint. Failed verification restores the previous managed route. Existing unmanaged host routes are never replaced.
5. Start a workload whose target matches the selected address. The selected `/32` route is restored after reboot. **Clear managed route** removes only the helper's host route.

Selecting an appliance does not change a default route, the management interface, DHCP, or appliance policies. The appliance still needs routing/NAT and access to target TCP 8090 and UDP 9000. HTTP health verifies TCP only; use a voice/video workload to verify UDP/media delivery and observe the appliance/NetEm counters for the WAN path.

With a managed target route, you can keep the normal management default gateway on `eth0` for updates and management replies, because the more specific `/32` target route uses the selected appliance. If you previously removed the management default gateway for the manual deployment, restore it in Proxmox **after selecting and verifying the managed target route**. Do not add a second competing data-interface default gateway. On a nonstandard installation, root can change `management_interface` in `/var/lib/netem-traffic-simulator-admin/policy.json`; it defaults to `eth0`.

### Stable-release updates

Use **Check for updates**, then **Install update** while the workload is stopped. **Install update** is enabled only when the last checked release is newer than the running version; the UI re-evaluates a cached check after every install, and the server refuses the request otherwise. While the update runs, a full-page update screen tracks it (installing, restarting, reconnecting), survives the service restart and reloads into the new version, or shows the worker's message if the update was rolled back. The screen is part of the version you update *from*. The worker revalidates the newest stable semantic-version GitHub release, clones only the fixed repository/tag, rejects modified installed files, keeps a rollback copy of the application and virtual environment, and runs the native installer. Passwords, API keys, certificates and runtime data remain outside the application directory. A failed installation restores the old application and virtual environment and attempts to restart the old service; inspect systemd logs if recovery fails. Updates require Internet access, enough disk space for the backup, and may briefly interrupt the management connection while the service restarts.

Releases v0.3.0 to v0.5.0 shipped a stale `trafficgen/version.txt` (`0.2.0`), so **Install update** to one of them fails with `Release tag and packaged version disagree` and **Check for updates** always offers an update. Installations on those versions can update normally from the UI to a later release. To install one of the affected releases from the console instead, run as root on the simulator (replace `v0.5.0` with the tag you want):

```bash
git clone --depth 1 --branch v0.5.0 https://github.com/kriziw/netem-traffic-simulator.git /var/tmp/ntg-update
cp /var/tmp/ntg-update/version.txt /var/tmp/ntg-update/trafficgen/version.txt
bash /var/tmp/ntg-update/scripts/install-lxc.sh
rm -rf /var/tmp/ntg-update
```

### Remote controlled target updates

The simulator's **Updates & Appliance Routing → Controlled target updates** panel
can check and install stable releases on your virtual ISP modem using a dedicated
target API key. The simulator and target remain separate installations: updating
the simulator does not automatically update the target.

Enable remote updates once from the modem console using the current release's
source checkout. Run as root, replacing the address with the modem's configured
management IPv4:

```bash
NETEM_TARGET_MANAGEMENT_HOST=192.168.0.201 bash scripts/install-target.sh
```

The installer prints a dedicated API key and TLS certificate SHA-256 fingerprint.
Copy them into the simulator's **Connect target management** form together with
the modem's management IPv4. This key is separate from the simulator integration
API key. The modem and simulator must share a directly connected management IPv4
LAN; the simulator uses its configured management interface (normally eth0).
The API listens on HTTPS **8091**, bound to that modem address. Allow access from
the simulator on your management firewall. Workload traffic still uses TCP 8090
and UDP 9000 through the appliance.

Then use **Check target updates**, stop workloads, and **Install target update**.
Progress and the installed version appear on the same page; the simulator stays
online while the target restarts. **Refresh target status** checks connectivity and
recovers the result of a task started before leaving the page. A busy target rejects
additional jobs. The remote worker rechecks the latest stable release, rejects
modified application files, keeps a rollback copy of code and the virtual
environment, and verifies the restarted target's version. Failure restores the
previous application and services. An interrupted or unreachable task remains
unverified: reconnect and refresh status before retrying.

The target's HTTPS API runs as its own unprivileged service. A separate root worker
accepts only release checks and installs from this fixed repository; clients cannot
choose commands, paths, repositories or arbitrary package URLs. The simulator
verifies the independently copied certificate fingerprint before sending the key
and never follows redirects. Saved credentials are root-only and excluded from
status responses. **Disconnect & remove API key** removes the simulator's saved
connection; it does not disable the modem's management service.

Subsequent target installs preserve its management address, API key and certificate.
To revoke a key, generate a replacement on the modem as root and reconnect the
simulator with it:

```bash
umask 077
printf 'ntt_%s\\n' "$(openssl rand -hex 32)" > /etc/netem-traffic-target-manager/api.key
chown root:trafficgen-target-manager /etc/netem-traffic-target-manager/api.key
chmod 0640 /etc/netem-traffic-target-manager/api.key
```

The API reads the key per request, so rotation immediately rejects the old key.
Keep the printed key private. If you replace the TLS certificate, independently
verify its new fingerprint before reconnecting.

For an existing simulator, run the current `scripts/install-lxc.sh` once as root
to install the new privileged-worker actions. On targets where remote management
has not been enabled, console updates with `scripts/install-target.sh` continue
to work as before.

Integration clients can inspect `GET /api/v1/network` and select an existing saved appliance with authenticated `POST /api/v1/network/select` and `{"appliance_id":"..."}`. Selection returns `202` with a job ID; poll the network endpoint until that job completes or fails before starting a workload. Network controls require the same Bearer API key as workload controls. GUI controls retain administrator login and CSRF checks.

Diagnostics:

```bash
systemctl status netem-traffic-simulator-admin.path --no-pager
journalctl -u netem-traffic-simulator-admin.service -n 100 --no-pager
ip route get 198.18.0.1
```

### Recover a data interface from the GUI

The routing page lists down and unaddressed data NICs and checks the saved route against current interface/address/kernel-route state. A saved selection is not presented as proof of an active path. Missing, down, unaddressed, no-carrier and mismatched routes show an actionable error, refreshed while the page is open.

Stop the workload, choose a data NIC, enter the simulator LAN address with its prefix (for example `eth1`, `10.250.10.10/24`), and click **Enable & save interface**. The root worker brings it up and adds that address without changing management or a default gateway. It saves this configuration outside the service-writable directories and reapplies it at boot before the benchmark route. Existing different addressing and overlap with management are rejected. **Verify & select** then checks target health through the appliance.

**Stop restoring** removes only the simulator's saved boot configuration; it does not remove a live address or route. For installations where Proxmox owns static addressing, configure the same address there and stop simulator restoration to avoid conflicting ownership. The simulator cannot add Proxmox NICs or fix a host bridge/link-down setting; those remain Proxmox tasks.

### Appliance identity and automatic field population

Connect a dedicated read-only Proxmox API token under **Updates & Appliance Routing → Read-only Proxmox inventory**. Give the token's user and the privilege-separated token **PVEAuditor** on the required inventory scope (for a lab, `/` with propagation). Use the GUI's host IPv4, token ID (`user@realm!token`) and secret fields. The host must be on the management LAN; HTTPS port 8006 and API paths are fixed. A self-signed certificate requires explicit opt-in and is pinned on first connection; verify the displayed SHA-256 fingerprint independently. A changed certificate is rejected until you disconnect/reconnect. Credentials are stored root-only with mode 0600, excluded from UI/API status, and removed by **Disconnect & remove credentials**. No VM write operations or guest commands are used. See [Proxmox user/token permissions](https://pve.proxmox.com/pve-docs/chapter-pveum.html).

Click **Detect appliances on data LANs**. The scan matches current LAN neighbor MAC addresses against VM NIC configurations and correlates VM names/product metadata with fixed unauthenticated HTTP/HTTPS identity probes. When an existing `lldpd` daemon is available, matching LLDP advertisements also contribute identity; the simulator does not install or reconfigure that daemon. Probes bind to the candidate's data NIC/source IPv4, do not use proxies, send no credentials and do not follow redirects. At most 128 candidates receive web identity probes per scan; other discovered peers remain available with inventory/Unknown hints. The inventory connection reads at most 128 visible non-template QEMU VMs, with partial-read warnings. Use a narrower token scope for larger installations.

Select an appliance from the dropdown to populate its name, vendor, data interface, LAN gateway and any model/firmware hints. The controlled-target address is preserved. All fields remain editable. Save the profile, stop workloads and **Verify & select** to validate routing through that gateway. Identity does not prove a device's gateway role or current firmware authenticity.

Fingerprint families include Fortinet, VeloCloud, Cisco (including Catalyst/vEdge/Meraki markers), HPE Aruba EdgeConnect/Silver Peak, Palo Alto Networks/Prisma/CloudGenix, Versa, Juniper Session Smart/vSRX, Check Point, Sophos, Forcepoint, Barracuda, Peplink FusionHub, legacy Citrix/NetScaler SD-WAN, Huawei, Nokia/Nuage, Ekinops, Cato, SonicWall and WatchGuard. These are best-effort signature families, not a claim that every version exposes a detectable identity. Generic virtual-NIC MACs, nginx or VMware branding alone do not establish an SD-WAN vendor. Silent/ambiguous appliances remain Unknown. Model and firmware fields are blank unless supplied by an observed product hint; VM metadata is labelled as an inventory hint, not verified live software. Rescan after changing VM metadata or upgrading an appliance.
