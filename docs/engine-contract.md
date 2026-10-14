# Application engine contract (workload-utility-v2)

New runs freeze the effective application policy, model version, seed and executable
workload manifest. Adjustments append configuration epochs. An interval spanning an
adjustment is explicitly incomplete; it is never silently judged using the latest
configuration. Historical transactions without a contract are explicitly labelled
`current_policy_regrade`; existing saved time-series values are retained.

## Utility and compliance

For each application, delivered operations receive elastic deadline credit
`min(100, 100 * target_ms / elapsed_ms)`. Twice the target means half credit.
`hard_deadline: true` instead gives zero credit to late operations. Transport
success credit is relative to `success_min_pct`. The weakest measured quality
component multiplies transport credit. Packet loss already accounts for unanswered
media packets; it is not counted a second time as a failed entire media burst.

Overall utility uses configured activity share multiplied by application criticality
weight, normalized across the expected workload. Defaults are standard 1, important
2, critical 4; `criticality_weight` is configurable. Observed transaction counts do
not decide application importance. Critical target violations remain separate
`critical_alerts`, even when overall utility is high. Compatibility field `critical`
maps to critical/standard on policy input.

Strict target compliance remains separate from utility. A target miss can fail a
business acceptance check without meaning that the whole site is unusable. A small
application failure cannot set unrelated applications to zero. Incomplete samples
do not establish either health or an outage.

Application sustained-goodput targets apply to the whole application, not separately
to each endpoint/persona/egress. Per-transfer goodput uses completed payload divided
by total operation duration in both scoring and diagnosis. Interface wire rate is a
different measurement. Transfer targets need actual transfer evidence.

## Evidence

Five observations are needed per expected application and twenty in the overall
window. Missing expected applications are unknown and are not renormalized away.
All consumers must inspect `scoring_complete`, `evidence_issues`, and `truncated`
before making a complete verdict. Explanations include configured shares, normalized
weights, criticality factors, component evidence, violations and lost score points.

DEM APIs accept `run_id`, `since`, `until` as exact half-open completion-time intervals
up to 24 hours. They drain completed events before querying, exclude future events,
and reject invalid bounds. A 50,000-row limit invalidates completeness. Queries are
attributed by completion time; a request crossing a phase belongs to its completion
phase. HTTP/UDP cancellations have explicit records. Live results include outstanding
operation count/age; overdue outstanding work invalidates a healthy verdict.

Successful-operation latency, all-attempt latency and cancellation count are
separate. UDP operation latency is the mean RTT of delivered echo packets; its P95
is a percentile of burst measurements, not packet P95 or perceptual MOS. RTT
variation is not one-way jitter. Collection is bounded between 0.5 and 10 seconds,
based on the configured deadline. Packets absent at collection expiry are not proof
of physical loss rather than excessive delay. Packet RTT and adjacent-sequence RTT-variation histograms are now persisted and merged;
reported histogram percentiles are bucket upper bounds. Late replies are counted
separately from packets missing at collection expiry. Physical loss cannot be
distinguished from a reply arriving after that finite observation boundary.

## Generation

`generation_mode: users` retains user actions and think time. The manifest resolves
exact persona/device counts and expected action shares. Per-user seeded random
choices are repeatable given the same lifecycle; network timing is not deterministic.

`generation_mode: rate`, with `paced_mbps`, schedules bounded 256 KiB downloads on an
arrival clock independent of responses. Existing `paced_mbps` requests select this
mode automatically. The user count is the concurrency limit, not a rate multiplier.
Offered/admitted payload bytes, missed admissions and queued demand are reported in
`run.rate_demand`. Scheduling stalls and exhausted worker capacity do not silently
lower the declared offered rate or create an unbounded catch-up burst. This mode
targets download payload demand, not both-direction wire throughput. Sparse UDP
users are separately counted and slots are released on shutdown.

## Operational validation

The regression suite covers the reported partial voice failure, elastic/hard
deadlines, criticality, missing coverage, policy history, exact windows, data loss,
row caps, future timestamps, cancellation, persona counts and bounded rate demand.
Linux CI remains required for routing, service, installer and socket tests. Lab
acceptance must measure achieved rate, packet timing, generator/target CPU and
observer overhead together; configuration alone is not proof of delivered capacity.

## Completion of the remaining measurement contract

- Directional rate mode accepts `rate_down_mbps` and `rate_up_mbps`; legacy
  `paced_mbps` remains download-only input. Each direction has an independent
  arrival budget, with offered, admitted, dispatched and delivered payload counters.
  The admission tolerance is 5% plus one 256 KiB quantum over a 30-second window.
  This is a measurement tolerance, not a promise that an overloaded generator meets it.
- `media_pattern: sustained` creates one-second synthetic packet windows throughout
  `media_session_s` (default 30, range 1–300). Reply collection overlaps the next
  sending window and concurrency is bounded. Voice/video request equal-size echoes;
  cameras send synthetic upstream UDP payload. `bursts` retains compatibility.
  Neither mode claims a real codec, perceptual MOS or a DNS-protocol implementation.
- Operation starts, deadline expiry, completion and cancellation are persisted with
  run, operation, controller phase, configuration epoch and path identity. A stop
  blocks new actions and allows `completion_grace_s` (default 2, maximum 10) before
  cancellation. Event loss invalidates evidence. Historical queries expose outstanding
  and overdue work at their end boundary.
- Exact queries accept an ordered JSON `intervals` list of up to 64 disjoint active
  segments inside `since`/`until`. Paused completions are excluded, and sustained
  rates use total active duration. Attribution remains by completion time; crossing
  operations and overdue-at-boundary counts remain explicit.
- `sustained_scope` selects application, endpoint, persona or egress budgets.
  Per-member compliance uses the weakest member; application utility averages member
  utility. Missing identities invalidate coverage. Per-operation throughput uses
  `min_down_mbps`/`min_up_mbps`. Explicit direction/expected size preserves failed
  transfers as zero delivery. Application scope is the whole site's application budget.
- Worker snapshots use monotonic freshness plus wall-clock consistency. Clock steps
  detected during execution invalidate the affected run's reproducibility.
- All live, page, custom-window and fallback aggregation requests use the supervised
  process. Eight pending requests and short cached results bound observer work;
  identical queries deduplicate. Unavailable aggregation returns incomplete evidence.
  Only the explicit `TRAFFICGEN_DEM_WORKER=0` development/test option computes locally.
- Packet source-address transitions include the bounds between adjacent observations.
  These are packet observations, not appliance control-plane switch timestamps;
  reordering, missing packets and NAT can widen or invalidate the inference.

The complete deployed topology/kernel/offload/capacity acceptance matrix still requires
an identified disposable lab. Passing unit and namespace tests does not certify it.


## Compliance, coverage and rollout

Ordinary latency compliance uses configured P95, not the maximum. Reliability and
ordinary latency breaches are observations until there are at least three misses
and the one-sided 95% Wilson lower bound exceeds the allowed miss fraction (5%
for P95). Operations are the sampling unit, not correlated packets in one burst.
This is a fixed-window evidence rule, not a causal or repeated-testing guarantee.
Explicit hard deadlines retain zero tolerance. Alerts expose observation,
violation and outage severity. Confirmed critical violations fail business
acceptance even when another application lacks samples; observations cannot pass.

A provisional headline describes only applications with sufficient measurement.
`coverage_pct` is their configured criticality-weighted share. `score_range` bounds
the full-workload score by assigning unknown applications 0 through 100. A
provisional value cannot pass acceptance. Pending overdue work exposes `at_risk`,
not a fabricated outage. Coverage is not the proportion of successful work.

`legacy_score` is a one-release shadow calculation frozen from f0aea4c; it never
controls v2 utility or acceptance. Existing site score thresholds remain policy
choices, not calibrated equivalents of the legacy scale. Deploy controlled target
first, simulator second, platform third. Sustained media refuses targets without
`media-full-echo-v1` before starting a run. The target still supports legacy echo.
Database additions are additive; back up the database before deployment and roll
back consumers before simulator/target. Old readers do not understand v2
acceptance; mixed-version operation is transitional, not acceptance certification.
