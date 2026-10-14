"""Append-only operator disposition; original publications remain non-replayable."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime,timezone

TABLE='publication_quarantine'
DISPOSITION='unresolved_no_replay'
REASON='publication-workspace-collision-before-network-v1'


def pending_rows(db,project):
    from .macos_execution import ExecutionBlocked
    rows=db.execute("SELECT request,fingerprint FROM execution WHERE project=? AND state='started'",(project,)).fetchall()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return rows
    quarantined=set()
    # Validate every audit even if its original was deleted or falsely finished.
    audits=db.execute('SELECT request,fingerprint,intent_sha256,source_sha,task_fingerprint,disposition,reason '
                     'FROM publication_quarantine WHERE project=?',(project,)).fetchall()
    for rid,fp,digest,source,task_fp,disposition,reason in audits:
        original=db.execute('SELECT e.fingerprint,e.state,e.result,i.payload FROM execution e '
            'LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request '
            'WHERE e.project=? AND e.request=?',(project,rid)).fetchone()
        try:intent=json.loads(original[3]) if original else None
        except (ValueError,TypeError):intent=None
        if (not original or original[:3]!=(fp,'started',None) or not isinstance(intent,dict)
                or digest!=hashlib.sha256(original[3].encode()).hexdigest()
                or source!=intent.get('source_sha') or task_fp!=intent.get('request_fingerprint')
                or disposition!=DISPOSITION or reason!=REASON or intent.get('operation')!='git_publish'):
            raise ExecutionBlocked('publication quarantine differs from unchanged original reservation')
        quarantined.add(rid)
    return [(rid,fp) for rid,fp in rows if rid not in quarantined]


def has_quarantine(db,project,request):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()
        and db.execute('SELECT 1 FROM publication_quarantine WHERE project=? AND request=?',(project,request)).fetchone())


def append_quarantine(db,project,request,*,expected_intent,approval_reference,dry_run=False):
    """Caller proves exact known defect under maintenance fences before calling.

    No execution row or intent is updated, no result is supplied, and the
    original lookup/reservation continue refusing replay indefinitely.
    """
    from .macos_execution import ExecutionBlocked
    if not isinstance(approval_reference,str) or not approval_reference.strip():
        raise ExecutionBlocked('explicit operator approval reference is required')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('BEGIN IMMEDIATE')
    try:
        original=db.execute('SELECT e.fingerprint,e.state,e.result,i.payload FROM execution e '
            'LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request '
            'WHERE e.project=? AND e.request=?',(project,request)).fetchone()
        if (not original or original[1:3]!=('started',None)
                or json.loads(original[3] or 'null')!=expected_intent
                or expected_intent.get('operation')!='git_publish'):
            raise ExecutionBlocked('original publication reservation or intent changed')
        digest=hashlib.sha256(original[3].encode()).hexdigest()
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()
        if exists and db.execute('SELECT 1 FROM publication_quarantine WHERE project=? AND request=?',(project,request)).fetchone():
            raise ExecutionBlocked('original quarantine already recorded; no repeated action')
        proof={'request_id':request,'original_state':'started','original_result':None,
               'original_intent_sha256':digest,'disposition':DISPOSITION,'reason':REASON,
               'approval_reference':approval_reference,'terminal_success':False,'replay_permitted':False}
        if dry_run:db.rollback();return proof
        db.execute('CREATE TABLE IF NOT EXISTS publication_quarantine ('
            'project TEXT,request TEXT,fingerprint TEXT,intent_sha256 TEXT,source_sha TEXT,task_fingerprint TEXT,'
            'disposition TEXT,reason TEXT,approval_reference TEXT,created_at TEXT,PRIMARY KEY(project,request))')
        db.execute('INSERT INTO publication_quarantine VALUES(?,?,?,?,?,?,?,?,?,?)',
            (project,request,original[0],digest,expected_intent['source_sha'],expected_intent['request_fingerprint'],
             DISPOSITION,REASON,approval_reference,datetime.now(timezone.utc).isoformat()))
        if db.execute('SELECT e.fingerprint,e.state,e.result,i.payload FROM execution e '
                'LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request '
                'WHERE e.project=? AND e.request=?',(project,request)).fetchone()!=original:
            raise ExecutionBlocked('original publication changed during quarantine')
        db.commit();return proof
    except BaseException:db.rollback();raise
