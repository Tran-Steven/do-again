"""Sealed, read-only native Git manifest helper, run as the execution UID."""
import json
import re
import subprocess
from pathlib import Path
from .macos_execution import ExecutionBlocked


def tree_manifest(git, metadata, head):
    if not re.fullmatch('[0-9a-f]{40}',head):raise ExecutionBlocked('invalid source head')
    result=subprocess.run([str(git),'--no-pager','--no-replace-objects',
        '--literal-pathspecs','--git-dir='+str(metadata),'ls-tree','-r','-z','--full-tree',head],
        capture_output=True,timeout=15)
    if result.returncode or len(result.stdout)>1024*1024:
        raise ExecutionBlocked('native source manifest failed or oversized')
    manifest={}
    for entry in result.stdout.rstrip(b'\0').split(b'\0'):
        metadata,name=entry.split(b'\t');mode,kind,blob=metadata.decode('ascii').split()
        name=name.decode('utf-8')
        if kind!='blob' or mode not in ('100644','100755') or name in manifest:
            raise ExecutionBlocked('source tree contains unsupported entries')
        manifest[name]={'mode':mode,'blob_sha':blob}
    if not manifest or len(manifest)>10000:raise ExecutionBlocked('source manifest outside bounds')
    return manifest
