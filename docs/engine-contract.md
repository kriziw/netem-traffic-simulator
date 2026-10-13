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
of physical loss rather than excessive delay. Real media codecs/session models and
packet-latency histograms remain a separate modelling extension.

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
