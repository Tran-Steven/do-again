"""Root-owned development attestations derived from native effects, not stdout.

Stores records in the existing supervisor database. The protected pipeline is
the only producer; no socket operation accepts worker-supplied attestations.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import stat
import re
from contextlib import closing

from ..core.schema import canonical_json
from .macos_execution import ExecutionBlocked


def _hash(value): return hashlib.sha256(canonical_json(value)).hexdigest()


class DevelopmentAttestations:
    def __init__(self, path, *, owner_uid=0):
        self.path, self.owner_uid = path, owner_uid
        self._check()
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS development_attestation '
                       '(project TEXT, request TEXT, fingerprint TEXT, payload TEXT, '
                       'digest TEXT, PRIMARY KEY(project,request))')
            db.commit()

    def _check(self):
        for p in (self.path, *self.path.parents):
            info = p.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022:
                # A system temporary-directory ancestor may have another owner
                # in offline qualification. Production owner 0 has no exception.
                if self.owner_uid == 0 or p == self.path or p == self.path.parent:
                    raise ExecutionBlocked('attestation database ancestry is not protected')
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ExecutionBlocked('attestation database is aliased or not regular')
        if os.geteuid() != self.owner_uid:
            raise ExecutionBlocked('only database owner may produce native attestations')

    def record(self, project, request, fingerprint, value):
        self._check()
        if value.get('request_fingerprint') != fingerprint:
            raise ExecutionBlocked('attestation fingerprint differs')
        encoded = canonical_json(value).decode(); digest = _hash(value)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT fingerprint,payload,digest FROM development_attestation '
                                  'WHERE project=? AND request=?', (project, request)).fetchone()
            if existing:
                if existing != (fingerprint, encoded, digest):
                    raise ExecutionBlocked('original development attestation cannot be replaced')
                return value
            db.execute('INSERT INTO development_attestation VALUES(?,?,?,?,?)',
                       (project, request, fingerprint, encoded, digest))
        return value

    def observe(self, project, request, fingerprint):
        self._check()
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as db:
            row = db.execute('SELECT fingerprint,payload,digest FROM development_attestation '
                             'WHERE project=? AND request=?', (project, request)).fetchone()
        if row is None: raise ExecutionBlocked('native development attestation is missing')
        value = json.loads(row[1])
        if row[0] != fingerprint or _hash(value) != row[2]:
            raise ExecutionBlocked('native development attestation identity/digest differs')
        return value


def produce_attestation(*, ledger, store, project, request_id, fingerprint,
                        source_sha, base_commit, stage_ids):
    """All stage IDs are constructed by the sealed pipeline, not request args.

    The test stage includes root-observed commit/tree measurements and captured
    native child outcomes. Tests are counted from fixed child invocations, never
    worker-provided summary text. Publication is the existing broker result.
    """
    if set(stage_ids) != {'commit', 'test', 'publication'}:
        raise ExecutionBlocked('development stage identity set differs')
    if (len(set(stage_ids.values())) != 3
            or request_id in stage_ids.values()
            or not re.fullmatch('[0-9a-f]{40}', source_sha)
            or not re.fullmatch('[0-9a-f]{40}', base_commit)):
        raise ExecutionBlocked('development stages or source binding invalid')
    stages = {}
    for stage, rid in stage_ids.items():
        observed = ledger.observe_request(project, rid, fingerprint)
        result = observed.get('result')
        intent = ledger.intent(project, rid)
        expected_op = {'commit': 'git_commit', 'test': 'execute', 'publication': 'git_publish'}[stage]
        if (observed.get('state') != 'terminal' or observed.get('source_sha') != source_sha
                or observed.get('request_fingerprint') != fingerprint
                or not isinstance(result, dict) or result.get('returncode') != 0
                or result.get('timed_out', False) is not False
                or not intent or intent.get('operation') != expected_op):
            raise ExecutionBlocked('native development stage missing, uncertain or failed')
        stages[stage] = result
    commit, test, publication = (stages[k] for k in ('commit', 'test', 'publication'))
    authority = commit.get('authority', {})
    head, branch = authority.get('repo_head'), authority.get('repo_branch')
    run = test.get('native_test_run')
    precommit=(isinstance(run,dict) and run.get('commit_before')==base_commit
        and run.get('commit_after')==base_commit and commit.get('tested_tree_sha256')==run.get('tree_before'))
    if (not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head)
            or head == base_commit or not isinstance(branch, str)
            or not branch.startswith('do-again/task-')
            or commit.get('base_sha') != base_commit or not commit.get('paths')
            or not isinstance(run, dict) or (not precommit and (run.get('commit_before') != head
            or run.get('commit_after') != head))
            or run.get('tree_before') != run.get('tree_after')
            or not isinstance(run.get('tree_before'), str)
            or not re.fullmatch('[0-9a-f]{64}', run['tree_before'])
            or not isinstance(run.get('cases'), list) or not run['cases']
            or any(not isinstance(case, dict) or type(case.get('exit_code')) is not int or case['exit_code'] != 0
                   or case.get('timed_out') is not False for case in run['cases'])
            or publication.get('head') != head or publication.get('branch') != branch
            or publication.get('repository') != 'Tran-Steven/jobpipe'):
        raise ExecutionBlocked('commit, independent test measurements or publication differ')
    proof = {'request_fingerprint': fingerprint, 'source_sha': source_sha,
        'implementation_commit': head, 'test_commit': head, 'base_commit': base_commit,
        'tests_passed': True, 'tests_executed': len(run['cases']), 'test_exit_code': 0,
        'changed_paths': commit['paths'], 'publication_commit': publication['head'],
        'repository': publication['repository'], 'branch': branch,
        'worker_network_denied': run.get('network_denied') is True,
        'application_submissions': 0, 'stage_ids': stage_ids,
        'test_run_sha256': _hash(run)}
    if not proof['worker_network_denied']:
        raise ExecutionBlocked('native test network confinement evidence is absent')
    return store.record(project, request_id, fingerprint, proof)
