"""Unprivileged UI access to a narrow systemd administration queue."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

ADMIN_DIR = Path('/var/lib/netem-traffic-simulator-admin')


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def status(settings):
    state = read_json(ADMIN_DIR / 'status.json', {})
    ready = (ADMIN_DIR / 'install-manifest.json').is_file()
    queued = (settings.runtime_dir / 'admin-request.json').exists()
    active = state.get('state') == 'running' and time.time() - state.get('timestamp', 0) < 1800
    return {'ready': ready, 'busy': queued or active, 'job': state,
            'selected': read_json(ADMIN_DIR / 'route.json'),
            'interface_configs': read_json(ADMIN_DIR / 'interfaces.json', {}),
            'release': read_json(ADMIN_DIR / 'release.json', {})}


def enqueue(settings, action, payload=None):
    if action not in ('check_update', 'install_update', 'scan', 'route', 'clear_route', 'configure_interface', 'forget_interface'):
        raise ValueError('Unsupported administration action.')
    state = status(settings)
    if not state['ready']:
        raise ValueError('Run the current install-lxc.sh once as root to enable host management.')
    if state['busy']:
        raise ValueError('Another administration task is running.')
    job = {'id': uuid.uuid4().hex, 'action': action, 'payload': payload or {}, 'timestamp': time.time()}
    # Publish a complete request atomically; hard-link refuses an existing queued request.
    temporary = settings.runtime_dir / ('request-' + job['id'] + '.tmp')
    temporary.write_text(json.dumps(job))
    temporary.chmod(0o600)
    try:
        os.link(temporary, settings.runtime_dir / 'admin-request.json')
    except FileExistsError as exc:
        raise ValueError('Another administration task is queued.') from exc
    finally:
        temporary.unlink(missing_ok=True)
    return job
