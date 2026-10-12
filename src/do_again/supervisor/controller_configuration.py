"""Pure sealed V1A configuration validation, shared by factory and installer."""
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
from ..core.schema import canonical_json
from ..service.offline_continuation import digest, validate_scope
from .macos_execution import ExecutionBlocked


def validate_controller_configuration(config, *, now=None, epoch=None):
    scope = config.get('controller_scope')
    if scope is None:
        if any(config.get(k) is not None for k in ('controller_goal','controller_profile','hosted_planning_scope','browser_planning_scope')):
            raise ExecutionBlocked('controller capabilities without a controller scope')
        return None
    now = now or datetime.now(timezone.utc)
    goal, profile = config.get('controller_goal'), config.get('controller_profile')
    if (config.get('development_scope', {}).get('kind') != 'jobpipe-offline-v1'
            or config.get('production_ready') is not False
            or config.get('live_canary') is not None or config.get('codex_canary') is not None
            or not isinstance(goal, dict) or len(canonical_json(goal)) > 32768
            or config.get('controller_goal_sha256') != digest(goal)):
        raise ExecutionBlocked('controller requires separate offline execution and sealed goal')
    validate_scope(scope, binding={'repository':'Tran-Steven/jobpipe','branch':'operator-control',
        'source_sha':config.get('source_sha'),'goal_sha256':digest(goal),
        'epoch':scope.get('epoch') if epoch is None else epoch}, now=now)
    fields={'kind','allowed_paths','test_cases','test_files','test_runner','public_evidence','offline_tasks'}
    if not isinstance(profile, dict) or set(profile) != fields or profile['kind'] != 'native-development-v1':
        raise ExecutionBlocked('sealed development profile absent or unknown capabilities')
    if profile['test_runner'] not in {'unittest','pytest'}:
        raise ExecutionBlocked('test runner not explicitly approved')
    paths=profile['allowed_paths']; cases=profile['test_cases']; tests=profile['test_files']
    if (not isinstance(paths,list) or not 1<=len(paths)<=128 or any(not isinstance(p,str) for p in paths) or len(set(paths))!=len(paths)
            or not isinstance(cases,list) or len(cases)!=3 or any(not isinstance(c,str) for c in cases) or len(set(cases))!=len(cases)
            or not isinstance(tests,dict) or not tests
            or profile['public_evidence'] is not True):
        raise ExecutionBlocked('bounded source/test/evidence profile invalid')
    for p in [*paths,*tests]:
        if not isinstance(p,str):raise ExecutionBlocked('development path must be a string')
        path=PurePosixPath(p)
        if (not isinstance(p,str) or str(path)!=p or path.is_absolute() or '..' in path.parts
                or path.parts[0].startswith('.') or 'node_modules' in path.parts
                or set(part.casefold() for part in path.parts)&{'candidate_data','profiles','resumes','private','secrets','credentials'}
                or '\\' in p):
            raise ExecutionBlocked('development path outside exact file scope')
    if set(paths)&set(tests) or any(not re.fullmatch('[0-9a-f]{40}', str(v)) for v in tests.values()):
        raise ExecutionBlocked('approved baseline tests must remain immutable')
    if any(not isinstance(c,str) or not re.fullmatch(r'(?:[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*|tests/(?:[A-Za-z_][A-Za-z_0-9]*/)*test_[A-Za-z_0-9]+\.py(?:::[A-Za-z_][A-Za-z_0-9]*)*)',c) for c in cases):
        raise ExecutionBlocked('test case profile contains command syntax')
    tasks=profile['offline_tasks']
    if scope['planner']=='sealed-offline':
        if config.get('browser_planning_scope') is not None:
            raise ExecutionBlocked('offline planner cannot inherit browser authority')
        if config.get('hosted_planning_scope') is not None:
            raise ExecutionBlocked('offline planner cannot inherit hosted authority')
        if not isinstance(tasks,list) or len(tasks)!=3:
            raise ExecutionBlocked('offline qualification requires three sealed task alternatives')
        if [t.get('case') for t in tasks if isinstance(t,dict)] != cases:
            raise ExecutionBlocked('offline priorities must match the sealed test sequence')
        for task in tasks:
            if (not isinstance(task,dict) or set(task)!={'case','gap','acceptance','script'}
                    or task['case'] not in cases
                    or any(not isinstance(task[k],str) or not 1<=len(task[k])<=limit
                           for k,limit in [('gap',1000),('acceptance',1000),('script',16000)])):
                raise ExecutionBlocked('offline task outside sealed qualification profile')
    elif scope['planner']=='codex-cli':
        if config.get('browser_planning_scope') is not None:
            raise ExecutionBlocked('Codex planner cannot inherit browser authority')
        if tasks != []:
            raise ExecutionBlocked('hosted planner cannot inherit offline scripts')
        from .controller_codex_helper import validate_hosted_scope
        validate_hosted_scope(config,config.get('hosted_planning_scope'),now)
    elif scope['planner']=='chatgpt-browser-cdp':
        if tasks != [] or config.get('hosted_planning_scope') is not None:
            raise ExecutionBlocked('browser planner cannot inherit offline scripts or Codex authority')
        from ..service.browser_planner import validate_browser_planner_grant
        validate_browser_planner_grant(
            config.get('browser_planning_scope'), source_sha=config['source_sha'],
            goal_sha256=digest(goal), epoch=scope['epoch'], now=now)
    else:
        raise ExecutionBlocked('unknown controller planner provider')
    return scope
