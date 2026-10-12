"""Explicit operator-side CDP driver for a separately authorized planner.

This module is not installed in the privileged V1A controller. It requires an
operator-authenticated bridge that attests the browser profile and durably
records dispatch before a single Send gesture. No call may be replayed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..supervisor.macos_execution import ExecutionBlocked
from . import runtime


class ChatGPTPlannerDriver:
    def __init__(self, *, repo: Path, profile_attestor, record_dispatch):
        self.repo = repo
        self.profile_attestor = profile_attestor
        self.record_dispatch = record_dispatch

    def send_once(self, message, *, conversation_id, call_id, profile_sha256):
        if os.name != 'posix' or os.geteuid() == 0:
            raise ExecutionBlocked('browser planner requires non-root operator session')
        if not callable(self.profile_attestor) or not callable(self.record_dispatch):
            raise ExecutionBlocked('browser planner needs profile attestation and durable dispatch journal')
        if self.profile_attestor() != profile_sha256:
            raise ExecutionBlocked('browser profile identity changed')
        record = runtime.project_record(self.repo)
        bound = str(record.get('chat_url') or '')
        if (not bound.startswith('https://chatgpt.com/c/')
                or runtime._chat_id(bound) != conversation_id):
            raise ExecutionBlocked('project browser conversation does not match planner grant')
        status = runtime.ensure_browser_running(verify_auth=True)
        target = runtime._find_chatgpt_target(int(status['port']), bound)
        if target is None:
            raise ExecutionBlocked('bound ChatGPT tab absent; no new tab or chat created')
        target, _ = runtime.wait_for_authenticated(
            int(status['port']), chat_url=bound, target=target, timeout=30.0)
        if runtime._chat_id(target.url) != conversation_id:
            raise ExecutionBlocked('authenticated browser target changed conversation')

        def before_dispatch():
            if self.profile_attestor() != profile_sha256:
                raise ExecutionBlocked('browser profile changed immediately before dispatch')
            if runtime._chat_id(target.url) != conversation_id:
                raise ExecutionBlocked('browser target changed immediately before dispatch')
            self.record_dispatch({
                'call_id': call_id, 'conversation_id': conversation_id,
                'profile_sha256': profile_sha256,
                'message_sha256': hashlib.sha256(message.encode()).hexdigest(),
                'state': 'dispatch_started',
            })

        # send_message locks the browser Send gesture and fails uncertain after
        # its durable dispatch boundary. Never retry from this method.
        result = runtime.send_message(
            target, message, timeout=120.0, wait_for_response=True,
            before_dispatch=before_dispatch, background_focus=True)
        if (not isinstance(result, dict)
                or runtime._chat_id(str(result.get('chat_url') or '')) != conversation_id):
            raise ExecutionBlocked('browser reply moved to another conversation')
        return {'conversation_id': conversation_id, 'response': result.get('response')}
