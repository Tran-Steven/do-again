"""Bound public source exposure without silently truncating growing test files."""
from __future__ import annotations
import ast,hashlib


def summarize_public_sources(contents,*,budget,implementation_paths):
    sources={};inventory={}
    ordered=sorted(contents,key=lambda path:(path not in implementation_paths,path))
    for path in ordered:
        data=contents[path]
        entry={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:
            inventory[path]=dict(entry,exposure='digest_only',inventory_reason='not_utf8')
            continue
        if len(data)<=budget:
            sources[path]=text;budget-=len(data)
            continue
        entry.update(exposure='inventory_only',full_content_exposed=False,
            guidance='Read this exact local file in the confined implementation script. '
                     'Preserve existing regressions and append distinct tests; this inventory is not the full source.')
        if len(data)>1024*1024:
            entry['inventory_reason']='file_exceeds_bounded_ast_input'
        else:
            try:
                module=ast.parse(text)
                names=[n.name for n in module.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
                entry['top_level_definitions']=[name[:96] for name in names[:48]]
                entry['inventory_truncated']=len(names)>48 or any(len(n)>96 for n in names[:48])
            except (SyntaxError,ValueError,RecursionError,MemoryError):entry['inventory_reason']='source_not_parseable'
        inventory[path]=entry
    return sources,inventory
