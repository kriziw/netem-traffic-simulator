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

from .maintenance import ADMIN_DIR, read_json, version_tuple
from .appliance_identity import enrich_candidates
from .proxmox_inventory import validate_config, collect
from .network import discover, ip_json, run, validate_route, validate_interface, interfaces

TARGET_ROLE = os.environ.get('NETEM_ADMIN_ROLE') == 'target'
APP_DIR = Path('/opt/netem-traffic-simulator')
RUNTIME_DIR = Path('/var/lib/netem-traffic-target-manager' if TARGET_ROLE else '/var/lib/netem-traffic-simulator')
if TARGET_ROLE:
    ADMIN_DIR = Path('/var/lib/netem-traffic-target-admin')
SERVICE_NAME = 'netem-traffic-target' if TARGET_ROLE else 'netem-traffic-simulator'
REPOSITORY = 'https://github.com/kriziw/netem-traffic-simulator.git'
RELEASE_API = 'https://api.github.com/repos/kriziw/netem-traffic-simulator/releases/latest'
ROUTE_PROTOCOL = '186'
SYSTEMD_DIR = Path('/etc/systemd/system')


def atomic_json(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.admin-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def management():
    return read_json(ADMIN_DIR / 'policy.json', {}).get('management_interface', 'eth0')


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
    if (APP_DIR / 'requirements.txt').is_file():
        paths.append(APP_DIR / 'requirements.txt')
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
        for path in SYSTEMD_DIR.glob(SERVICE_NAME + '*'):
            if path.is_file() and not path.is_symlink():
                units[path] = path.read_bytes()
        try:
            installer = 'install-target.sh' if TARGET_ROLE else 'install-lxc.sh'
            run(['bash', str(source / 'scripts' / installer)], timeout=900)
            run(['systemctl', 'is-active', SERVICE_NAME], timeout=10)
            if TARGET_ROLE:
                run(['systemctl', 'is-active', 'netem-traffic-target-manager'], timeout=10)
                for attempt in range(10):
                    try:
                        health = json.loads(run(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error',
                            '--connect-timeout', '5', '--max-time', '10', 'http://127.0.0.1:8090/health'], timeout=12))
                        break
                    except (ValueError, OSError):
                        if attempt == 9:
                            raise
                        time.sleep(1)
                if not isinstance(health, dict) or health.get('service') != 'netem-traffic-target' or version_tuple(str(health.get('version', ''))) != version_tuple(release['tag']):
                    raise ValueError('Updated target did not report the expected version.')
        except Exception as exc:
            # Restore code and the old venv, leaving secrets and workload history intact.
            run(['systemctl', 'stop', SERVICE_NAME], timeout=20)
            if TARGET_ROLE:
                run(['systemctl', 'stop', 'netem-traffic-target-manager'], timeout=20)
            shutil.rmtree(APP_DIR)
            shutil.copytree(backup, APP_DIR, symlinks=True)
            for path, contents in units.items():
                path.write_bytes(contents)
            atomic_json(ADMIN_DIR / 'install-manifest.json', old_manifest)
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'restart', SERVICE_NAME], timeout=30)
            if TARGET_ROLE:
                run(['systemctl', 'restart', 'netem-traffic-target-manager'], timeout=30)
            raise ValueError('Update failed; previous installation restored. ' + str(exc)) from exc
    return {'version': release.get('display_tag', release['tag']),
            'message': 'Update installed; target restarted.' if TARGET_ROLE else 'Update installed; simulator restarted.'}


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



def configure_interface(payload, persist=True):
    config = validate_interface(payload, management())
    name = config['interface']
    saved = read_json(ADMIN_DIR / 'interfaces.json', {})
    if name in saved and saved[name] != config:
        raise ValueError('Stop restoring the old saved interface configuration before changing it.')
    link = next(r for r in ip_json('address', 'show') if r.get('ifname', '').split('@')[0] == name)
    was_up = 'UP' in link.get('flags', [])
    had_address = any(a.get('family') == 'inet' and f"{a['local']}/{a['prefixlen']}" == config['address'] for a in link.get('addr_info', []))
    added = False
    try:
        run(['ip', 'link', 'set', 'dev', name, 'up'])
        if not had_address:
            run(['ip', '-4', 'address', 'add', config['address'], 'dev', name])
            added = True
        rows = interfaces(management())
        actual = next((r for r in rows if r['interface'] == name), None)
        if not actual or not actual['up'] or config['address'] not in actual['addresses']:
            raise ValueError('The data interface did not become configured. Check container network permissions.')
        if persist:
            saved[name] = config
            atomic_json(ADMIN_DIR / 'interfaces.json', saved)
    except Exception:
        if added:
            run(['ip', '-4', 'address', 'del', config['address'], 'dev', name])
        if not was_up:
            run(['ip', 'link', 'set', 'dev', name, 'down'])
        raise
    return {'message': f"{name} enabled with {config['address']}. Saved for boot restoration; verify the appliance next."}


def forget_interface(payload):
    name = str(payload.get('interface', ''))
    saved = read_json(ADMIN_DIR / 'interfaces.json', {})
    saved.pop(name, None)
    atomic_json(ADMIN_DIR / 'interfaces.json', saved)
    return {'message': 'Boot restoration removed. Current addresses and routing are unchanged.'}


def restore_network():
    failures = []
    for config in read_json(ADMIN_DIR / 'interfaces.json', {}).values():
        try:
            configure_interface(config, persist=False)
        except Exception as exc:
            failures.append(str(exc))
    route = read_json(ADMIN_DIR / 'route.json')
    if route:
        try:
            apply_route(route, verify=False)
        except Exception as exc:
            failures.append(str(exc))
    if failures:
        raise ValueError('; '.join(failures))


def inventory_configure(payload):
    config = validate_config(payload, management())
    result = collect(config)
    config['fingerprint'] = result['fingerprint']
    atomic_json(ADMIN_DIR / 'proxmox-secret.json', config, mode=0o600)
    (ADMIN_DIR / 'discovery.json').unlink(missing_ok=True)
    atomic_json(ADMIN_DIR / 'proxmox-status.json', {'connected': True, 'host': config['host'],
        'token_id': config['token_id'], 'fingerprint': config['fingerprint'], 'vm_count': result['vm_count'], 'warnings': result['warnings']})
    atomic_json(ADMIN_DIR / 'proxmox-inventory.json', result)
    return {'message': f"Read-only Proxmox inventory connected: {result['vm_count']} visible VMs. Scan data LANs to match their MAC addresses."}


def appliance_scan():
    config = read_json(ADMIN_DIR / 'proxmox-secret.json')
    inventory = read_json(ADMIN_DIR / 'proxmox-inventory.json', {})
    warning = None
    if config:
        try:
            # Recheck the management NIC/subnet and the pinned certificate on every scan.
            inventory = collect(validate_config(config, management()))
            atomic_json(ADMIN_DIR / 'proxmox-inventory.json', inventory)
            atomic_json(ADMIN_DIR / 'proxmox-status.json', {'connected': True, 'host': config['host'],
                'token_id': config['token_id'], 'fingerprint': config['fingerprint'], 'vm_count': inventory['vm_count'], 'warnings': inventory['warnings']})
        except ValueError as exc:
            warning = str(exc)
            inventory = {}  # Do not label current devices using stale/failed inventory.
            atomic_json(ADMIN_DIR / 'proxmox-inventory.json', {})
            atomic_json(ADMIN_DIR / 'proxmox-status.json', {'connected': False, 'host': config['host'], 'error': warning})
    result = discover(management(), scan=True)
    result['candidates'] = enrich_candidates(result['candidates'], result['interfaces'], inventory, probe=True)
    result['scanned_at'] = time.time()
    result['message'] = f"Found {len(result['candidates'])} LAN candidates. Product hints require confirmation; verify the gateway before routing."
    if warning: result['warning'] = warning
    atomic_json(ADMIN_DIR / 'discovery.json', result)
    return result


def inventory_disconnect():
    for name in ('proxmox-secret.json', 'proxmox-status.json', 'proxmox-inventory.json', 'discovery.json'):
        (ADMIN_DIR / name).unlink(missing_ok=True)
    return {'message': 'Proxmox credentials and cached identity data removed.'}

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
        if not isinstance(payload, dict):
            raise ValueError('Administration payload must be an object.')
        if TARGET_ROLE and job['action'] not in ('check_update', 'install_update'):
            raise ValueError('Target worker accepts only release checks and installation.')
        if job['action'] == 'check_update':
            result = check_release()
        elif job['action'] == 'install_update':
            result = install_release(str(payload.get('tag', '')))
        elif job['action'] == 'scan':
            result = appliance_scan()
        elif job['action'] == 'route':
            result = apply_route(payload)
        elif job['action'] == 'configure_inventory':
            result = inventory_configure(payload)
        elif job['action'] == 'disconnect_inventory':
            result = inventory_disconnect()
        elif job['action'] == 'configure_interface':
            result = configure_interface(payload)
        elif job['action'] == 'forget_interface':
            result = forget_interface(payload)
        elif job['action'] == 'clear_route':
            result = clear_route()
        elif job['action'] in ('configure_target', 'disconnect_target', 'target_status', 'target_check', 'target_install'):
            from .remote_target import perform
            result = perform(job['action'], payload, ADMIN_DIR, management())
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
            restore_network()
        else:
            process_request()


if __name__ == '__main__':
    main()
