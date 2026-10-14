"""Bounded open-loop directional payload demand. Demand never waits for a response.

The generator records missed admissions instead of silently reducing the offered
rate when all workers are busy. Wire throughput remains a separate measurement.
"""
import math
import time
import gevent
from gevent.queue import Queue, Full


class RateScheduler:
    chunk_kb = 256

    def __init__(self, config):
        self.config = config
        self.jobs = Queue(maxsize=max(1, config["target_users"]))
        self.next_at = {}
        self.greenlet = None
        self.offered_bytes = 0
        self.admitted_bytes = 0
        self.missed_requests = 0
        self.started_bytes = {direction: 0 for direction in ("down", "up")}
        self.delivered_bytes = dict(self.started_bytes)
        self.direction_counts = {direction: {"offered_bytes": 0, "admitted_bytes": 0, "missed_requests": 0}
                                 for direction in self.started_bytes}
        self.started_at = time.monotonic()
        self.last_admitted = {"down": float("-inf"), "up": float("-inf")}

    def offer(self, now):
        waits = []
        # Alternate successful admissions, not wakeups: commensurate rates can
        # otherwise present simultaneous arrivals on the same turn forever.
        directions = sorted(self.rates().items(), key=lambda item: self.last_admitted[item[0]])
        for direction, rate in directions:
            if not rate:
                continue
            interval = self.chunk_kb * 1024 * 8 / (rate * 1_000_000)
            due = self.next_at.setdefault(direction, now)
            if now >= due:
                count = math.floor((now - due) / interval) + 1
                counts = self.direction_counts[direction]
                counts["offered_bytes"] += count * self.chunk_kb * 1024
                counts["missed_requests"] += count - 1
                try:
                    self.jobs.put_nowait((direction, self.chunk_kb, now))
                    counts["admitted_bytes"] += self.chunk_kb * 1024
                    self.last_admitted[direction] = now
                except Full:
                    counts["missed_requests"] += 1
                self.next_at[direction] += count * interval
            waits.append(max(0, self.next_at[direction] - now))
        for key in ("offered_bytes", "admitted_bytes", "missed_requests"):
            setattr(self, key, sum(counts[key] for counts in self.direction_counts.values()))
        return min(waits, default=.1)

    def rates(self):
        return {"down": self.config.get("rate_down_mbps", self.config.get("paced_mbps", 0)) or 0,
                "up": self.config.get("rate_up_mbps", 0) or 0}

    def dispatched(self, direction, kb):
        self.started_bytes[direction] += kb * 1024

    def completed(self, direction, payload_bytes):
        self.delivered_bytes[direction] += max(0, payload_bytes)

    def run(self):
        while True:
            gevent.sleep(self.offer(time.monotonic()))

    def start(self):
        self.greenlet = gevent.spawn(self.run)
        return self

    def stop(self):
        if self.greenlet is not None:
            self.greenlet.kill()
        self.missed_requests += self.jobs.qsize()

    def snapshot(self):
        return {"target_mbps": sum(self.rates().values()), "scope": "directional_payload",
                "timestamp_monotonic": time.monotonic(),
                "chunk_bytes": self.chunk_kb * 1024,
                "tolerance": {"window_seconds": 30, "relative": .05, "quantization_bytes": self.chunk_kb * 1024},
                "directions": {direction: dict(counts, target_mbps=self.rates()[direction],
                                  started_bytes=self.started_bytes[direction], delivered_bytes=self.delivered_bytes[direction])
                               for direction, counts in self.direction_counts.items()},
                "offered_bytes": self.offered_bytes, "admitted_bytes": self.admitted_bytes,
                "missed_requests": self.missed_requests, "queued_requests": self.jobs.qsize()}
