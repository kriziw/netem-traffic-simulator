"""Runs work that takes real CPU time on OS threads, so the workload's event loop keeps serving users meanwhile.

Under gevent every greenlet shares one event loop. A call that computes for hundreds of milliseconds, such as a DEM
summary or a database write, holds that loop for the whole call. On a pool thread the loop switches back to users every
few milliseconds: the work takes the same time, but the users are not stalled behind it.
"""

import gevent.threadpool

POOL_SIZE = 4
_pool = None


def offloaded(function, *args, **kwargs):
    """Runs function on a pool thread and returns its result. The calling greenlet waits; the event loop does not."""
    global _pool
    if _pool is None:
        _pool = gevent.threadpool.ThreadPool(maxsize=POOL_SIZE)
    return _pool.apply(function, args, kwargs)
