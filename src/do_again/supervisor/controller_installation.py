"""Administrator-stage controller validation; never edits installed state."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from .controller_configuration import validate_controller_configuration
from .macos_execution import ExecutionBlocked


def apply_controller_grant(config,grant,hosted=None,*,browser=None,now=None):
    if not isinstance(grant,dict) or set(grant) not in ({'scope','goal','profile'}, {'scope','goal','profile','reviewed_task_scope'}):
        raise ExecutionBlocked('controller grant package requires exact scope, goal and profile')
    from ..service.offline_continuation import digest
    config.update(controller_scope=grant['scope'],controller_goal=grant['goal'],
        controller_goal_sha256=digest(grant['goal']),controller_profile=grant['profile'])
    if 'reviewed_task_scope' in grant:config['reviewed_task_scope']=grant['reviewed_task_scope']
    if hosted is not None and browser is not None:
        raise ExecutionBlocked('controller cannot combine hosted and browser planner grants')
    if hosted is not None:config['hosted_planning_scope']=hosted
    if browser is not None:config['browser_planning_scope']=browser
    validate_controller_configuration(config,now=now)


def validate_controller_stage(stage,config,*,now=None):
    validate_controller_configuration(config,now=now)
    scope=config.get('controller_scope')
    if scope is None:return
    if config['controller_profile']['test_runner']=='pytest':
        packages=Path(stage)/'runtimes/test-packages'
        if not config.get('controller_test_artifacts') or not (packages/'pytest/__init__.py').is_file():
            raise ExecutionBlocked('pytest requires approved sealed offline test dependencies')
    if scope['planner']=='codex-cli':
        payload=Path(stage)/'runtimes/codex/bin/codex'
        if payload.is_symlink() or not payload.is_file() or payload.stat().st_nlink!=1:
            raise ExecutionBlocked('native planning payload absent or aliased')
        if hashlib.sha256(payload.read_bytes()).hexdigest()!=config['hosted_planning_scope']['payload_sha256']:
            raise ExecutionBlocked('staged planning payload hash differs')
    elif (Path(stage)/'runtimes/codex').exists():
        raise ExecutionBlocked('offline planner stage contains an unapproved hosted payload')


def prepare_test_wheels(payload,config,directory):
    """Offline extraction of already approved wheels using existing validators."""
    from .dependencies import approved_artifact
    from .wheels import validate_wheel,install_wheel
    jp=next(p for p in config['projects'] if p['account']=='_doagain_jp')
    artifacts=config.get('dependency_artifacts',{}).get(jp['key'],[])
    if not artifacts:raise ExecutionBlocked('no approved offline test dependency artifacts')
    import tempfile,shutil
    directory=Path(directory);destination=Path(payload)/'runtimes/test-packages'
    destination.mkdir(parents=True)
    for item in artifacts:
        approved=approved_artifact(config,jp['key'],item['id'])
        wheel=directory/(item['id']+'.whl')
        if wheel.is_symlink() or not wheel.is_file() or wheel.stat().st_nlink!=1:
            raise ExecutionBlocked('approved offline wheel is missing or aliased')
        validate_wheel(wheel.read_bytes(),approved)
        with tempfile.TemporaryDirectory() as temporary:
            unpacked=Path(temporary)/'unpacked'
            install_wheel(wheel,unpacked,approved)
            for source in unpacked.rglob('*'):
                target=destination/source.relative_to(unpacked)
                if source.is_dir():target.mkdir(parents=True,exist_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():raise ExecutionBlocked('approved test wheels have conflicting files')
                    shutil.copyfile(source,target)
    config['controller_test_artifacts']=[{'id':v['id'],'sha256':v['sha256']} for v in artifacts]
