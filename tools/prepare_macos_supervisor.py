"""Build a private, hash-sealed installation bundle without administrator access."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def git(repo, *args):
    return subprocess.check_output(['git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-C',str(repo),*args],text=True).strip()


def require_fresh_sonary_snapshot(repo: Path) -> str:
    """Reject a stale remote-tracking ref before constructing a private bundle.

    This check is read-only. It never fetches, checks out, or modifies the
    operator's Sonary worktree, existing PR branches, or stored changes.
    """
    local=git(repo,'rev-parse','refs/remotes/origin/main')
    remote=git(repo,'ls-remote','origin','refs/heads/main')
    match=re.fullmatch(r'([0-9a-f]{40})\trefs/heads/main',remote)
    if not re.fullmatch(r'[0-9a-f]{40}',local) or match is None:
        raise ValueError('Sonary remote main identity is unavailable; refusing enrollment')
    if local!=match.group(1):
        raise ValueError('Sonary origin/main is stale; fetch origin main before preparing enrollment')
    return local


def require_fresh_jobpipe_snapshot(repo: Path, *, pinned_sha: str | None = None) -> str:
    """Fail before bundling if jobpipe's protected source is stale.

    A maintenance-only package upgrade intentionally keeps project worktrees
    pinned. It does NOT migrate the trusted worker snapshot or rewrite journals.
    Never present such a bundle as an activated jobpipe development release.
    """
    local=git(repo,'rev-parse','refs/remotes/origin/main')
    remote=git(repo,'ls-remote','origin','refs/heads/main')
    matched=re.fullmatch(r'([0-9a-f]{40})\trefs/heads/main',remote)
    if (not re.fullmatch('[0-9a-f]{40}',local) or matched is None
            or local!=matched.group(1)):
        raise ValueError('jobpipe origin/main is stale or unavailable; fetch main and retry preflight')
    if pinned_sha is not None and (not re.fullmatch('[0-9a-f]{40}',pinned_sha)
                                    or pinned_sha!=local):
        raise ValueError('jobpipe protected snapshot is stale: pinned '+str(pinned_sha)+
                         ', origin/main '+local+
                         '; maintenance-only upgrade cannot migrate it. '
                         'Reconcile the protected snapshot before activation; '
                         'do not overwrite the worktree or resume the worker')
    return local


def available_ids(count=2):
    occupied=set()
    for category,attribute in (('Users','UniqueID'),('Groups','PrimaryGroupID')):
        result=subprocess.check_output(['/usr/bin/dscl','.','-list','/'+category,attribute],text=True)
        for line in result.splitlines():
            try:occupied.add(int(line.split()[-1]))
            except (ValueError,IndexError):continue
    free=[value for value in range(400,500) if value not in occupied]
    if len(free)<count:raise ValueError('no collision-free execution identities available')
    return free[:count]


def project_key(repo):
    return hashlib.sha256(os.fsencode(repo.resolve())).hexdigest()


def seal_runtime(payload: Path) -> str:
    if sys.platform != 'darwin':raise ValueError('macOS runtime preparation required')
    base=Path(sys.base_prefix).resolve()
    if not (base/'Python').is_file():raise ValueError('framework Python runtime required')
    root=payload/'runtimes/python';(root/'bin').mkdir(parents=True)
    shutil.copyfile(base/'Resources/Python.app/Contents/MacOS/Python',root/'bin/python3')
    (root/'bin/python3').chmod(0o755)
    shutil.copyfile(base/'Python',root/'Python')
    shutil.copytree(base/'lib',root/'lib',symlinks=False,
                    ignore=shutil.ignore_patterns('site-packages','__pycache__','*.pyc','pkgconfig','_tkinter*','tkinter','idlelib'))
    # Resolve only packaged framework dependencies; reject any other host library.
    native=[root/'bin/python3',root/'Python',*root.rglob('*.dylib'),*root.rglob('*.so')]
    for binary in native:
        deps=subprocess.check_output(['/usr/bin/otool','-L',str(binary)],text=True)
        changes=[]
        for line in deps.splitlines():
            if not line.startswith('\t'):continue
            dependency=line.strip().split(' (',1)[0]
            if dependency.startswith(str(base)+'/'):
                target=root/Path(dependency).relative_to(base)
                if not target.is_file():raise ValueError('unpackaged native dependency: '+dependency)
                new='@loader_path/'+os.path.relpath(target,binary.parent)
                changes.extend(['-change',dependency,new])
            elif not dependency.startswith(('/usr/lib/','/System/','@loader_path/')):
                raise ValueError('untrusted native runtime dependency')
        if changes:
            subprocess.run(['/usr/bin/install_name_tool',*changes,str(binary)],check=True,capture_output=True)
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(binary)],check=True,capture_output=True)
    result=subprocess.check_output([str(root/'bin/python3'),'-I','-S','-B','-c',
        'import sys,sqlite3,ctypes,socket,subprocess,ssl,resource;print(sys.base_prefix)'],text=True).strip()
    if Path(result).resolve()!=root.resolve():raise ValueError('runtime relocation proof failed')
    return '/Library/Application Support/DoAgainSupervisor/current/runtimes/python/bin/python3'


def snapshot_bundle(repo: Path, sha: str, destination: Path) -> None:
    # Advertise an ordinary, exact snapshot head without mutating source refs.
    with tempfile.TemporaryDirectory() as directory:
        staging=Path(directory)/'snapshot.git'
        git(repo,'init','--bare',str(staging))
        git(staging,'-c','protocol.file.allow=always','fetch','--no-tags',str(repo),sha)
        git(staging,'update-ref','refs/heads/snapshot',sha)
        git(staging,'bundle','create',str(destination),'refs/heads/snapshot')


def seal_git(payload: Path) -> str:
    """Copy the Apple-signed Git binary; no developer-tool launcher at execution."""
    binary=Path(subprocess.check_output(['/usr/bin/xcrun','--find','git'],text=True).strip()).resolve(strict=True)
    approved=(binary==Path('/Library/Developer/CommandLineTools/usr/bin/git') or
              (len(binary.parts)==8 and binary.parts[1]=='Applications'
               and re.fullmatch(r'Xcode(?:_[A-Za-z0-9.]+)?\.app',binary.parts[2])
               and binary.parts[3:]==('Contents','Developer','usr','bin','git')))
    if not binary.is_absolute() or not approved:
        raise ValueError('Git must come from the approved Apple developer tools')
    for path in (binary,*binary.parents):
        if path.is_symlink():
            raise ValueError('Git toolchain contains a path alias')
    if not binary.is_file() or binary.stat().st_nlink!=1:
        raise ValueError('Git executable is not an unaliased regular file')
    target=payload/'runtimes/git/bin/git'
    target.parent.mkdir(parents=True)
    shutil.copyfile(binary,target)
    target.chmod(0o755)
    # Verify the copied bytes, eliminating a source-change race. Apple identity
    # is authoritative even on hosted runners whose Xcode directory is user-owned.
    try:
        subprocess.run(['/usr/bin/codesign','--verify','--strict','-R',
                        '=anchor apple and identifier \"com.apple.git\"',str(target)],
                       check=True,capture_output=True)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return '/Library/Application Support/DoAgainSupervisor/current/runtimes/git/bin/git'


def require_planner_grants_before_staging(controller_grant, hosted_planner_grant, codex_native_payload):
    """Fail before packaging a runtime when the requested planner has no authority."""
    if controller_grant is None:
        return
    path=Path(controller_grant)
    if path.is_symlink() or not path.is_file():
        raise ValueError('controller grant must be a regular private proposal')
    proposal=json.loads(path.read_text())
    if not isinstance(proposal,dict) or set(proposal)!={'scope','goal','profile'}:
        raise ValueError('controller grant requires exact scope, goal and profile')
    scope=proposal['scope']
    if not isinstance(scope,dict):
        raise ValueError('controller scope must be a mapping')
    planner=scope.get('planner')
    if planner=='codex-cli':
        if hosted_planner_grant is None or codex_native_payload is None:
            raise ValueError('codex-cli controller requires both a separately approved hosted-planner grant and exact native Codex payload before staging; offline-only staging cannot activate autonomous coding')
    elif planner=='sealed-offline':
        if hosted_planner_grant is not None or codex_native_payload is not None:
            raise ValueError('sealed-offline controller cannot inherit hosted planner access')
    else:
        raise ValueError('unrecognized controller planner; no stage prepared')


def prepare(source: Path, jobpipe: Path, do_again_repo: Path, output: Path, *, ids=None, dependency_lock=None, canary_grant=None, codex_grant=None, classify_canary_partial=False, sonary_repo: Path | None = None, jobpipe_offline_development=False, jobpipe_migration_sha=None, controller_grant=None, hosted_planner_grant=None, codex_native_payload=None, controller_test_wheels=None):
    if controller_grant is None and (hosted_planner_grant is not None or codex_native_payload is not None or controller_test_wheels is not None):
        raise ValueError('hosted capabilities require a separate controller package')
    if controller_grant is not None and (canary_grant is not None or codex_grant is not None or classify_canary_partial):
        raise ValueError('controller cannot inherit browser or synthetic model canary authority')
    if canary_grant is not None and codex_grant is not None:
        raise ValueError('browser and Codex canary grants cannot coexist')
    if jobpipe_offline_development and (canary_grant is not None or codex_grant is not None or classify_canary_partial):
        raise ValueError('jobpipe offline development and synthetic canary grants cannot coexist')
    if jobpipe_migration_sha is not None and (not jobpipe_offline_development
            or not isinstance(jobpipe_migration_sha,str)
            or not re.fullmatch('[0-9a-f]{40}',jobpipe_migration_sha)):
        raise ValueError('jobpipe migration requires an exact target and offline development')
    if codex_grant is not None and classify_canary_partial:
        raise ValueError('Codex canary cannot inherit browser partial-publication disposition')
    if classify_canary_partial and canary_grant is None:
        raise ValueError('explicit partial disposition requires a fresh sealed canary grant')
    require_planner_grants_before_staging(controller_grant,hosted_planner_grant,codex_native_payload)
    if output.exists():raise ValueError('output already exists; refusing overwrite')
    source=source.resolve();jobpipe=jobpipe.resolve();do_again_repo=do_again_repo.resolve()
    if sonary_repo is not None:
        sonary_repo=sonary_repo.resolve(strict=True)
    if git(source,'status','--porcelain'):raise ValueError('commit and validate supervisor source before preparation')
    source_sha=git(source,'rev-parse','HEAD')
    uid=os.getuid();gid=os.getgid();identity=pwd.getpwuid(uid)
    if uid==0:raise ValueError('prepare the bundle as the operator, not root')
    installed=None
    installed_path=Path('/Library/Application Support/DoAgainSupervisor/current/config.json')
    if sonary_repo is not None and sonary_repo != Path(identity.pw_dir)/'Sonary':
        raise ValueError('Sonary requires its exact operator-home checkout')
    parents={str(do_again_repo):'_doagain_da',str(jobpipe):'_doagain_jp'}
    sonary={str(sonary_repo):'_doagain_so'} if sonary_repo is not None else {}
    enrolling_sonary=False
    if installed_path.exists():
        sys.path.insert(0,str(source/'src'))
        from do_again.supervisor.macos_execution import private_root_file
        private_root_file(installed_path)
        installed=json.loads(installed_path.read_text())
        if installed.get('production_ready') is not False or installed['operator_uid']!=uid:
            raise ValueError('existing supervisor requires guarded upgrade reconciliation')
        actual={p['repo']:p['account'] for p in installed['projects']}
        if actual not in (parents,{**parents,**sonary}) or (len(actual)==3 and not sonary):
            raise ValueError('existing installation scope differs or Sonary was omitted')
        for project in installed['projects']:
            account=pwd.getpwnam(project['account'])
            if (account.pw_uid,account.pw_gid,account.pw_dir,account.pw_shell)!=(project['uid'],project['gid'],'/var/empty','/usr/bin/false'):
                raise ValueError('existing execution identity changed')
        ids=[next(p['uid'] for p in installed['projects'] if p['repo']==str(repo))
             for repo in (do_again_repo,jobpipe)]
        if sonary:
            previous=next((p for p in installed['projects'] if p['account']=='_doagain_so'),None)
            if previous is None:
                enrolling_sonary=True
                ids.extend(available_ids(1))
            else:
                ids.append(previous['uid'])
    else:
        if sonary:
            raise ValueError('Sonary requires an existing two-project maintenance installation')
        if ids is None:
            ids=available_ids()
    if classify_canary_partial and (installed is None or not installed.get('live_canary')):
        raise ValueError('partial disposition requires the exact installed canary scope')
    if (len(ids)!=(3 if sonary else 2) or len(set(ids))!=len(ids)
            or any(type(value) is not int or not 400<=value<500 or value==uid for value in ids)):
        raise ValueError('invalid execution IDs')
    for repo,name in [(source,'do-again'),(jobpipe,'jobpipe'),*([(sonary_repo,'Sonary')] if sonary else [])]:
        remote=git(repo,'remote','get-url','origin')
        if remote not in (f'https://github.com/Tran-Steven/{name}.git',
                          f'git@github.com:Tran-Steven/{name}.git'):
            raise ValueError('source repository identity does not match approved projects')
    if enrolling_sonary:
        require_fresh_sonary_snapshot(sonary_repo)
    migrating=False
    if jobpipe_offline_development:
        pinned=next((p['source_sha'] for p in (installed or {}).get('projects',[])
                     if p.get('account')=='_doagain_jp'),None)
        latest=require_fresh_jobpipe_snapshot(
            jobpipe,pinned_sha=None if jobpipe_migration_sha else pinned)
        if jobpipe_migration_sha is not None:
            if (installed is None or pinned is None or pinned==latest
                    or jobpipe_migration_sha!=latest or enrolling_sonary):
                raise ValueError('jobpipe migration requires a changed installed pin and current verified main')
            try:
                git(jobpipe,'merge-base','--is-ancestor',pinned,latest)
            except subprocess.CalledProcessError as exc:
                raise ValueError('jobpipe protected snapshot migration is not a fast-forward') from exc
            migrating=True
    output.mkdir(parents=True,mode=0o700)
    payload=output/'payload';payload.mkdir(mode=0o700)
    package=payload/'package/do_again';package.parent.mkdir()
    package.mkdir()
    for entry in git(source,'ls-tree','-r',source_sha,'src/do_again').splitlines():
        metadata,name=entry.split('\t',1)
        if metadata.split()[0] not in {'100644','100755'}:raise ValueError('non-regular source payload')
        relative=Path(name).relative_to('src/do_again')
        if any(character in str(relative) for character in ('\n','\r','\\')):raise ValueError('unsupported payload filename')
        target=package/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(subprocess.check_output(['git','-C',str(source),'show',source_sha+':'+name]))
    (payload/'bootstrap.py').write_bytes(subprocess.check_output(['git','-C',str(source),'show',source_sha+':tools/macos_supervisor_bootstrap.py']))
    (payload/'worker-bootstrap.py').write_bytes(subprocess.check_output(['git','-C',str(source),'show',source_sha+':tools/macos_worker_bootstrap.py']))
    (payload/'install.py').write_bytes(subprocess.check_output(['git','-C',str(source),'show',source_sha+':tools/macos_supervisor_install.py']))
    runtime=seal_runtime(payload)
    native_git=seal_git(payload)
    ca=Path('/private/etc/ssl/cert.pem')
    for path in (ca,*ca.parents):
        if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o022:
            raise ValueError('OS certificate roots are mutable or aliased')
    if not ca.is_file() or ca.stat().st_nlink!=1:
        raise ValueError('OS certificate roots are not regular sealed input')
    (payload/'runtimes/trust').mkdir()
    shutil.copyfile(ca,payload/'runtimes/trust/ca.pem')
    (payload/'snapshots').mkdir()
    projects=[]
    scopes=[(source,do_again_repo,'do-again','_doagain_da','HEAD'),
            (jobpipe,jobpipe,'jobpipe','_doagain_jp','origin/main')]
    if sonary:
        scopes.append((sonary_repo,sonary_repo,'Sonary','_doagain_so','origin/main'))
    for index,(repo,canonical,name,account,ref) in enumerate(scopes):
        if installed:
            previous=next((p for p in installed['projects'] if p['repo']==str(canonical)),None)
            if previous is not None and not (migrating and account=='_doagain_jp'):
                ref=previous['source_sha']
        sha=git(repo,'rev-parse',ref)
        bundle=f'snapshots/{name}.bundle'
        snapshot_bundle(repo,sha,payload/bundle)
        key=project_key(canonical)
        projects.append({'repo':str(canonical),'key':key,'uid':ids[index],'gid':ids[index],
                         'account':account,'worktree':f'/private/var/do-again-execution/{key}/worktree',
                         'github_repository':'Tran-Steven/'+name,
                         'executables':[runtime,'/bin/bash','/bin/sh','/bin/zsh',
                                        native_git,'/usr/bin/make','/usr/bin/true','/bin/launchctl'],
                         'source_sha':sha,'bundle':bundle})
    config={'schema_version':1,'source_sha':source_sha,'operator_uid':uid,'operator_gid':gid,
            'operator_home':identity.pw_dir,'python':runtime,'git':native_git,
            'authority_path':'/Library/Application Support/DoAgainSupervisor/state/supervisor.sqlite',
            'legacy_authority_path':str(Path(identity.pw_dir)/'.do_again/supervisor/authority.sqlite'),
            'production_ready':False,
            'recovery_contract':{'authority_schema':1,'effect_schema':1,
                                 'worker_admission':'fenced-v1','mode':'maintenance-only'},
            'projects':projects}
    if enrolling_sonary:
        config['scope_enrollment']={'kind':'sonary_add_only',
                                    'previous_source_sha':installed['source_sha']}
    if migrating:
        old=next(p for p in installed['projects'] if p['account']=='_doagain_jp')
        updated=next(p for p in projects if p['account']=='_doagain_jp')
        config['snapshot_migration']={
            'kind':'jobpipe_fast_forward_v1',
            'installed_source_sha':installed['source_sha'],
            'project_key':updated['key'],
            'from_sha':old['source_sha'],
            'to_sha':updated['source_sha']}
    sys.path.insert(0,str(source/'src'))
    config['dependency_artifacts']={}
    if dependency_lock is not None:
        lock=Path(dependency_lock).resolve(strict=True)
        if (Path(dependency_lock).is_symlink() or lock.stat().st_uid!=uid
                or lock.stat().st_nlink!=1 or lock.stat().st_mode&0o022):
            raise ValueError('dependency approval lock must be privately controlled by the operator')
        approvals=json.loads(lock.read_text())
        if not isinstance(approvals,dict) or set(approvals)-{'do-again','jobpipe','Sonary'}:
            raise ValueError('dependency approval scope is not authorized')
        from do_again.supervisor.dependencies import approved_artifact
        for project in projects:
            name={'_doagain_da':'do-again','_doagain_jp':'jobpipe','_doagain_so':'Sonary'}[project['account']]
            entries=approvals.get(name,[])
            if not isinstance(entries,list) or len(entries)>64:
                raise ValueError('dependency approval lock exceeds project limits')
            config['dependency_artifacts'][project['key']]=entries
            for artifact in entries:
                approved_artifact(config,project['key'],artifact['id'])
    from do_again.supervisor.authority import AuthorityRegistry
    legacy=AuthorityRegistry(Path(config['legacy_authority_path']))
    from do_again.supervisor.authority import AuthorityDenied
    for project in projects:
        try:
            status=legacy.status(Path(project['repo']))
        except AuthorityDenied:
            if project['account']!='_doagain_so':raise
            status=None
        accepted={'maintenance','paused','stopped'} if project['account']=='_doagain_so' else {'maintenance'}
        if status is not None and status['intent'] not in accepted:
            raise ValueError('installation requires closed project admission')
        if status is None:
            prior=next((p for p in (installed or {}).get('projects',[])
                        if p['account']=='_doagain_so'),None)
            project['goal_revision']=(prior['goal_revision'] if prior is not None
                                      else 'sonary-enrolled-maintenance-v1')
        else:
            project['goal_revision']=status['goal_revision']
    if jobpipe_offline_development:
        from do_again.supervisor.jobpipe_development import require_jobpipe_grant
        from do_again.supervisor.macos_execution import project_from_dict
        jobpipe_project=next(p for p in projects if p['account']=='_doagain_jp')
        issued=datetime.now(timezone.utc).replace(microsecond=0)
        expires=issued+timedelta(days=14)
        config['development_scope']={
            'kind':'jobpipe-offline-v1', 'source_sha':source_sha,
            'project_key':jobpipe_project['key'], 'goal_revision':jobpipe_project['goal_revision'],
            'issued_at_utc':issued.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'expires_at_utc':expires.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'network_access':False, 'application_submissions':False}
        require_jobpipe_grant(config,project_from_dict(jobpipe_project),now=issued)
    if controller_grant is not None:
        from do_again.supervisor.controller_installation import apply_controller_grant
        def private_proposal(path):
            path=Path(path)
            if path.is_symlink() or path.stat().st_uid!=uid or path.stat().st_mode&0o077 or path.stat().st_nlink!=1:
                raise ValueError('controller authorization proposal must be private and unaliased')
            return json.loads(path.read_text())
        if 'development_scope' not in config:
            if installed is None or installed.get('development_scope',{}).get('kind')!='jobpipe-offline-v1':
                raise ValueError('controller requires existing or explicitly prepared offline execution authority')
            # Rebind only source identity through the administrator-approved stage;
            # do not silently renew or broaden the existing offline grant.
            config['development_scope']=dict(installed['development_scope'],source_sha=source_sha)
        grant=private_proposal(controller_grant)
        hosted=private_proposal(hosted_planner_grant) if hosted_planner_grant is not None else None
        apply_controller_grant(config,grant,hosted)
        jp=next(p for p in projects if p['account']=='_doagain_jp')
        from do_again.supervisor.jobpipe_development import require_jobpipe_grant
        from do_again.supervisor.macos_execution import project_from_dict
        require_jobpipe_grant(config,project_from_dict(jp))
        for path,blob in config['controller_profile']['test_files'].items():
            if git(jobpipe,'rev-parse',jp['source_sha']+':'+path)!=blob:
                raise ValueError('controller baseline test hash differs from sealed jobpipe snapshot')
        if hosted is not None:
            if codex_native_payload is None:raise ValueError('hosted planner requires exact native CLI payload')
            native=Path(codex_native_payload)
            if native.is_symlink() or not native.is_file() or native.stat().st_nlink!=1:
                raise ValueError('native CLI payload must be regular and unaliased')
            if hashlib.sha256(native.read_bytes()).hexdigest()!=hosted['payload_sha256']:
                raise ValueError('native CLI payload differs from hosted authorization')
            dependencies=subprocess.run(['/usr/bin/otool','-L',str(native)],check=True,capture_output=True,text=True).stdout.splitlines()[1:]
            if any(not line.strip().startswith(('/usr/lib/','/System/Library/')) for line in dependencies):
                raise ValueError('Codex payload has unsealed external dependencies')
            target=payload/'runtimes/codex/bin/codex';target.parent.mkdir(parents=True)
            shutil.copyfile(native,target);target.chmod(0o755)
        from do_again.supervisor.controller_installation import validate_controller_stage,prepare_test_wheels
        if controller_test_wheels is not None:
            prepare_test_wheels(payload,config,controller_test_wheels)
        validate_controller_stage(payload,config)
    selected_grant=canary_grant if canary_grant is not None else codex_grant
    if selected_grant is not None:
        grant_path=Path(selected_grant).resolve(strict=True)
        if (Path(selected_grant).is_symlink() or grant_path.stat().st_uid!=uid
                or grant_path.stat().st_nlink!=1 or grant_path.stat().st_mode&0o077):
            raise ValueError('canary grant must be privately controlled by the operator')
        if codex_grant is not None:
            config['codex_canary']=json.loads(grant_path.read_text())
            from do_again.supervisor.model_native import codex_project
            _,_,canary=codex_project(config)
        else:
            config['live_canary']=json.loads(grant_path.read_text())
            from do_again.supervisor.live_canary import scope,require_fresh_conversation
            require_fresh_conversation(installed,config)
            _,_,canary=scope(config)
        # Exact public Do Again baseline only, not a model-authored tree.
        with tempfile.TemporaryDirectory() as tmp:
            bare=Path(tmp)/'public.git'
            subprocess.run(['git','init','--bare',str(bare)],check=True,capture_output=True)
            subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
                '-c','credential.helper=','-C',str(bare),'fetch','--no-tags',
                'https://github.com/Tran-Steven/do-again.git',
                canary['source_sha']+':refs/heads/baseline'],check=True,capture_output=True)
            git(bare,'bundle','create',str(payload/canary['bundle']),'refs/heads/baseline')
    (payload/'config.json').write_text(json.dumps(config,sort_keys=True,indent=2)+'\n')
    files={}
    for p in sorted(payload.rglob('*')):
        if p.is_symlink():raise ValueError('bundle symlink forbidden')
        if p.is_file():
            if p.stat().st_nlink!=1:raise ValueError('bundle hardlink forbidden')
            files[str(p.relative_to(payload))]=hashlib.sha256(p.read_bytes()).hexdigest()
    manifest={'schema_version':1,'source_sha':source_sha,'files':files}
    (payload/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    # Authenticate manifest separately, then verify every payload file using
    # root-owned system tools before any installer code is executed as root.
    sums=''.join(digest+'  '+name+'\n' for name,digest in files.items())
    sums+=hashlib.sha256((payload/'manifest.json').read_bytes()).hexdigest()+'  manifest.json\n'
    (payload/'MANIFEST.sha256').write_text(sums)
    sums_digest=hashlib.sha256((payload/'MANIFEST.sha256').read_bytes()).hexdigest()
    stage=f'/Library/Application Support/DoAgainSupervisor/staging-{source_sha[:12]}'
    q=shlex.quote
    command='\n'.join([
        'set -eu', 'umask 077',
        'test ! -L '+q('/Library/Application Support/DoAgainSupervisor'),
        'test ! -e '+q('/Library/Application Support/DoAgainSupervisor')+' || test "$(/usr/bin/stat -f %u '+q('/Library/Application Support/DoAgainSupervisor')+')" = 0',
        '/usr/bin/install -d -o root -g wheel -m 755 '+q('/Library/Application Support/DoAgainSupervisor'),
        'test ! -e '+q(stage),
        '/usr/bin/ditto --noqtn '+q(str(payload))+' '+q(stage),
        'test -z "$(/usr/bin/find '+q(stage)+' ! -type f ! -type d -print -quit)"',
        'test -z "$(/usr/bin/find '+q(stage)+' -type f -links +1 -print -quit)"',
        '/usr/sbin/chown -R root:wheel '+q(stage),
        '/usr/bin/find '+q(stage)+' -type d -exec /bin/chmod 755 {} +',
        '/usr/bin/find '+q(stage)+' -type f -exec /bin/chmod 644 {} +',
        'cd '+q(stage),
        'test "$(/usr/bin/shasum -a 256 MANIFEST.sha256 | /usr/bin/cut -d " " -f 1)" = '+q(sums_digest),
        '/usr/bin/shasum -a 256 -c MANIFEST.sha256 >/dev/null',
        '/usr/bin/python3 -I -S -B '+q(stage+'/install.py')+' --stage '+q(stage)
        +(' --classify-canary-partial-publication --partial-disposition-source '+q(installed['source_sha'])
          +' --partial-disposition-nonce '+q(installed['live_canary']['nonce']) if classify_canary_partial else ''),
    ])+'\n'
    (output/'administrator-install.sh').write_text(command)
    (output/'administrator-install.sh').chmod(0o600)
    # Escape for an AppleScript string; never interpolate sensitive runtime data.
    script_digest=hashlib.sha256(command.encode()).hexdigest()
    shell='test "$(/usr/bin/shasum -a 256 '+q(str(output/'administrator-install.sh'))+' | /usr/bin/cut -d " " -f 1)" = '+q(script_digest)+' && /bin/sh '+q(str(output/'administrator-install.sh'))
    escaped=shell.replace('\\','\\\\').replace('"','\\"')
    (output/'authenticate.applescript').write_text('do shell script "'+escaped+'" with administrator privileges\n')
    (output/'authenticate.applescript').chmod(0o600)
    return {'source_sha':source_sha,'checksum_manifest_sha256':sums_digest,
            'projects':[{k:p[k] for k in ('account','uid','gid','source_sha')} for p in projects]}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--do-again-repo',type=Path,required=True)
    parser.add_argument('--jobpipe-repo',type=Path,required=True)
    parser.add_argument('--sonary-repo',type=Path,help='Explicitly add or retain Sonary as a third protected scope')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependency-lock',type=Path)
    grants=parser.add_mutually_exclusive_group()
    grants.add_argument('--canary-grant',type=Path)
    grants.add_argument('--codex-grant',type=Path)
    grants.add_argument('--jobpipe-offline-development',action='store_true',help='Seal a 14-day jobpipe-only offline coding grant; does not resume or start workers')
    parser.add_argument('--controller-test-wheels',type=Path,help='Offline directory of preapproved wheels named by artifact id')
    parser.add_argument('--controller-grant',type=Path,help='Private exact controller scope/goal/profile for administrator review')
    parser.add_argument('--hosted-planner-grant',type=Path,help='Separate private hosted-model authority; never inferred from execution')
    parser.add_argument('--codex-native-payload',type=Path,help='Existing native Codex CLI payload matching the hosted grant')
    parser.add_argument('--jobpipe-migration-sha',help='Exact remote jobpipe main SHA for opt-in protected fast-forward')
    parser.add_argument('--classify-canary-partial-publication',action='store_true')
    args=parser.parse_args()
    print(json.dumps(prepare(args.source,args.jobpipe_repo,args.do_again_repo,args.output,
                             dependency_lock=args.dependency_lock,canary_grant=args.canary_grant,
                             codex_grant=args.codex_grant,
                             classify_canary_partial=args.classify_canary_partial_publication,
                              sonary_repo=args.sonary_repo,
                              jobpipe_offline_development=args.jobpipe_offline_development,
                              jobpipe_migration_sha=args.jobpipe_migration_sha,controller_grant=args.controller_grant,
                              hosted_planner_grant=args.hosted_planner_grant,codex_native_payload=args.codex_native_payload,controller_test_wheels=args.controller_test_wheels),indent=2))
