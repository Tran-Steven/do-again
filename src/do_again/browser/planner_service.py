"""Opt-in operator-owned ChatGPT planner bridge service.

Not launched automatically by import, pip install, or supervisor upgrade.
Requires an independently approved private browser grant, bound existing
project chat and operator-owned browser profile. Only root can make requests.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys

from ..core.schema import canonical_json
from ..service.browser_planner import validate_browser_planner_grant
from ..supervisor.macos_execution import ExecutionBlocked
from . import runtime
from .planner_driver import ChatGPTPlannerDriver
from .planner_bridge_server import OperatorBrowserBridge, DurableDeliveryReservation, DurableReplyJournal


def private_operator_directory(directory, *, create=False):
    directory = Path(directory)
    if directory.is_symlink():
        raise ExecutionBlocked('operator bridge directory is aliased')
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 501
            or info.st_mode & 0o077):
        raise ExecutionBlocked('operator bridge directory is not private')
    return directory


def load_private_browser_grant(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ExecutionBlocked('operator browser grant path is not absolute or unaliased')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 501
            or info.st_nlink != 1 or info.st_mode & 0o077
            or info.st_size > 32768):
        raise ExecutionBlocked('browser grant is not a private operator-owned regular file')
    try:
        grant = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ExecutionBlocked('browser grant cannot be decoded') from exc
    validate_browser_planner_grant(
        grant, source_sha=grant.get('source_sha') if isinstance(grant,dict) else None,
        goal_sha256=grant.get('goal_sha256') if isinstance(grant,dict) else None,
        epoch=grant.get('epoch') if isinstance(grant,dict) else None,
        now=datetime.now(timezone.utc))
    return grant


def profile_fingerprint():
    config = runtime.load_config()
    path = Path(str(config.get('profile_dir') or ''))
    if not path.is_absolute() or path.is_symlink():
        raise ExecutionBlocked('ChatGPT browser profile path is not absolute or unaliased')
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 501
            or info.st_mode & 0o077):
        raise ExecutionBlocked('ChatGPT browser profile ownership or privacy differs')
    identity = {'path':str(path), 'dev':info.st_dev,
                'inode':info.st_ino, 'uid':info.st_uid}
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def serve_forever(*, repo, grant_path):
    if sys.platform != 'darwin' or os.geteuid() != 501:
        raise ExecutionBlocked('browser planner service requires a macOS operator login')
    grant = load_private_browser_grant(grant_path)
    if profile_fingerprint() != grant['profile_sha256']:
        raise ExecutionBlocked('browser grant does not identify the live operator profile')
    record = runtime.project_record(Path(repo).resolve())
    url = str(record.get('chat_url') or '')
    if runtime._chat_id(url) != grant['conversation_id']:
        raise ExecutionBlocked('operator browser project chat differs from sealed grant')
    root = private_operator_directory(runtime.browser_paths().root)
    delivery = private_operator_directory(root / 'planner-delivery', create=True)
    dispatched = private_operator_directory(root / 'planner-dispatch', create=True)
    replies = private_operator_directory(root / 'planner-replies', create=True)
    driver = ChatGPTPlannerDriver(
        repo=Path(repo).resolve(), profile_attestor=profile_fingerprint,
        record_dispatch=DurableDeliveryReservation(dispatched))
    handler = OperatorBrowserBridge(
        grant=grant, driver=driver,
        reserve_delivery=DurableDeliveryReservation(delivery), replies=DurableReplyJournal(replies))
    path = root / 'planner.sock'
    if path.exists() or path.is_symlink():
        raise ExecutionBlocked('existing browser planner socket requires reconciliation; no takeover')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(1)
        original = path.lstat()
        try:
            while True:
                with listener.accept()[0] as connection:
                    # The grant is short-lived. Expired approval ends service.
                    validate_browser_planner_grant(
                        grant, source_sha=grant['source_sha'],
                        goal_sha256=grant['goal_sha256'], epoch=grant['epoch'],
                        now=datetime.now(timezone.utc))
                    try:
                        handler.handle_once(connection)
                    except (ExecutionBlocked, runtime.BrowserError, OSError):
                        # Never reissue a request or click Send after uncertainty.
                        # A bad inbound request is terminal only for that connection.
                        continue
        finally:
            try:
                current = path.lstat()
                if (stat.S_ISSOCK(current.st_mode)
                        and current.st_dev == original.st_dev
                        and current.st_ino == original.st_ino
                        and current.st_uid == 501):
                    path.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the opt-in operator-owned ChatGPT planner bridge')
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--grant', type=Path, required=True)
    args = parser.parse_args(argv)
    serve_forever(repo=args.repo, grant_path=args.grant)


if __name__ == '__main__':
    main()
