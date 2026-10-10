"""Trusted GitHub Git-data publication. No Git process receives credentials."""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode, parse_qs

from .git_capabilities import SHA, _safe_relative
from .macos_execution import ExecutionBlocked
from .network import https_bytes


def validate_control_branch(branch: str) -> str:
    """Only the legacy branch or an isolated canary namespace can be sealed."""
    if not isinstance(branch, str) or not re.fullmatch(
            r'operator-control|do-again/canary-[0-9a-f]{24}/control', branch):
        raise ExecutionBlocked('control branch is outside the sealed namespace')
    return branch


class GitHubRepository:
    def __init__(self, repository: str, token: str, *, control: bool = False,
                 control_branch: str = "operator-control"):
        if repository not in {'Tran-Steven/do-again','Tran-Steven/jobpipe'}:
            raise ExecutionBlocked('repository publication scope is excluded')
        if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_]{20,255}',token):
            raise ExecutionBlocked('repository credential is unavailable')
        validate_control_branch(control_branch)
        if not control and control_branch != "operator-control":
            raise ExecutionBlocked("control branch cannot expand publication authority")
        self.control_branch = control_branch
        self.repository = repository
        self._token = token
        self.control = control

    def request(self, method: str, endpoint: str, payload=None):
        branch = r'do-again/task-[0-9a-f]{20}'
        read = (re.fullmatch(r'git/commits/[0-9a-f]{40}|pulls/[1-9][0-9]*',endpoint)
                or re.fullmatch(r'git/ref/heads/(?:main|'+branch+r')',endpoint))
        if endpoint.startswith('pulls?'):
            query = parse_qs(endpoint[6:],keep_blank_values=True)
            heads = query.get('head',[])
            prefix = self.repository.split('/')[0]+':'
            read = (len(heads)==1 and heads[0].startswith(prefix)
                    and bool(re.fullmatch(branch,heads[0][len(prefix):]))
                    and query=={'state':['all'],'head':heads,'base':['main'],'per_page':['100']})
        write = method=='POST' and endpoint in {'git/blobs','git/trees','git/commits','git/refs','pulls'}
        update = method=='PATCH' and bool(re.fullmatch('git/refs/heads/'+branch+'|pulls/[1-9][0-9]*',endpoint))
        if self.control:
            read = bool(re.fullmatch(r'actions/runs/[1-9][0-9]*|pulls/[1-9][0-9]*|git/(?:commits|blobs|trees)/[0-9a-f]{40}|git/trees/[0-9a-f]{40}\?recursive=1',endpoint) or endpoint == 'git/ref/heads/' + self.control_branch)
            if endpoint.startswith('actions/workflows/ci.yml/runs?'):
                query = parse_qs(endpoint.split('?',1)[1],keep_blank_values=True)
                read = (set(query)=={'head_sha','branch','event','per_page'}
                        and len(query['head_sha'])==1
                        and bool(SHA.fullmatch(query['head_sha'][0]))
                        and len(query['branch'])==1
                        and bool(re.fullmatch(branch,query['branch'][0]))
                        and query['event']==['pull_request'] and query['per_page']==['100'])
            write = method=='POST' and endpoint in {'git/blobs','git/trees','git/commits'}
            update = (method=='PATCH' and endpoint=='git/refs/heads/'+self.control_branch
                      and isinstance(payload,dict) and set(payload)=={'sha','force'}
                      and payload['force'] is False and isinstance(payload['sha'],str)
                      and SHA.fullmatch(payload['sha']))
        if not ((method=='GET' and read) or write or update):
            raise ExecutionBlocked('repository API operation is outside the publication capability')
        body = None if payload is None else json.dumps(payload).encode()
        status, content = https_bytes('https://api.github.com/repos/'+self.repository+'/'+endpoint,
                                     host='api.github.com',method=method,body=body,limit=1024*1024,
                                     allow_query=True,headers={'Authorization':'Bearer '+self._token,
                                     'Accept':'application/vnd.github+json','Content-Type':'application/json',
                                     'User-Agent':'DoAgainSupervisor','X-GitHub-Api-Version':'2022-11-28'})
        if method == 'GET' and status == 404:return None
        if not 200 <= status < 300:
            raise ExecutionBlocked('repository API rejected the scoped operation')
        try:return json.loads(content)
        except (ValueError,UnicodeError):raise ExecutionBlocked('repository API evidence is invalid') from None


def validate_publication_paths(export: dict) -> None:
    """Reject unsupported authority before either object upload or reservation recovery."""
    entries = export.get('entries')
    if not isinstance(entries,list) or not 1 <= len(entries) <= 128:
        raise ExecutionBlocked('publication requires bounded exact file changes')
    for entry in entries:
        path = entry.get('path') if isinstance(entry,dict) else None
        if not _safe_relative(path):
            raise ExecutionBlocked('publication file path is invalid')
        if path.casefold() == '.github/workflows' or path.casefold().startswith('.github/workflows/'):
            raise ExecutionBlocked('workflow changes require separately authorized publication authority')


def publish_commit(api: GitHubRepository, export: dict, branch: str, title: str, body: str,
                   *, effect_guard) -> dict:
    """Every mutation holds the same final authority fence as local execution."""
    if not re.fullmatch('do-again/task-[0-9a-f]{20}',branch):
        raise ExecutionBlocked('publication requires the supervisor-reserved goal branch')
    if not all(isinstance(export.get(k),str) and SHA.fullmatch(export[k]) for k in ('head','parent','tree')):
        raise ExecutionBlocked('publication commit evidence is incomplete')
    if not isinstance(title,str) or not 1 <= len(title.strip()) <= 200 or not isinstance(body,str) or len(body.encode()) > 16000:
        raise ExecutionBlocked('pull request content is invalid')
    validate_publication_paths(export)
    ref_endpoint = 'git/ref/heads/'+branch
    reference = api.request('GET',ref_endpoint)
    observed = None if reference is None else reference['object']['sha']
    if observed not in (None, export['parent'], export['head']):
        raise ExecutionBlocked('remote branch changed; publication requires reconciliation')
    # Bind an existing PR before moving its branch. GitHub's list endpoint may
    # still show its old head after the reference update; that is not absence.
    owned = branch_pull_requests(api,branch)
    if len(owned)>1 or any(pr.get('state')!='open' for pr in owned):
        raise ExecutionBlocked('pull request ownership is conflicting or closed')
    if owned and owned[0]['head']['sha'] not in (export['parent'],export['head']):
        raise ExecutionBlocked('existing pull request head requires reconciliation')
    if observed != export['head']:
        parent = api.request('GET','git/commits/'+export['parent'])
        if parent is None or parent['sha'] != export['parent'] or not SHA.fullmatch(parent['tree']['sha']):
            raise ExecutionBlocked('remote base is unavailable; synchronization is required')
        entries = []
        for entry in export['entries']:
            if entry['sha'] is not None:
                with effect_guard():
                    blob = api.request('POST','git/blobs',{'content':entry['content'],'encoding':'base64'})
                if blob.get('sha') != entry['sha']:
                    raise ExecutionBlocked('remote blob identity differs')
            entries.append({k:entry[k] for k in ('path','mode','type','sha')})
        with effect_guard():
            tree = api.request('POST','git/trees',{'base_tree':parent['tree']['sha'],'tree':entries})
        if tree.get('sha') != export['tree']:raise ExecutionBlocked('remote tree identity differs')
        with effect_guard():
            commit = api.request('POST','git/commits',{k:export[k] for k in ('message','author','committer')} |
                                 {'tree':export['tree'],'parents':[export['parent']]})
        if commit.get('sha') != export['head']:raise ExecutionBlocked('remote commit identity differs')
        # Recheck after object preparation, immediately before the branch effect.
        with effect_guard():
            current = api.request('GET',ref_endpoint)
            current_sha = None if current is None else current['object']['sha']
            if current_sha != observed:raise ExecutionBlocked('remote branch changed before publication')
            if observed is None:
                api.request('POST','git/refs',{'ref':'refs/heads/'+branch,'sha':export['head']})
            else:
                api.request('PATCH','git/refs/heads/'+branch,{'sha':export['head'],'force':False})
        reference = api.request('GET',ref_endpoint)
        if reference is None or reference['object']['sha'] != export['head']:
            raise ExecutionBlocked('remote branch publication is uncertain')
    matching = branch_pull_requests(api,branch)
    if len(matching)>1 or any(pr['state']!='open' for pr in matching):
        raise ExecutionBlocked('pull request ownership is conflicting or closed')
    if owned:
        if matching and matching[0]['number']!=owned[0]['number']:
            raise ExecutionBlocked('pull request identity changed during publication')
        matching=[api.request('GET','pulls/'+str(owned[0]['number']))]
    elif matching:
        matching=[api.request('GET','pulls/'+str(matching[0]['number']))]
    if matching and (matching[0] is None or matching[0]['head']['sha']!=export['head']
                     or matching[0]['head']['ref']!=branch
                     or matching[0]['head']['repo']['full_name']!=api.repository
                     or matching[0]['base']['repo']['full_name']!=api.repository
                     or matching[0]['base']['ref']!='main' or matching[0]['state']!='open'):
        raise ExecutionBlocked('existing pull request update is uncertain; no second creation')
    if not matching:
        with effect_guard():
            created = api.request('POST','pulls',{'title':title,'body':body,'head':branch,'base':'main','draft':True})
        if isinstance(created.get('number'),bool) or not isinstance(created.get('number'),int) or created['number']<1:
            raise ExecutionBlocked('pull request identity is invalid')
        pulls = api.request('GET','pulls/'+str(created['number']))
        if (pulls['head']['sha']!=export['head'] or pulls['head']['ref']!=branch
                or pulls['head']['repo']['full_name']!=api.repository
                or pulls['base']['repo']['full_name']!=api.repository or pulls['base']['ref']!='main'
                or pulls['state']!='open'):
            raise ExecutionBlocked('pull request creation is uncertain')
        matching = [pulls]
    pr = matching[0]
    if pr.get('title')!=title or pr.get('body')!=body:
        with effect_guard():
            api.request('PATCH','pulls/'+str(pr['number']),{'title':title,'body':body})
        pr=api.request('GET','pulls/'+str(pr['number']))
    if pr.get('title')!=title or pr.get('body')!=body or pr['head']['sha']!=export['head']:
        raise ExecutionBlocked('pull request payload acknowledgment is uncertain')
    if pr.get('html_url') != 'https://github.com/'+api.repository+'/pull/'+str(pr['number']):
        raise ExecutionBlocked('pull request URL is not exact repository evidence')
    return {'repository':api.repository,'head':export['head'],'branch':branch,
            'pull_request':pr['number'],'url':pr['html_url']}


def matching_pull_requests(api: GitHubRepository, branch: str, head: str) -> list:
    return [pr for pr in branch_pull_requests(api,branch) if pr['head']['sha']==head]


def branch_pull_requests(api: GitHubRepository, branch: str) -> list:
    query = urlencode({'state':'all','head':api.repository.split('/')[0]+':'+branch,'base':'main','per_page':100})
    pulls = api.request('GET','pulls?'+query)
    if not isinstance(pulls,list):raise ExecutionBlocked('pull request inventory is invalid')
    if len(pulls)>=100:raise ExecutionBlocked('pull request inventory exceeds its bound')
    return [pr for pr in pulls if pr['head']['ref']==branch
            and pr['head']['repo']['full_name']==api.repository and pr['base']['ref']=='main'
            and pr['base']['repo']['full_name']==api.repository]
