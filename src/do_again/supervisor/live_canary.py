"""One sealed, two-task canary scope; production authority stays false."""
from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

from ..core.schema import atomic_json
from .authority import project_identity
from .macos_execution import EXECUTION_ROOT, INSTALL_ROOT, ExecutionBlocked


def require_fresh_conversation(previous, candidate):
    """One-shot canary identities must never share an earlier canary's chat.

    A withdrawn canary may retain a possibly-submitted prompt in its exact
    conversation. A fresh nonce/binding generation does not make that chat
    safe to reuse: its old user message and unsent composer are ambiguous.
    """
    old = previous.get('live_canary') if isinstance(previous,dict) else None
    new = candidate.get('live_canary') if isinstance(candidate,dict) else None
    if (isinstance(old,dict) and isinstance(new,dict)
            and old.get('chat_url') and old['chat_url']==new.get('chat_url')):
        raise ExecutionBlocked('fresh canary requires a distinct ChatGPT conversation')


def scope(config):
    value = config.get('live_canary')
    if (not isinstance(value,dict) or set(value) != {'nonce','baseline','parent_epoch','chat_url','binding_identity'}
            or config.get('production_ready') is not False
            or not re.fullmatch(r'[0-9a-f]{24}',str(value.get('nonce','')))
            or not re.fullmatch(r'[0-9a-f]{40}',str(value.get('baseline','')))
            or type(value.get('parent_epoch')) is not int or value['parent_epoch'] < 1
            or not re.fullmatch(r'https://chatgpt\.com/c/[A-Za-z0-9-]{16,80}',str(value.get('chat_url','')))
            or not re.fullmatch(r'[0-9a-f]{64}',str(value.get('binding_identity','')))):
        raise ExecutionBlocked('sealed canary grant is missing or invalid')
    home = Path(config['operator_home'])
    parents = [p for p in config['projects'] if p.get('account')=='_doagain_da']
    if len(parents)!=1 or parents[0]['repo']!=str(home/'do-again'):
        raise ExecutionBlocked('canary parent is not the installed Do Again project')
    parent = parents[0]
    repo = home/'.do_again/live-canary'/value['nonce']
    if any(path.is_symlink() for path in (repo,*repo.parents)):
        raise ExecutionBlocked('canary identity path is aliased')
    key = project_identity(repo)
    project = dict(parent,repo=str(repo),key=key,worktree=str(EXECUTION_ROOT/key/'worktree'),
                   source_sha=value['baseline'],goal_revision='live-canary-'+value['nonce'],
                   control_branch='do-again/canary-'+value['nonce']+'/control',
                   bundle='snapshots/canary.bundle',github_repository='Tran-Steven/do-again')
    return value,parent,project


def effect_authorized(broker):
    """Only the Root-constructed scoped broker may substitute canary authority."""
    canary = getattr(broker,'canary',None)
    codex = getattr(broker,'codex',None)
    if canary is not None and codex is not None:
        raise ExecutionBlocked('browser and Codex canary authorities cannot coexist')
    if codex is not None:
        codex.check()
        return True
    if canary is None:
        return broker.config.get('production_ready') is True
    canary.check()
    return True


class CanaryAuthority:
    def __init__(self,broker,parent,grant):
        self.broker,self.parent,self.grant = broker,parent,grant

    def parent_check(self):
        status=self.parent.registry.status(self.parent.project.repo)
        if (self.parent.config.get('production_ready') is not False
                or status['intent']!='maintenance' or status['epoch']!=self.grant['parent_epoch']
                or self.parent.ledger.pending(self.parent.project.key)):
            raise ExecutionBlocked('canary parent maintenance authority changed')
        # The root-installed parent roster is immutable. Keep every sibling
        # (jobpipe and optional Sonary) quiescent throughout this two-task run.
        # A synthetic ChatGPT canary may never activate a real project worker.
        for sibling in self.parent.config.get('projects',[]):
            if sibling.get('account') == '_doagain_da':
                continue
            if sibling.get('account') not in {'_doagain_jp','_doagain_so'}:
                raise ExecutionBlocked('canary sibling identity is excluded')
            peer=self.parent.registry.status(Path(sibling['repo']))
            if (peer.get('intent')!='maintenance'
                    or self.parent.ledger.pending(sibling['key'])):
                raise ExecutionBlocked('canary sibling maintenance authority changed')

    def check(self):
        self.parent_check()
        activation=json.loads((self.broker.state/'canary-activation.json').read_text())
        status=self.broker.registry.status(self.broker.project.repo)
        if (activation.get('source_sha')!=self.broker.config['source_sha']
                or activation.get('nonce')!=self.grant['nonce']
                or activation.get('epoch')!=status['epoch'] or status['intent']!='active'
                or status['goal_revision']!='live-canary-'+self.grant['nonce']
                or type(activation.get('started_at')) not in {int,float}
                or not activation['started_at'] <= time.time() < activation['started_at']+7200):
            raise ExecutionBlocked('canary authorization is revoked, expired, or changed')

    def check_binding(self):
        from ..browser.runtime import binding_identity
        # The supervisor runs as root; HOME and DO_AGAIN_HOME are not the
        # operator's browser authority. Derive this exact read-only path.
        path=Path(self.broker.config['operator_home'])/'.do_again/browser/projects'/(self.broker.project.key[:12]+'.json')
        try:
            if any(p.is_symlink() for p in (path,*path.parents)):
                raise ExecutionBlocked('canary binding record is aliased')
            info=path.stat()
            if info.st_uid!=self.broker.config['operator_uid'] or info.st_nlink!=1 or info.st_mode&0o022:
                raise ExecutionBlocked('canary binding record ownership is unsafe')
            record=json.loads(path.read_text())
        except (OSError,ValueError) as exc:
            raise ExecutionBlocked('operator canary binding record is unavailable') from exc
        if (record.get('chat_url')!=self.grant['chat_url']
                or binding_identity(record)!=self.grant['binding_identity']):
            raise ExecutionBlocked('canary conversation binding changed')

    def rid(self,task,stage):
        return 'canary-'+self.grant['nonce']+'-'+str(task)+'-'+stage

    def task(self,rid):
        match=re.fullmatch('canary-'+self.grant['nonce']+r'-([12])-(edit(?:-repair)?|test(?:-repair)?|commit|publish|ci)',str(rid))
        if match is None:raise ExecutionBlocked('request is outside the two-task canary budget')
        return int(match[1]),match[2]

    def terminal_execution(self,task,stage):
        rid=self.rid(task,stage)
        intent=self.broker.ledger.intent(self.broker.project.key,rid)
        if (not intent or intent.get('operation')!='execute'
                or intent.get('source_sha')!=self.broker.config['source_sha']):return None
        observed=self.broker.ledger.observe_request(self.broker.project.key,rid,intent.get('request_fingerprint'))
        if observed.get('state')!='terminal':return None
        result=observed['result']
        return result if result.get('source_sha')==self.broker.config['source_sha'] else None

    def repair_gate(self,task,stage):
        if not stage.endswith('-repair'):return
        failed=self.terminal_execution(task,'test')
        if failed is None or type(failed.get('returncode')) is not int or failed['returncode']==0:
            raise ExecutionBlocked('one canary repair requires an original terminal failed test')
        if stage=='test-repair':
            edited=self.terminal_execution(task,'edit-repair')
            if edited is None or edited.get('returncode')!=0 or edited.get('timed_out'):
                raise ExecutionBlocked('repair testing requires its successful confined repair edit')

    def paths(self):
        nonce=self.grant['nonce']
        return ['canary_live_'+nonce+'.py','tests/test_live_canary_'+nonce+'.py']

    def second_ready(self):
        proof=json.loads((self.broker.state/'canary-ci-1.json').read_text())
        ack=json.loads((self.broker.state/'canary-ack-1.json').read_text())
        original=self.broker.ledger.intent(self.broker.project.key,self.rid(1,'publish'))
        if (not original or proof.get('state')!='terminal' or proof.get('conclusion')!='success'
                or proof.get('head_sha')!=original.get('head')
                or proof.get('source_sha')!=self.broker.config['source_sha']
                or ack.get('source_sha')!=self.broker.config['source_sha']
                or ack.get('chat_url')!=self.grant['chat_url']
                or ack.get('binding_identity')!=self.grant['binding_identity']
                or self.rid(1,'publish') not in ack.get('request_ids',[])
                or ack.get('assistant_acknowledged') is not True or ack.get('message_visible') is not True):
            raise ExecutionBlocked('second task lacks exact first-task CI and receipt acknowledgment')
        if not self.ci_acknowledged(1,proof):
            raise ExecutionBlocked('second task requires durable acknowledgment of its exact CI continuation')

    def ci_acknowledged(self,task,proof):
        path=self.broker.state/('canary-ci-ack-'+str(task)+'.json')
        if not path.exists():return False
        ack=json.loads(path.read_text())
        marker='DO_AGAIN_CANARY_CI_'+self.grant['nonce']+'_'+str(task)+'_'+str(proof.get('run_id'))
        return (type(proof.get('run_id')) is int and proof['run_id']>0
                and ack.get('source_sha')==self.broker.config['source_sha']
                and ack.get('chat_url')==self.grant['chat_url']
                and ack.get('binding_identity')==self.grant['binding_identity']
                and ack.get('purpose')=='ci_continuation' and ack.get('batch_marker')==marker
                and ack.get('assistant_acknowledged') is True and ack.get('message_visible') is True)

    def request(self,value,path):
        rid=path.rsplit('/',1)[1][:-5]
        task,stage=self.task(rid)
        operations={'edit':'scratch_script','test':'run_tests','commit':'git_commit',
                    'publish':'git_publish','ci':'ci_observe'}
        if value.get('request_id')!=rid or value.get('operation')!=operations[stage.removesuffix('-repair')]:
            raise ExecutionBlocked('canary request operation or file identity differs from its stage')

    def packet(self,packet):
        operation=packet.get('operation')
        if operation in {'status','probe','worker_register','browser_tick','browser_reconcile',
                         'control_sync','control_reconcile','execution_observe','git_publication_reconcile','canary_reconcile'}:
            return
        if operation=='control_publish':
            path=packet.get('path','')
            if path=='automation/do_again/agent_status.json':return
            match=re.fullmatch(r'automation/do_again/(claims|receipts)/([^/]+)\.json',path)
            if match is None:raise ExecutionBlocked('canary control writes are restricted to its claims and receipts')
            task,stage=self.task(match[2])
            if match[1]=='claims':
                if task==2:self.second_ready()
                self.repair_gate(task,stage)
            return
        if operation=='ci_observe':
            task,stage=self.task(packet.get('request_id'))
            if stage!='publish':raise ExecutionBlocked('canary CI must observe its own publication')
            return
        task,stage=self.task(packet.get('request_id'))
        self.repair_gate(task,stage)
        stage=stage.removesuffix('-repair')
        expected={'edit':'execute','test':'execute','commit':'git_commit','publish':'git_publish'}
        if expected.get(stage)!=operation:raise ExecutionBlocked('canary capability does not match its reserved stage')
        if task==2:self.second_ready()
        python=self.broker.config['python']
        if operation=='execute' and (type(packet.get('timeout')) not in {int,float} or not 0<packet['timeout']<=120):
            raise ExecutionBlocked('canary execution is bounded to 120 seconds')
        if stage=='edit' and (packet.get('argv',[])[:2]!=[python,'-c'] or packet.get('cwd')!='.'):
            raise ExecutionBlocked('canary editing requires the confined Python capability')
        if stage=='test' and packet.get('argv') != [python,'-m','unittest','discover','-s','tests','-p',
                                                  'test_live_canary_'+self.grant['nonce']+'.py']:
            raise ExecutionBlocked('canary testing requires its exact discovered regression')
        if stage=='commit':
            if packet.get('paths')!=self.paths():raise ExecutionBlocked('canary commit cannot change other files')
            stage='test-repair' if self.broker.ledger.intent(self.broker.project.key,self.rid(task,'edit-repair')) else 'test'
            tested=self.terminal_execution(task,stage)
            if tested is None or tested.get('returncode')!=0 or tested.get('timed_out'):
                raise ExecutionBlocked('canary test has not passed')

    def export(self,export):
        entries=export.get('entries',[])
        if (len(entries)!=2 or sorted(e.get('path') for e in entries)!=sorted(self.paths())
                or any(e.get('sha') is None or e.get('mode')!='100644' for e in entries)):
            raise ExecutionBlocked('canary publication may only contain its two regular synthetic files')

    def ci(self,result,request_id):
        task,stage=self.task(request_id)
        if stage=='publish' and result.get('state')=='terminal' and result.get('conclusion')=='success':
            atomic_json(self.broker.state/('canary-ci-'+str(task)+'.json'),
                        dict(result,source_sha=self.broker.config['source_sha']))

    def browser(self,result):
        for ack in result.get('acknowledgments',[]):
            if (ack.get('chat_url')!=self.grant['chat_url'] or ack.get('binding_identity')!=self.grant['binding_identity']
                    or ack.get('assistant_acknowledged') is not True or ack.get('message_visible') is not True):
                raise ExecutionBlocked('canary acknowledgment binding differs')
            for task in (1,2):
                if self.rid(task,'publish') in ack.get('request_ids',[]):
                    atomic_json(self.broker.state/('canary-ack-'+str(task)+'.json'),
                                dict(ack,source_sha=self.broker.config['source_sha']))
                path=self.broker.state/('canary-ci-'+str(task)+'.json')
                if path.exists():
                    proof=json.loads(path.read_text())
                    marker='DO_AGAIN_CANARY_CI_'+self.grant['nonce']+'_'+str(task)+'_'+str(proof.get('run_id'))
                    if (proof.get('source_sha')==self.broker.config['source_sha']
                            and proof.get('conclusion')=='success'
                            and ack.get('purpose')=='ci_continuation' and ack.get('batch_marker')==marker):
                        atomic_json(self.broker.state/('canary-ci-ack-'+str(task)+'.json'),
                                    dict(ack,source_sha=self.broker.config['source_sha']))
        completed=self.broker.state/'canary-ci-2.json'
        if completed.exists() and (self.broker.state/'canary-ack-2.json').exists():
            proof=json.loads(completed.read_text())
            if (proof.get('source_sha')==self.broker.config['source_sha'] and proof.get('conclusion')=='success'
                    and self.ci_acknowledged(2,proof)):
                self.broker.registry.set_intent(self.broker.project.repo,'maintenance',
                    goal_revision='live-canary-'+self.grant['nonce'])
                atomic_json(self.broker.state/'canary-complete.json',{'source_sha':self.broker.config['source_sha'],
                    'nonce':self.grant['nonce'],'completed_at':time.time(),'production_ready':False})


def create_broker(config,parent):
    from .macos_server import ProjectBroker
    grant,_,project=scope(config)
    # Scope is derived from immutable installation configuration, not packets.
    child_config=dict(config,projects=[project,*[p for p in config['projects']
                     if p['account'] in {'_doagain_jp','_doagain_so'}]],dependency_artifacts={})
    child=ProjectBroker(child_config,project)
    child.canary=CanaryAuthority(child,parent,grant)
    parent.live_canary_child=child
    @contextmanager
    def admission():
        with parent.admission():
            child.canary.parent_check()
            with ProjectBroker.admission(child):yield
    child.admission=admission
    return child


def activate(broker):
    from .macos_server import verify_installation
    with broker.lock,broker.admission():
        verify_installation(broker.config)
        status=broker.registry.status(broker.project.repo)
        if status['intent']!='maintenance' or not broker._verified() or broker.ledger.pending(broker.project.key):
            raise ExecutionBlocked('canary activation requires its verified quiescent scope')
        path=broker.state/'canary-activation.json'
        if path.exists():raise ExecutionBlocked('canary has already been activated; reconcile rather than replay')
        epoch=status['epoch']+1
        atomic_json(path,{'source_sha':broker.config['source_sha'],'nonce':broker.canary.grant['nonce'],
                          'epoch':epoch,'started_at':time.time()})
        actual=broker.registry.set_intent(broker.project.repo,'active',goal_revision=status['goal_revision'])
        if actual!=epoch:raise ExecutionBlocked('canary activation epoch changed')
        return {'epoch':epoch,'production_ready':False,'scope':'live_canary'}


def reconcile(broker):
    from .control_history import reconcile_control
    from .publication import reconcile_publication
    from .browser_broker import reconcile_browser
    pending=broker.ledger.pending(broker.project.key)
    if len(pending)>16:raise ExecutionBlocked('canary reconciliation exceeds its bound')
    results=[]
    for row in pending:
        rid=row['request_id'];intent=broker.ledger.intent(broker.project.key,rid) or {}
        handler={'control_publish':reconcile_control,'git_publish':reconcile_publication,
                 'browser_tick':reconcile_browser}.get(intent.get('operation'))
        if handler is None:
            results.append({'request_id':rid,'state':'post_dispatch_uncertain','replay':False});continue
        op={'control_publish':'control_reconcile','git_publish':'git_publication_reconcile',
            'browser_tick':'browser_reconcile'}[intent['operation']]
        results.append(handler(broker,{'operation':op,'request_id':rid}))
    return {'observations':results,'replay':False,'remaining':broker.ledger.pending(broker.project.key)}

def objective(config):
    grant,_,project=scope(config);nonce=grant['nonce']
    code='canary_live_'+nonce+'.py';test='tests/test_live_canary_'+nonce+'.py'
    return f"""Explicitly authorized Do Again live canary. Production is DISABLED.
Repository: Tran-Steven/do-again. Control branch: {project['control_branch']}.
Initial worktree HEAD: {grant['baseline']}. Two synthetic tasks only.
Use the ordinary Do Again JSON request/receipt workflow through GitHub. You,
the ChatGPT agent, must author the coding requests; the confined worker must
execute them, test them, commit and open the draft PR through its broker.
Do not perform development or publication outside that worker.

Task 1: create {code} defining canonical_label(text): lower-case ASCII input,
trim and collapse whitespace into hyphens. Create {test} with unittest
regressions for case, whitespace and empty input. No application/person data.
Task 2: after broker CI continuation and exact receipt acknowledgment, extend
the same function to reject non-ASCII input with ValueError and extend the
same test file. Preserve task 1 behavior. Stop after these two tasks.

Issue one request at a time under automation/do_again/requests, with normal
schema_version=1, fresh issued_at_utc/expires_at_utc, and the expected repo_head
from the last receipt's authority_after. Initial expected head is above.
Allowed request IDs: canary-{nonce}-1-edit, -1-test, -1-commit, -1-publish,
-1-ci, and the corresponding five -2- IDs. After an original terminal failed
test only, one repair pair is permitted per task: -1-edit-repair and
-1-test-repair (or -2-edit-repair and -2-test-repair). Repair uses new identities;
never modify or replay a consumed request. No other replacement or retry IDs.
Uncertain/started operations do not authorize repair. If repair testing fails,
stop and report the missing evidence; do not repeat another equivalent attempt.
edit: scratch_script, Python, cwd '.', timeout <=120 seconds, writing only
{code} and {test}. Use actual source line breaks, validate source using compile
before writing, and avoid double-escaping nested JSON/Python strings. The model
must author both implementation and tests; do not infer success from writing.
test: run_tests, discover=true, start_directory='tests',
pattern='test_live_canary_{nonce}.py', timeout <=120 seconds. commit: git_commit,
paths=[{code!r},{test!r}], message explaining this synthetic change. publish:
git_publish with title/body only; broker creates/updates a DRAFT PR, never main.
ci: ci_observe, args.original_request_id equal the task's -publish request ID.

Read every matching receipt and handle failure without replaying uncertainty.
Acknowledge each delivery using its exact acknowledgment token. Publication
is not CI success. If CI is waiting, wait for the normal durable CI continuation;
do not create polling requests or more tasks. Continue task 2 autonomously
when broker-verified CI and first receipt acknowledgment permit it.
If the GitHub request-writing tool is unavailable, state that exact missing
capability rather than pretending to publish requests.
"""
