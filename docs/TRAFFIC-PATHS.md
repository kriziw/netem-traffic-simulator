# Purpose-based traffic steering

The simulator can send its full configured workload through any of three scopes:

| Mode | Data path | Purpose |
| --- | --- | --- |
| Appliance | Simulator → SD-WAN appliance → NetEm → router/target | Appliance validation |
| Direct NetEm | Simulator → one NetEm WAN → router/target | Scenario/metric calibration without appliance effects |
| Clean baseline | Simulator → router/target | Generator/router/target baseline without appliance or NetEm |

All modes use the same Locust workload engine, HTTP and UDP applications, user
limit (up to 5,000), population controls, activity levels and traffic patterns.
These are not probe-only paths. Actual throughput depends on generator, target,
router and link capacity; selecting a path does not promise wire-speed saturation.

The full workload is pinned to one path for the duration of the run. Independent
bounded HTTP health probes can use all configured paths at the same time, including
the same target IP. Unhealthy paths never cause automatic workload failover.
There is one active full workload, not simultaneous full workloads on all paths.

## Proxmox wiring

Keep the management NIC separate. Add/address data NICs for the desired paths:

- Appliance: the existing corporate LAN / SD-WAN appliance gateway.
- Direct NetEm: attach to the appliance-facing side of the selected NetEm WAN
  bridge. The router gateway is reachable through that transparent WAN bridge.
- Clean baseline: attach to the router-facing bridge, avoiding NetEm.

Each policy needs a distinct source IPv4 address assigned to its data interface.
Multiple addresses on one NIC are supported when the gateways are reachable on
that LAN. Independent sides of NetEm normally require separate Proxmox NIC/bridge
attachments. Do not reuse the appliance's WAN IP or another device's address.

The router must route replies to every simulator source address. Without appliance
SNAT, the old FortiGate return path is insufficient. If data interfaces use an
overlapping subnet on different bridges, configure explicit `/32` return routes
to each simulator source on the router. The controlled target stays on
`198.18.0.0/15`; do not use its management address as the workload target.

NetEm and the simulator do not create Proxmox bridges or reconfigure ISP routers.
Use the simulator's existing interface configuration tools after wiring the NICs.
Verify the physical path with WAN counters during the first calibration workload:
successful kernel/target checks alone cannot prove which external bridge was crossed.

## Simple network setup and DHCP

Open **Network setup**, choose a detected data interface, and use **Get address
automatically (DHCP)** if it has no address. Attaching a NIC to a Proxmox bridge
does not start a DHCP client when guest addressing is Manual. The app now runs
a per-interface BusyBox DHCP client with automatic lease renewal and boot startup.
Its fixed root-owned hook applies only the address/prefix and records the router
gateway. It ignores offered default routes and DNS, keeping management routing
separate. Management interfaces and already-addressed/unmanaged NICs cannot be
taken over. Existing Proxmox/ifupdown DHCP configuration is also refused.

Choose one traffic purpose: through the appliance, through NetEm WAN1, or direct
to the router. DHCP-provided address and gateway fill the selected path. Other
WAN choices, manual addresses, target/WAN overrides and gateway probes are under
Advanced. The page watches task/lease changes and refreshes without interrupting
edited path fields. There are no example addresses masquerading as live settings.

Confirm the Proxmox bridge and choose **Verify traffic path**, then select the
verified path in **Traffic paths**. The app does not create Proxmox NICs or bridges.
For the current bridge template: vmbr3 is the appliance LAN, vmbr1 is WAN1 before
NetEm, and vmbr4 is the clean ISP1 baseline. Management stays on vmbr0.

No DHCP reply means the client continues trying; check bridge attachment, DHCP
service, NetEm forwarding and UDP 67/68 filtering. DHCP requires an attached NIC
and a reachable server; the app cannot infer a router's network without a reply.
Manual configuration remains available for networks without DHCP. Stop app DHCP
before changing to a manual address; paths using it must first be removed.

DHCP lease loss/expiry or a changed source prevents verification/start of the old
source-bound path. Stale DHCP addresses are not restored as static addresses at
boot. Same-source lease recovery restores saved policies; a changed lease source
requires explicit re-verification/selection. No automatic workload failover is
introduced. Router reservations are useful for repeatable tests.

Advanced probes gather bounded interface-local ARP evidence, including before an
IP is assigned, and available neighbor/vendor evidence. Shared ISP gateways leave
NetEm-versus-baseline recommendations ambiguous; confirm Proxmox wiring and WAN
counters. Calibration/baseline NICs sharing a subnet need symmetric return routing
and appropriate ARP handling; a separate baseline subnet/VLAN avoids that ambiguity.

## Configuration and selection

In NetEm, use **Platform Health → Traffic steering**. Select appliance paths from
the inventory, or configure a direct NetEm/clean router path. Every row has a
**Use for full workload** action. Start the workload from Tests afterward.

The simulator's own **Traffic paths** menu provides the same configuration,
selection and unused-path removal. Up to eight policy paths are supported. Each
has an immutable source, target and data interface; add a new path when wiring
changes. Remove unused paths to free a policy slot. Switching/configuring/removing
is refused during workloads or pending administration tasks.

Configuration is verified asynchronously through the privileged administration
queue. It does not select that path for workloads. Selection verifies again and
only publishes the workload choice after success. Saved policies and selection
are restored at boot; the existing watchdog can repair the selected policy.

## Routing implementation and boundaries

Each configured path receives a reserved Linux table (21000–21007) and rule pair
(22000–22015). Rules match only its source `/32` and controlled target `/32`.
HTTP connection pools and UDP sockets bind to that source. Target-monitor probes
for a run use the same affinity. Management and system default routes are unchanged.

A terminal unreachable rule prevents fallback to another table/default route if
a selected table disappears. A missing/unverifiable policy cannot start a workload
or send a health probe via another path. Reserved tables/rules already owned by
another configuration are refused, not overwritten. Failed policy verification
restores the previous owned policy and retains the previous workload choice.

Workload history records the path's name, mode, interface, source and gateway.
Older runs have unknown/unrecorded path metadata. HTTP health-response time is
service latency and is separate from workload DEM and ICMP latency.

## NetEm interpretation

- Clean baseline permits the full workload, but WAN scenarios/site plans are
  refused because traffic intentionally avoids NetEm. No false bypass/SD-WAN
  steering conclusion is generated.
- Direct NetEm permits generic/custom tests on its assigned WAN. Appliance
  steering assertions and multi-WAN site plans are not applicable.
- Appliance paths retain license/health preflight and normal SD-WAN tests.
- Direct modes verify routing/target readiness without requiring an unused
  appliance's license. Appliance management monitoring continues in every mode.

## Authenticated simulator API

- `GET /api/v1/network/paths` — configured policy paths and selected workload path.
- `GET /api/v1/network/paths/health` — independent, briefly cached observations.
- `PUT /api/v1/network/paths/<id>` — queue verified path configuration; returns 202.
  Fields: name, mode, wan_id (direct NetEm only), interface, gateway, source
  (optional with a single interface address), target.
- `POST /api/v1/network/paths/select` with `{"id":"..."}` — queue selection.
- `DELETE /api/v1/network/paths/<id>` — remove an unused policy through the queue.

Existing appliance selection now prepares a source-bound appliance path. Existing
legacy routes remain compatible until selected again. A target address still uses
one workload configuration; no arbitrary host routing, automatic failover,
appliance impersonation or production forwarding is provided.
