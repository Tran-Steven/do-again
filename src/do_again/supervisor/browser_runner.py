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
    if ci.get('canary_nonce') and ci.get('state')=='terminal' and ci.get('task')==1:
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
    from ..browser.errors import BrowserAuthRequired, BrowserSubmissionUncertain
    try:
        result = _tick()
    except (BrowserAuthRequired, BrowserSubmissionUncertain) as exc:
        outcome='waiting_for_human' if isinstance(exc,BrowserAuthRequired) else 'awaiting_ack'
        atomic_json(Path(sys.argv[2])/'attention.json',{'state':outcome,'reason':type(exc).__name__,
            'action':'Inspect the original event and bound conversation; do not replay'})
        result={'state':'completed','delivered':0,'liveness':outcome,
                'delivery_acknowledged':False,'outbox_preserved':True}
    # The fixed sealed helper has no effects after this durable terminal receipt.
    # Loss of stdout can therefore be reconciled by the broker without CDP access.
    atomic_json(Path(sys.argv[2]) / 'browser_tick_receipts' / (request_id + '.json'),
        {'schema_version':1,'request_id':request_id,'source_sha':source,'epoch':epoch,'result':result})
    print(json.dumps(result),flush=True)
