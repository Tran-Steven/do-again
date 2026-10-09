"""Root-stage installer. Called only after a sealed bundle is authenticated."""
from __future__ import annotations
import argparse
from contextlib import contextmanager, ExitStack
import sqlite3
import grp
import hashlib
import json
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT=Path('/Library/Application Support/DoAgainSupervisor')
EXEC=Path('/private/var/do-again-execution')
LABEL='io.github.tran-steven.do-again.supervisor'
PLIST=Path('/Library/LaunchDaemons')/(LABEL+'.plist')


def run(args, **kwargs):
    result=subprocess.run(args, capture_output=True, text=True, timeout=120, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed ({result.returncode}); installation remains in maintenance')
    return result


def atomic(path, data):
    temporary=path.with_name(path.name+'.pending')
    with temporary.open('w') as stream:
        json.dump(data,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
    temporary.chmod(0o600);os.replace(temporary,path)


def secure_directory(path, mode=0o755):
    for p in (path,*path.parents):
        if p.is_symlink():raise RuntimeError('symlinked installation directory')
        if p.exists() and (p.stat().st_uid!=0 or p.stat().st_mode&0o022):
            raise RuntimeError('installation directory is not root protected')
    path.mkdir(parents=True,exist_ok=True,mode=mode)
    os.chown(path,0,0);os.chmod(path,mode)


def verify_stage(stage):
    manifest=json.loads((stage/'manifest.json').read_text())
    for path in (stage, *stage.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise RuntimeError('stage contains an alias or special file')
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise RuntimeError('stage is not immutable root-owned state')
    actual={str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file()}
    if actual!=set(manifest['files'])|{'manifest.json','MANIFEST.sha256'}:
        raise RuntimeError('unexpected stage files')
    for name,digest in manifest['files'].items():
        p=stage/name
        if Path(name).is_absolute() or '..' in Path(name).parts or p.is_symlink():
            raise RuntimeError('invalid staged path')
        if p.stat().st_uid!=0 or p.stat().st_mode&0o022 or p.stat().st_nlink!=1:
            raise RuntimeError('stage is mutable or aliased')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('staged artifact hash mismatch')
    return manifest


def create_account(project, journal_path, journal):
    name,uid,gid=project['account'],project['uid'],project['gid']
    try: existing=pwd.getpwnam(name)
    except KeyError:existing=None
    if existing:
        known=journal.get('accounts',{}).get(name)
        if known!={'uid':uid,'gid':gid,'created':True}:
            raise RuntimeError('existing execution account lacks installation provenance')
        if (existing.pw_uid,existing.pw_gid,existing.pw_dir,existing.pw_shell)!=(uid,gid,'/var/empty','/usr/bin/false'):
            raise RuntimeError('existing execution account configuration drift')
        if grp.getgrgid(gid).gr_name!=name:raise RuntimeError('execution group drift')
        return
    try:pwd.getpwuid(uid)
    except KeyError:pass
    else:raise RuntimeError('execution UID was allocated after preparation')
    try:grp.getgrgid(gid)
    except KeyError:pass
    else:raise RuntimeError('execution GID was allocated after preparation')
    journal['pending_account']={'name':name,'uid':uid,'gid':gid}
    atomic(journal_path,journal)
    group='/Groups/'+name
    user='/Users/'+name
    for args in ([group], [group,'PrimaryGroupID',str(gid)], [user],
                 [user,'UniqueID',str(uid)], [user,'PrimaryGroupID',str(gid)],
                 [user,'UserShell','/usr/bin/false'], [user,'NFSHomeDirectory','/var/empty'],
                 [user,'Password','*'], [user,'IsHidden','1'], [user,'RealName','Do Again isolated execution']):
        run(['/usr/bin/dscl','.','-create',*args])
    entry=pwd.getpwnam(name)
    if (entry.pw_uid,entry.pw_gid,entry.pw_shell)!=(uid,gid,'/usr/bin/false'):
        raise RuntimeError('new execution identity did not verify')
    journal.setdefault('accounts',{})[name]={'uid':uid,'gid':gid,'created':True}
    journal.pop('pending_account',None);atomic(journal_path,journal)


def provision_worktree(config, project, current):
    target=Path(project['worktree'])
    key=project['key']
    if target!=EXEC/key/'worktree':raise RuntimeError('invalid allocated worktree')
    secure_directory(EXEC)
    secure_directory(target.parent)
    for name in ('requests','probes'):secure_directory(target.parent/name,0o711)
    partial = target.exists()
    if partial:
        if target.is_symlink():raise RuntimeError('existing worktree is aliased')
        if target.stat().st_uid==project['uid']:
            metadata=target/'.git';head=metadata/'HEAD'
            if (metadata.is_symlink() or metadata.stat().st_uid!=config['operator_uid'] or
                    head.is_symlink() or head.stat().st_nlink!=1 or
                    head.read_text().strip()!=project['source_sha']):
                raise RuntimeError('existing workspace snapshot drift; reconciliation required')
            return  # Preserve an already provisioned engineering tree.
        if (target.stat().st_uid!=config['operator_uid'] or
                {p.name for p in target.iterdir()}!={'.git'} or
                (target/'.git').is_symlink() or (target/'.git').stat().st_uid!=config['operator_uid']):
            raise RuntimeError('partial workspace has edits or unknown ownership; reconciliation required')
    else:
        target.mkdir(mode=0o700)
        os.chown(target,config['operator_uid'],config['operator_gid'])
    env={'PATH':'/usr/bin:/bin','HOME':config['operator_home'],
         'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
    options={'user':config['operator_uid'],'group':config['operator_gid'],'extra_groups':[], 'env':env}
    git=['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
         '-c','protocol.file.allow=always','-c','submodule.recurse=false']
    bundle=current/project['bundle']
    if not partial:
        run([*git,'clone','--no-hardlinks','--no-checkout',str(bundle),str(target)],**options)
    # Bundles may advertise remote-tracking refs outside clone's heads refspec.
    # Import the exact approved object before materializing the snapshot.
    run([*git,'-C',str(target),'fetch','--no-tags',str(bundle),project['source_sha']],**options)
    run([*git,'-C',str(target),'checkout','--detach',project['source_sha']],**options)
    observed=run([*git,'-C',str(target),'rev-parse','HEAD'],**options).stdout.strip()
    if observed!=project['source_sha']:raise RuntimeError('workspace source identity mismatch')
    # Source files are writable only by their execution identity. Git metadata
    # remains operator-owned and is separately denied by the native profile.
    for directory,dirs,files in os.walk(target,followlinks=False):
        if Path(directory)==target:dirs[:]=[name for name in dirs if name!='.git']
        for path in [Path(directory),*(Path(directory)/name for name in files)]:
            os.chown(path,project['uid'],project['gid'],follow_symlinks=False)
    target.chmod(0o750)


def verify_worker_quiescence(config):
    """A maintenance runtime cutover never replaces code beneath a live worker."""
    for project in config['projects']:
        journal=ROOT/'state'/project['key']/'worker-deployment.json'
        if journal.exists():
            if journal.is_symlink() or journal.stat().st_uid!=0 or journal.stat().st_nlink!=1:
                raise RuntimeError('worker deployment ownership is invalid')
            record=json.loads(journal.read_text())
            if record.get('phase') not in {'staged','withdrawn'}:
                raise RuntimeError('worker deployment requires guarded withdrawal before cutover')
        label='io.github.tran-steven.do-again.worker.'+project['key'][:12]
        observed=subprocess.run(['/bin/launchctl','print',f"gui/{config['operator_uid']}/{label}"],
            capture_output=True,text=True,timeout=30,user=config['operator_uid'],group=config['operator_gid'],
            extra_groups=[],env={'PATH':'/usr/bin:/bin','HOME':config['operator_home']})
        if observed.returncode==0:
            raise RuntimeError('loaded engineering worker blocks immutable runtime cutover')
        if observed.returncode!=113 or 'Could not find service' not in observed.stderr:
            raise RuntimeError('worker service absence is unproven; cutover deferred')


@contextmanager
def preview_cutover(config, registry, live):
    """Upgrade only this sealed, maintenance-only helper under admission fences.

    Legacy project daemons, browsers, and service definitions are outside this
    cutover. Production helpers require the later guarded rollout procedure.
    """
    with ExitStack() as fences:
        if live:
            import fcntl
            old=json.loads((ROOT/'current/config.json').read_text())
            verify_stage(ROOT/'current')
            fields=('repo','key','uid','gid','account','worktree','source_sha')
            scoped=lambda c:sorted(tuple(p[k] for k in fields) for p in c['projects'])
            if old.get('production_ready') is not False or config.get('production_ready') is not False or scoped(old)!=scoped(config):
                raise RuntimeError('production or changed-scope helper upgrade requires guarded rollout')
            for project in sorted(config['projects'],key=lambda p:p['key']):
                path=ROOT/'state'/project['key']/'admission.lock'
                if path.is_symlink() or path.parent.is_symlink():raise RuntimeError('aliased admission fence')
                file=fences.enter_context(path.open('a'))
                fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Recheck immediately before the service effect, while spawn and
            # operator-intent transitions are excluded by the same fences.
            for project in config['projects']:
                if registry.status(Path(project['repo']))['intent']=='active':
                    raise RuntimeError('authority admits execution; cutover deferred')
            with sqlite3.connect(registry.path) as db:
                if db.execute("SELECT 1 FROM execution WHERE state='started' LIMIT 1").fetchone():
                    raise RuntimeError('ambiguous execution blocks cutover')
            from do_again.supervisor.macos_execution import MacOSProcesses
            inventory=MacOSProcesses()
            if any(inventory.owned(p['uid']) for p in config['projects']):
                raise RuntimeError('dedicated execution still active; cutover deferred')
            verify_worker_quiescence(config)
            run(['/bin/launchctl','bootout','system/'+LABEL])
            stopped=subprocess.run(['/bin/launchctl','print','system/'+LABEL],capture_output=True)
            if stopped.returncode!=113:raise RuntimeError('helper stop is uncertain; cutover deferred')
        yield


def runtime_executables(config, current):
    expected={'python':current/'runtimes/python/bin/python3',
              'git':current/'runtimes/git/bin/git'}
    if any(config.get(key)!=str(path) for key,path in expected.items()):
        raise RuntimeError('invalid sealed runtime executable path')
    return tuple(expected.values())


def install(stage):
    if sys.platform!='darwin' or os.geteuid()!=0:raise RuntimeError('administrator authentication required')
    os.umask(0o077)
    manifest=verify_stage(stage)
    config=json.loads((stage/'config.json').read_text())
    if config['source_sha']!=manifest['source_sha']:raise RuntimeError('configuration source mismatch')
    if len(config['projects'])!=2 or len({p['uid'] for p in config['projects']})!=2:
        raise RuntimeError('two independently scoped execution identities required')
    if any(p['uid'] in (0,config['operator_uid']) for p in config['projects']):raise RuntimeError('invalid execution identity')
    secure_directory(ROOT)
    secure_directory(ROOT/'state',0o700)
    journal_path=ROOT/'state/installation.json'
    existing_installation=journal_path.exists()
    journal=json.loads(journal_path.read_text()) if existing_installation else {'schema_version':1,'accounts':{}}
    if journal.get('pending_account'):raise RuntimeError('partial identity creation requires operator reconciliation')
    # No installer rewrites operator intent, live journals, browser bindings,
    # receipts, configurations, primary checkouts, or excluded project files.
    authority=Path(config['legacy_authority_path'])
    if authority.is_symlink() or authority.stat().st_uid!=config['operator_uid'] or authority.stat().st_mode&0o077:
        raise RuntimeError('operator authority journal is not privately owned')
    current=ROOT/'current'
    live=subprocess.run(['/bin/launchctl','print','system/'+LABEL],capture_output=True)
    if live.returncode not in (0,113):raise RuntimeError('cannot establish supervisor service quiescence')
    if current.exists() and not existing_installation:
        raise RuntimeError('existing package has unknown installation provenance')
    sys.path.insert(0,str(stage/'package'))
    from do_again.supervisor.authority import AuthorityRegistry
    registry=AuthorityRegistry(Path(config['authority_path']),owner_uid=0)
    if existing_installation and not registry.path.exists():
        raise RuntimeError('previous installation lost its effect journal; reconciliation required')
    if registry.path.exists():
        for project in config['projects']:
            if registry.status(Path(project['repo']))['intent'] not in {'maintenance','paused','stopped'}:
                raise RuntimeError('existing authority admits work; upgrade deferred')
    legacy=AuthorityRegistry(authority,owner_uid=config['operator_uid'])
    for project in config['projects']:
        from do_again.supervisor.macos_execution import project_from_dict
        scoped=project_from_dict(project);scoped.validate(config['operator_uid'])
        if scoped.key != project['key'] or project['account'] not in {'_doagain_da','_doagain_jp'}:
            raise RuntimeError('invalid project scope')
        status=legacy.status(Path(project['repo']))
        if status['intent']!='maintenance' or status['goal_revision']!=project['goal_revision']:
            raise RuntimeError('operator admission changed after preparation')
    if config.get('production_ready') is not False:
        raise RuntimeError('initial installation must remain in maintenance')
    if PLIST.exists() and not existing_installation:
        raise RuntimeError('existing service definition has unknown provenance')
    runtimes=runtime_executables(config,current)
    for project in config['projects']:create_account(project,journal_path,journal)
    with preview_cutover(config,registry,live.returncode==0):
        if current.exists():
            # Retain earlier immutable package. Effect journals always remain newer.
            previous=ROOT/('previous-'+uuid.uuid4().hex)
            os.rename(current,previous)
        os.rename(stage,current)
        for runtime in runtimes:runtime.chmod(0o755)
        if not registry.path.exists():
            registry.initialize()
            for project in config['projects']:
                registry.set_intent(Path(project['repo']),'maintenance',goal_revision=project['goal_revision'])
        else:
            for project in config['projects']:
                if registry.status(Path(project['repo']))['intent'] not in {'maintenance','paused','stopped'}:
                    raise RuntimeError('existing authority admits work; upgrade deferred')
        for project in config['projects']:provision_worktree(config,project,current)
        definition={'Label':LABEL,'ProgramArguments':[config['python'],'-I','-S','-B',str(current/'bootstrap.py')],
                    'UserName':'root','RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},
                    'WorkingDirectory':str(current),'EnvironmentVariables':{'PATH':'/usr/bin:/bin'},
                    'StandardOutPath':str(ROOT/'state/helper.out.log'),
                    'StandardErrorPath':str(ROOT/'state/helper.err.log')}
        temporary=PLIST.with_suffix('.pending')
        temporary.write_bytes(plistlib.dumps(definition));temporary.chmod(0o644)
        os.chown(temporary,0,0);os.replace(temporary,PLIST)
        run(['/bin/launchctl','bootstrap','system',str(PLIST)])
        journal['source_sha']=config['source_sha'];journal['status']='installed_maintenance'
        atomic(journal_path,journal)
        print('SUPERVISOR_INSTALLED_MAINTENANCE')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--stage',type=Path,required=True)
    args=parser.parse_args();install(args.stage.resolve())
