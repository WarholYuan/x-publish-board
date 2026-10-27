#!/usr/bin/env python3
"""Package /opt/xcollect/extension into the public zip (no token)."""
import os
import zipfile
from pathlib import Path

SRC = Path(os.environ.get('XCOLLECT_EXTENSION_DIR', '/opt/xcollect/extension'))
OUT = Path(os.environ.get('EXTENSION_ZIP_OUT', '/var/www/x-publish-board/xcollect/extension.zip'))


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix('.zip.tmp')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(SRC):
            dirs.sort()
            for name in sorted(files):
                full = Path(root) / name
                arc = full.relative_to(SRC.parent).as_posix()
                info = zipfile.ZipInfo.from_file(full, arc)
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, full.read_bytes())
    os.chmod(tmp, 0o644)
    tmp.replace(OUT)
    print(f'wrote {OUT} ({OUT.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
