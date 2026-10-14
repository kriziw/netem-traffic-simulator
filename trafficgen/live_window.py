"""Live observation scope, independent of immutable scoring contracts and report queries."""
import json
import math
import time

from .database import connect

DEFAULT_WINDOW_SECONDS = 15


def window_seconds(value=DEFAULT_WINDOW_SECONDS):
    if isinstance(value, bool):
        raise ValueError('Live window must be an integer from 5 to 120 seconds.')
    try:
        number = float(value)
    except (ValueError, TypeError):
        number = float('nan')
    if not math.isfinite(number) or not number.is_integer() or not 5 <= number <= 120:
        raise ValueError('Live window must be an integer from 5 to 120 seconds.')
    return int(number)


def read_context(path, run_id):
    with connect(path) as conn:
        row = conn.execute('SELECT payload FROM live_contexts WHERE run_id=?', (run_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def initialize(path, run):
    context = dict(base_window_seconds=run.get('live_window_seconds', DEFAULT_WINDOW_SECONDS),
                   window_seconds=run.get('live_window_seconds', DEFAULT_WINDOW_SECONDS),
                   run_since=run['started_at'], since=run['started_at'], phase_id=None, until=None, revision=0)
    store(path, run['run_id'], context)
    return context


def store(path, run_id, context):
    with connect(path) as conn:
        conn.execute('INSERT OR REPLACE INTO live_contexts(run_id,payload) VALUES (?,?)', (run_id, json.dumps(context)))


def change_context(path, run_id, previous, raw, now=None):
    if not isinstance(raw, dict) or set(raw) - {'window_seconds', 'phase_id', 'paused', 'reset'}:
        raise ValueError('Invalid live measurement context.')
    if not isinstance(raw.get('paused', False), bool) or not isinstance(raw.get('reset', False), bool):
        raise ValueError('Live pause/reset flags must be booleans.')
    phase = raw.get('phase_id')
    if not isinstance(phase, str) or not 1 <= len(phase) <= 240:
        if not raw.get('reset'):
            raise ValueError('A live phase identity is required.')
    now = time.time() if now is None else now
    context = dict(previous)
    if raw.get('reset'):
        context.update(window_seconds=context['base_window_seconds'], since=context['run_since'], phase_id=None, until=None)
    else:
        seconds = window_seconds(raw.get('window_seconds', context['window_seconds']))
        if phase != context['phase_id'] or (context['until'] is not None and not raw.get('paused')):
            context['since'] = now
        context.update(window_seconds=seconds, phase_id=phase,
                       until=(context['until'] if context['until'] is not None else now) if raw.get('paused') else None)
    if context != previous:
        context['revision'] += 1
        store(path, run_id, context)
    return context


def query_arguments(context, now=None):
    now = time.time() if now is None else now
    end = context.get('until') if context.get('until') is not None else now
    start = max(context['since'], end - context['window_seconds'])
    if start >= end or end > now + .1:
        raise ValueError('No current-phase observation interval yet.')
    return dict(window_seconds=context['window_seconds'], since=start, until=end)


def annotate(summary, context):
    summary.update(configured_window_seconds=context['window_seconds'], live_phase_id=context['phase_id'],
                   live_context_revision=context['revision'], window_scope='live_phase' if context['phase_id'] else 'live_run',
                   collecting_evidence=summary.get('scoring_complete') is not True,
                   live_paused=context['until'] is not None)
    return summary


def measure(path, run_id, context, media_mode='strict', query=None, diagnosis=True):
    from .dem import dem_summary, summarize_rows
    query = query or (lambda **arguments: dem_summary(path, **arguments))
    try:
        arguments = query_arguments(context)
    except ValueError:
        # A boundary can be acknowledged in the same clock tick, or the clock
        # can move backwards. Neither condition authorizes using previous data.
        summary = dict(summarize_rows([]), run_id=run_id, applications={}, personas={}, endpoints={},
            diagnosis={'findings': [], 'egress': {}}, timestamp=time.time(), since=context['since'],
            until=context.get('until') or time.time(), window_seconds=0, truncated=False,
            evidence_issues=['live_interval_unavailable'], interactive_p95_ms=None,
            requests_per_second=0, failures_per_second=0, at_risk=False)
    else:
        summary = query(run_id=run_id, media_mode=media_mode, diagnosis=diagnosis, **arguments)
    return annotate(summary, context)
