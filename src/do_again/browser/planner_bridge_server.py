"""Operator-side handler for one already authorized browser planning request.

It never listens automatically and cannot start a browser on import. A future
approved launchd service must create the private Unix socket and construct the
handler with a sealed source/epoch grant and durable reservation callback.
"""
from __future__ import annotations

import hashlib
import os

from ..core.schema import canonical_json
from ..service.browser_planner import browser_grant_digest, validate_reserved_browser_call
from ..service.browser_bridge import (
    CALL_ID, authenticated_peer_uid, read_frame, write_frame,
)
from ..supervisor.macos_execution import ExecutionBlocked


import json
import re
import stat
from pathlib import Path

def private_operator_directory(directory_fd):
    metadata = os.fstat(directory_fd)
    return (stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == 501
            and not (metadata.st_mode & 0o077))


class DurableDeliveryReservation:
    """Create-once user-private delivery journal; duplicate calls fail closed."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def __call__(self, identity):
        if (not isinstance(identity, dict)
                or set(identity) not in ({'call_id', 'conversation_id', 'profile_sha256', 'message_sha256'},
                                     {'call_id', 'conversation_id', 'profile_sha256', 'message_sha256', 'state'})
                or ('state' in identity and identity['state'] != 'dispatch_started')
                or not isinstance(identity.get('call_id'), str)
                or not CALL_ID.fullmatch(identity['call_id'])):
            raise ExecutionBlocked('browser delivery reservation identity invalid')
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            directory_fd = os.open(self.directory, flags)
        except OSError as exc:
            raise ExecutionBlocked('private browser delivery directory unavailable') from exc
        try:
            if not private_operator_directory(directory_fd):
                raise ExecutionBlocked('browser delivery journal ownership or privacy differs')
            name = identity['call_id'] + '.json'
            mode = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            try:
                fd = os.open(name, mode, 0o600, dir_fd=directory_fd)
            except OSError as exc:
                raise ExecutionBlocked('browser delivery was already reserved or cannot be recorded') from exc
            try:
                data = canonical_json(identity) + bytes([10])
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

class OperatorBrowserBridge:
    def __init__(self, *, grant, driver, reserve_delivery):
        self.grant = grant
        self.driver = driver
        self.reserve_delivery = reserve_delivery

    def handle_once(self, connection):
        if os.geteuid() != 501:
            raise ExecutionBlocked('browser bridge handler must run under approved operator UID')
        if authenticated_peer_uid(connection) != 0:
            raise ExecutionBlocked('browser bridge request is not from root supervisor')
        request = read_frame(connection)
        if set(request) == {'operation','nonce','grant_sha256'}:
            nonce = request['nonce']
            if (request['operation'] != 'probe' or not isinstance(nonce,str)
                    or not re.fullmatch('[0-9a-f]{32}',nonce)
                    or request['grant_sha256'] != browser_grant_digest(self.grant)):
                raise ExecutionBlocked('browser bridge probe malformed')
            write_frame(connection, {
                'operation':'probe-result', 'nonce':nonce,
                'conversation_id':self.grant['conversation_id'],
                'profile_sha256':self.grant['profile_sha256'],
                'grant_sha256':browser_grant_digest(self.grant)})
            return {'state':'probed','model_calls':0}
        if (set(request) != {'call_id', 'conversation_id', 'profile_sha256', 'grant_sha256', 'message'}
                or not isinstance(request['call_id'], str)
                or not CALL_ID.fullmatch(request['call_id'])
                or request['conversation_id'] != self.grant['conversation_id']
                or request['profile_sha256'] != self.grant['profile_sha256']
                or request['grant_sha256'] != browser_grant_digest(self.grant)
                or not isinstance(request['message'], str)
                or len(request['message'].encode()) > self.grant['max_prompt_bytes'] + 2048):
            raise ExecutionBlocked('browser bridge request differs from approved scope')
        if self.grant.get('kind') == 'browser-planning-persistent-v1':
            validate_reserved_browser_call(request['call_id'], self.grant)
        elif not re.fullmatch(r'planner-[a-f0-9]{20}-[123]', request['call_id']):
            raise ExecutionBlocked('browser call exceeds legacy three-call qualification')
        identity = {
            'call_id': request['call_id'],
            'conversation_id': request['conversation_id'],
            'profile_sha256': request['profile_sha256'],
            'message_sha256': hashlib.sha256(request['message'].encode()).hexdigest(),
        }
        # Exactly one durable reservation for the message before any CDP work.
        # Crash, timeout, or uncertain UI delivery can never trigger replay.
        if not callable(self.reserve_delivery):
            raise ExecutionBlocked('browser bridge has no durable delivery reservation')
        self.reserve_delivery(identity)
        result = self.driver.send_once(
            request['message'], conversation_id=request['conversation_id'],
            call_id=request['call_id'], profile_sha256=request['profile_sha256'])
        if (not isinstance(result, dict)
                or result.get('conversation_id') != request['conversation_id']
                or not isinstance(result.get('response'), str)
                or len(result['response'].encode()) > self.grant['max_answer_bytes']):
            raise ExecutionBlocked('browser driver response changed identity or exceeded limit')
        reply = {'call_id': request['call_id'],
                 'conversation_id': request['conversation_id'],
                 'grant_sha256':browser_grant_digest(self.grant),
                 'response': result['response']}
        write_frame(connection, reply)
        return identity
