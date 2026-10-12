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

## Guided network setup

Open **Network setup** in the simulator. Attach data NICs in Proxmox, then refresh.
The page detects guest interfaces and prefills single existing IPv4 addresses.
Default role suggestions use eth1-eth5. **Probe detected interfaces** uses known
appliance/ISP gateway hints to send a bounded ARP request on up to six data NICs,
including unaddressed ones. Up to six gateway hints and two addressed vendor
identity checks are allowed. Existing neighbor and LLDP evidence is included
when available. Down links are enabled briefly and restored; the probe assigns
no guest addresses or routes. Missing tools/permissions and absent replies yield
Unknown, not a device-down verdict. Without a known gateway or neighbor evidence,
it cannot infer an arbitrary subnet.

Fresh gateway replies produce role candidates that can prefill the NIC and
gateway in a setup card. ISP gateway reachability always remains ambiguous
between a NetEm calibration interface and a clean baseline; confirm the bridge
attachment in Proxmox. Recommendations expire after 15 minutes and never apply
networking automatically.
The editable template maps management to vmbr0, appliance LAN to vmbr3,
WAN1/WAN2 calibration to vmbr1/vmbr2, and clean baselines to vmbr4/vmbr5.
Bridge labels are wiring guidance, not discovery or Proxmox provisioning.

Enter a unique simulator IPv4/prefix, gateway, controlled target data IP and
NetEm WAN ID for calibration. **Save plan** persists a draft without networking
changes. **Configure address & verify path** queues guest interface validation,
address enablement/persistence and source-bound path verification as one task.
It does not select the workload path or add a default gateway. After success,
open **Traffic paths** and select **Use for full workload**.

If target verification fails, the address remains saved for diagnosis; the
previous workload selection is retained. Configuration is blocked during runs
or administration tasks. Conflicting existing addresses must be changed in
Proxmox. Changed source/interface/target definitions create a new policy;
remove unused older policies from Traffic paths.

Calibration and baseline NICs sharing an ISP subnet need explicit ARP/address
handling and symmetric return routing. Prefer a separate baseline router
subnet/VLAN. Confirm physical traversal with WAN counters before scaling up.

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
