"""Immutable trusted operator helper. Arguments are selected only by root broker."""
import json
import sys
from pathlib import Path


def _tick():
    from ..service.daemon import _drain_browser_outbox, _pending_outbox
    repo,state,control=map(Path,sys.argv[1:4])
    ci=json.loads(sys.argv[4])
    from ..browser.errors import BrowserAuthRequired, BrowserSubmissionUncertain
    from ..core.schema import atomic_json
    guard=None
    if ci.get('canary_nonce'):
        from .immutable_worker import read_worker_configuration
        from .live_canary import scope
        grant,_,project=scope(read_worker_configuration())
        if grant['nonce']!=ci['canary_nonce'] or str(repo)!=project['repo']:
            raise ValueError('canary browser scope differs')
        guard={'chat_url':grant['chat_url'],'binding_identity':grant['binding_identity']}
    try:
        delivered=_drain_browser_outbox(repo,state,daemon_pid=int(sys.argv[5]),binding_guard=guard)
    except (BrowserSubmissionUncertain, BrowserAuthRequired) as exc:
        # The helper finished and the durable outbox remains authoritative.
        # Record a blocked observation, not a successful delivery or a lost
        # helper result. Subsequent ticks reconcile read-only without replay.
        outcome='waiting_for_human' if isinstance(exc,BrowserAuthRequired) else 'awaiting_ack'
        atomic_json(state/'attention.json',{'state':outcome,'reason':type(exc).__name__,
            'action':'Inspect the exact original conversation; do not replay uncertain messages'})
        return {'state':'completed','delivered':0,'liveness':outcome,
            'delivery_acknowledged':False,'outbox_preserved':True}
    from ..service.liveness import check_liveness
    from ..core.schema import atomic_json
    def attention(repo,config,value,reason='idle'):
        # Local durable, redacted attention; no host gh or uncertain issue retry.
        record=dict(value,issue_report='local_attention',attention_reason=reason)
        atomic_json(state/'attention.json',{'state':record.get('state'),'reason':reason,
                    'action':'Inspect the original task/delivery evidence; do not replay uncertain effects'})
        return record
    acknowledgments=[]
    from ..browser import runtime as browser
    record=browser.project_record(repo)
    for path in sorted((state/'browser_delivery_evidence').glob('*.json'))[-20:]:
        evidence=json.loads(path.read_text())
        if (evidence.get('state')=='reconciled' and evidence.get('message_visible') is True
                and evidence.get('assistant_acknowledged') is True
                and evidence.get('chat_url')==record.get('chat_url')
                and evidence.get('binding_identity')==browser.binding_identity(record)
                and evidence.get('acknowledgment_token') and evidence.get('payload_sha256')):
            acknowledgments.append(evidence)
    if ci.get('canary_nonce') and ci.get('state')=='terminal' and ci.get('task') in (1,2):
        from ..service.daemon import _queue_continuation
        rid=ci['publication_request_id']
        if any(rid in ack.get('request_ids',[]) for ack in acknowledgments):
            marker='DO_AGAIN_CANARY_CI_'+ci['canary_nonce']+'_'+str(ci['task'])+'_'+str(ci['run_id'])
            prompt=(marker+': Broker-verified exact-head CI conclusion '+str(ci['conclusion'])+'. '
                'Read the original publication receipt and CI evidence. '
                + ('The first receipt is acknowledged. Continue with the second and final bounded synthetic task now.'
                   if ci['task']==1 and ci['conclusion']=='success' else
                   'Stop at the two-task boundary; report failure if CI is unsuccessful.'))
            _queue_continuation(state,prompt,purpose='ci_continuation',marker=marker,binding=record)
    outcome='delivery_acknowledged'
    if ci.get('canary_nonce'):
        outcome='canary_wait'
    elif not delivered and not _pending_outbox(state):
        outcome=check_liveness(repo,control,state,ci_probe=lambda binding:ci,reporter=attention,
            settings={'continuous':True,'report_stalls':False,'issue_repo':'',
                      'idle_seconds':60,'recovery_seconds':300})
    return {'state':'completed','delivered':delivered,'liveness':outcome,'acknowledgments':acknowledgments}


def _tick_with_root_budget():
    """Leave root's 60s helper timeout enough time to record terminal proof."""
    import signal
    import threading
    if sys.platform != 'darwin' or threading.current_thread() is not threading.main_thread():
        return _tick()
    # This helper runs in a separate, root-pinned operator process. A
    # cancellation at 45s precedes the root broker's hard kill at 60s.
    previous = signal.getsignal(signal.SIGALRM)
    def deadline(_signum, _frame):
        raise TimeoutError('browser helper reached its pre-root deadline')
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 45.0)
    try:
        return _tick()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _pre_dispatch_timeout_result(state: Path) -> dict | None:
    """Only a durable *pre-gesture* intent can be classified as unsent.

    The Send implementation fsyncs dispatch_started through before_dispatch
    before the first mouse gesture. An absent/malformed intent or any
    post-dispatch state is NOT evidence of a non-send. Never touch it.
    """
    p=state/'browser_submission_uncertain.json'
    if p.is_symlink() or not p.is_file():
        return None
    try:
        intent=json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(intent,dict) or intent.get('schema_version')!=1
            or intent.get('state')!='preparing'
            or intent.get('payload_sha256') is not None
            or intent.get('acknowledgment_token') is not None
            or not isinstance(intent.get('request_ids'),list)
            or not 1<=len(intent['request_ids'])<=20
            or any(not isinstance(r,str) or not r for r in intent['request_ids'])):
        return None
    outbox=state/'browser_outbox'
    if outbox.is_symlink() or not outbox.is_dir() or any(
            (outbox/(request_id+'.json')).is_symlink()
            or not (outbox/(request_id+'.json')).is_file()
            for request_id in intent['request_ids']):
        return None
    return {'state':'completed','delivered':0,
            'liveness':'pre_dispatch_blocked',
            'delivery_acknowledged':False,'outbox_preserved':True}


def main():
    # Fixed identity arguments are supplied by the root broker, never scripts.
    from .macos_execution import REQUEST_ID
    from .control_history import SHA
    from ..core.schema import atomic_json
    if len(sys.argv) != 9 or not REQUEST_ID.fullmatch(sys.argv[6]) or not SHA.fullmatch(sys.argv[7]):
        raise ValueError('browser helper requires its original broker identity')
    request_id, source, epoch = sys.argv[6], sys.argv[7], int(sys.argv[8])
    if epoch < 1:
        raise ValueError('browser helper epoch is invalid')
    from ..browser.errors import BrowserAuthRequired, BrowserSubmissionUncertain, BrowserError
    state=Path(sys.argv[2])
    pre_dispatch_timeout=False
    try:
        result = _tick_with_root_budget()
    except (BrowserAuthRequired, BrowserSubmissionUncertain) as exc:
        outcome='waiting_for_human' if isinstance(exc,BrowserAuthRequired) else 'awaiting_ack'
        atomic_json(state/'attention.json',{'state':outcome,'reason':type(exc).__name__,
            'action':'Inspect the original event and bound conversation; do not replay'})
        result={'state':'completed','delivered':0,'liveness':outcome,
                'delivery_acknowledged':False,'outbox_preserved':True}
    except (TimeoutError, BrowserError) as exc:
        # A timeout or unknown browser failure is terminal *only* when the
        # original fsynced journal proves the Send gesture was never begun.
        # Post-dispatch and missing evidence must stay unresolved.
        result=_pre_dispatch_timeout_result(state)
        if result is None:
            raise
        pre_dispatch_timeout=True
        atomic_json(state/'attention.json',{'state':'pre_dispatch_blocked',
            'reason':type(exc).__name__,
            'action':'Browser helper stopped before Send; preserve outbox and verify authentication.'})
    # Durable terminal proof is written BEFORE retiring the preparing record.
    # A crash here leaves the original receipt recoverable through the broker.
    atomic_json(state / 'browser_tick_receipts' / (request_id + '.json'),
        {'schema_version':1,'request_id':request_id,'source_sha':source,'epoch':epoch,'result':result})
    if pre_dispatch_timeout:
        intent=state/'browser_submission_uncertain.json'
        # Only the unique guarded helper can be in this broker-held effect.
        # The receipt is already durable; future ticks can retry *pre-Send*
        # after browser authentication without replaying a previous Send.
        if _pre_dispatch_timeout_result(state) is not None:
            intent.unlink(missing_ok=True)
    print(json.dumps(result),flush=True)
