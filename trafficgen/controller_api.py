"""Data and commands for native NetEm controls. Never returns operator HTML."""
from types import SimpleNamespace

from flask import jsonify, request

from . import __version__, maintenance, network_setup, traffic_paths, data_dhcp, setup_probe
from .auth import bearer_required
from .config import rotate_api_key
from .experience_policy import load_policy, save_policy
from .network import interfaces, address_table
from .profiles import profile_payload


def install(app, settings, system_snapshot, management_interface, host_available, execute_setup, execute_system):
    @app.get('/api/v1/controller/settings')
    @bearer_required
    def controller_settings():
        try:
            system = system_snapshot()
            inventory = interfaces(management_interface())
            saved = maintenance.read_json(settings.runtime_dir / network_setup.FILE, {})
            paths = maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, [])
            return jsonify(version=__version__, system=system,
                network=dict(management=management_interface(), interfaces=inventory,
                    cards=network_setup.cards(saved, inventory, paths, management_interface()), paths=paths,
                    leases={nic: data_dhcp.lease(maintenance, nic) for nic in maintenance.read_json(maintenance.ADMIN_DIR / data_dhcp.FILE, {})},
                    probe=maintenance.read_json(maintenance.ADMIN_DIR / setup_probe.FILE, {})),
                experience=dict(policy=load_policy(settings.database_path), applications=profile_payload()['applications']))
        except (ValueError, OSError) as exc:
            return {'error': str(exc)}, 409

    @app.post('/api/v1/controller/commands/<command>')
    @bearer_required
    def controller_command(command):
        raw = request.get_json(silent=True)
        if not isinstance(raw, dict):
            return {'error': 'Provide a JSON object.'}, 400
        if command not in ('experience', 'probe') and any(not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()):
            return {'error': 'Command fields must be text values.'}, 400
        try:
            if command == 'experience':
                controller = app.config['TRAFFICGEN_CONTROLLER']
                with controller.lock:
                    host_available(require_idle=True)
                    policy = save_policy(settings.database_path, raw)
                return jsonify(message='Experience targets saved.', policy=policy)
            if command == 'network-setup':
                return jsonify(execute_setup(raw)), 202
            if command == 'system':
                # Configuration remains idle-only; diagnostic checks can run live.
                host_available(require_idle=raw.get('action') not in ('check_update', 'target_status', 'target_check', 'scan'))
                return jsonify(execute_system(raw)), 202
            if command == 'dhcp':
                host_available(require_idle=True)
                interface = raw.get('interface', '')
                data_dhcp.validate_nic(SimpleNamespace(management=management_interface, interfaces=interfaces), interface)
                if raw.get('action') not in ('start', 'stop'):
                    raise ValueError('Choose start or stop.')
                job = maintenance.enqueue(settings, 'data_dhcp' if raw['action'] == 'start' else 'stop_data_dhcp', {'interface': interface})
                return jsonify(message='Addressing task queued.', job=job), 202
            if command == 'probe':
                host_available(require_idle=True)
                setup_probe.validate(raw, address_table(), management_interface())
                return jsonify(message='Network discovery queued.', job=maintenance.enqueue(settings, 'probe_setup', raw)), 202
            if command == 'rotate-key':
                host_available(require_idle=True)
                return jsonify(message='Controller API key rotated.', api_key=rotate_api_key(settings))
            return {'error': 'Unsupported controller command.'}, 400
        except (ValueError, OSError, TypeError) as exc:
            return {'error': str(exc)}, 409
