"""Explicit installed qualification, never production admission or task progress."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ..core.schema import atomic_json, canonical_json
from .macos_execution import EXECUTION_ROOT, ExecutionBlocked, MacOSProcesses, capture, launch_spec

# Qualification-only approval. No production dependency policy is expanded.
ARTIFACT = {
    'id':'qualification-packaging-25', 'name':'packaging', 'version':'25.0',
    'url':'https://files.pythonhosted.org/packages/20/12/38679034af332785aac8774540895e234f4d07f7545804097de4b666afd8/packaging-25.0-py3-none-any.whl',
    'sha256':'29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484',
}


def qualification_gate(broker) -> dict:
    from .macos_server import verify_installation, worktree_authority
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('capability qualification requires the installed macOS supervisor')
    verify_installation(broker.config)
    status = broker.registry.status(broker.project.repo)
    if (status['intent'] != 'maintenance' or broker.config.get('production_ready') is not False
            or not broker._verified() or MacOSProcesses().owned(broker.project.uid)):
        raise ExecutionBlocked('capability qualification requires verified, drained maintenance')
    return {'operator':status, 'authority':worktree_authority(broker.project.worktree)}


def reconcile_session(broker, session: dict) -> dict:
    """A repeated invocation may observe an old effect, never dispatch it again."""
    from .publication import reconcile_publication
    if session['phase'] == 'complete':
        return session['result']
    if session['phase'] != 'publication_started':
        raise ExecutionBlocked('interrupted qualification requires evidence review; automatic repetition is blocked')
    packet = session['publication_packet']
    fingerprint = hashlib.sha256(canonical_json(packet)).hexdigest()
    try:
        receipt = broker.ledger.lookup(broker.project.key, packet['request_id'], fingerprint)
    except ExecutionBlocked:
        receipt = reconcile_publication(broker, {'operation':'git_publication_reconcile',
                                                'request_id':packet['request_id']})
    if not receipt or receipt.get('state') != 'succeeded':
        raise ExecutionBlocked('qualification publication remains uncertain; no effect was retried')
    return receipt


def qualify_capabilities(broker) -> dict:
    from .macos_server import machine_identity
    from .git_capabilities import copy_git_data
    from .git_broker import commit_via_broker, seal_metadata
    from .dependencies import install_via_broker
    from .publication import publish_via_broker, read_credential
    with broker.lock:
        before = qualification_gate(broker)
        identity = machine_identity(broker.config)
        nonce = hashlib.sha256(canonical_json(identity)).hexdigest()[:24]
        record = broker.state / ('capabilities-' + nonce + '.json')
        if record.exists():
            session = json.loads(record.read_text())
            if session['identity'] != identity or session['before'] != before:
                raise ExecutionBlocked('qualification authority changed; original evidence must be reviewed')
            recovered = reconcile_session(broker, session)
            if session['phase'] == 'complete':return recovered
            result = finish_result(broker, before, identity, session['dependency'], recovered)
            session.update(phase='complete',result=result)
            atomic_json(record,session)
            return result
        if broker.ledger.pending(broker.project.key):
            raise ExecutionBlocked('unresolved execution blocks capability qualification')
        publish = broker.project.account == '_doagain_da'
        if publish:read_credential(broker)  # Fail before any fixture effects if not enrolled.
        session = {'identity':identity,'before':before,'phase':'preparation_started'}
        atomic_json(record,session)
        @contextmanager
        def guard():
            with broker.probe_admission():
                if (broker.registry.status(broker.project.repo) != before['operator']
                        or broker.config.get('production_ready') is not False or not broker._verified()):
                    raise ExecutionBlocked('qualification authority changed before effect')
                yield
        root = EXECUTION_ROOT / broker.project.key / 'capability-probes' / nonce
        root.mkdir(parents=True, mode=0o711)
        root.chmod(0o711);root.parent.chmod(0o711)
        fixture = root / 'worktree'
        fixture.mkdir(mode=0o700);os.chown(fixture,broker.project.uid,broker.project.gid)
        # Private, fixed synthetic authority; every effect holds the real
        # maintenance fence. This is never socket-selectable by a script.
        status = {'intent':'active','epoch':1,'goal_revision':'synthetic-capability-' + nonce}
        config = dict(broker.config, production_ready=True,
                      dependency_artifacts={broker.project.key:[ARTIFACT]})
        private = SimpleNamespace(project=replace(broker.project,worktree=fixture), config=config,
                    registry=SimpleNamespace(status=lambda repo:dict(status)), ledger=broker.ledger,
                    state=broker.state, lock=threading.Lock(), admission=guard,
                    _verified=broker._verified)
        scratch,cache = root/'scratch',root/'cache'
        for path in (scratch,cache):
            path.mkdir(mode=0o700);os.chown(path,broker.project.uid,broker.project.gid)
        # A scratch repository permits initial checkout without changing the
        # protected fixture metadata or using clone helpers/configuration.
        staging = scratch/'repository';staging.mkdir(mode=0o700)
        os.chown(staging,broker.project.uid,broker.project.gid)
        copy_git_data(broker.project.worktree/'.git',staging/'.git')
        for path in (staging/'.git',*(staging/'.git').rglob('*')):
            os.chown(path,broker.project.uid,broker.project.gid)
        def run(argv, *, selected_scratch=scratch, selected_cache=cache):
            packet = {'operation':'execute','request_id':'qualification-'+nonce,
                      'argv':argv,'cwd':'.','timeout':60}
            outcome = capture(launch_spec(private.project,packet,selected_scratch,selected_cache),60,
                              start_guard=guard)
            if (outcome['returncode'] or outcome['timed_out'] or outcome.get('stdout_truncated')
                    or outcome.get('stderr_truncated')):
                raise ExecutionBlocked('confined qualification command failed: '+outcome['stderr'][:1024])
            return outcome['stdout'].strip()
        base = before['authority']['repo_head']
        run([config['git'],'--no-replace-objects','--git-dir='+str(staging/'.git'),
             '--work-tree='+str(fixture),'checkout','--force','--detach',base])
        copy_git_data(staging/'.git',fixture/'.git')
        seal_metadata(fixture/'.git')
        # Immutable snapshot is copied only as inert Git data; scripts cannot
        # write protected .git. Actual broker promotion is exercised below.
        proof_file = fixture/'do-again-native-capability-proof.txt'
        if proof_file.exists():raise ExecutionBlocked('qualification fixture path conflicts with source')
        proof_file.write_text('Synthetic installed capability qualification: '+nonce+'\n')
        os.chown(proof_file,broker.project.uid,broker.project.gid)
        request = {'operation':'dependency_install','request_id':'qualification-dependency-'+nonce,
                   'expected_head':base,'expected_epoch':1,'artifact_id':ARTIFACT['id'],
                   'request_fingerprint':hashlib.sha256(('dependency:'+nonce).encode()).hexdigest()}
        dependency = install_via_broker(private,request)
        if dependency.get('state') != 'succeeded':
            raise ExecutionBlocked('installed dependency qualification failed; durable receipt retained')
        if install_via_broker(private,request) != dependency:
            raise ExecutionBlocked('dependency receipt replay differs')
        dependency_root = Path(dependency['site_packages']).parent
        code = 'import sys;sys.path.insert(0,sys.argv[1]);import packaging;assert packaging.__version__=="25.0";print("VERIFIED")'
        observed = run([config['python'],'-I','-S','-B','-c',code,dependency['site_packages']],
                       selected_scratch=dependency_root.parent/'scratch',selected_cache=dependency_root)
        if observed != 'VERIFIED':raise ExecutionBlocked('installed dependency import lacks evidence')
        session.update(phase='dependency_verified',dependency=dependency)
        atomic_json(record,session)
        publication = None
        if publish:
            receipt = commit_via_broker(private,{'operation':'git_commit','request_id':'qualification-commit-'+nonce,
                       'expected_head':base,'expected_epoch':1,'paths':[proof_file.name],
                       'message':'Synthetic native capability qualification '+nonce})
            if receipt.get('state') != 'succeeded':
                raise ExecutionBlocked('qualification commit failed; durable receipt retained')
            packet = {'operation':'git_publish','request_id':'qualification-publication-'+nonce,
                      'expected_head':receipt['authority']['repo_head'],'expected_epoch':1,
                      'request_fingerprint':hashlib.sha256(('publication:'+nonce).encode()).hexdigest(),
                      'title':'[Qualification only] Installed macOS Git publication '+nonce,
                      'body':'Synthetic native capability proof. Do not merge. No production task or acceptance progress is claimed.\nInstalled source: '+config['source_sha']+'\n'}
            session.update(phase='publication_started',publication_packet=packet)
            atomic_json(record,session)  # Durable before the capability can dispatch.
            publication = publish_via_broker(private,packet)
            if publication.get('state') != 'succeeded':
                raise ExecutionBlocked('qualification publication failed; original evidence retained')
            if publish_via_broker(private,packet) != publication:
                raise ExecutionBlocked('publication receipt replay differs')
        result = finish_result(broker,before,identity,dependency,publication)
        session.update(phase='complete',result=result)
        atomic_json(record,session)
        return result


def finish_result(broker, before: dict, identity: dict, dependency: dict, publication: dict | None) -> dict:
    if qualification_gate(broker) != before:
        raise ExecutionBlocked('live authority changed during qualification; result is not accepted')
    return {'verified':True,'evidence_class':'synthetic_installed_capability',
            'identity':identity,'dependency':dependency,'publication':publication,
            'live_authority_unchanged':True,'production_ready':False,
            'publication_measured':publication is not None}
