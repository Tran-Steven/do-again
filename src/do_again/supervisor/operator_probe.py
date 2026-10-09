"""Real operator Agent/socket qualification under fixed synthetic authority."""
import json
import os
import signal
import socket
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path
from ..core.schema import atomic_json, canonical_json, utc_now, request_fingerprint
from .macos_execution import INSTALL_ROOT, SOCKET_ROOT, ExecutionBlocked, peer_uid, receive_packet


def run_operator_loop(broker,private,root,control,state,policy,remote,rpc,guard,nonce):
    from .control_history import blob_sha
    from ..core.broker_executor import BrokerExecutor
    from .macos_execution import MacOSProcesses
    operator=broker.config['operator_uid'];group=broker.config['operator_gid']
    # Compatibility files belong to the production operator. Model processes
    # remain confined under the project's distinct execution identity.
    control.parent.chmod(0o711);policy.chmod(0o644)
    for directory in (control,state):
        for path in (directory,*directory.rglob('*')):
            if path.is_symlink():raise ExecutionBlocked('qualification state contains an alias')
            os.chown(path,operator,group);path.chmod(0o700 if path.is_dir() else 0o600)
    address=SOCKET_ROOT/('q-'+broker.project.key[:12]+'-'+nonce+'.sock')
    if address.exists() or address.is_symlink():raise ExecutionBlocked('existing qualification socket is preserved')
    binding=root/'operator-binding.json'
    atomic_json(binding,{'source_sha':broker.config['source_sha'],'nonce':nonce,'repo':str(broker.project.repo),
        'socket':str(address),'control':str(control),'state':str(state),'policy':str(policy)})
    binding.chmod(0o644)
    tasks=[('calculation_operator.py','def total(x,y): return x+y\n'),
        ('test_calculation_operator.py','import unittest,calculation_operator\nclass Test(unittest.TestCase):\n def test_total(self): self.assertEqual(calculation_operator.total(2,3),5)\n'),
        ('README_operator.md','Synthetic operator qualification: tested addition.\n')]
    requests=[]
    for index,(filename,content) in enumerate(tasks):
        now=utc_now();script='from pathlib import Path;Path('+repr(filename)+').write_text('+repr(content)+')'
        if index==2:
            script+=';import unittest;result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName("test_calculation_operator"));assert result.wasSuccessful()'
        requests.append({'schema_version':1,'request_id':'operator-proof-'+nonce+'-'+str(index),
            'issued_at_utc':now.isoformat(),'expires_at_utc':(now+timedelta(minutes=10)).isoformat(),
            'operation':'scratch_script','args':{'language':'python','content':script},
            'expected':{'repo_head':(private.project.worktree/'.git/HEAD').read_text().strip()},
            'limits':{'timeout_seconds':20}})
    status=rpc(broker.project.repo,{'operation':'status'})
    packet_builder=BrokerExecutor(repo=broker.project.repo,policy_path=policy,state_dir=state)
    expected={r['request_id']:packet_builder._packet(r,status) for r in requests}
    queued={};started={};completed=set();handoffs=[];errors=[]
    def enqueue(index):
        request=requests[index];data=canonical_json(request)+b'\n';sha=blob_sha(data)
        remote.blobs[sha]=data
        tree=remote.commits[remote.head]['tree']['sha']
        remote.trees[tree]['automation/do_again/requests/'+request['request_id']+'.json']=sha
        private.control_cache=None;queued[request['request_id']]=time.monotonic()
    enqueue(0)
    stop=threading.Event()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address));os.chown(address,operator,group);address.chmod(0o600)
        listener.listen(4);listener.settimeout(0.2)
        def serve():
            while not stop.is_set():
                try:connection,_=listener.accept()
                except socket.timeout:continue
                except OSError:return
                with connection:
                    try:
                        connection.settimeout(30)
                        if peer_uid(connection)!=operator:raise ExecutionBlocked('qualification peer is not the operator')
                        packet=receive_packet(connection)
                        if packet.get('operation') not in {'status','execute','control_sync','control_publish'}:
                            raise ExecutionBlocked('qualification has no administrative interface')
                        with guard():pass
                        if packet['operation']=='execute':
                            rid=packet.get('request_id')
                            if packet!=expected.get(rid):raise ExecutionBlocked('qualification execution differs from the fixed task')
                            started[rid]=time.monotonic();handoffs.append(started[rid]-queued[rid])
                        result=rpc(broker.project.repo,packet)
                        if (packet['operation']=='control_publish' and '/receipts/operator-proof-' in packet['path']
                                and result.get('state')=='succeeded'):
                            value=packet['value'];rid=value.get('request_id')
                            if (rid not in started or value.get('request_fingerprint')!=request_fingerprint(
                                    next(r for r in requests if r['request_id']==rid)) or value.get('state')!='succeeded'):
                                raise ExecutionBlocked('qualification receipt lacks original task evidence')
                            if rid not in completed:
                                completed.add(rid)
                                if len(completed)<3:enqueue(len(completed))
                        response={'ok':True,'result':result}
                    except Exception as exc:
                        errors.append(type(exc).__name__);response={'ok':False,'blocked':True,'error':str(exc)}
                    try:connection.sendall(canonical_json(response)+b'\n')
                    except OSError:pass
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        code=('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
              'from do_again.supervisor.qualification_agent import main;main()')
        def run(once):
            if not once:
                from .operator_service import run_operator_service
                return run_operator_service(broker,root,binding,nonce,guard)
            with guard():
                proc=subprocess.Popen([broker.config['python'],'-I','-S','-B','-c',code,str(binding),
                    *(['--once'] if once else [])],user=operator,group=group,extra_groups=[],
                    cwd=str(INSTALL_ROOT/'current'),env={'PATH':'/usr/bin:/bin','HOME':broker.config['operator_home']},
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,close_fds=True)
            kernel=MacOSProcesses().identity(proc.pid)
            if not kernel or kernel[0]!=operator:
                if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL)
                proc.wait(timeout=10)
                raise ExecutionBlocked('controller kernel identity differs')
            try:stdout,stderr=proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=10)
                raise ExecutionBlocked('operator qualification timed out; fixed fixture cannot replay')
            if proc.returncode or len(stdout)>65536 or len(stderr)>65536:
                raise ExecutionBlocked('operator Agent failed: '+stderr.decode(errors='replace')[-1024:])
            value=json.loads(stdout)
            if value.get('controller_uid')!=operator or value.get('source_sha')!=broker.config['source_sha'] or value.get('engine')!='sealed_daemon':
                raise ExecutionBlocked('operator Agent source or identity evidence differs')
            return value
        try:
            value=run(False)
            if value['tasks']!=3 or len(completed)!=3 or errors or max(handoffs)>30:
                raise ExecutionBlocked('unattended operator task handoff lacks evidence')
            count=len(started)
            replay=run(True)
            if replay['tasks']!=0 or len(started)!=count:
                raise ExecutionBlocked('operator restart repeated a terminal execution')
        finally:
            stop.set();listener.close();thread.join(timeout=2)
            if thread.is_alive():raise ExecutionBlocked('qualification socket thread remains unresolved')
            address.unlink(missing_ok=True)
    return {'controller_uid':operator,'native_execution_uid':private.project.uid,'tasks':3,
        'authenticated_root_socket':True,'unattended_request_handoff':True,
        'handoff_seconds':handoffs,'restart_without_reexecution':True,
        'scope':'synthetic_request_intake','browser_delivery':'not_measured','engine':'sealed_daemon',
        'launchd_service':value['launchd_service'],'service_pid':value['service_pid'],
        'service_withdrawn':value['service_withdrawn']}
