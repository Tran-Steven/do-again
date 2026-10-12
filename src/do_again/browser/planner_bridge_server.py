"""Operator-side handler for one already authorized browser planning request.

It never listens automatically and cannot start a browser on import. A future
approved launchd service must create the private Unix socket and construct the
handler with a sealed source/epoch grant and durable reservation callback.
"""
from __future__ import annotations

import hashlib
import os

from ..core.schema import canonical_json
from ..service.browser_bridge import (
    CALL_ID, authenticated_peer_uid, read_frame, write_frame,
)
from ..supervisor.macos_execution import ExecutionBlocked


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
        if (set(request) != {'call_id', 'conversation_id', 'profile_sha256', 'message'}
                or not isinstance(request['call_id'], str)
                or not CALL_ID.fullmatch(request['call_id'])
                or request['conversation_id'] != self.grant['conversation_id']
                or request['profile_sha256'] != self.grant['profile_sha256']
                or not isinstance(request['message'], str)
                or len(request['message'].encode()) > self.grant['max_prompt_bytes'] + 2048):
            raise ExecutionBlocked('browser bridge request differs from approved scope')
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
                 'response': result['response']}
        write_frame(connection, reply)
        return identity
