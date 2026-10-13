"""Bounded open-loop download demand. Demand never waits for a response.

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
        self.next_at = None
        self.greenlet = None
        self.offered_bytes = 0
        self.admitted_bytes = 0
        self.missed_requests = 0

    def offer(self, now):
        interval = self.chunk_kb * 1024 * 8 / (self.config["paced_mbps"] * 1_000_000)
        if self.next_at is None:
            self.next_at = now
        if now < self.next_at:
            return self.next_at - now
        count = math.floor((now - self.next_at) / interval) + 1
        self.offered_bytes += count * self.chunk_kb * 1024
        # Never replay an unbounded catch-up burst after a scheduling stall.
        self.missed_requests += count - 1
        try:
            self.jobs.put_nowait((self.chunk_kb, now))
            self.admitted_bytes += self.chunk_kb * 1024
        except Full:
            self.missed_requests += 1
        self.next_at += count * interval
        return max(0, self.next_at - now)

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
        return {"target_mbps": self.config["paced_mbps"], "scope": "download_payload",
                "offered_bytes": self.offered_bytes, "admitted_bytes": self.admitted_bytes,
                "missed_requests": self.missed_requests, "queued_requests": self.jobs.qsize()}
