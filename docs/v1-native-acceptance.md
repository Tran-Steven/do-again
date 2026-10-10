# Do Again v1.0 acceptance gate (non-browser Codex transport)

**Status: blocked pending installed native qualification and live synthetic acceptance.**
This file is a release checklist, not a declaration of production readiness.
No release, merge, tag, real job application, Sonary work, or browser login is
authorized by the existence of the code below.

## Fixed scope and transport

- Protected macOS root broker source must exactly equal the **reviewed CI-passing
  candidate commit**, with its immutable manifest and administrator-approved
  installation. An old, unrelated source's native probes do not qualify the
  new candidate.
- The root-installed `codex_canary` grant and the legacy browser
  `live_canary` grant are mutually exclusive. **Do not** reinterpret, replay
  or migrate an old ChatGPT conversation grant.
- The sealed parent Do Again and sibling jobpipe projects must both remain
  in quiescent verified maintenance, with unchanged parent epoch and source.
- The dedicated Codex child must have its own root-installed project identity,
  isolated worktree, native sandbox, immutable isolated CLI digest,
  pre-staged launchd worker, one-shot root activation, and no browser/CDP.
- Codex included usage must be explicitly allowed by the authenticated account
  quota gate. A signed-in CLI alone is not sufficient. Never automatically
  consume available full resets, purchase credits, or retry an ambiguous model
  call.
- Exactly two non-browser synthetic model calls are authorized, at most once
  each per nonce, with private `O_EXCL` fsynced journals. A started, timed-out
  or uncertain model call cannot be retried.

## Synthetic acceptance: native effects

Each task must execute only:

```text
root-gated canonical model edit
    -> exact native test
    -> two-file native git commit
    -> draft-only native PR publication
    -> read-only CI observation
```

Only these two files are writable or publishable:

- `canary_live_<24-hex-nonce>.py`
- `tests/test_live_canary_<24-hex-nonce>.py`

Each follow-up control request must be derived by the root broker from its
predecessor's exact Git blob, remote receipt and durable native execution
ledger. The worker can select only the next fixed stage and **cannot** author
commands, GitHub refs, publication metadata, or a repair/replay payload.
Original in-flight or uncertain effects block advancement.

Task two is unavailable until the root-owned `codex-ci-task-one.json`
checkpoint binds an actual terminal successful CI run to the original draft PR,
exact Git head, installed source and parent epoch. A successful polling receipt
or user-authored success claim is not this proof. Task two uses its own
distinct one-shot model reservation and the resulting first-task CI head.

Final acceptance requires a different second commit, the **same draft PR**,
a terminal successful second run on its exact head, and a root-written
`codex-two-task-complete.json` certificate. On success the native child must
return to maintenance; the two parent projects must stay in maintenance and
`production_ready` must remain **false**. This evidence alone is not permission
to submit applications.

## v1 gate — all mandatory

1. Candidate PR has full cross-platform CI green on exactly its final SHA.
2. A bounded, administrator-authorized protected installation uses that same
   SHA; all previous runtime/grant state is preserved or explicitly classified,
   never overwritten to discard an uncertain effect.
3. A fresh, separately sealed Codex grant is installed and a new dedicated
   child is initialized, staged and native-probed. Native probes pass for the
   candidate on the Do Again parent, jobpipe sibling **and** isolated child.
4. Login and backend included-usage permission pass with no credential logs.
5. The two synthetic model proposals complete with distinct reserved identities.
6. Every edit/test/commit/PR/CI step has exact original request, root ledger,
   remote receipt and GitHub provenance without ambiguous retries.
7. First and second CI are terminal success on one draft PR with different
   exact heads; immutable root completion evidence is present and verified.
8. Child worker/process/service is withdrawn or conclusively inactive, parent
   projects are still in maintenance, and native confinement still passes.
9. Release PR review, version synchronization and package provenance checks
   pass. Only then may a separate explicit release action merge, tag and
   publish v1.0.0.

**If any gate is missing, keep the PR draft and leave v1 unpublished.**
Never replace a missing real acceptance receipt with an offline test assertion
or an invented acknowledgment.
