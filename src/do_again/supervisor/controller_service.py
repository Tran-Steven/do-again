"""Authenticated entrypoint for the separately installed controller backend.

No backend is installed by this source candidate. It must be constructed from
sealed configuration by the approved installer/immutable runtime integration.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

from .macos_execution import ExecutionBlocked
from .jobpipe_development import require_broker_jobpipe_development
from ..service.offline_continuation import validate_scope


def controller_service(broker, packet):
    operations = {'controller_start', 'controller_stop', 'controller_status', 'controller_tick'}
    if (not isinstance(packet, dict) or set(packet) != {'operation'}
            or packet['operation'] not in operations):
        raise ExecutionBlocked('controller cannot accept caller-selected goals, requests or grants')
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('controller requires immutable root broker')
    from .macos_server import verify_installation
    with broker.lock:
        with broker.admission():
            verify_installation(broker.config)
            require_broker_jobpipe_development(broker)
            status = broker.registry.status(broker.project.repo)
            scope = broker.config.get('controller_scope')
            binding = {'repository': 'Tran-Steven/jobpipe', 'branch': 'operator-control',
                'goal_sha256': broker.config.get('controller_goal_sha256'),
                'source_sha': broker.config['source_sha'], 'epoch': status['epoch']}
            now = datetime.now(timezone.utc)
            validate_scope(scope, binding=binding, now=now)
            backend = getattr(broker, 'controller_backend', None)
            if backend is None or backend.controller.scope != scope:
                raise ExecutionBlocked('sealed controller backend has not been installed and qualified')
            operation = packet['operation']
            if operation == 'controller_tick': return backend.tick(now)
            return getattr(backend.controller, operation.split('_', 1)[1])(now)
