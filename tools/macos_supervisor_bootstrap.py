"""Installed immutable entry point; never import a writable checkout."""
from __future__ import annotations
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path('/Library/Application Support/DoAgainSupervisor/current')


def main():
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise SystemExit('root macOS service required')
    manifest = json.loads((ROOT/'manifest.json').read_text())
    for relative,digest in manifest['files'].items():
        p=ROOT/relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or p.is_symlink():
            raise SystemExit('invalid installed manifest')
        for ancestor in (p,*p.parents):
            info=ancestor.stat()
            if ancestor.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:
                raise SystemExit('mutable installed supervisor')
        if not p.is_file() or p.stat().st_nlink!=1 or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise SystemExit('installed supervisor drift')
    sys.path.insert(0,str(ROOT/'package'))
    from do_again.supervisor.macos_server import main as serve
    serve()


if __name__=='__main__':main()
