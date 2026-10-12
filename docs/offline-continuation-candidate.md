# Offline continuation candidate — not deployable

The native continuation state machine and driver are prepared for the existing
immutable worker and root broker. Dedicated controller transport reuses the
existing GitHub snapshot/blob/CAS/readback protocol. Broker dispatch validates a
separate scope before invoking a qualified backend. No backend is installed or
constructed by this candidate in the running supervisor.

The initial scope permits three implementation requests, one outstanding, zero
execution retries, a maximum two-hour grant, fifteen-minute request lifetimes,
and at most ten minutes per execution. Exhaustion completes the validation run;
failure or mismatched evidence blocks it. Recovery observes the original reserved
identity. It never spends another request to replay uncertain work.

The broker must own the journal, hold its existing admission/effect lock, verify
installed code, obtain scope from root-sealed configuration, and verify remote
blobs against native execution/test evidence before calling these methods. The
Python constructor is not an authorization boundary. Neither an arbitrary
caller-supplied scope nor an evidence dictionary is a sealed grant/attestation.

Start/stop/status are tested candidate CLI commands. They are not installed in
the active runtime. Stop prevents another task but preserves the outstanding
identity; it does not cancel a claimed execution. A future CLI must call the
authenticated operator socket, not mutate this journal directly.

Missing before deployment:

1. Sealed Codex helper identity, hosted-call authorization and redacted goal/
   roadmap evidence collection bound to exact source. The pluggable planner
   interface and three-call reservation journal are implemented; no hosted
   transport helper has been authorized or installed.
2. Installer validation and construction of a root-owned qualified backend,
   plus immutable daemon scheduling. The new broker dispatch fails closed until
   that backend is present. Worker control_publish denial remains unchanged.
3. Native result, tests, code-change and integration attestations. Initial scope
   is proposal-only; merging main requires separately explicit integration scope.
4. Worker receipt-observer wiring without browser effects,
   and isolated safety qualification before administrator installation.
5. Three real meaningful development cycles and restart tests through GitHub.

Run local tests from this isolated checkout:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_offline_continuation.py -v
```

The controller-loop simulation makes three real changes to a disposable fixture
module, executes its assertions, and uses an in-memory GitHub API and simulated
native proof. It cannot be cited as autonomous jobpipe development progress.

Candidate operator interface (after qualified administrator installation):

```sh
do-again controller start /Users/steventran/jobpipe
do-again controller status /Users/steventran/jobpipe
do-again controller stop /Users/steventran/jobpipe
```

These commands never accept a goal or grant from the caller; both must already
be bound in sealed configuration. They do not install, activate a grant, cancel
a claimed execution or acknowledge browser delivery.
