"""Authenticated one-shot integration over the existing native broker."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

from ..core.schema import canonical_json, atomic_json
from .macos_execution import EXECUTION_ROOT, INSTALL_ROOT, ExecutionBlocked, MacOSProcesses
from .native_test_evidence import measure_committed_tree
from .controller_native_effects import NativeControllerEffects
from .reviewed_task import ASSETS, BASE, PATHS, ReviewedTransaction, sha, validate_scope


def require_private_journal(directory, *, owner_uid=0):
    if directory.is_symlink() or directory.stat().st_uid != owner_uid or directory.stat().st_mode & 0o077:
        raise ExecutionBlocked('reviewed recovery journal is not root-private')


def require_original_baseline(path, content, *, owner_uid=0):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != content or path.stat().st_uid != owner_uid or path.stat().st_mode & 0o222:
        raise ExecutionBlocked('original independent baseline changed')


def prepare_original_baseline(root, content, *, owner_uid=0):
    # The daemon's restrictive umask also applies to mkdir(parents=True).
    # Permit traversal to this public baseline only; recovery journals remain private.
    root.mkdir(mode=0o711, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != owner_uid:
        raise ExecutionBlocked('baseline traversal directory ownership or alias differs')
    root.chmod(0o711)
    directory = root / 'baseline'
    directory.mkdir(mode=0o711, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != owner_uid:
        raise ExecutionBlocked('baseline traversal directory ownership or alias differs')
    # Pytest must enumerate this directory; it contains only approved public tests.
    directory.chmod(0o755)
    baseline = directory / 'test_original_baseline.py'
    if not baseline.exists():
        with baseline.open('xb') as stream:
            stream.write(content)
            stream.flush(); os.fsync(stream.fileno())
        baseline.chmod(0o444)
    require_original_baseline(baseline, content, owner_uid=owner_uid)
    return baseline, directory


def execute_reviewed_task(broker, packet):
    if packet != {'operation': 'controller_amended_task'}:
        raise ExecutionBlocked('reviewed task accepts no caller-selected payload')
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('reviewed task requires installed root macOS broker')
    from .macos_server import verify_installation
    from .jobpipe_development import require_broker_jobpipe_development
    from .git_broker import atomic_swap
    scope = validate_scope(broker.config, broker.project)
    if scope is None:
        raise ExecutionBlocked('no administrator-sealed reviewed task scope')
    rid = scope['request_id']; fp = sha(canonical_json(scope))
    with broker.lock:
        verify_installation(broker.config)
        prior = broker.ledger.observe_request(broker.project.key, rid, fp)
        if prior['state'] == 'terminal':
            current = broker.registry.status(broker.project.repo)
            if current['intent'] == 'active' and current['epoch'] == scope['epoch']+1:
                MacOSProcesses().drain(broker.project.uid)
                with broker.admission():
                    broker.registry.set_intent(broker.project.repo, 'maintenance', goal_revision=current['goal_revision'])
            return prior['result']
        status = broker.registry.status(broker.project.repo)
        recovering_active = (prior['state'] == 'post_dispatch_uncertain'
            and status['intent'] == 'active' and status['epoch'] == scope['epoch']+1)
        if ((status['intent'] != 'maintenance' and not recovering_active) or not broker._verified()
                or (prior['state'] == 'not_started' and status['epoch'] != scope['epoch'])):
            raise ExecutionBlocked('reviewed task requires exact drained maintenance epoch')
        require_broker_jobpipe_development(broker)
        # A dedicated task never starts the browser planner or publishes control Git.
        directory = broker.state / ('reviewed-' + rid)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require_private_journal(directory)
        saved_transaction = directory / 'transaction.json'
        if saved_transaction.exists() and json.loads(saved_transaction.read_text()).get('phase') == 'blocked':
            raise ExecutionBlocked('original recovery is blocked; no task replay')
        prepared = directory / 'baseline-manifest.json'
        started = datetime.now(timezone.utc).isoformat()
        with broker.admission():
            if prior['state'] == 'not_started':
                if broker.ledger.pending(broker.project.key) or MacOSProcesses().owned(broker.project.uid):
                    raise ExecutionBlocked('another native task/process is unresolved')
                broker.ledger.reserve(broker.project.key, rid, fp, intent={
                    'operation': 'reviewed_task', 'source_sha': broker.config['source_sha'],
                    'request_fingerprint': fp, 'publication': 'disabled',
                    'original_request_id': scope['original_request_id']})
            active_epoch = status['epoch'] if recovering_active else broker.registry.set_intent(
                broker.project.repo, 'active', goal_revision=status['goal_revision'])
        try:
            deadline = time.monotonic() + 600
            broker._reviewed_deadline = deadline
            @contextmanager
            def admission():
                with broker.admission():
                    if time.monotonic() >= deadline:
                        raise ExecutionBlocked('reviewed task execution lifetime expired')
                    verify_installation(broker.config)
                    current = broker.registry.status(broker.project.repo)
                    if current['intent'] != 'active' or current['epoch'] != active_epoch or not broker._verified():
                        raise ExecutionBlocked('reviewed task authority changed')
                    require_broker_jobpipe_development(broker)
                    yield

            profile = dict(broker.config['controller_profile'])
            effects = NativeControllerEffects(broker, profile, admission, lambda request: ())
            manifest = (json.loads(prepared.read_text())['manifest'] if prepared.exists()
                        else effects.manifest(rid + '-baseline-manifest', BASE))
            initial_structure = {}
            def structure():
                result = {}
                for path in [broker.project.worktree, *broker.project.worktree.rglob('*')]:
                    relative = path.relative_to(broker.project.worktree)
                    if relative.parts and relative.parts[0] == '.git':
                        continue
                    info = path.lstat()
                    result[str(relative)] = [info.st_mode, info.st_uid, info.st_gid,
                                             info.st_ino if path.is_dir() else None]
                return result
            if prepared.exists():
                saved = json.loads(prepared.read_text())
                if saved['request_fingerprint'] != fp:
                    raise ExecutionBlocked('original task snapshot fingerprint differs')
                initial_structure.update(saved['structure']); started = saved['started_at_utc']
            else:
                initial_structure.update(structure())
                atomic_json(prepared, {'manifest':manifest, 'structure':initial_structure,
                    'request_fingerprint':fp, 'started_at_utc':started})
            def measure(overlay, head):
                if structure() != initial_structure:
                    raise ExecutionBlocked('source parent/mode/ownership drift')
                expected = {name: dict(entry) for name, entry in manifest.items()}
                for name, data in overlay.items():
                    expected[name]['blob_sha'] = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                measured = measure_committed_tree(broker.project.worktree, commit=head,
                    manifest=expected, read_authority=effects.head)
                return {'authority_head':head, 'tree_sha256':measured['tree_sha256'],
                        'source_state':'exact_reviewed_source_binding'}

            root = EXECUTION_ROOT / broker.project.key / 'requests' / rid
            content = ASSETS['files']['tests/test_ats_question_triage.py']['before'].encode()
            baseline, baseline_dir = prepare_original_baseline(root, content)
            def test(case):
                selected = str(baseline) if case == 'original_baseline' else (
                    'tests/test_ats_question_triage.py' if case == 'complete_reviewed_file' else case)
                code = ('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
                        'from do_again.supervisor.reviewed_test_runner import main;'
                        'sys.exit(main(sys.argv[1],sys.argv[2]))')
                result = effects.capture(rid+'-tests', [broker.config['python'], '-I','-S','-B','-c',code,
                    selected, str(INSTALL_ROOT/'current/runtimes/test-packages')], 60,
                    read_only_source=True, extra_read_paths=(baseline_dir,))
                marker = '\nNATIVE_PYTEST_OBSERVATION='
                report = result.get('stdout', '').rpartition(marker)[2].strip()
                try:
                    counts = json.loads(report)
                except (ValueError, TypeError):
                    counts = {}
                if counts and counts.get('execution_uid') != broker.project.uid:
                    raise ExecutionBlocked('pytest child UID differs from dedicated execution identity')
                if result.get('stdout_truncated') or result.get('stderr_truncated'):
                    raise ExecutionBlocked('native pytest capture was truncated')
                return {'case': case, 'returncode': result['returncode'], 'timed_out': result['timed_out'],
                        'network_denied': result['network_denied'], 'pytest_observation': counts,
                        'stdout_sha256': sha(result.get('stdout','').encode()),
                        'stderr_sha256': sha(result.get('stderr','').encode())}
            def commit(tested_tree):
                with admission():
                    pass
                packet = {'operation':'git_commit','request_id':rid+'-commit','expected_head':BASE,
                    'expected_epoch':active_epoch,'paths':list(PATHS),
                    'message':'Apply reviewed ATS contact-control safety amendment\n\nCo-authored-by: Codex <267193182+codex@users.noreply.github.com>',
                    'request_fingerprint':fp, 'tested_tree_sha256':tested_tree}
                # Git performs its own admission checks. Do not nest POSIX flocks.
                from .git_broker import commit_via_broker
                return commit_via_broker(broker, packet)
            transaction = ReviewedTransaction(broker.project.worktree, directory,
                measure=measure, test=test, commit=commit,
                drain=lambda:MacOSProcesses().drain(broker.project.uid), swap_git=atomic_swap)
            result = transaction.execute({'request_id':rid, 'request_fingerprint':fp})
            result.update(request_id=rid, request_fingerprint=fp, source_sha=broker.config['source_sha'],
                native_attempt_id=rid, installed_source_sha=broker.config['source_sha'],
                original_request_id=scope['original_request_id'],
                original_request_fingerprint=scope['original_request_fingerprint'],
                original_planner_call_id=ASSETS['lineage']['original_planner_call_id'],
                amendment_sha256=sha(canonical_json({'assets':ASSETS,'policy':scope['policy']})),
                execution_uid=broker.project.uid, epoch=active_epoch, pinned_base_commit=BASE,
                original_task=ASSETS['lineage'], acceptance_revision=ASSETS['acceptance_revision'],
                policy=scope['policy'], asset_sha256=scope['asset_sha256'],
                sealed_contract_sha256=sha(canonical_json({'scope':scope,'assets':ASSETS})),
                started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())
            broker.ledger.finish(broker.project.key, rid, result)
            return result
        finally:
            broker.__dict__.pop('_reviewed_deadline', None)
            MacOSProcesses().drain(broker.project.uid)
            with broker.admission():
                current = broker.registry.status(broker.project.repo)
                if current['intent'] == 'active' and current['epoch'] == active_epoch:
                    broker.registry.set_intent(broker.project.repo, 'maintenance', goal_revision=status['goal_revision'])
