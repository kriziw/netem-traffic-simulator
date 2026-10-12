# Controller branding API

`GET /api/v1/branding` returns the installed presentation revision. `PUT /api/v1/branding` validates and atomically installs a version 1 bundle. Both require the existing controller Bearer key. A bundle contains schema_version (1), branding (name/subtitle/mark, logo/favicon, tokens, body and optional heading font families, fonts) and assets (referenced local filenames mapped to base64).

The limit is 12 MB per request, 2 MB per embedded asset and 8 MB of decoded assets. Other API requests retain their existing 1 MB limit. Invalid configuration cannot replace the last good presentation. Images/fonts use safe allowed formats; SVG scripts and external document references are rejected. HTTPS asset URLs are browser-loaded references; the server does not download arbitrary URLs.

Public read-only branding/status, branding/theme.css and branding/assets routes serve only the installed presentation and referenced assets. The operator UI remains read-only. NetEm owns configuration, checks the revision every 30 seconds and retries after reconnecting; browser presentation updates every ten seconds without resetting workloads or charts. The last installed presentation survives restart/offline operation.

Font entries have file, weight (100–900), and optional family (body or heading). The heading role uses heading_font_family; body entries use font_family. Arial is the fallback. Uploaded assets are served locally, so fonts/images do not require cross-origin access to NetEm. Use a font host supporting CORS when deliberately using external URLs.
