"""Bounded read-only commit export, executed only under native confinement."""
from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .git_capabilities import SHA, _safe_relative
from .macos_execution import ExecutionBlocked


def export_commit(git: Path, metadata: Path, head: str) -> dict:
    if not SHA.fullmatch(head):
        raise ExecutionBlocked('publication requires an exact commit')
    budget = 128 * 1024
    def read(*args):
        nonlocal budget
        command = [str(git),'--no-pager','--no-replace-objects','--literal-pathspecs',
                   '--git-dir='+str(metadata),*args]
        # Native confinement, resource limits and the outer bounded capture also
        # cover these descendants. Never run configuration, filters or hooks.
        result = subprocess.run(command,capture_output=True,timeout=15)
        budget -= len(result.stdout)
        if result.returncode or budget < 0:
            raise ExecutionBlocked('commit export failed or exceeded its budget')
        return result.stdout
    if int(read('cat-file','-s',head)) > budget:
        raise ExecutionBlocked('commit exceeds publication budget')
    raw = read('cat-file','commit',head).decode('utf-8')
    headers, message = raw.split('\n\n',1)
    fields = {}
    for line in headers.splitlines():
        key, value = line.split(' ',1)
        if key not in {'tree','parent','author','committer'} or key in fields:
            raise ExecutionBlocked('publication supports canonical unsigned single-parent commits')
        fields[key] = value
    if set(fields) != {'tree','parent','author','committer'} or not all(SHA.fullmatch(fields[k]) for k in ('tree','parent')):
        raise ExecutionBlocked('commit ancestry cannot be bound')
    def person(value):
        matched = re.fullmatch(r'([^\n<>]+) <([^\n<>]+)> ([0-9]+) ([+-])([0-9]{2})([0-9]{2})',value)
        if matched is None:raise ExecutionBlocked('unsupported commit identity')
        name,email,stamp,sign,hours,minutes = matched.groups()
        offset = timedelta(hours=int(hours),minutes=int(minutes)) * (1 if sign=='+' else -1)
        date = datetime.fromtimestamp(int(stamp),timezone(offset)).isoformat(timespec='seconds')
        return {'name':name,'email':email,'date':date}
    changes = read('diff-tree','--no-commit-id','--no-renames','--raw','-z','-r',head).split(b'\x00')
    entries = []
    for index in range(0,len(changes)-1,2):
        mode_before, mode_after, before, after, status = changes[index].decode('ascii').split()
        path = changes[index+1].decode('utf-8')
        if not _safe_relative(path) or status not in {'A','M','D','T'} or mode_after not in {'000000','100644','100755','120000'}:
            raise ExecutionBlocked('publication change is not an exact supported file')
        entry = {'path':path,'mode':mode_before[1:] if status=='D' else mode_after,'type':'blob','sha':None}
        if status != 'D':
            if not SHA.fullmatch(after):raise ExecutionBlocked('invalid blob identity')
            if int(read('cat-file','-s',after)) > budget:
                raise ExecutionBlocked('blob exceeds publication budget')
            content = read('cat-file','blob',after)
            entry.update(sha=after,content=base64.b64encode(content).decode('ascii'))
        entries.append(entry)
    if not entries or len(entries)>128:
        raise ExecutionBlocked('publication requires a bounded useful commit')
    return {'head':head,'parent':fields['parent'],'tree':fields['tree'],'message':message,
            'author':person(fields['author']),'committer':person(fields['committer']),'entries':entries}


if __name__ == '__main__':
    import sys
    print(json.dumps(export_commit(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3])))
