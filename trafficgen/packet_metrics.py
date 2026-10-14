"""Mergeable packet RTT distributions; bucket upper bounds are explicit, never burst percentiles."""
import bisect
import math

BOUNDS_MS = tuple([i / 10 for i in range(1, 101)] + list(range(11, 101)) +
                  list(range(110, 1001, 10)) + list(range(1100, 10001, 100)) + [30000, 60000, 600000])


def histogram(values):
    bins = {}
    for value in values:
        index = min(len(BOUNDS_MS) - 1, bisect.bisect_left(BOUNDS_MS, max(0, value)))
        key = str(BOUNDS_MS[index])
        bins[key] = bins.get(key, 0) + 1
    return bins


def merge(histograms):
    bins = {}
    for item in histograms:
        for bound, count in item.items():
            bins[bound] = bins.get(bound, 0) + count
    return bins


def percentile(bins, quantile):
    count = sum(bins.values())
    rank = max(1, math.ceil(count * quantile))
    cumulative = 0
    for bound in sorted(bins, key=float):
        cumulative += bins[bound]
        if cumulative >= rank:
            return float(bound)
    return None
