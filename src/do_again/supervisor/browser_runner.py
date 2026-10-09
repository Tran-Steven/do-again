"""Immutable trusted operator helper. Arguments are selected only by root broker."""
import json
import sys
from pathlib import Path


def main():
    from ..service.daemon import _drain_browser_outbox, _pending_outbox
    repo,state,control=map(Path,sys.argv[1:4])
    ci=json.loads(sys.argv[4])
    from ..browser.errors import BrowserAuthRequired, BrowserSubmissionUncertain
    from ..core.schema import atomic_json
    try:
        delivered=_drain_browser_outbox(repo,state,daemon_pid=int(sys.argv[5]))
    except (BrowserSubmissionUncertain, BrowserAuthRequired) as exc:
        # The helper finished and the durable outbox remains authoritative.
        # Record a blocked observation, not a successful delivery or a lost
        # helper result. Subsequent ticks reconcile read-only without replay.
        outcome='waiting_for_human' if isinstance(exc,BrowserAuthRequired) else 'awaiting_ack'
        atomic_json(state/'attention.json',{'state':outcome,'reason':type(exc).__name__,
            'action':'Inspect the exact original conversation; do not replay uncertain messages'})
        print(json.dumps({'state':'completed','delivered':0,'liveness':outcome,
            'delivery_acknowledged':False,'outbox_preserved':True}),flush=True)
        return
    from ..service.liveness import check_liveness
    from ..core.schema import atomic_json
    def attention(repo,config,value,reason='idle'):
        # Local durable, redacted attention; no host gh or uncertain issue retry.
        record=dict(value,issue_report='local_attention',attention_reason=reason)
        atomic_json(state/'attention.json',{'state':record.get('state'),'reason':reason,
                    'action':'Inspect the original task/delivery evidence; do not replay uncertain effects'})
        return record
    outcome='delivery_acknowledged'
    if not delivered and not _pending_outbox(state):
        outcome=check_liveness(repo,control,state,ci_probe=lambda binding:ci,reporter=attention,
            settings={'continuous':True,'report_stalls':False,'issue_repo':'',
                      'idle_seconds':60,'recovery_seconds':300})
    print(json.dumps({'state':'completed','delivered':delivered,'liveness':outcome}),flush=True)
