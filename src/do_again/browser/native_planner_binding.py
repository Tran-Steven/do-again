"""A native planner binding separate from the user's ordinary project chat."""
import json
import os
import stat
from pathlib import Path
from . import runtime
from ..supervisor.macos_execution import ExecutionBlocked


def bound_planner_url(repo, grant):
    path=runtime._project_browser_dir(Path(repo).resolve())/'native_planner.json'
    if not path.exists() and not path.is_symlink():
        # Retain the explicitly approved historical binding until setup chooses
        # a separate one. No old chat or delivery journal is rewritten.
        url=str(runtime.project_record(Path(repo).resolve()).get('chat_url') or '')
    else:
        fd=os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=501 or info.st_nlink!=1
                    or info.st_mode & 0o077 or info.st_size>8192):
                raise ExecutionBlocked('native planner binding privacy differs')
            with os.fdopen(fd,closefd=False) as stream:value=json.load(stream)
        finally:os.close(fd)
        if (set(value)!={'repo','chat_url','profile_sha256'}
                or value['repo']!=str(Path(repo).resolve())
                or value['profile_sha256']!=grant['profile_sha256']):
            raise ExecutionBlocked('native planner binding identity differs')
        url=value['chat_url']
    if (not isinstance(url,str) or url!='https://chatgpt.com/c/'+grant['conversation_id']):
        raise ExecutionBlocked('native planner binding differs from sealed conversation')
    return url


def _fresh_record(path, value):
    from ..core.schema import canonical_json
    fd=os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(canonical_json(value)+b'\n');stream.flush()
        os.fsync(fd)
    finally:os.close(fd)
    parent=os.open(path.parent,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)


def ensure_native_reasoning(target):
    """Choose a verified unlocked thinking level on the observed five-step UI."""
    import time
    # The historical three-step UI remains governed by its original strict
    # implementation. The newer menu has a separate observed control shape.
    deadline=time.monotonic()+10
    shape=None
    while not isinstance(shape,dict) and time.monotonic()<deadline:
        shape=runtime.cdp.evaluate(target,r"""(() => {
const b=[...document.querySelectorAll('button[aria-label="Select ChatGPT model"]')]
 .filter(e=>e.getBoundingClientRect().width>0);
if(b.length!==1)return null;
return {text:b[0].innerText.trim(),open:b[0].getAttribute('aria-expanded')};})()""",timeout=10)
        if not isinstance(shape,dict):time.sleep(0.2)
    if not isinstance(shape,dict):raise ExecutionBlocked('native planner model selector unavailable before Send')
    if shape['open']!='true':
        runtime.cdp.evaluate(target,r"""(() => {const b=document.querySelector('button[aria-label="Select ChatGPT model"]');b.click();return true;})()""",timeout=10,user_gesture=True)
        time.sleep(0.2)
    slider=runtime.cdp.evaluate(target,r"""(() => {
const s=[...document.querySelectorAll('[role="slider"][aria-valuenow]')];
if(s.length!==1)return null;
return {min:s[0].getAttribute('aria-valuemin'),max:s[0].getAttribute('aria-valuemax')};})()""",timeout=10)
    if slider=={'min':'0','max':'2'}:
        for kind in ('keyDown','keyUp'):runtime.cdp.target_call(target,'Input.dispatchKeyEvent',{'type':kind,'key':'Escape','code':'Escape','windowsVirtualKeyCode':27,'nativeVirtualKeyCode':27},timeout=10)
        runtime._ensure_high_reasoning(target);return 'High'
    if slider!={'min':'0','max':'4'}:
        raise ExecutionBlocked('native planner reasoning control changed before Send')
    runtime.cdp.target_call(target,'Emulation.setFocusEmulationEnabled',{'enabled':True},timeout=10)
    try:
        runtime.cdp.evaluate(target,r"""(() => {const b=document.querySelector('button[aria-label="Select ChatGPT model"]');if(b?.getAttribute('aria-expanded')!=='true')b?.click();return true;})()""",timeout=10,user_gesture=True)
        time.sleep(0.2)
        focused=runtime.cdp.evaluate(target,r"""(() => {const s=document.querySelector('[role="slider"][aria-valuenow]');if(!s)return false;s.focus({preventScroll:true});return document.activeElement===s;})()""",timeout=10,user_gesture=True)
        if focused is not True:raise ExecutionBlocked('native reasoning control could not be focused in background')
        for key,code,keycode in [('Home','Home',36),('ArrowRight','ArrowRight',39),('ArrowRight','ArrowRight',39)]:
            for kind in ('keyDown','keyUp'):
                runtime.cdp.target_call(target,'Input.dispatchKeyEvent',{'type':kind,'key':key,'code':code,
                    'windowsVirtualKeyCode':keycode,'nativeVirtualKeyCode':keycode},timeout=10)
        time.sleep(0.3)
        observed=runtime.cdp.evaluate(target,r"""(() => {const s=document.querySelector('[role="slider"][aria-valuenow]');const m=[...document.querySelectorAll('[role="menu"]')].find(e=>e.getBoundingClientRect().width>0);return {value:s?.getAttribute('aria-valuenow'),text:(m?.innerText||'').slice(0,500)};})()""",timeout=10)
        if (not isinstance(observed,dict) or observed['value']!='2'
                or observed['text'].splitlines()[0] not in {'Thinking','Deep','High'}):
            raise ExecutionBlocked('requested native thinking level not independently confirmed unlocked')
        paywall=runtime.cdp.evaluate(target,r"""(() => [...document.querySelectorAll('[role="dialog"]')].filter(e=>e.getBoundingClientRect().width>0).some(e=>/upgrade|subscribe|access options/i.test(e.innerText)))()""",timeout=10)
        if paywall is not False:raise ExecutionBlocked('native thinking selection requires access; no prompt submitted')
        for _ in range(2):
            for kind in ('keyDown','keyUp'):
                runtime.cdp.target_call(target,'Input.dispatchKeyEvent',{'type':kind,'key':'Escape','code':'Escape','windowsVirtualKeyCode':27,'nativeVirtualKeyCode':27},timeout=10)
        closed=runtime.cdp.evaluate(target,r"""(() => document.querySelector('button[aria-label="Select ChatGPT model"]')?.getAttribute('aria-expanded')==='false')()""",timeout=10)
        if closed is not True:raise ExecutionBlocked('native model menu remains open before Send')
        return observed['text'].splitlines()[0]
    finally:
        runtime.cdp.target_call(target,'Emulation.setFocusEmulationEnabled',{'enabled':False},timeout=10)


def prepare_planner_chat(repo):
    """Explicit one-time setup; an unresolved Send is never repeated."""
    import hashlib
    import re
    import secrets
    import sys
    from .planner_service import profile_fingerprint, private_operator_directory
    if sys.platform!='darwin' or os.geteuid()!=501:
        raise ExecutionBlocked('native planner setup requires the operator Mac login')
    repo=Path(repo).resolve(strict=True)
    root=runtime._project_browser_dir(repo)
    for ancestor in (root,*root.parents):
        if ancestor.is_symlink():raise ExecutionBlocked('native planner setup path is aliased')
        if ancestor==runtime.browser_paths().root:break
    directory=private_operator_directory(root/'native-planner-setup',create=True)
    binding=root/'native_planner.json'
    with runtime._file_lock(directory/'setup.lock',timeout=1):
        profile=profile_fingerprint()
        result_path=directory/'result.json'
        if result_path.exists():
            result=json.loads(result_path.read_text())
            grant={'conversation_id':result['conversation_id'],'profile_sha256':profile}
            if bound_planner_url(repo,grant)!=result['chat_url']:
                raise ExecutionBlocked('saved native planner setup binding changed')
            return result
        for prior in directory.glob('attempt-*'):
            if (prior/'intent.json').exists() and not (prior/'preflight-error.json').exists():
                raise ExecutionBlocked('original dedicated setup attempt unresolved; no new Send permitted')
        if (directory/'intent.json').exists():
            if not (directory/'preflight-error.json').exists():
                raise ExecutionBlocked('original native planner setup unresolved; inspect its bound tab, never resend')
            directory=private_operator_directory(directory/('attempt-'+secrets.token_hex(8)),create=True)
        nonce=secrets.token_hex(12)
        marker='DO_AGAIN_PROJECT_READY_'+nonce
        prompt=('Create a dedicated Do Again native planner conversation for Tran-Steven/jobpipe. '
            'Wait for exact call-bound JSON planning requests. Do not invoke GitHub or other tools yourself. '
            'This is setup only; no development work is requested. Reply with exactly '+marker+'.')
        intent={'repo':str(repo),'profile_sha256':profile,'readiness_marker':marker,
                'message':prompt,'message_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
        _fresh_record(directory/'intent.json',intent)
        state=runtime.ensure_browser_running(verify_auth=False)
        with runtime._file_lock(runtime.browser_paths().root/'blank-chat.lock',timeout=30):
            target=runtime.cdp.create_target(int(state['port']),runtime.CHATGPT_URL,background=True)
            _fresh_record(directory/'target.json',{'target_id':target.id,'port':int(state['port'])})
            target,_=runtime.wait_for_authenticated(int(state['port']),chat_url=target.url,target=target,timeout=30)
            try:
                reasoning=ensure_native_reasoning(target)
            except Exception as exc:
                # This section cannot call Send. Its positively observed failure
                # permits a new setup attempt, without judging any old call.
                _fresh_record(directory/'preflight-error.json',{'stage':'reasoning_before_Send',
                    'state':'known_pre_dispatch_failure','send_attempted':False,'error':type(exc).__name__})
                raise
            def dispatch():
                if profile_fingerprint()!=profile:raise ExecutionBlocked('planner setup profile changed before Send')
                _fresh_record(directory/'dispatch.json',dict(intent,target_id=target.id,state='dispatch_started'))
            answer=runtime.send_message(target,prompt,timeout=180,wait_for_response=True,
                before_dispatch=dispatch,background_focus=True)
        url=str(answer.get('chat_url') or '');cid=runtime._chat_id(url)
        if not re.fullmatch(r'[0-9a-f-]{36}',cid) or answer.get('response','').strip()!=marker:
            raise ExecutionBlocked('original planner setup acknowledgment differs; no new Send permitted')
        if binding.exists() or binding.is_symlink():
            raise ExecutionBlocked('native planner binding already exists; retain original setup result for review')
        value={'repo':str(repo),'chat_url':url,'profile_sha256':profile}
        _fresh_record(binding,value)
        runtime._record_owned_chat(repo,url,created_reason='native_planner_setup')
        result=dict(value,conversation_id=cid,setup_model_calls=1,development_cycles=0,reasoning=reasoning)
        _fresh_record(result_path,result)
        return result
