"""Build a private, hash-sealed installation bundle without administrator access."""
from __future__ import annotations
import argparse
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


def available_ids():
    occupied=set()
    for category,attribute in (('Users','UniqueID'),('Groups','PrimaryGroupID')):
        result=subprocess.check_output(['/usr/bin/dscl','.','-list','/'+category,attribute],text=True)
        for line in result.splitlines():
            try:occupied.add(int(line.split()[-1]))
            except (ValueError,IndexError):continue
    free=[value for value in range(400,500) if value not in occupied]
    if len(free)<2:raise ValueError('no collision-free execution identities available')
    return free[:2]


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


def prepare(source: Path, jobpipe: Path, do_again_repo: Path, output: Path, *, ids=None, dependency_lock=None):
    if output.exists():raise ValueError('output already exists; refusing overwrite')
    source=source.resolve();jobpipe=jobpipe.resolve();do_again_repo=do_again_repo.resolve()
    if git(source,'status','--porcelain'):raise ValueError('commit and validate supervisor source before preparation')
    source_sha=git(source,'rev-parse','HEAD')
    uid=os.getuid();gid=os.getgid();identity=pwd.getpwuid(uid)
    if uid==0:raise ValueError('prepare the bundle as the operator, not root')
    installed=None
    installed_path=Path('/Library/Application Support/DoAgainSupervisor/current/config.json')
    if installed_path.exists():
        sys.path.insert(0,str(source/'src'))
        from do_again.supervisor.macos_execution import private_root_file
        private_root_file(installed_path)
        installed=json.loads(installed_path.read_text())
        if installed.get('production_ready') is not False or installed['operator_uid']!=uid:
            raise ValueError('existing supervisor requires guarded upgrade reconciliation')
        expected={str(do_again_repo):'_doagain_da',str(jobpipe):'_doagain_jp'}
        if {p['repo']:p['account'] for p in installed['projects']}!=expected:
            raise ValueError('existing installation project scope differs')
        for project in installed['projects']:
            account=pwd.getpwnam(project['account'])
            if (account.pw_uid,account.pw_gid,account.pw_dir,account.pw_shell)!=(project['uid'],project['gid'],'/var/empty','/usr/bin/false'):
                raise ValueError('existing execution identity changed')
        ids=[next(p['uid'] for p in installed['projects'] if p['repo']==str(repo)) for repo in (do_again_repo,jobpipe)]
    elif ids is None:ids=available_ids()
    if len(ids)!=2 or len(set(ids))!=2 or any(not 400<=value<500 or value==uid for value in ids):raise ValueError('invalid execution IDs')
    for repo,name in ((source,'do-again'),(jobpipe,'jobpipe')):
        remote=git(repo,'remote','get-url','origin')
        if remote not in (f'https://github.com/Tran-Steven/{name}.git',f'git@github.com:Tran-Steven/{name}.git'):
            raise ValueError('source repository identity does not match approved projects')
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
    for index,(repo,canonical,name,account,ref) in enumerate((
        (source,do_again_repo,'do-again','_doagain_da','HEAD'),
        (jobpipe,jobpipe,'jobpipe','_doagain_jp','origin/main'))):
        if installed:
            ref=next(p['source_sha'] for p in installed['projects'] if p['repo']==str(canonical))
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
    sys.path.insert(0,str(source/'src'))
    config['dependency_artifacts']={}
    if dependency_lock is not None:
        lock=Path(dependency_lock).resolve(strict=True)
        if (Path(dependency_lock).is_symlink() or lock.stat().st_uid!=uid
                or lock.stat().st_nlink!=1 or lock.stat().st_mode&0o022):
            raise ValueError('dependency approval lock must be privately controlled by the operator')
        approvals=json.loads(lock.read_text())
        if not isinstance(approvals,dict) or set(approvals)-{'do-again','jobpipe'}:
            raise ValueError('dependency approval scope is not authorized')
        from do_again.supervisor.dependencies import approved_artifact
        for project in projects:
            name='do-again' if project['account']=='_doagain_da' else 'jobpipe'
            entries=approvals.get(name,[])
            if not isinstance(entries,list) or len(entries)>64:
                raise ValueError('dependency approval lock exceeds project limits')
            config['dependency_artifacts'][project['key']]=entries
            for artifact in entries:
                approved_artifact(config,project['key'],artifact['id'])
    from do_again.supervisor.authority import AuthorityRegistry
    legacy=AuthorityRegistry(Path(config['legacy_authority_path']))
    for project in projects:
        status=legacy.status(Path(project['repo']))
        if status['intent'] != 'maintenance':raise ValueError('installation requires closed project admission')
        project['goal_revision']=status['goal_revision']
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
        '/usr/bin/python3 -I -S -B '+q(stage+'/install.py')+' --stage '+q(stage),
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
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependency-lock',type=Path)
    args=parser.parse_args()
    print(json.dumps(prepare(args.source,args.jobpipe_repo,args.do_again_repo,args.output,
                             dependency_lock=args.dependency_lock),indent=2))
