from __future__ import annotations
import json
import socket
from pathlib import Path
from .authority import project_identity
from .macos_execution import ExecutionBlocked, MAX_PACKET, MAX_OUTPUT, SOCKET_ROOT, peer_uid
from ..core.schema import canonical_json


def broker_request(repo: Path, packet: dict) -> dict:
    """Project is selected by the trusted daemon's socket, never packet content."""
    path = SOCKET_ROOT / f'{project_identity(repo)[:24]}.sock'
    return _request(path, packet)


def operator_request(repo: Path, intent: str) -> dict:
    return _request(SOCKET_ROOT / 'operator.sock', {'operation':'set_intent',
                    'project':project_identity(repo),'intent':intent})


def enroll_github_credential(repo: Path, token: str) -> dict:
    """Trusted operator only; caller must never print or journal the token."""
    return _request(SOCKET_ROOT/'operator.sock',{'operation':'set_github_token',
                    'project':project_identity(repo),'token':token})


def _request(path: Path, packet: dict) -> dict:
    payload = canonical_json(packet) + b'\n'
    if len(payload) > MAX_PACKET:
        raise ExecutionBlocked('request exceeds broker limit')
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(600 if packet == {'operation':'qualify_capabilities'}
                              else float(packet.get('timeout', 60)) + 30)
        try:
            connection.connect(str(path))
            if peer_uid(connection) != 0:
                raise ExecutionBlocked('broker peer is not root')
            connection.sendall(payload)
            data = bytearray()
            while b'\n' not in data:
                part = connection.recv(65536)
                if not part or len(data) + len(part) > 12 * MAX_OUTPUT + 65536:
                    raise ExecutionBlocked('invalid or incomplete broker response')
                data.extend(part)
            response = json.loads(bytes(data).split(b'\n', 1)[0])
        except (OSError, ValueError) as exc:
            raise ExecutionBlocked('supervisor unavailable; execution remains blocked') from exc
    if response.get('ok') is not True:
        raise ExecutionBlocked(response.get('error', 'supervisor refused execution'))
    return response['result']
