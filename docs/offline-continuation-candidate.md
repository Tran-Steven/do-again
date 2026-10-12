# Offline continuation candidate — not deployable

The native continuation state machine is prepared for the existing immutable
worker and root broker. It performs no GitHub, browser, model, or installation
effects. It is not connected to a running controller.

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

Start/stop/status exist as tested state-machine methods only. They are not
installed CLI commands. Stop prevents another task but preserves the outstanding
identity; it does not cancel a claimed execution. A future CLI must call the
authenticated operator socket, not mutate this journal directly.

Missing before deployment:

1. Approved offline planner and sealed engine identity, a goal snapshot and
   roadmap evidence collection bound to exact source.
2. Separate controller capability, installer validation and root broker dispatch.
3. Canonical request publication with existing GitHub snapshot/CAS/readback and
   reserved-ID reconciliation. Worker control_publish denial remains unchanged.
4. Native result, tests, code-change and integration attestations. Initial scope
   is proposal-only; merging main requires separately explicit integration scope.
5. Worker receipt-observer wiring without browser effects, CLI operator controls,
   and isolated safety qualification before administrator installation.
6. Three real meaningful development cycles and restart tests through GitHub.

Run local tests from this isolated checkout:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_offline_continuation.py -v
```

The three-cycle test uses fake proposals and completion evidence. It verifies
journal transitions and budgets only and cannot be cited as autonomous jobpipe
development progress.
