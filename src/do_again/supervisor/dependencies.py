"""Exact approved artifact retrieval; installation always runs as the confined UID."""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ..core.schema import canonical_json
from .macos_execution import EXECUTION_ROOT, INSTALL_ROOT, ExecutionBlocked, MacOSProcesses, capture, launch_spec
from .network import https_bytes
from .wheels import MAX_WHEEL, validate_wheel


def approved_artifact(config: dict, key: str, identifier: str) -> dict:
    if not isinstance(identifier, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,79}', identifier):
        raise ExecutionBlocked('invalid dependency identity')
    entries = config.get('dependency_artifacts', {}).get(key, [])
    matches = [item for item in entries if item.get('id') == identifier]
    if len(matches) != 1:
        raise ExecutionBlocked('dependency is not uniquely approved for this project')
    item = matches[0]
    if (set(item) != {'id','name','version','url','sha256'}
            or not all(isinstance(v, str) and v for v in item.values())
            or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
            or not item['url'].startswith('https://files.pythonhosted.org/packages/')
            or not item['url'].endswith('.whl')):
        raise ExecutionBlocked('dependency approval is incomplete')
    parsed = urlsplit(item['url'])
    if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ExecutionBlocked('dependency origin is not admitted')
    return dict(item)


def install_via_broker(broker, packet: dict) -> dict:
    from .git_broker import validate_packet
    from .macos_server import verify_installation, worktree_authority
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('dependency capability requires the immutable macOS broker')
    if set(packet) != {'operation','request_id','expected_head','expected_epoch','artifact_id','request_fingerprint'}:
        raise ExecutionBlocked('dependency requests cannot select origins, arguments or administrative authority')
    # Reuse the strict identity/epoch validator, without exposing a Git operation.
    validate_packet({k:v for k,v in packet.items() if k not in {'artifact_id','operation'}} |
                    {'operation':'git_commit','paths':['placeholder'],'message':'dependency'})
    if packet['operation'] != 'dependency_install':
        raise ExecutionBlocked('unknown dependency capability')
    artifact = approved_artifact(broker.config, broker.project.key, packet['artifact_id'])
    fingerprint = hashlib.sha256(canonical_json(packet)).hexdigest()
    with broker.lock:
        def gate():
            status = broker.registry.status(broker.project.repo)
            if (status['intent'] != 'active' or status['epoch'] != packet['expected_epoch']
                    or not status.get('goal_revision') or broker.config.get('production_ready') is not True
                    or not broker._verified()
                    or worktree_authority(broker.project.worktree)['repo_head'] != packet['expected_head']):
                raise ExecutionBlocked('authority, head or native qualification blocks dependency installation')
        verify_installation(broker.config)
        with broker.admission():
            gate()
            recovered = broker.ledger.lookup(broker.project.key, packet['request_id'], fingerprint)
            if recovered is not None:
                return recovered
            if MacOSProcesses().owned(broker.project.uid):
                raise ExecutionBlocked('dedicated identity has unresolved processes')
            broker.ledger.reserve(broker.project.key, packet['request_id'], fingerprint,
                intent={'operation':packet['operation'],'source_sha':broker.config['source_sha'],
                        'request_fingerprint':packet.get('request_fingerprint')})
        root = EXECUTION_ROOT / broker.project.key / 'requests' / packet['request_id']
        scratch, cache = root / 'scratch', root / 'cache'
        for path in (scratch, cache):
            path.mkdir(parents=True, mode=0o700)
            os.chown(path, broker.project.uid, broker.project.gid)
        for path in (root, root.parent):
            path.chmod(0o711)
        try:
            with broker.admission():
                gate()
                status, content = https_bytes(artifact['url'], host='files.pythonhosted.org', limit=MAX_WHEEL)
            if status != 200:
                raise ExecutionBlocked('approved artifact retrieval failed')
            validate_wheel(content, artifact)
            wheel = scratch / 'approved.whl'
            with wheel.open('xb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            wheel.chmod(0o444)
        except Exception:
            result = {'operation':'dependency_install','returncode':1,'state':'failed_pre_install',
                      'error':'approved dependency could not be prepared','source_sha':broker.config['source_sha']}
            broker.ledger.finish(broker.project.key, packet['request_id'], result)
            return result
        destination = cache / 'site-packages'
        # Only this immutable module handles extraction; it runs unprivileged.
        code = ('import sys;sys.path.insert(0,' + repr(str(INSTALL_ROOT / 'current/package')) + ');'
                'from do_again.supervisor.wheels import install_wheel;from pathlib import Path;import json;'
                'install_wheel(Path(sys.argv[1]),Path(sys.argv[2]),json.loads(sys.argv[3]));'
                'print("DEPENDENCY_INSTALLED")')
        argv = [broker.config['python'],'-I','-S','-B','-c',code,str(wheel),str(destination),canonical_json(artifact).decode()]
        from contextlib import contextmanager
        admitted = False
        @contextmanager
        def admission():
            nonlocal admitted
            with broker.admission():
                gate()
                admitted = True
                yield
        # A crash after installation admission keeps the started reservation.
        try:
            spec = launch_spec(broker.project, {'operation':'execute','request_id':packet['request_id'],
                               'argv':argv,'cwd':'.','timeout':60}, scratch, cache)
            outcome = capture(spec, 60, start_guard=admission)
        except Exception:
            if admitted:
                raise  # An installation effect may have happened; never replay.
            result = {'operation':'dependency_install','returncode':1,'state':'failed_pre_install',
                      'error':'dependency installation admission closed','source_sha':broker.config['source_sha']}
            broker.ledger.finish(broker.project.key, packet['request_id'], result)
            return result
        succeeded = outcome['returncode'] == 0 and not outcome['timed_out'] and outcome['stdout'].strip() == 'DEPENDENCY_INSTALLED'
        result = {'operation':'dependency_install','state':'succeeded' if succeeded else 'failed',
                  'returncode':0 if succeeded else 1,'artifact_id':artifact['id'],'sha256':artifact['sha256'],
                  'site_packages':str(destination) if succeeded else None,
                  'execution_uid':broker.project.uid,'source_sha':broker.config['source_sha']}
        broker.ledger.finish(broker.project.key, packet['request_id'], result)
        return result
