"""Read existing CI for an original broker publication; never trigger a run."""
from __future__ import annotations

from urllib.parse import urlencode

from .macos_execution import ExecutionBlocked, REQUEST_ID


def observe_ci(broker, packet: dict) -> dict:
    if (set(packet) != {'operation','request_id'} or packet['operation'] != 'ci_observe'
            or not isinstance(packet['request_id'],str)
            or not REQUEST_ID.fullmatch(packet['request_id'])):
        raise ExecutionBlocked('CI observation accepts one original publication identity only')
    from .macos_server import verify_installation, worktree_authority
    from .control_history import api_for
    # The caller is already authenticated by ProjectBroker. Hold its admission
    # fence so a changing goal/head cannot turn an old run into current success.
    with broker.lock, broker.admission():
        verify_installation(broker.config)
        intent = broker.ledger.intent(broker.project.key,packet['request_id'])
        if not intent or intent.get('operation') != 'git_publish':
            raise ExecutionBlocked('CI requires an original broker publication')
        observed = broker.ledger.observe_request(broker.project.key,packet['request_id'],
                                                intent.get('request_fingerprint'))
        if observed['state'] != 'terminal':
            return {'state':'publication_uncertain','returncode':1,'replay':False}
        publication = observed['result']
        configured = next(p for p in broker.config['projects'] if p['key']==broker.project.key)
        repository = configured.get('github_repository')
        head, branch, number = (publication.get(k) for k in ('head','branch','pull_request'))
        if (publication.get('state') != 'succeeded' or publication['returncode'] != 0
                or observed['source_sha'] != broker.config['source_sha']
                or publication.get('repository') != repository
                or intent.get('repository') != repository or intent.get('head') != head
                or intent.get('branch') != branch or type(number) is not int or number < 1
                or worktree_authority(broker.project.worktree)['repo_head'] != head):
            raise ExecutionBlocked('CI publication source, repository, or current head differs')
        api = api_for(broker)
        pr = api.request('GET','pulls/'+str(number))
        if (not isinstance(pr,dict) or pr.get('state') != 'open'
                or pr.get('number') != number or pr.get('draft') is not True
                or pr.get('head',{}).get('sha') != head or pr['head'].get('ref') != branch
                or pr['head'].get('repo',{}).get('full_name') != repository
                or pr.get('base',{}).get('ref') != 'main'
                or pr['base'].get('repo',{}).get('full_name') != repository):
            raise ExecutionBlocked('CI pull request binding differs')
        query = urlencode({'head_sha':head,'branch':branch,'event':'pull_request','per_page':100})
        inventory = api.request('GET','actions/workflows/ci.yml/runs?'+query)
        if (not isinstance(inventory,dict) or type(inventory.get('total_count')) is not int
                or not 0 <= inventory['total_count'] <= 100
                or not isinstance(inventory.get('workflow_runs'),list)
                or len(inventory['workflow_runs']) != inventory['total_count']):
            raise ExecutionBlocked('CI discovery is incomplete or exceeds its bound')
        runs = inventory['workflow_runs']
        if not runs:
            return {'state':'waiting','returncode':0,'repository':repository,'head_sha':head,
                    'pull_request':number,'run_id':None,'replay':False}
        for run in runs:
            validate_run(run,repository,head,branch,number)
        latest = max(runs,key=lambda run:run['id'])
        run = api.request('GET','actions/runs/'+str(latest['id']))
        validate_run(run,repository,head,branch,number)
        if (run['id'] != latest['id'] or run.get('run_attempt') != latest.get('run_attempt')
                or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
            raise ExecutionBlocked('CI run changed during observation')
        status, conclusion = run.get('status'), run.get('conclusion')
        if status not in {'queued','in_progress','completed','waiting','requested','pending'}:
            raise ExecutionBlocked('CI status is unrecognized')
        if status == 'completed' and not isinstance(conclusion,str):
            raise ExecutionBlocked('completed CI lacks a conclusion')
        return {'state':'terminal' if status=='completed' else 'waiting',
                'returncode':int(status=='completed' and conclusion!='success'),
                'repository':repository,'head_sha':head,'pull_request':number,
                'run_id':run['id'],'run_attempt':run['run_attempt'],
                'status':status,'conclusion':conclusion,
                'url':'https://github.com/'+repository+'/actions/runs/'+str(run['id']),
                'replay':False}


def validate_run(run, repository, head, branch, number):
    if (not isinstance(run,dict) or type(run.get('id')) is not int or run['id'] < 1
            or run.get('head_sha') != head or run.get('head_branch') != branch
            or run.get('event') != 'pull_request' or run.get('path') != '.github/workflows/ci.yml'
            or run.get('repository',{}).get('full_name') != repository
            or run.get('head_repository',{}).get('full_name') != repository
            or not isinstance(run.get('pull_requests'),list)
            or not any(type(pr.get('number')) is int and pr['number']==number
                       for pr in run['pull_requests'] if isinstance(pr,dict))):
        raise ExecutionBlocked('CI run is not evidence for the original publication')
