"""Private presentation mirrored from the authenticated NetEm controller."""
import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, abort, jsonify, request, url_for
from .auth import bearer_required
from .config import _write_secret

TOKENS = {'bg', 'bg-deep', 'surface', 'surface-2', 'surface-3', 'surface-hover', 'border', 'border-strong',
          'text', 'text-soft', 'muted', 'muted-2', 'accent', 'accent-strong', 'blue', 'warning', 'danger', 'success'}
MAGIC = {'.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff', '.webp': b'RIFF',
         '.ico': b'\x00\x00\x01\x00', '.woff': b'wOFF', '.woff2': b'wOF2'}


def remote_url(value):
    if not isinstance(value, str) or len(value) > 2048 or not re.fullmatch(r'[A-Za-z0-9:/?&=._~%+#@!$()*;,\[\]-]+', value):
        raise ValueError('Invalid HTTPS asset URL.')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid HTTPS asset URL.')
    return value


def validate(raw):
    if not isinstance(raw, dict) or raw.get('schema_version') != 1 or set(raw) != {'schema_version', 'branding', 'assets'}:
        raise ValueError('Provide a version 1 branding bundle.')
    brand, assets = raw['branding'], raw['assets']
    if not isinstance(brand, dict) or not isinstance(assets, dict) or len(assets) > 16:
        raise ValueError('Invalid brand or assets.')
    if set(brand) - {'name', 'subtitle', 'mark', 'logo', 'favicon', 'tokens', 'font_family', 'fonts', 'heading_font_family'}:
        raise ValueError('Unknown branding fields.')
    for key in ('name', 'subtitle', 'mark'):
        if key in brand and (not isinstance(brand[key], str) or not brand[key].strip() or len(brand[key]) > 120):
            raise ValueError('Invalid branding text.')
    tokens = brand.get('tokens', {})
    if not isinstance(tokens, dict) or any(key not in TOKENS or not isinstance(value, str) or not re.fullmatch('#[0-9a-fA-F]{6}', value) for key, value in tokens.items()):
        raise ValueError('Invalid branding colours.')
    for key in ('font_family', 'heading_font_family'):
        if key in brand and (not isinstance(brand[key], str) or not re.fullmatch('[A-Za-z][A-Za-z0-9 -]{0,63}', brand[key])):
            raise ValueError('Invalid font family.')
    referenced = set()
    def reference(filename, suffixes):
        if isinstance(filename, str) and filename.startswith('https://'):
            remote_url(filename)
            return
        if not isinstance(filename, str) or not re.fullmatch('[A-Za-z0-9_./-]+', filename) or '..' in Path(filename).parts or filename.startswith('/') or Path(filename).suffix.lower() not in suffixes:
            raise ValueError('Invalid asset reference.')
        referenced.add(filename)
    for key in ('logo', 'favicon'):
        if brand.get(key):
            reference(brand[key], {'.png', '.svg', '.jpg', '.jpeg', '.webp', '.ico'})
    fonts = brand.get('fonts', [])
    if not isinstance(fonts, list) or len(fonts) > 12:
        raise ValueError('Invalid fonts.')
    for font in fonts:
        if not isinstance(font, dict) or type(font.get('weight', 400)) is not int or not 100 <= font.get('weight', 400) <= 900 or font.get('family', 'body') not in ('body', 'heading'):
            raise ValueError('Invalid font weight or role.')
        reference(font.get('file'), {'.woff', '.woff2'})
    if set(assets) != referenced:
        raise ValueError('Embed exactly the referenced local assets.')
    total = 0
    for filename, encoded in assets.items():
        if not isinstance(encoded, str) or len(encoded) > 2 * 1024 * 1024 * 4 // 3 + 8:
            raise ValueError('Asset too large.')
        data = base64.b64decode(encoded, validate=True)
        total += len(data)
        suffix = Path(filename).suffix.lower()
        if not data or len(data) > 2 * 1024 * 1024 or total > 8 * 1024 * 1024:
            raise ValueError('Asset too large.')
        if suffix == '.svg':
            if b'<svg' not in data[:4096].lower() or re.search(rb'<script|<foreignobject|<iframe|<embed|<object|\son[a-z]+\s*=|javascript:|(?:xlink:)?href\s*=\s*["\']\s*(?!#)', data, re.I):
                raise ValueError('Unsafe SVG.')
        elif not data.startswith(MAGIC[suffix]) or (suffix == '.webp' and data[8:12] != b'WEBP'):
            raise ValueError('Invalid asset format.')
    return raw


def install(app, settings):
    path = settings.runtime_dir / 'controller-branding.json'
    cache = {'stamp': None, 'value': None}
    def bundle():
        if not path.exists():
            return None
        stamp = path.stat().st_mtime_ns
        if cache['stamp'] != stamp:
            try:
                value = validate(json.loads(path.read_text(encoding='utf-8')))
            except (ValueError, TypeError, OSError):
                value = None
            cache.update(stamp=stamp, value=value)
        return cache['value']
    def revision(raw):
        return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest() if raw else None
    def asset_url(filename):
        return filename if filename.startswith('https://') else url_for('controller_brand_asset', filename=filename)
    @app.context_processor
    def brand_context():
        raw = bundle()
        return {'branding': raw['branding'] if raw else {}, 'branding_enabled': bool(raw), 'branding_asset_url': asset_url}
    @app.get('/api/v1/branding')
    @bearer_required
    def controller_brand_status():
        return jsonify(revision=revision(bundle()))
    @app.put('/api/v1/branding')
    @bearer_required
    def controller_brand_save():
        try:
            raw = validate(request.get_json(silent=True))
            _write_secret(path, json.dumps(raw))
            return jsonify(revision=revision(raw), message='Controller brand applied.')
        except (ValueError, TypeError, OSError) as exc:
            return {'error': str(exc)}, 400
    @app.get('/branding/status')
    def controller_brand_live_status():
        raw = bundle()
        brand = raw['branding'] if raw else {}
        return jsonify(revision=revision(raw), enabled=bool(raw), name=brand.get('name', 'Traffic execution agent'),
                       subtitle=brand.get('subtitle', 'Read-only execution monitor'), mark=brand.get('mark', 'NT'),
                       logo=asset_url(brand['logo']) if brand.get('logo') else None,
                       favicon=asset_url(brand['favicon']) if brand.get('favicon') else None)
    @app.get('/branding/assets/<path:filename>')
    def controller_brand_asset(filename):
        raw = bundle()
        if not raw or filename not in raw['assets']:
            abort(404)
        suffix = Path(filename).suffix.lower()
        response = Response(base64.b64decode(raw['assets'][filename]), mimetype={'.svg':'image/svg+xml', '.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp', '.ico':'image/x-icon', '.woff':'font/woff', '.woff2':'font/woff2'}[suffix])
        response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
        return response
    @app.get('/branding/theme.css')
    def controller_brand_stylesheet():
        raw = bundle()
        if not raw:
            abort(404)
        brand = raw['branding']
        tokens = brand.get('tokens', {})
        aliases = {'panel':'surface', 'panel2':'surface-2', 'line':'border', 'muted2':'muted-2', 'warn':'warning'}
        declarations = [f'--{key}:{value}' for key, value in tokens.items()]
        declarations += [f'--{key}:var(--{value})' for key, value in aliases.items() if value in tokens]
        family = brand.get('font_family', 'Arial')
        heading = brand.get('heading_font_family', family)
        css = ':root{' + ';'.join(declarations) + '}'
        for font in brand.get('fonts', []):
            font_family = heading if font.get('family') == 'heading' else family
            css += f'@font-face{{font-family:"{font_family}";src:url("{asset_url(font["file"])}");font-weight:{font.get("weight",400)};font-display:swap;}}'
        css += f'html,body{{font-family:"{family}",Arial,sans-serif}}h1,h2,h3{{font-family:"{heading}",Arial,sans-serif}}'
        css += '.sidebar,.login-body{background:var(--bg-deep,var(--bg))}.card,.login-card{background:var(--panel)}.topbar{background:var(--bg)}.btn{background:var(--panel2);border-color:var(--line);color:var(--text)}.btn.primary{background:var(--accent-strong,var(--accent));color:#fff}.nav-link{color:var(--text-soft,var(--text))}.nav-link.active{background:var(--panel2);border-color:var(--line)}'
        css += '.pill.good,.status.good{color:var(--success,var(--accent))}.pill,.status,.crumb{color:var(--text-soft,var(--text))}.nav-link:hover,.btn:hover{background:var(--surface-hover,var(--panel2));color:var(--text)}'
        return Response(css, mimetype='text/css')
