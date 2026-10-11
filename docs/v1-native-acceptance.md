# Do Again v1.0 acceptance gate — ChatGPT-first

**Status: not released.** This is a gate, not an implementation claim.
The primary product is ChatGPT-to-Git-to-local-execution-to-receipt, using
the user's ordinary ChatGPT conversations or an authorized dedicated ChatGPT
web session. An inference API key, Codex CLI, Codex Work quota, purchased
credits or banked quota reset **must not be required** for v1.

## Contract

- **Regular ChatGPT conversation**: the model can publish an exact typed
  request to the dedicated Git control branch with GitHub tools. The native
  Do Again worker performs the operation and publishes a durable receipt. The
  model observes that receipt. No CDP or CLI model call is required. The
  operator can run `do-again chatgpt prepare` and later
  `do-again chatgpt check <original-id>` for a harmless status-only handshake.
  `check` returns **git_receipt_only**, not native, autonomous or deployment
  acceptance. No request is submitted automatically by `prepare`.
  Local terminal ledger verification additionally requires an owner- and
  permission-checked POSIX filesystem. On platforms without that verification
  the result remains unverified even if the Git receipt and local JSON match.
  POSIX ledger verification uses descriptor-based file reads, rejects aliases,
  hardlinks and writable directory ownership boundaries, and bounds local
  receipt input to 1 MiB.
- **ChatGPT web automation**: the existing dedicated Chrome profile and
  authenticated conversation can transport requests, receipts, and CI
  continuations without stealing focus. If ChatGPT requires a visible human
  challenge, returns an ambiguous submission outcome, or lacks an authenticated
  session, stop and surface the original event; never bypass the challenge,
  click a second time, or claim unattended readiness.
- **Codex CLI**: an optional experimental alternative transport. Its isolated
  two-task canary and root grants remain preserved for future opt-in work, but
  must **not** be a release dependency, the default transport, or a reason to
  redeem usage credits. A Codex-only passing run is not a ChatGPT acceptance.
- **Execution**: all code, testing, Git mutations and native/CI qualification
  continue through the authenticated root broker and its original receipts.
  No normal ChatGPT message or browser DOM claim grants execution or production.
  The parent Do Again and jobpipe projects remain in maintenance during the
  synthetic canary. Real job applications are excluded.
- The previously installed browser canary was bound to its own chat and nonce.
  Do not repurpose or replay that grant. A fresh candidate requires explicit
  operator authorization, new identity and separate root-installed scope.

## Mandatory release evidence

1. The final release candidate SHA passes all cross-platform CI jobs with
   Python/npm versions synchronized **after** ChatGPT qualification.
2. The final protected Mac source matches that SHA. An administrator-approved
   installation preserves original effect and browser delivery journals.
   Native sandbox and worker identity probes pass for both parents and the
   dedicated isolated ChatGPT canary.
3. A **real ordinary ChatGPT conversation** authors at least one harmless
   request through GitHub; its actual original request, worker result, receipt
   fingerprint and Git remote history are checked. A prepared prompt or
   ChatGPT assertion alone is insufficient.
4. A **fresh dedicated ChatGPT web conversation**, if used for unattended
   acceptance, proves authenticated background/headless delivery through the
   browser's original write-once dispatch journal, **actual** exact assistant
   acknowledgment and immutable receipt evidence. No login bypass, fabricated
   ACK, silent visible focus stealing, or uncertain resend is accepted.
5. ChatGPT (not Codex CLI) originates **two sequential isolated synthetic
   changes**, with edit → native test → confined Git commit → same draft PR →
   read-only exact-head CI. The second requires the first PR receipt, actual
   passing CI and the original conversation's acknowledgment. Distinct heads
   on one PR and native proof must be present. This test does not submit
   applications or change Sonary.
6. A bad receipt, interrupted session, expired request, unreadable browser,
   native blocker, or uncertain Git effect leaves the task unaccepted. It must
   be explicitly reconciled by read-only evidence or stopped—not inferred to
   have succeeded.
7. The isolated child stops and parent projects remain native-verified,
   quiescent and in maintenance; `production_ready=false`. Release packaging
   and tags must not silently authorize production.
8. Review the PR, verify published artifacts/provenance and exact source,
   then separately approve merge/tag and publish `v1.0.0`.

## Release blockers tracked separately

- **Code CI**: necessary but not sufficient. A 17/17 result does not prove a
  real ChatGPT browser workflow or administrator-qualified native deployment.
- **Host interaction**: the operator-controlled Mac needs an approved
  administrator dialog. Unattended sudo bypass is not acceptable.
- **Browser session**: if ChatGPT challenges a headless session, request human
  authentication and retain the original chat; do not solve by switching the
  inference transport to Codex.
- **Model quota**: Codex quota and reset credits are irrelevant to this
  ChatGPT-first gate. Never spend them by default.

**If any mandatory evidence is missing, keep the PR draft and do not tag or
publish v1.0.0.** A manual Git receipt is progress, not proof of unattended
ChatGPT messaging or a native two-task release canary.
