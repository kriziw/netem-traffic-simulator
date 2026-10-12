"""Fixed operator screens delegated to the authenticated NetEm controller.

Local browser sessions observe execution. Only the bearer-authenticated controller
can render and submit the operator screens, mounted below /simulator in NetEm.
"""
from flask import g, request, session
from .auth import bearer_required
from .config import ensure_api_key

GET_PATHS = {
    '/', '/workloads', '/dem', '/ui/status', '/dem/data', '/traffic-paths',
    '/settings/network-setup', '/settings/network-setup/status',
    '/settings/system', '/settings/system/data', '/settings/api',
    '/static/app.css', '/static/app.js',
}
POST_PATHS = {
    '/workloads/start', '/workloads/adjust', '/workloads/stop', '/experience/settings',
    '/traffic-paths/action', '/settings/network-setup', '/settings/network-setup/dhcp',
    '/settings/network-setup/probe', '/settings/system/action', '/settings/api/rotate',
}


def install(app):
    @app.post('/api/v1/controller/view')
    @bearer_required
    def controller_view():
        raw = request.get_json(silent=True) or {}
        if not isinstance(raw, dict):
            return {'error': 'Controller request must be an object.'}, 400
        method, path = raw.get('method', 'GET'), raw.get('path', '/')
        if not isinstance(method, str) or not isinstance(path, str):
            return {'error': 'Controller screen and method must be text.'}, 400
        messages = raw.get('messages', [])
        if (not isinstance(raw.get('csrf_token', ''), str) or not isinstance(messages, list)
                or any(not isinstance(item, (list, tuple)) or len(item) != 2
                       or any(not isinstance(value, str) for value in item) for item in messages)):
            return {'error': 'Invalid controller form context.'}, 400
        allowed = GET_PATHS if method == 'GET' else POST_PATHS if method == 'POST' else set()
        if path not in allowed:
            return {'error': 'Unsupported controller screen or action.'}, 400
        fields, query = raw.get('fields', {}), raw.get('query', {})
        if any(not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in values.items()) for values in (fields, query)):
            return {'error': 'Controller fields must be text values.'}, 400
        auth = request.headers.get('Authorization', '')
        source_origin = request.host_url
        # A new internal request preserves existing field validation, routing
        # locks and view behavior. No arbitrary URL, file or callable is accepted.
        try:
            with app.test_request_context(path, base_url=source_origin, method=method, data=fields if method == 'POST' else None,
                                          query_string=query, headers={'Authorization': auth},
                                          environ_overrides={'SCRIPT_NAME': '/simulator'}):
                g.controller_ui = True
                session['trafficgen_admin'] = True
                session['csrf_token'] = raw.get('csrf_token', '')
                session['_flashes'] = raw.get('messages', [])
                response = app.full_dispatch_request()
                response.direct_passthrough = False
                result = {'status': response.status_code, 'body': response.get_data(as_text=True),
                          'content_type': response.content_type, 'location': response.headers.get('Location'),
                          'messages': session.get('_flashes', [])}
                if path == '/settings/api/rotate' and method == 'POST' and response.status_code < 400:
                    result['api_key'] = ensure_api_key(app.config['TRAFFICGEN_SETTINGS'])
                response.close()
                return result
        finally:
            g.controller_ui = False
