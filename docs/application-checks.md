# Continuous application checks

Start a user-mode workload with `application_checks: true` (or select the checkbox
on Corporate workloads). The catalog advertises `continuous-application-checks-v1`.
NetEm enables this automatically for sites with at most 25 simulated users when
the capability is available. Existing API callers retain the default of off.

Each application with a positive share in the executable workload manifest gets
one independent check worker. Workers use the same application handlers, controlled
target, HTTP source binding and UDP source binding as employee traffic. They are
outside the Locust employee population and do not claim persona slots.

HTTP objects are capped at 32 KiB, with two-second connection/read timeouts. UDP
checks send at most eight packets per burst at the application's existing packet
interval (voice 20 ms, video 5 ms, OT telemetry 50 ms). Delayed sends never catch
up with back-to-back packets. Intentional packet spacing is added to the monitoring
operation's lifecycle budget; packet RTT/loss/jitter are still scored against the
original application targets, without that allowance.
Checks start at staggered offsets and run at most once every 0.2–2 seconds per
application, depending on the current live window and application count. The normal
15-second window with at least three applications uses 1.875-second intervals.
There is at most one check per application in flight; slow operations never trigger
catch-up bursts. Applications removed from the manifest stop receiving checks;
newly enabled applications start receiving them. Checks stop with the workload,
including controller lease expiry, and pause when the controller freezes the live
context. Rate/calibration mode rejects this option.

The schedule aims for eight observations per application and 24 overall per window
before service time. It does not guarantee statistical confidence or completion
under congestion, outage, host overload, or long response times. Existing sample
thresholds, deadlines and failure/acceptance rules still apply.

Checks contribute real transactions to the existing application experience score,
weighted by the configured workload shares. These are lightweight synthetic
transactions, not additional employees or full-size business transfers, and do not
establish link capacity. Do not use them for bulk-capacity acceptance. Calibration
uses its separate rate-controlled workload.
Optional transfer-speed targets that require larger objects still need employee
transfers of sufficient size; small monitoring objects cannot supply that evidence.

`measurement_sources` in DEM/status/experience responses separates
`application_checks`, `user_transactions`, `check_bytes_down` and `check_bytes_up`.
Each application's summary includes the same counters. Check endpoints use the
`check-` prefix and persona `application_monitor`; raw records retain both for
exports. Endpoint/persona summaries exclude monitoring workers. Immutable scoring
contracts retain the monitoring flag, and exact phase/window filtering remains in
effect for checks and employee traffic alike.

If a monitoring sender misses a packet interval by more than 50 ms (or one whole
interval, whichever is larger), it records `application_check_generator_lag`.
Affected-window scores and network findings become unconfirmed rather than blaming
the network for generator stalls. Raw measurements and the generator event remain
available for inspection. Existing user and calibration pacing are unchanged.

HTTP targets need no new endpoints. Target-reported source metadata and NetEm's
WAN-address mapping are still needed to identify which WAN carried a check.
