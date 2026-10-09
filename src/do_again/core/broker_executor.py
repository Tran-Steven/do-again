"""Agent execution uses the authenticated project broker, never a local fallback."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .schema import OperatorError, REQUEST_ID_RE, read_json, request_fingerprint
from ..supervisor.macos_client import broker_request


class BrokerExecutor:
    def __init__(self, *, repo: Path, policy_path: Path, state_dir: Path, rpc=None):
        self.repo = repo.resolve()
        self.policy = read_json(policy_path)
        self.rpc = rpc

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if sys.platform != 'darwin':
            raise OperatorError('native execution boundary unavailable on this platform; no local fallback')
        rpc = self.rpc or broker_request
        operation = request['operation']
        if operation not in self.policy.get('allowed_operations', []):
            raise OperatorError(f'operation is not allowed: {operation}')
        before = rpc(self.repo, {'operation': 'status'})
        if before.get('operator_intent') != 'active' or before.get('production_ready') is not True:
            raise OperatorError('operator authority or production migration blocks execution')
        if before.get('enforcement_verified') is not True:
            raise OperatorError('native enforcement is not verified')
        actual = before.get('authority', {})
        for key, value in request.get('expected', {}).items():
            if key not in actual or actual[key] != value:
                raise OperatorError(f'authority fence cannot be established: {key}')
        if operation == 'status':
            result = before
        else:
            packet = self._packet(request, before)
            result = rpc(self.repo, packet)
        after = rpc(self.repo, {'operation': 'status'})
        return {'operation': operation, 'request_fingerprint': request_fingerprint(request),
                'authority_before': actual, 'authority_after': after.get('authority', {}),
                'result': result}

    def recover(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Only an authenticated, original terminal result can repair a receipt."""
        if sys.platform!='darwin':
            raise OperatorError('native recovery unavailable; no local fallback')
        if request['operation'] not in self.policy.get('allowed_operations',[]):
            raise OperatorError('recovery operation is not allowed')
        rpc=self.rpc or broker_request
        fingerprint=request_fingerprint(request)
        result=rpc(self.repo,{'operation':'execution_observe','request_id':request['request_id'],
                             'request_fingerprint':fingerprint})
        if result.get('state')!='terminal':return None
        if result.get('request_fingerprint')!=fingerprint or result.get('replay') is not False:
            raise OperatorError('broker recovery identity differs')
        if not isinstance(result.get('result'),dict):
            raise OperatorError('broker terminal evidence is invalid')
        return {'operation':request['operation'],'request_fingerprint':fingerprint,
                'result':result['result'],'recovered_read_only':True,
                'source_sha':result['source_sha']}

    def _packet(self, request: dict[str, Any], status: dict[str, Any]) -> dict:
        args = request.get('args', {})
        if args.get('env'):
            raise OperatorError('script-supplied environment is not admitted')
        if request['operation'] == 'git_publication_reconcile':
            original = args.get('original_request_id')
            if (set(args) != {'original_request_id'} or not isinstance(original, str)
                    or not REQUEST_ID_RE.fullmatch(original)):
                raise OperatorError('publication reconciliation requires one original request identity')
            return {'operation':'git_publication_reconcile','request_id':original}
        if request['operation'] == 'git_publish':
            if set(args) != {'title','body'}:
                raise OperatorError('publication accepts pull request content only')
            return {'operation':'git_publish','request_id':request['request_id'],
                    'title':args['title'],'body':args['body'],
                    'expected_head':status.get('authority',{}).get('repo_head'),
                    'expected_epoch':status.get('epoch'),
                    'request_fingerprint':request_fingerprint(request)}
        if request['operation'] == 'dependency_install':
            if set(args) != {'artifact_id'}:
                raise OperatorError('dependencies accept an approved artifact identity only')
            return {'operation':'dependency_install','request_id':request['request_id'],
                    'artifact_id':args['artifact_id'],
                    'expected_head':status.get('authority',{}).get('repo_head'),
                    'expected_epoch':status.get('epoch'),
                    'request_fingerprint':request_fingerprint(request)}
        if request['operation'] == 'git_commit':
            if set(args) != {'paths', 'message'}:
                raise OperatorError('Git accepts exact file paths and a message only')
            return {'operation':'git_commit','request_id':request['request_id'],
                    'paths':args['paths'],'message':args['message'],
                    'expected_head':status.get('authority',{}).get('repo_head'),
                    'expected_epoch':status.get('epoch'),
                    'request_fingerprint':request_fingerprint(request)}
        worktree = Path(status['worktree'])
        executables = [Path(value) for value in status['executables']]

        def runner(name: str) -> str:
            candidates = [p for p in executables if p.name == name]
            if len(candidates) != 1:
                raise OperatorError(f'no unique supervisor executable for {name}')
            return str(candidates[0])

        def relative(value: Any) -> str:
            if not isinstance(value, str) or '\x00' in value:
                raise OperatorError('path must be a string')
            path = Path(value)
            if path.anchor:
                if not path.is_absolute():
                    raise OperatorError('rooted path is outside the project')
                try:
                    path = path.resolve().relative_to(self.repo)
                except ValueError as exc:
                    raise OperatorError('path is outside the project') from exc
            if '..' in path.parts:
                raise OperatorError('path escapes the assigned worktree')
            return path.as_posix()

        values = args.get('argv', [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise OperatorError('argv must be a string list')
        operation = request['operation']
        if operation == 'scratch_script':
            language = args.get('language')
            content = args.get('content')
            if not isinstance(content, str) or not content.strip() or len(content.encode()) > 65536:
                raise OperatorError('scratch source must be nonempty and at most 65536 bytes')
            if language == 'python':
                argv = [runner('python3'), '-c', content, *values]
            elif language == 'bash':
                argv = [runner('bash'), '-c', content, 'do-again-scratch', *values]
            else:
                raise OperatorError('scratch language must be python or bash')
        elif operation == 'run_tests':
            argv = [runner('python3'), '-m', 'unittest']
            if args.get('discover') is True:
                argv += ['discover', '-s', relative(args.get('start_directory', 'tests')),
                         '-p', str(args.get('pattern', 'test_*.py'))]
            else:
                modules = args.get('modules')
                if not isinstance(modules, list) or not modules or not all(isinstance(v, str) for v in modules):
                    raise OperatorError('test modules must be a nonempty string list')
                argv += modules
        elif operation == 'repo_script':
            path = relative(args.get('path', ''))
            prefixes = tuple(self.policy.get('repo_script_prefixes', []))
            if not path.startswith(prefixes):
                raise OperatorError('repository script path is not approved')
            suffix = Path(path).suffix
            language = {'.py': 'python3', '.sh': 'bash', '.bash': 'bash', '.zsh': 'zsh'}.get(suffix)
            if language is None:
                raise OperatorError('repository script requires an admitted interpreter')
            argv = [runner(language), str(worktree / path), *values]
        elif operation == 'extended_exec':
            if not values:
                raise OperatorError('extended execution requires argv')
            name = Path(values[0]).name
            if name in self.policy.get('hard_denied_binaries', []) or name not in self.policy.get('extended_exec_binaries', []):
                raise OperatorError('extended executable is not approved')
            argv = [runner(name), *values[1:]]
        else:
            raise OperatorError(f'operation requires a scoped broker implementation: {operation}')
        timeout = request.get('limits', {}).get('timeout_seconds', self.policy.get('default_timeout_seconds', 120))
        maximum = min(3600, self.policy.get('absolute_timeout_seconds', 3600))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= maximum:
            raise OperatorError('invalid execution timeout')
        return {'operation': 'execute', 'request_id': request['request_id'], 'argv': argv,
                'cwd': relative(args.get('cwd', '.')), 'timeout': timeout,
                'request_fingerprint': request_fingerprint(request),
                'expected_head': status.get('authority', {}).get('repo_head'),
                'expected_authority': request.get('expected', {})}
