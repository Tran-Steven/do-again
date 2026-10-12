"""Bound local Unix-socket transport for an explicitly authorized planner call.

No browser session is launched here. A sealed root broker connects to a
separately owned operator service. Both sides must authenticate the Unix
socket peer; protocol messages are bounded and carry reserved call identity.
"""
from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
from pathlib import Path

from ..core.schema import canonical_json
from ..supervisor.macos_execution import ExecutionBlocked

CALL_ID = re.compile(r'planner-[0-9a-f]{20}-[123]\\Z')
MAX_FRAME = 100000


def read_frame(connection):
    buffer = bytearray()
    while b'\\n' not in buffer:
        chunk = connection.recv(min(8192, MAX_FRAME + 1 - len(buffer)))
        if not chunk:
            raise ExecutionBlocked('browser bridge closed before a complete frame')
        buffer.extend(chunk)
        if len(buffer) > MAX_FRAME:
            raise ExecutionBlocked('browser bridge frame exceeds bound')
    raw, suffix = bytes(buffer).split(b'\\n', 1)
    if suffix:
        raise ExecutionBlocked('browser bridge sent more than one frame')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionBlocked('browser bridge frame is not valid JSON') from exc
    if not isinstance(value, dict):
        raise ExecutionBlocked('browser bridge frame must be an object')
    return value


def write_frame(connection, value):
    data = canonical_json(value) + b'\\n'
    if len(data) > MAX_FRAME:
        raise ExecutionBlocked('browser bridge output exceeds bound')
    connection.sendall(data)


def authenticated_peer_uid(connection):
    if sys.platform != 'darwin' or not hasattr(connection, 'getpeereid'):
        raise ExecutionBlocked('browser bridge needs macOS kernel peer credentials')
    uid, _ = connection.getpeereid()
    return uid


class BrokerBrowserBridge:
    """Root-to-operator bridge; a failed/uncertain request must not be retried."""

    def __init__(self, socket_path, *, expected_uid=501):
        self.socket_path = Path(socket_path)
        if expected_uid != 501:
            raise ExecutionBlocked('browser operator UID must equal approved identity')
        self.expected_uid = expected_uid

    def send_once(self, message, *, conversation_id, call_id, profile_sha256):
        path = self.socket_path
        if not path.is_absolute() or path.is_symlink():
            raise ExecutionBlocked('browser bridge socket must be absolute and unaliased')
        try:
            info = path.lstat()
        except OSError as exc:
            raise ExecutionBlocked('approved browser bridge socket unavailable') from exc
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.expected_uid
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ExecutionBlocked('browser bridge socket ownership or mode differs')
        if not isinstance(message, str) or not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise ExecutionBlocked('browser bridge requires a reserved planner call')
        request = {'call_id': call_id, 'conversation_id': conversation_id,
                   'profile_sha256': profile_sha256, 'message': message}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(120.0)
            client.connect(str(path))
            if authenticated_peer_uid(client) != self.expected_uid:
                raise ExecutionBlocked('browser bridge peer is not the approved operator')
            write_frame(client, request)
            reply = read_frame(client)
        if (set(reply) != {'call_id', 'conversation_id', 'response'}
                or reply['call_id'] != call_id
                or reply['conversation_id'] != conversation_id
                or not isinstance(reply['response'], str)):
            raise ExecutionBlocked('browser bridge result differs from reserved call')
        return {'conversation_id': conversation_id, 'response': reply['response']}
