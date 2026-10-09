"""Immutable trusted operator helper. Arguments are selected only by root broker."""
import json
import sys
from pathlib import Path


def main():
    from ..service.daemon import _drain_browser_outbox, _pending_outbox
    repo,state,control=map(Path,sys.argv[1:4])
    ci=json.loads(sys.argv[4])
    delivered=_drain_browser_outbox(repo,state,daemon_pid=int(sys.argv[5]))
    from ..service.liveness import check_liveness
    from ..core.schema import atomic_json
    def attention(repo,config,value,reason='idle'):
        # Local durable, redacted attention; no host gh or uncertain issue retry.
        record=dict(value,issue_report='local_attention',attention_reason=reason)
        atomic_json(state/'attention.json',{'state':record.get('state'),'reason':reason,
                    'action':'Inspect the original task/delivery evidence; do not replay uncertain effects'})
        return record
    outcome='delivery_visible'
    if not delivered and not _pending_outbox(state):
        outcome=check_liveness(repo,control,state,ci_probe=lambda binding:ci,reporter=attention,
            settings={'continuous':True,'report_stalls':False,'issue_repo':'',
                      'idle_seconds':60,'recovery_seconds':300})
    print(json.dumps({'state':'completed','delivered':delivered,'liveness':outcome}),flush=True)
