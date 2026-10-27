#!/usr/bin/env python3
"""Static guardrails for the MV3 extension and the public zip."""
import json
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get('XCOLLECT_EXTENSION_DIR', str(Path(__file__).resolve().parents[2] / 'extension')))
ZIP = Path(os.environ.get('EXTENSION_ZIP_OUT', '/var/www/x-publish-board/xcollect/extension.zip'))
TOKEN_PATH = Path(os.environ.get('XCOLLECT_TOKEN_FILE', '/opt/xcollect/collect-token.txt'))
TOKEN = TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else ''

m = json.loads((ROOT / 'manifest.json').read_text())
assert m['manifest_version'] == 3
assert set(m['permissions']) == {'storage', 'alarms'}, m['permissions']
assert set(m['host_permissions']) == {
    'https://x.com/*', 'https://twitter.com/*', 'https://YOUR_DOMAIN/*'
}, m['host_permissions']
assert m['background']['service_worker'] == 'background.js'
assert m['content_scripts'][0]['matches'] == ['https://x.com/*', 'https://twitter.com/*']
for key_obj in (m['action']['default_icon'], m['icons']):
    for size, rel in key_obj.items():
        p = ROOT / rel
        assert p.exists() and p.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n', p
for rel in ('background.js', 'content.js', 'popup.js', 'popup.html', 'README.md'):
    assert (ROOT / rel).exists(), rel

js = '\n'.join((ROOT / f).read_text() for f in ('background.js', 'content.js', 'popup.js'))
for forbidden in ('webRequest', 'declarativeNetRequest', 'chrome.cookies', 'document.cookie',
                  'XMLHttpRequest', 'eval(', 'new Function(', 'innerHTML ='):
    assert forbidden not in js, forbidden
# content script must not POST directly; token stays in background
content = (ROOT / 'content.js').read_text()
assert 'fetch(' not in content and 'collectToken' not in content
assert 'https://YOUR_DOMAIN/collect-api/ingest' in (ROOT / 'background.js').read_text()

with zipfile.ZipFile(ZIP) as z:
    names = z.namelist()
    assert 'extension/manifest.json' in names
    assert not any('token' in n.lower() for n in names)
    if TOKEN:
        for n in names:
            assert TOKEN.encode() not in z.read(n), f'token leaked in {n}'

if TOKEN:
    for p in ROOT.rglob('*'):
        if p.is_file():
            assert TOKEN.encode() not in p.read_bytes(), f'token leaked in {p}'

print('validate_extension: PASS (MV3, permissions, no forbidden APIs, zip clean, token absent)')
