"""Root-sealed browser planner helper; unavailable until native bridge qualification.

The helper does not start an operator browser process. It only calls an
already authenticated, operator-owned Unix socket after a durable controller
planner reservation. Missing qualification is fatal, not a Codex fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from ..service.browser_bridge import BrokerBrowserBridge
from ..service.browser_planner import (
    BrowserResponseTransport, browser_grant_digest, validate_browser_planner_grant,
)
from .macos_execution import ExecutionBlocked, private_root_file
from ..service.offline_continuation import controller_artifact, persistent_scope


class SealedBrowserPlannerHelper:
    def __init__(self, config, state, admission):
        self.config, self.state, self.admission = config, state, admission
        grant = config.get('browser_planning_scope')
        self.validate(grant, datetime.now(timezone.utc))
        qualification = Path(state) / controller_artifact(config['controller_scope'], 'browser-planner-qualification.json')
        private_root_file(qualification)
        try:
            proof = json.loads(qualification.read_text())
        except (ValueError, OSError) as exc:
            raise ExecutionBlocked('browser bridge qualification unreadable') from exc
        scope = config['controller_scope']
        if (not isinstance(proof, dict)
                or set(proof) != {'state', 'source_sha', 'goal_sha256',
                                  'epoch', 'grant_sha256', 'operator_uid',
                                  'browser_calls', 'peer_uid'}
                or proof['state'] != 'qualified_operator_bridge'
                or proof['source_sha'] != config['source_sha']
                or proof['goal_sha256'] != scope['goal_sha256']
                or proof['epoch'] != scope['epoch']
                or proof['grant_sha256'] != browser_grant_digest(grant)
                or proof['operator_uid'] != 501
                or proof['peer_uid'] != 501
                or proof['browser_calls'] != 0):
            raise ExecutionBlocked('operator browser bridge lacks source-bound native qualification')
        self.socket_path = Path(config['operator_home']) / '.do_again/browser/planner.sock'

    def validate(self, grant, now):
        scope = self.config['controller_scope']
        return validate_browser_planner_grant(
            grant, source_sha=self.config['source_sha'],
            goal_sha256=scope['goal_sha256'], epoch=scope['epoch'], now=now)

    def authorize(self, now):
        with self.admission():
            return self.validate(self.config.get('browser_planning_scope'), now)

    def transport(self, prompt, schema, *, call, grant):
        with self.admission():
            self.validate(grant, datetime.now(timezone.utc))
        client = BrokerBrowserBridge(self.socket_path)
        return BrowserResponseTransport(client.send_once, client.observe_once)(
            prompt, schema, call=call, grant=grant)


def _audited_epoch_13_to_15(broker, previous, scope, previous_source):
    """Require original root-private quarantine evidence for the known prior
    browser proof. Only epoch 15 or the exact subsequent maintenance epoch 17
    is eligible, and only when the previous source was the original 7d0af...
    controller. This grants no new model call or replay permission.
    """
    from .macos_execution import private_root_file
    from .authority import AuthorityRegistry
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    prior = 'offline-5d95b82d0952982adda5-1-probe'
    fingerprint = 'ec7355ba8ca0c9270c00a8386c3c9c3eb376fc289ec6ee416fc8bcd84f63d747'
    if (previous.get('source_sha') != previous_source
            or previous.get('epoch') != 13 or scope['epoch'] not in (15, 17)
            or previous.get('browser_calls') != 0):
        return False
    archive = Path(broker.state) / ('controller-blocked-'+previous_source[:12]+'-retained.json')
    audit_db = Path(broker.config['authority_path'])
    private_root_file(archive)
    private_root_file(audit_db)
    try:
        state = json.loads(archive.read_text())
        if (state.get('state') != 'blocked' or state.get('requests') != []
                or state.get('outstanding') is not None or state.get('acknowledged') != []):
            return False
        with closing(sqlite3.connect(audit_db.as_uri()+'?mode=ro',uri=True)) as db:
            audit = db.execute(
                'SELECT fingerprint,source_sha,prior_state,disposition '
                'FROM v1a_abandoned_probe_audit WHERE project=? AND request=?',
                (broker.project.key,prior)).fetchone()
            original = db.execute(
                'SELECT fingerprint,state,result FROM execution WHERE project=? AND request=?',
                (broker.project.key,prior)).fetchone()
        return (audit == (fingerprint,previous_source,'started','quarantined_unresolved_not_replayable')
                and original == (fingerprint,'abandoned',None))
    except (OSError,ValueError,sqlite3.Error):
        return False


def qualify_operator_browser_bridge(broker):
    """Administrator maintenance-only, zero-model-call native peer proof.

    This is not an activation command. It records an exclusive root-owned
    qualification after verifying the already configured user-side listener.
    """
    import os
    import sys
    from ..core.schema import canonical_json
    from .controller_configuration import validate_controller_configuration
    from .macos_server import verify_installation

    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('browser qualification requires root macOS runtime')
    verify_installation(broker.config)
    if broker.project.account != '_doagain_jp':
        raise ExecutionBlocked('browser qualification is only scoped to jobpipe')
    status = broker.registry.status(broker.project.repo)
    if status['intent'] != 'maintenance':
        raise ExecutionBlocked('browser qualification requires maintenance intent')
    scope = validate_controller_configuration(
        broker.config, epoch=status['epoch'])
    if scope is None or scope['planner'] != 'chatgpt-browser-cdp':
        raise ExecutionBlocked('installed controller has no sealed browser grant')
    grant = broker.config['browser_planning_scope']
    client = BrokerBrowserBridge(
        Path(broker.config['operator_home']) / '.do_again/browser/planner.sock')
    peer = client.probe_authorized(
        conversation_id=grant['conversation_id'],
        profile_sha256=grant['profile_sha256'],
        grant_sha256=browser_grant_digest(grant))
    if peer['peer_uid'] != 501:
        raise ExecutionBlocked('operator browser peer identity differs')
    proof = {
        'state':'qualified_operator_bridge',
        'source_sha':broker.config['source_sha'],
        'goal_sha256':scope['goal_sha256'],
        'epoch':scope['epoch'],
        'grant_sha256':browser_grant_digest(grant),
        'operator_uid':501, 'peer_uid':peer['peer_uid'],
        'browser_calls':0,
    }
    path = Path(broker.state) / controller_artifact(scope, 'browser-planner-qualification.json')
    # The very first successful V1A bridge proof was minted by source
    # 9ea8d930. A source-bound security fix needs a NEW proof. Preserve
    # the old sealed evidence without giving it authority for this source.
    # Fail closed if the previous grant had any controller/model-call state.
    original_source = '7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191'
    recent_source = 'de3de3314a4195133f25132ec7874785ee661b9a'
    if persistent_scope(scope) and (path.exists() or path.is_symlink()):
        raise ExecutionBlocked('persistent bridge proof already exists; no replacement')
    if path.exists() or path.is_symlink():
        private_root_file(path)
        try:
            previous = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            raise ExecutionBlocked('previous bridge qualification is unreadable') from exc
        if isinstance(previous, dict) and previous.get('source_sha') == broker.config['source_sha']:
            raise ExecutionBlocked('bridge qualification already exists')
        previous_source = previous.get('source_sha') if isinstance(previous,dict) else None
        # The only additional admissible change is the administrator-installed
        # de3de... -> V1A maintenance epoch 17 upgrade. It cannot inherit a
        # previous browser send, controller journal, other source, or arbitrary
        # epoch. The original 7d0af... quarantined proof requires the exact
        # separately audited immutable root ledger below.
        epoch_ok = (
            previous_source == original_source
            and (previous.get('epoch') == scope['epoch']
                 or _audited_epoch_13_to_15(broker,previous,scope,previous_source))
        ) or (
            previous_source == recent_source
            and previous.get('epoch') == 15 and scope['epoch'] == 17
        )
        if (not isinstance(previous, dict)
                or previous.get('state') != 'qualified_operator_bridge'
                or previous_source not in (original_source,recent_source)
                or broker.config['source_sha'] == previous_source
                or not epoch_ok
                or previous.get('goal_sha256') != scope['goal_sha256']
                or previous.get('browser_calls') != 0
                or previous.get('operator_uid') != 501
                or previous.get('peer_uid') != 501
                or (previous_source == recent_source
                    and (not isinstance(previous.get('grant_sha256'),str)
                         or len(previous.get('grant_sha256')) != 64))):
            raise ExecutionBlocked('previous bridge proof is not eligible for guarded requalification')
        if any((Path(broker.state) / name).exists() for name in (
                'browser-planning-calls.json', 'controller.json',
                'planning-calls.json')):
            raise ExecutionBlocked('previous controller state requires manual reconciliation')
        retained = path.with_name('browser-planner-qualification-' + previous_source[:12] + '-retained.json')
        if retained.exists() or retained.is_symlink():
            raise ExecutionBlocked('previous bridge qualification already retained')
        os.rename(path, retained)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ExecutionBlocked('browser qualification already exists or cannot be sealed') from exc
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(canonical_json(proof) + bytes([10]))
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    private_root_file(path)
    return proof
