"""Root-only worker. Fixed actions; no command, URL or filesystem path from the UI."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

from .maintenance import ADMIN_DIR, read_json
from .network import discover, ip_json, run, validate_route

APP_DIR = Path('/opt/netem-traffic-simulator')
RUNTIME_DIR = Path('/var/lib/netem-traffic-simulator')
REPOSITORY = 'https://github.com/kriziw/netem-traffic-simulator.git'
RELEASE_API = 'https://api.github.com/repos/kriziw/netem-traffic-simulator/releases/latest'
ROUTE_PROTOCOL = '186'
SYSTEMD_DIR = Path('/etc/systemd/system')


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.admin-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def management():
    return read_json(ADMIN_DIR / 'policy.json', {}).get('management_interface', 'eth0')


def version_tuple(value):
    value = value.removeprefix('netem-traffic-simulator-')
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('Only stable semantic-version releases are supported.')
    return tuple(map(int, value.lstrip('v').split('.')))


def check_release():
    request = Request(RELEASE_API, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'NetEm-Traffic-Simulator'})
    with urlopen(request, timeout=15) as response:
        release = json.loads(response.read(1024 * 1024))
    tag = str(release.get('tag_name', ''))
    latest = version_tuple(tag)
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Release is not stable.')
    current = (APP_DIR / 'trafficgen/version.txt').read_text().strip()
    result = {'tag': tag, 'current': current, 'available': latest > version_tuple(current),
              'display_tag': 'v' + '.'.join(map(str, latest)),
              'url': 'https://github.com/kriziw/netem-traffic-simulator/releases/tag/' + tag,
              'checked_at': time.time()}
    atomic_json(ADMIN_DIR / 'release.json', result)
    return result


def files_manifest():
    paths = []
    for directory in ('trafficgen', 'scripts', 'deploy'):
        paths.extend(p for p in (APP_DIR / directory).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and not p.is_symlink())
    paths.extend(APP_DIR / name for name in ('requirements.txt', 'version.txt') if (APP_DIR / name).is_file())
    return {str(p.relative_to(APP_DIR)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def record_install():
    atomic_json(ADMIN_DIR / 'install-manifest.json', files_manifest())
    if not (ADMIN_DIR / 'policy.json').exists():
        atomic_json(ADMIN_DIR / 'policy.json', {'management_interface': 'eth0'})


def install_release(expected_tag):
    release = check_release()  # Resolve again in the trusted worker, not from client metadata.
    if not release['available'] or release['tag'] != expected_tag:
        raise ValueError('Release changed or no update is available. Check for updates again.')
    if files_manifest() != read_json(ADMIN_DIR / 'install-manifest.json'):
        raise ValueError('Installed application files were modified. Resolve local edits before updating.')
    size = sum(p.stat().st_size for p in APP_DIR.rglob('*') if p.is_file())
    if shutil.disk_usage(APP_DIR).free < size * 2 + 512 * 1024 * 1024:
        raise ValueError('Insufficient free disk space for an update and rollback copy.')
    with tempfile.TemporaryDirectory(prefix='trafficgen-update-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        source = root / 'source'
        run(['git', 'clone', '--quiet', '--depth', '1', '--branch', release['tag'], '--', REPOSITORY, str(source)], timeout=120)
        if version_tuple((source / 'trafficgen/version.txt').read_text().strip()) != version_tuple(release['tag']):
            raise ValueError('Release tag and packaged version disagree.')
        backup = root / 'backup'
        shutil.copytree(APP_DIR, backup, symlinks=True)
        old_manifest = read_json(ADMIN_DIR / 'install-manifest.json')
        units = {}
        for path in SYSTEMD_DIR.glob('netem-traffic-simulator*'):
            if path.is_file() and not path.is_symlink():
                units[path] = path.read_bytes()
        try:
            run(['bash', str(source / 'scripts/install-lxc.sh')], timeout=900)
            run(['systemctl', 'is-active', 'netem-traffic-simulator'], timeout=10)
        except Exception as exc:
            # Restore code and the old venv, leaving secrets and workload history intact.
            run(['systemctl', 'stop', 'netem-traffic-simulator'], timeout=20)
            shutil.rmtree(APP_DIR)
            shutil.copytree(backup, APP_DIR, symlinks=True)
            for path, contents in units.items():
                path.write_bytes(contents)
            atomic_json(ADMIN_DIR / 'install-manifest.json', old_manifest)
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'restart', 'netem-traffic-simulator'], timeout=30)
            raise ValueError('Update failed; previous installation restored. ' + str(exc)) from exc
    return {'version': release.get('display_tag', release['tag']), 'message': 'Update installed; simulator restarted.'}


def exact_routes(target):
    return ip_json('route', 'show', 'exact', target + '/32')


def managed(route):
    return str(route.get('protocol')) == ROUTE_PROTOCOL


def route_command(action, route):
    return ['ip', '-4', 'route', action, route['target'] + '/32', 'via', route['gateway'],
            'dev', route['interface'], 'src', route['source'], 'proto', ROUTE_PROTOCOL]


def apply_route(payload, verify=True):
    route = validate_route(payload, management())
    target = route['target']
    existing = exact_routes(target)
    if any(not managed(r) for r in existing):
        raise ValueError('An unmanaged host route already exists for this target. Resolve it before selecting an appliance.')
    previous = read_json(ADMIN_DIR / 'route.json')
    if existing and (not previous or any(r.get('dev') != previous['interface'] or r.get('gateway') != previous['gateway'] for r in existing)):
        raise ValueError('Existing host route is not the saved managed route.')
    if previous and previous['target'] != target:
        raise ValueError('Clear the current managed route before changing the target address.')
    run(route_command('replace', route))
    try:
        actual = ip_json('route', 'get', target)[0]
        if actual.get('dev') != route['interface'] or actual.get('gateway') != route['gateway']:
            raise ValueError('Kernel route does not match the selected appliance.')
        if verify:
            # Fixed lab health endpoint; do not use proxies or a management-side address.
            response = run(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error',
                            '--connect-timeout', '5', '--max-time', '10', f'http://{target}:8090/health'], timeout=12)
            if json.loads(response).get('service') != 'netem-traffic-target':
                raise ValueError('The selected path did not reach the controlled target.')
        atomic_json(ADMIN_DIR / 'route.json', route)
    except Exception:
        if previous:
            run(route_command('replace', validate_route(previous, management())))
        else:
            run(route_command('del', route))
        raise
    return {'route': route, 'verified': verify}


def clear_route():
    selected = read_json(ADMIN_DIR / 'route.json')
    if selected:
        route = selected
        if any(managed(r) for r in exact_routes(route['target'])):
            run(route_command('del', route))
        (ADMIN_DIR / 'route.json').unlink(missing_ok=True)
    return {'message': 'Managed target route removed; existing default routes are unchanged.'}


def process_request():
    request_path = RUNTIME_DIR / 'admin-request.json'
    try:
        fd = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    except OSError as exc:
        request_path.unlink(missing_ok=True)
        atomic_json(ADMIN_DIR / 'status.json', {'state': 'failed', 'message': str(exc), 'timestamp': time.time()})
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError('Invalid administration request file.')
        with os.fdopen(fd) as stream:
            job = json.load(stream)
        if not isinstance(job, dict) or not re.fullmatch('[a-f0-9]{32}', str(job.get('id', ''))):
            raise ValueError('Invalid job identity.')
    except Exception as exc:
        request_path.unlink(missing_ok=True)
        atomic_json(ADMIN_DIR / 'status.json', {'state': 'failed', 'message': str(exc), 'timestamp': time.time()})
        return
    state = {'id': job['id'], 'action': job.get('action'), 'state': 'running', 'timestamp': time.time()}
    # Mark running before consuming the request so workload-start sees a continuous busy state.
    atomic_json(ADMIN_DIR / 'status.json', state)
    request_path.unlink(missing_ok=True)
    try:
        payload = job.get('payload', {})
        if job['action'] == 'check_update':
            result = check_release()
        elif job['action'] == 'install_update':
            result = install_release(str(payload.get('tag', '')))
        elif job['action'] == 'scan':
            result = discover(management(), scan=True)
            atomic_json(ADMIN_DIR / 'discovery.json', result)
        elif job['action'] == 'route':
            result = apply_route(payload)
        elif job['action'] == 'clear_route':
            result = clear_route()
        else:
            raise ValueError('Unsupported administration action.')
        state.update(state='completed', result=result, message=result.get('message', 'Task completed.'))
    except Exception as exc:
        state.update(state='failed', message=str(exc))
    state['timestamp'] = time.time()
    atomic_json(ADMIN_DIR / 'status.json', state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--record-install', action='store_true')
    parser.add_argument('--restore-route', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Root administration worker required.')
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    if args.record_install:
        record_install()
        return
    with (ADMIN_DIR / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.restore_route:
            route = read_json(ADMIN_DIR / 'route.json')
            if route:
                apply_route(route, verify=False)
        else:
            process_request()


if __name__ == '__main__':
    main()
