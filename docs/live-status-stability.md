# Live status responsiveness

## Defects

* `_live_dem` held a shared semaphore while waiting up to five seconds for an
  aggregation request whenever the publisher had no matching fresh snapshot.
  Concurrent status callers queued behind it. The DEM history sampler also
  submitted fallback work to the same delayed publisher.
* `dem_worker.serve` executed explicit report queries between live publications.
  A slow historical query delayed every subsequent live publication. Publication
  then waited another full interval after computing instead of accounting for
  the computation time.
* Idle status reads unnecessarily aggregated recent transactions from all runs.
* Live snapshot reads inherited the five-second SQLite lock timeout.
* The history sampler assigned every exception to `persistence_error`. This flag
  invalidates application evidence for the rest of the run, even for a temporary
  snapshot-read failure with no lost application transaction.

## Changes

Production live status reads the published snapshot without taking the fallback
aggregation lock or submitting a report query. Missing, expired or mismatched
run/contract/phase evidence returns a valid pending DEM with the current phase
identity and null measurements. The run remains running; pending measurements
are not an API disconnect. Idle status returns empty measurements immediately.
The explicit `TRAFFICGEN_DEM_WORKER=0` development path retains local computation.

A dedicated report thread in the existing aggregation subprocess handles queued
reports while the main thread publishes live snapshots. Publication cadence
accounts for elapsed computation time. Report processing remains outside the
workload interpreter; Python CPU/GIL and disk contention can still affect timing.
This is scheduling isolation, not reserved CPU capacity. Explicit report APIs
retain their bounded five-second aggregation wait and existing evidence rules.
Snapshot SQLite reads use a 50 ms lock bound and return pending on contention.

History sampling errors use `dem_sampler_error`, exposed in running status and
cleared after a successful sample or new run. Transaction-writer failures and
transaction-loss counters retain their conservative evidence-invalidating effect.
No freshness, sample-count or acceptance gate has been relaxed.

Regression checks cover simultaneous status readers while the old lock is held,
HTTP status with a missing publisher, a blocked report while live snapshots
continue, real SQLite lock contention, and sampler/transaction error separation.
