"""Bounded application monitoring alongside the configured employee population."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CAPABILITY = 'continuous-application-checks-v1'
MAX_OBJECT_KB = 32
ENDPOINT_PREFIX = 'check-'


def packet_wait(interval, origin, sequence, now, monitoring=False):
    # Monitoring never repays scheduler lateness with back-to-back packets.
    return interval if monitoring else max(0, origin + (sequence + 1) * interval - now)


def check_interval(run):
    # Leave room for service time and five completed observations in a live window.
    window = (run.get('live_context') or {}).get('window_seconds', run.get('live_window_seconds', 15))
    count = max(1, len(enabled_applications(run)))
    return max(.2, min(2.0, window / max(8, 24 / count)))


def enabled_applications(run):
    if not run.get('application_checks') or run.get('generation_mode') == 'rate':
        return []
    return sorted(app for app, share in run.get('manifest', {}).get('application_shares', {}).items() if share > 0)


def bound_request(args, kwargs):
    """Cap objects before the normal request wrapper records expected bytes."""
    args, kwargs = list(args), dict(kwargs)
    url = args[1] if len(args) > 1 else kwargs.get('url', '')
    parts = urlsplit(url)
    query = [(key, str(min(MAX_OBJECT_KB, int(value))) if key == 'kb' else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    url = urlunsplit(parts._replace(query=urlencode(query)))
    if len(args) > 1:
        args[1] = url
    else:
        kwargs['url'] = url
    if isinstance(kwargs.get('data'), bytes):
        kwargs['data'] = kwargs['data'][:MAX_OBJECT_KB * 1024]
    kwargs['timeout'] = (2, 2)
    return args, kwargs


def measurement_counts(rows):
    checks = [row for row in rows if str(row.get('endpoint_id') or '').startswith(ENDPOINT_PREFIX)]
    return {'application_checks': len(checks), 'user_transactions': len(rows) - len(checks),
            'check_bytes_down': sum(int(row.get('response_length') or 0) for row in checks),
            'check_bytes_up': sum(int(row.get('bytes_up') or 0) for row in checks),
            'basis': 'user activity plus bounded synthetic application checks; not a capacity test'}
