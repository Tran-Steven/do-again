"""Append-only disposition of a reviewed read-only probe; never a result."""
import hashlib,json,re
from datetime import datetime,timezone

TABLE='planning_probe_quarantine'
DISPOSITION='unresolved_no_replay'
REASON='native-read-only-planning-source-budget-v1'

def exists(db,project,request):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()
        and db.execute('SELECT 1 FROM planning_probe_quarantine WHERE project=? AND request=?',(project,request)).fetchone())

def validate_audits(db,project):
    from .macos_execution import ExecutionBlocked
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return set()
    ids=set()
    for rid,fp,sha,source,probe_fp,disposition,reason in db.execute('SELECT request,fingerprint,intent_sha256,source_sha,probe_fingerprint,disposition,reason FROM planning_probe_quarantine WHERE project=?',(project,)):
        row=db.execute('SELECT e.fingerprint,e.state,e.result,i.payload FROM execution e LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request WHERE e.project=? AND e.request=?',(project,rid)).fetchone()
        try:intent=json.loads(row[3]) if row else None
        except (ValueError,TypeError):intent=None
        if (not row or row[:3]!=(fp,'started',None) or not isinstance(intent,dict)
                or sha!=hashlib.sha256(row[3].encode()).hexdigest()
                or intent!={'operation':'execute','source_sha':source,'request_fingerprint':probe_fp}
                or not isinstance(source,str) or not re.fullmatch('[0-9a-f]{40}',source)
                or not isinstance(probe_fp,str) or not re.fullmatch('[0-9a-f]{64}',probe_fp)
                or not re.fullmatch('offline-[0-9a-f]{20}-[1-9][0-9]*-probe',rid)
                or disposition!=DISPOSITION or reason!=REASON):
            raise ExecutionBlocked('planning probe quarantine differs from unchanged original reservation')
        ids.add(rid)
    return ids

def append(db,project,request,*,expected_intent,approval_reference,dry_run=False):
    from .macos_execution import ExecutionBlocked
    if not isinstance(approval_reference,str) or not approval_reference.strip():raise ExecutionBlocked('explicit operator approval reference required')
    db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT e.fingerprint,e.state,e.result,i.payload FROM execution e LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request WHERE e.project=? AND e.request=?',(project,request)).fetchone()
        if (not row or row[1:3]!=('started',None) or json.loads(row[3] or 'null')!=expected_intent
                or set(expected_intent)!={'operation','source_sha','request_fingerprint'} or expected_intent['operation']!='execute'
                or not re.fullmatch('offline-[0-9a-f]{20}-[1-9][0-9]*-probe',request) or exists(db,project,request)):
            raise ExecutionBlocked('original reviewed probe reservation differs or already has an audit')
        sha=hashlib.sha256(row[3].encode()).hexdigest()
        proof={'request_id':request,'original_state':'started','original_result':None,'original_intent_sha256':sha,
               'disposition':DISPOSITION,'reason':REASON,'approval_reference':approval_reference,
               'terminal_success':False,'replay_permitted':False,'tests_claimed_passed':False}
        if dry_run:db.rollback();return proof
        db.execute('CREATE TABLE IF NOT EXISTS planning_probe_quarantine (project TEXT,request TEXT,fingerprint TEXT,intent_sha256 TEXT,source_sha TEXT,probe_fingerprint TEXT,disposition TEXT,reason TEXT,approval_reference TEXT,created_at TEXT,PRIMARY KEY(project,request))')
        db.execute('INSERT INTO planning_probe_quarantine VALUES(?,?,?,?,?,?,?,?,?,?)',(project,request,row[0],sha,
            expected_intent['source_sha'],expected_intent['request_fingerprint'],DISPOSITION,REASON,approval_reference,datetime.now(timezone.utc).isoformat()))
        validate_audits(db,project);db.commit();return proof
    except BaseException:db.rollback();raise
