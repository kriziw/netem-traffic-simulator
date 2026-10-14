# Small-site evidence and target timing

Application utility is a measured result, separate from acceptance evidence.
One valid completed operation can produce a score labelled `limited_sample`.
The five-operation application gate, twenty-operation overall gate, statistical
violation rules, and explicit hard deadlines still govern acceptance. An absent
application remains unknown; missing required measurements do not earn credit.

`coverage_pct` now describes the configured workload/criticality weight with
valid measured utility. `evidence_coverage_pct` retains the weight that satisfies
the minimum application sample gate. `score_range` bounds unobserved application
contributions; it is **not a statistical confidence interval**. Sample count and
first/last observation timestamps remain available. Scores continue to use only
the selected live interval and current phase; older phases are never borrowed
to inflate sample count. Full-phase reporting retains the complete interval.

User actions use smooth weighted selection, shared by users of the same persona.
This reduces random application gaps while retaining configured action shares,
normal think time, payload choices and population. It does not create additional
user actions or guarantee five completions for rare applications every 15 seconds.
Long media sessions and slow transfers still reduce closed-loop action rates.
Packets within one session are not promoted to independent user transactions.

Application completion time covers request start through complete response.
It includes upload, queueing, target processing and downloading the response.
The HTTP timing object also reports request-to-headers and response-body P95;
those percentiles are not additive and neither is pure network latency.

A separate small UDP echo runs **at most once per second per active workload**
to the controlled target agent's configured UDP port, bound to the workload's
source address. It uses the existing agent echo protocol and has a two-second
reply bound. Only matching replies from the connected target count. It pauses
with the test and discards replies crossing a phase/context revision. These
checks appear as `target_timing` operation events, never application transactions,
and do not affect application scores or confidence. They add one 16-byte UDP
request and a small reply per second, plus transport/link overhead. This is a
small, nonzero load; extremely constrained links must account for it.

`timing.target_rtd` reports measured P50/P95/latest RTD (RTT), reply count,
attempt count and unanswered checks. This is round-trip timing to the target,
including endpoint scheduling and packet serialization. Application-aware
appliances may route this diagnostic UDP flow differently from other classes.
Existing media packet RTT distributions are retained separately. One-way network
latency is **not measured** and RTT is never divided by two. Configured NetEm
one-way delay is an impairment setting, not a measured path latency.

## Troubleshooting export API

`GET /api/v1/runs/<run_id>/evidence?since=<unix>&until=<unix>&kind=transactions`
requires the existing Bearer authentication. Kinds are `transactions`,
`operations` (including target timing), and `contracts`. Intervals are half-open,
finite, and at most 24 hours. Contracts include preceding run configuration.
Transactions use completion timestamps; operation events retain their own
timestamps, start times, phase identity and operation ID where recorded.

Use `next_cursor` and repeat the returned `ceiling` for subsequent pages. Each
page contains at most 2,000 rows (default 1,000). The ceiling freezes table IDs
against concurrent writes. A persistence barrier precedes reads; a timed-out
barrier returns 503 rather than an apparently complete export. This is an
evidence read, not a stop/drain of in-flight work. Normal raw-history retention
still applies; the platform archives evidence when each test finishes.
