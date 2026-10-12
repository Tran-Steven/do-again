"""Confined existing Codex CLI payload; hosted authority is never inferred.

No login/quota probes or discovery fallback. Authenticated administrator pins
native payload and endpoints; offline qualification executes help only.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from ..core.schema import canonical_json, parse_utc, atomic_json
from ..model_transport import _response_schema_valid, _validate_response
from .macos_execution import ExecutionBlocked, INSTALL_ROOT, EXECUTION_ROOT, private_root_file


def helper_profile(binary,work,auth=None,endpoints=(),runtime=None,resolvers=()):
    def quoted(p):return json.dumps(str(p))
    rules=['(version 1)','(deny default)','(allow process-fork)',
        '(allow process-exec (literal '+quoted(binary)+'))','(allow sysctl-read)',
        '(allow file-read-metadata)',
        '(allow file-read* (subpath "/System") (subpath "/usr/lib") (subpath "/private/var/db/dyld") (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal '+quoted(binary)+') (subpath '+quoted(work)+'))',
        '(allow file-write* (subpath '+quoted(work)+'))']
    if runtime is not None:
        rules.extend(['(allow process-exec (literal '+quoted(runtime)+'))',
            '(allow file-read* (subpath '+quoted(Path(runtime).parent.parent)+') (subpath '+quoted(INSTALL_ROOT/'current/package')+'))'])
    if auth is not None:rules.append('(allow file-read-data (literal '+quoted(auth)+'))')
    if resolvers:
        rules.extend(['(allow mach-lookup (global-name "com.apple.mDNSResponder"))',
            '(allow file-read-data (literal "/private/etc/resolv.conf") (literal "/private/etc/hosts"))'])
    for resolver in resolvers:
        address=ipaddress.ip_address(resolver)
        if address.version!=4 or address.is_unspecified or address.is_multicast:
            raise ExecutionBlocked('DNS resolver must be an explicitly approved IPv4 address')
        for protocol in ('udp','tcp'):
            rules.append('(allow network-outbound (remote '+protocol+' '+json.dumps(str(address)+':53')+'))')
    for endpoint in endpoints:
        address=ipaddress.ip_address(endpoint)
        if address.version!=4 or address.is_private or address.is_loopback or address.is_reserved:
            raise ExecutionBlocked('hosted endpoints require approved public IPv4 addresses')
        rules.append('(allow network-outbound (remote tcp '+json.dumps(str(address)+':443')+'))')
    return '\n'.join(rules)


def bounded_process(argv, *, work, environment, uid=None,gid=None,timeout=120,input_text=None):
    options={'cwd':work,'env':environment,'stdin':subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        'stdout':subprocess.PIPE,'stderr':subprocess.PIPE,'start_new_session':True,'close_fds':True,'umask':0o077}
    if uid is not None:options.update(user=uid,group=gid,extra_groups=[])
    # Stream to bounded temporary files, not unbounded memory pipes.
    with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
        options.update(stdout=stdout,stderr=stderr)
        process=subprocess.Popen(argv,**options)
        try:
            process.communicate(None if input_text is None else input_text.encode(),timeout=timeout)
            stdout.seek(0);stderr.seek(0)
            out=stdout.read(65537);err=stderr.read(65537)
            if len(out)>65536 or len(err)>65536:raise ExecutionBlocked('Codex helper output exceeded budget')
            return {'returncode':process.returncode,'stdout':out.decode(errors='replace'),'stderr':err.decode(errors='replace')}
        except subprocess.TimeoutExpired:
            raise ExecutionBlocked('Codex helper timed out; original call remains uncertain') from None
        finally:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=5)


class SealedCodexHelper:
    def __init__(self,config,state,admission):
        self.config,self.state,self.admission=config,state,admission
        grant=config.get('hosted_planning_scope')
        if not isinstance(grant,dict):raise ExecutionBlocked('separate hosted scope required before constructing Codex backend')
        self.binary=INSTALL_ROOT/'current/runtimes/codex/bin/codex'
        self.validate(grant,datetime.now(timezone.utc))
        proof_path=state/'codex-helper-qualification.json'
        private_root_file(proof_path)
        proof=json.loads(proof_path.read_text())
        if (proof.get('source_sha')!=config['source_sha'] or proof.get('payload_sha256')!=grant['payload_sha256']
                or proof.get('state')!='qualified_offline' or proof.get('hosted_calls')!=0
                or proof.get('network_denied') is not True or proof.get('execution_uid')!=501):
            raise ExecutionBlocked('native hosted helper lacks installed offline qualification')

    def validate(self,grant,now):
        validate_hosted_scope(self.config,grant,now)
        private_root_file(self.binary)
        if hashlib.sha256(self.binary.read_bytes()).hexdigest()!=grant['payload_sha256']:
            raise ExecutionBlocked('native Codex payload differs')
        return grant

    def authorize(self,now):
        with self.admission():return self.validate(self.config['hosted_planning_scope'],now)

    def transport(self,prompt,schema,*,call,grant):
        if not _response_schema_valid(schema) or len(prompt.encode())>32768:
            raise ExecutionBlocked('Codex helper input outside approved schema')
        with self.admission():self.validate(grant,datetime.now(timezone.utc))
        # Root-controlled parent; only the per-call directory is writable by operator.
        project=next(p for p in self.config['projects'] if p['account']=='_doagain_jp')
        root=EXECUTION_ROOT/project['key']/'planner-work';root.mkdir(mode=0o711,exist_ok=True)
        work=root/call['call_id'];work.mkdir(mode=0o700) # Never reuse an uncertain call directory.
        os.chown(work,grant['execution_uid'],grant['execution_gid'])
        auth=Path(self.config['operator_home'])/'.codex/auth.json'
        info=auth.lstat()
        if auth.is_symlink() or info.st_uid!=grant['execution_uid'] or info.st_mode&0o077 or info.st_nlink!=1:
            raise ExecutionBlocked('existing Codex authentication is not private')
        home=work/'home';home.mkdir(mode=0o700);os.chown(home,grant['execution_uid'],grant['execution_gid'])
        (home/'auth.json').symlink_to(auth) # Existing credential; no copied secret.
        schema_file=work/'schema.json';answer=work/'answer.json'
        provider_schema=json.loads(canonical_json(schema))
        for rules in provider_schema['properties'].values():rules.pop('maxLength')
        schema_file.write_bytes(canonical_json(provider_schema));schema_file.chmod(0o644)
        argv=[str(self.binary),'--ask-for-approval','never','--disable','shell_tool',
            '-c','web_search="disabled"','-c','mcp_servers={}','-c','features.multi_agent=false',
            '-c','features.js_repl=false','-c','features.apply_patch_freeform=false',
            'exec','--sandbox','read-only','--skip-git-repo-check','--ephemeral',
            '--model',grant['model'],'--output-schema',str(schema_file),'--output-last-message',str(answer),'-']
        runtime=self.config['python']
        shim=helper_shim(hosted=True)
        spec=['/usr/bin/sandbox-exec','-p',helper_profile(self.binary,work,auth,grant['network_endpoints'],runtime,grant['dns_resolvers']),
            runtime,'-I','-S','-B','-c',shim,*argv]
        with self.admission():
            result=bounded_process(spec,work=work,environment={'HOME':str(home),'CODEX_HOME':str(home),
                'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'},uid=grant['execution_uid'],
                gid=grant['execution_gid'],timeout=grant['max_call_seconds'],input_text=prompt)
        if result['returncode']!=0:raise ExecutionBlocked('Codex helper failed; original reserved call not retried')
        info=answer.lstat()
        if answer.is_symlink() or not answer.is_file() or info.st_nlink!=1 or info.st_size>65536:
            raise ExecutionBlocked('Codex answer missing, aliased or oversized')
        value=_validate_response(json.loads(answer.read_bytes()),schema)
        atomic_json(self.state/(call['call_id']+'-result.json'),{'call_id':call['call_id'],
            'state':'terminal','model':grant['model'],'payload_sha256':grant['payload_sha256'],
            'answer_sha256':hashlib.sha256(canonical_json(value)).hexdigest(),
            'finished_at_utc':datetime.now(timezone.utc).isoformat()})
        return value

def validate_hosted_scope(config,grant,now):
    from ..service.offline_continuation import digest
    scope=config['controller_scope']
    if grant is None:
        raise ExecutionBlocked('codex-cli requires a separate approved hosted planner grant')
    if isinstance(grant,dict) and ('network_endpoints' in grant or 'dns_resolvers' in grant):
        endpoints=grant.get('network_endpoints')
        resolvers=grant.get('dns_resolvers')
        if endpoints==[] or resolvers==[]:
            raise ExecutionBlocked('hosted planner requires explicitly approved public IPv4 endpoints and DNS resolvers; empty allowlists cannot activate autonomous development')
    required={'provider','transport','model','execution_uid','execution_gid','backend_sha256',
        'source_sha','goal_sha256','epoch','issued_at_utc','expires_at_utc',
        'max_calls_per_cycle','max_total_calls','max_call_seconds','max_total_seconds',
        'max_prompt_bytes','max_answer_bytes','worker_network_access','browser_messages',
        'application_submissions','network_endpoints','dns_resolvers','payload_sha256'}
    if (not isinstance(grant,dict) or set(grant)!=required or grant['source_sha']!=config['source_sha']
            or grant['goal_sha256']!=scope['goal_sha256'] or grant['epoch']!=scope['epoch']
            or grant['provider']!='openai' or grant['transport']!='codex-cli' or grant['model']!='gpt-6.1-sol'
            or grant['execution_uid']!=config['operator_uid'] or grant['execution_uid']!=501
            or grant['execution_gid']!=config['operator_gid']
            or any(grant[k]!=v or type(grant[k]) is not int for k,v in {
                'max_calls_per_cycle':1,'max_total_calls':3,'max_call_seconds':120,
                'max_total_seconds':360,'max_prompt_bytes':32768,'max_answer_bytes':65536}.items())
            or any(grant[k] is not False for k in ('worker_network_access','browser_messages','application_submissions'))
            or not isinstance(grant['network_endpoints'],list) or not 1<=len(grant['network_endpoints'])<=16
            or not isinstance(grant['dns_resolvers'],list) or not 1<=len(grant['dns_resolvers'])<=4):
        raise ExecutionBlocked('hosted scope identity/capabilities differ')
    issued=parse_utc(grant['issued_at_utc']);expires=parse_utc(grant['expires_at_utc'])
    if not issued<=now<expires or not 0<(expires-issued).total_seconds()<=7200:
        raise ExecutionBlocked('hosted grant expired or overlong')
    descriptor={'payload_sha256':grant['payload_sha256'],'helper':'confined-codex-v1',
                'source_sha':config['source_sha'],'network_endpoints':grant['network_endpoints'],'dns_resolvers':grant['dns_resolvers']}
    if digest(descriptor)!=grant['backend_sha256']:raise ExecutionBlocked('sealed helper descriptor differs')
    helper_profile('/sealed/codex','/sealed/work', endpoints=grant['network_endpoints'],resolvers=grant['dns_resolvers'])
    return grant



def helper_shim(hosted=False):
    return ('import sys,os,resource;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
            'from do_again.supervisor.execution_runner import kernel_groups,revoke_bootstrap_capabilities;'
            'assert os.getuid()==501 and os.geteuid()==501 and not (kernel_groups()-{os.getgid()});'
            +('' if hosted else 'revoke_bootstrap_capabilities();')+
            'resource.setrlimit(resource.RLIMIT_CORE,(0,0));'
            'resource.setrlimit(resource.RLIMIT_FSIZE,(65536,65536));'
            'resource.setrlimit(resource.RLIMIT_CPU,(120,120));'
            'resource.setrlimit(resource.RLIMIT_NOFILE,(128,128));'
            'resource.setrlimit(resource.RLIMIT_NPROC,(8,8));os.execv(sys.argv[1],sys.argv[1:])')


def qualify_codex_helper(config,state):
    """Administrator-maintenance readiness probe: fixed help, zero hosted calls."""
    import sys
    from .macos_server import verify_installation
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('native helper qualification requires administrator-installed runtime')
    verify_installation(config)
    grant=config.get('hosted_planning_scope')
    if grant is None:return
    validate_hosted_scope(config,grant,datetime.now(timezone.utc))
    binary=INSTALL_ROOT/'current/runtimes/codex/bin/codex';private_root_file(binary)
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=grant['payload_sha256']:
        raise ExecutionBlocked('qualification payload differs')
    project=next(p for p in config['projects'] if p['account']=='_doagain_jp')
    parent=EXECUTION_ROOT/project['key']/'planner-work';parent.mkdir(mode=0o711,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='qualification-',dir=parent) as directory:
        work=Path(directory);os.chown(work,grant['execution_uid'],grant['execution_gid'])
        runtime=config['python']
        argv=['/usr/bin/sandbox-exec','-p',helper_profile(binary,work,runtime=runtime),
            runtime,'-I','-S','-B','-c',helper_shim(hosted=True),str(binary),'exec','--help']
        result=bounded_process(argv,work=work,environment={'HOME':str(work),'CODEX_HOME':str(work),'PATH':'/usr/bin:/bin'},
            uid=grant['execution_uid'],gid=grant['execution_gid'],timeout=15)
        if result['returncode']!=0 or any(option not in result['stdout'] for option in
            ('--ephemeral','--output-schema','--output-last-message')):
            raise ExecutionBlocked('native helper offline qualification failed')
        atomic_json(Path(state)/'codex-helper-qualification.json',{
            'state':'qualified_offline','source_sha':config['source_sha'],'payload_sha256':grant['payload_sha256'],
            'execution_uid':501,'network_denied':True,'hosted_calls':0,
            'help_sha256':hashlib.sha256(result['stdout'].encode()).hexdigest(),
            'qualified_at_utc':datetime.now(timezone.utc).isoformat()})
