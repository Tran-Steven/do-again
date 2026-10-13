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
