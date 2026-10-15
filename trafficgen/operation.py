"""Lightweight operation lifecycle with monotonic deadlines and persistent evidence events."""
import time
import uuid

import gevent


class Operation:
    def __init__(self, config, context, emit=None, pacing_ms=0):
        self.config, self.context, self.emit = config, context, emit
        context.setdefault("operation_id", uuid.uuid4().hex)
        context.setdefault("started_at", time.time())
        self.started = time.monotonic()
        context["started_monotonic"] = self.started
        self.pending = config.setdefault("in_flight", {})
        self.pending[context["operation_id"]] = context
        self.target_ms = config.get("policy", {}).get(context.get("application"), {}).get("deadline_ms", 400) + pacing_ms
        if pacing_ms:
            context['deadline_budget_ms'] = self.target_ms
            context['intentional_pacing_ms'] = pacing_ms
        self.event("started")
        self.timer = gevent.spawn_later(self.target_ms / 1000, self.expire)

    def event(self, kind):
        if callable(self.emit):
            self.emit(dict(self.context, kind=kind, timestamp=time.time(),
                           elapsed_ms=(time.monotonic() - self.started) * 1000))

    def expire(self):
        self.context["deadline_expired"] = True
        self.event("deadline_expired")

    def close(self, cancelled=False):
        self.timer.kill()
        elapsed = (time.monotonic() - self.started) * 1000
        if elapsed > self.target_ms and not self.context.get("deadline_expired"):
            self.expire()
        drift = (time.time() - self.context["started_at"]) * 1000 - elapsed
        self.context["clock_discontinuity"] = abs(drift) > 1000
        self.event("cancelled" if cancelled else "completed")
        self.pending.pop(self.context["operation_id"], None)
