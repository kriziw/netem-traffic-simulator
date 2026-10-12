import base64
import copy


def auth(simulator):
    return {'Authorization': 'Bearer ' + simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()}


PACK = {'schema_version': 1, 'branding': {'name': 'Example brand', 'subtitle': 'WAN resilience lab', 'mark': 'BT',
    'logo': 'logo.png', 'tokens': {'accent': '#5514B4', 'surface': '#FFFFFF'}, 'font_family': 'BT Curve',
    'heading_font_family': 'BT Curve Headline', 'fonts': [{'file': 'https://assets.example.com/headline.woff2', 'weight': 400, 'family': 'heading'}]},
    'assets': {'logo.png': base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 32).decode()}}


def test_authenticated_brand_apply_updates_login_monitor_assets_and_theme(simulator):
    client = simulator.test_client()
    assert client.put('/api/v1/branding', json=PACK).status_code == 401
    assert client.put('/api/v1/branding', headers=auth(simulator), json=PACK).status_code == 200
    revision = client.get('/api/v1/branding', headers=auth(simulator)).json['revision']
    assert revision and client.get('/branding/status').json['name'] == 'Example brand'
    assert b'Example brand' in client.get('/login').data
    assert b'/branding/assets/logo.png' in client.get('/login').data
    assert b'--accent:#5514B4' in client.get('/branding/theme.css').data
    assert b'BT Curve Headline' in client.get('/branding/theme.css').data
    assert client.get('/branding/assets/logo.png').status_code == 200
    assert client.get('/branding/assets/../../config/api.key').status_code == 404
    with client.session_transaction() as state:
        state['trafficgen_admin'] = True
    assert b'Example brand' in client.get('/').data


def test_invalid_brand_cannot_replace_last_good_presentation(simulator):
    client = simulator.test_client()
    headers = auth(simulator)
    assert client.put('/api/v1/branding', headers=headers, json=PACK).status_code == 200
    revision = client.get('/api/v1/branding', headers=headers).json['revision']
    for change in ({'logo': 'javascript:alert(1)'}, {'logo': '../secret.png'},
                   {'tokens': {'accent': 'red;body{display:none}'}}, {'font_family': 'bad";css'}):
        raw = copy.deepcopy(PACK)
        raw['branding'].update(change)
        assert client.put('/api/v1/branding', headers=headers, json=raw).status_code == 400
        assert client.get('/api/v1/branding', headers=headers).json['revision'] == revision


def test_remote_images_are_references_and_safe_svg_is_required(simulator):
    client = simulator.test_client()
    raw = copy.deepcopy(PACK)
    raw['branding']['logo'] = 'https://assets.example.com/logo.png'
    raw['assets'] = {}
    assert client.put('/api/v1/branding', headers=auth(simulator), json=raw).status_code == 200
    assert client.get('/branding/status').json['logo'] == raw['branding']['logo']
    raw['branding']['logo'] = 'logo.svg'
    raw['assets'] = {'logo.svg': base64.b64encode(b'<svg><script>alert(1)</script></svg>').decode()}
    assert client.put('/api/v1/branding', headers=auth(simulator), json=raw).status_code == 400
