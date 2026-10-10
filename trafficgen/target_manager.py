"""HTTPS management surface for the controlled target; updates run in a root worker."""
from __future__ import annotations

import hmac
import ipaddress
import os
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request

from . import __version__, maintenance

CONFIG_DIR = Path('/etc/netem-traffic-target-manager')
RUNTIME_DIR = Path('/var/lib/netem-traffic-target-manager')
ADMIN_DIR = Path('/var/lib/netem-traffic-target-admin')


def create_app(config_dir=CONFIG_DIR, runtime_dir=RUNTIME_DIR, admin_dir=ADMIN_DIR):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 4096
    settings = SimpleNamespace(runtime_dir=runtime_dir)

    @app.before_request
    def authenticate():
        try:
            expected = (config_dir / 'api.key').read_text().strip()
        except OSError:
            expected = ''
        authorization = request.headers.get('Authorization', '')
        supplied = authorization[7:] if authorization.startswith('Bearer ') else ''
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return jsonify(error='Valid target management API key required.'), 401

    @app.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/api/v1/status')
    def status():
        state = maintenance.status(settings, admin_dir)
        return jsonify(service='netem-traffic-target-manager', version=__version__,
                       ready=state['ready'], busy=state['busy'],
                       release=state['release'], job=state['job'])

    def queue(action):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error='JSON object required.'), 400
        allowed = {'tag'} if action == 'install_update' else set()
        if set(payload) != allowed:
            return jsonify(error='Unsupported update parameters.'), 400
        try:
            if action == 'install_update':
                maintenance.version_tuple(str(payload['tag']))
                release = maintenance.release_status(admin_dir)
                if not release.get('available') or payload['tag'] != release.get('tag'):
                    raise ValueError('Check for a newer target release before installing.')
            job = maintenance.enqueue(settings, action, payload, admin_dir)
        except (ValueError, OSError):
            return jsonify(error='Target not ready, busy, or release changed. Refresh status and check again.'), 409
        return jsonify(job_id=job['id']), 202

    @app.post('/api/v1/check')
    def check():
        return queue('check_update')

    @app.post('/api/v1/update')
    def update():
        return queue('install_update')

    return app


def main():
    from werkzeug.serving import make_server

    host = str(ipaddress.IPv4Address(os.environ.get('NETEM_TARGET_MANAGER_HOST', '127.0.0.1')))
    if ipaddress.IPv4Address(host).is_unspecified or ipaddress.IPv4Address(host).is_multicast:
        raise SystemExit('Bind target management to a specific management IPv4 address.')
    app = create_app()
    server = make_server(host, 8091, app, threaded=True,
                         ssl_context=(str(CONFIG_DIR / 'tls.crt'), str(CONFIG_DIR / 'tls.key')))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
