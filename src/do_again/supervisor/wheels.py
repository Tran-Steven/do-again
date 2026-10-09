"""Offline, pure-Python wheel validation and confined extraction."""
from __future__ import annotations

import email.parser
import hashlib
import io
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .macos_execution import ExecutionBlocked

MAX_WHEEL = 20 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024


def normalize(name: str) -> str:
    return re.sub('[-_.]+', '-', name).lower()


def validate_wheel(content: bytes, artifact: dict) -> list[str]:
    if len(content) > MAX_WHEEL or hashlib.sha256(content).hexdigest() != artifact['sha256']:
        raise ExecutionBlocked('dependency artifact hash or size does not match approval')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as wheel:
            entries = wheel.infolist()
            if not entries or len(entries) > 4096 or sum(e.file_size for e in entries) > MAX_EXPANDED:
                raise ExecutionBlocked('dependency archive exceeds extraction budget')
            names = []
            for entry in entries:
                parts = entry.filename.split('/')
                if (entry.orig_filename != entry.filename
                        or any(ord(c)<32 for c in entry.orig_filename)
                        or PurePosixPath(entry.filename).is_absolute() or '\\' in entry.filename
                        or any(p in {'', '.', '..'} or ':' in p for p in (parts[:-1] if entry.is_dir() else parts))
                        or '\x00' in entry.filename or entry.flag_bits & 1
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or entry.filename.endswith('.pth')
                        or any(p.endswith('.data') for p in parts)):
                    raise ExecutionBlocked('dependency archive contains an unapproved path or installer effect')
                if entry.filename in names:
                    raise ExecutionBlocked('dependency archive contains duplicate paths')
                names.append(entry.filename)
            metadata = [n for n in names if n.endswith('.dist-info/METADATA')]
            descriptor = [n for n in names if n.endswith('.dist-info/WHEEL')]
            if len(metadata) != 1 or len(descriptor) != 1 or metadata[0].rsplit('/', 1)[0] != descriptor[0].rsplit('/', 1)[0]:
                raise ExecutionBlocked('dependency wheel identity is ambiguous')
            message = email.parser.BytesParser().parsebytes(wheel.read(metadata[0]))
            description = email.parser.BytesParser().parsebytes(wheel.read(descriptor[0]))
            if (normalize(message.get('Name', '')) != normalize(artifact['name'])
                    or message.get('Version') != artifact['version']
                    or description.get('Root-Is-Purelib', '').lower() != 'true'
                    or not description.get_all('Tag')
                    or any(not tag.endswith('-none-any') for tag in description.get_all('Tag'))):
                raise ExecutionBlocked('dependency wheel does not match approved pure-Python identity')
            if wheel.testzip() is not None:
                raise ExecutionBlocked('dependency archive checksum failed')
            return names
    except (zipfile.BadZipFile, KeyError, RuntimeError, ValueError):
        raise ExecutionBlocked('dependency wheel is invalid') from None


def install_wheel(artifact_file: Path, destination: Path, artifact: dict) -> None:
    """Called only inside native confinement as the project execution UID."""
    content = artifact_file.read_bytes()
    names = validate_wheel(content, artifact)
    destination.mkdir(mode=0o700)  # Never overwrite an existing environment.
    with zipfile.ZipFile(io.BytesIO(content)) as wheel:
        for name in names:
            target = destination / name
            if name.endswith('/'):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(wheel.read(name))


if __name__ == '__main__':
    import json
    import sys
    install_wheel(Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3]))
    print('DEPENDENCY_INSTALLED')
