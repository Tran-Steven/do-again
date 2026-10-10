# Do Again

[![PyPI](https://img.shields.io/pypi/v/do-again?label=PyPI)](https://pypi.org/project/do-again/)
[![npm](https://img.shields.io/npm/v/do-again?label=npm)](https://www.npmjs.com/package/do-again)
[![CI](https://github.com/Tran-Steven/do-again/actions/workflows/ci.yml/badge.svg)](https://github.com/Tran-Steven/do-again/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/do-again)](https://pypi.org/project/do-again/)
[![License](https://img.shields.io/github/license/Tran-Steven/do-again)](https://github.com/Tran-Steven/do-again/blob/main/LICENSE)
[![npm downloads](https://img.shields.io/npm/dw/do-again)](https://www.npmjs.com/package/do-again)

Do Again is a developer-alpha local execution bridge for AI coding workflows. An agent writes a scoped request to a dedicated Git control branch, a local policy-controlled runner executes it, and a durable receipt comes back through Git. Optional browser automation connects that loop to a dedicated ChatGPT conversation without embedding an inference API key in the project.

## Architecture

~~~text
ChatGPT / coding agent
        |
        | scoped request JSON
        v
operator-control branch
        |
        v
local Do Again agent
  | validate policy / TTL / fingerprint
  | acquire durable claim
  | write local started ledger
  | execute local operation
  v
durable receipt + local ledger
        |
        +--> Git control branch
        +--> project ChatGPT conversation (optional)
~~~

The Git control branch is the source of truth for requests and receipts. Local ledgers and Git-backed claims provide exact-once and replay protection. The browser layer is optional and does not replace Git transport.

### Regular ChatGPT — no Codex or browser required

Do Again's primary model interface is **your existing ChatGPT conversation**.
ChatGPT writes a typed request to the dedicated Git control branch through a
connected GitHub integration; Do Again's policy-controlled local worker runs it
and writes an exact matching receipt. **Codex CLI is optional** and not required
for the core product or v1 release. Background browser automation is another
way to deliver the same Git-backed requests and receipts.

For an initial status-only handoff from your existing ChatGPT conversation:

~~~bash
do-again chatgpt prepare /path/to/project
~~~

The command creates **one original, durable local intent** and prints a prompt.
Paste the prompt into the regular ChatGPT conversation that has GitHub access.
It does *not* submit a request, call any AI model or start browser automation.
The ChatGPT conversation must actually publish the request on the specified
control branch. After the worker publishes the original matching receipt:

~~~bash
do-again chatgpt check chatgpt-verify-<original-24-hex-nonce> /path/to/project
~~~

This reads the remote Git request and receipt using their exact identity. It
reports `verification=git_receipt_only`; it **does not** assert that native
confinement or an unattended browser loop has passed. Missing receipts,
mismatched fingerprints, expired/ambiguous effects and changed control branches
must never be treated as successful autonomous development. Use
`do-again verify` for the separate automated ChatGPT-browser round trip, which
requires a signed-in dedicated browser session.

**Release status:** v1.0.0 requires real ordinary ChatGPT and authenticated
ChatGPT web/native evidence in
[the acceptance checklist](docs/v1-native-acceptance.md). The experimental
[Codex transport](docs/non-browser-codex-transport.md) is not the default and
its allowance or quota reset is never a release prerequisite.


The [dedicated macOS execution-user preview](docs/macos-execution.md) provides a separately installed supervisor and synthetic enforcement probes. The Agent source routes admitted script/test execution through this authenticated broker. Legacy runtime preparation, install, restart and foreground run now fail closed; engineering automation remains held until immutable daemon deployment, scoped repository brokers and remaining production gates pass. These source changes do not retrofit confinement into older copied installations.

## Install

~~~bash
pip install do-again
~~~

or:

~~~bash
npm install -g do-again
~~~

The npm launcher requires Node.js 18+ and Python 3.11+.

## Setup and verification

From the target repository:

~~~bash
do-again setup
~~~

Browser-enabled setup uses a dedicated Chrome or Chromium profile. On first use it may open visibly so you can sign into ChatGPT and complete human verification. Do Again does not request or store ChatGPT credentials.

Setup configures the project, control branch, policy, project automation chat, and optionally the background service. Configuration is intentionally separate from end-to-end readiness.

Run:

~~~bash
do-again verify
~~~

A successful verification prints SETUP_OK and verification=end_to_end only after a unique read-only request has traveled through the real ChatGPT -> Git -> local agent -> receipt loop.

## Normal operation

~~~bash
do-again start
do-again status
do-again stop
do-again list
~~~

The stop command prints a short **work recap** and saves a local Markdown
snapshot in the project's private Do Again state directory under
`session_reports/`. It covers the current daemon session when the startup
timestamp is known; otherwise it explicitly falls back to the last 24 hours.
It shows successful, failed, and unfinished requests, project Git commits,
watchdog state, and browser-delivery warnings. A successful request is **not**
automatically labeled a shipped feature, and local commits may include work
outside Do Again. Recaps are never posted to GitHub or ChatGPT.

To view a digest without ending the session:

~~~bash
do-again summary
do-again summary --hours 8
do-again summary --json
~~~

To update a deliberately stopped project without starting it or replaying
pending requests, use the guarded offline runtime stage:

~~~bash
do-again upgrade /path/to/stopped-project --apply --offline
~~~

This refuses running services, started requests, unsettled browser deliveries,
and active executor children. Queued *unstarted* requests are preserved and
only the backed-up copied runtime is replaced. The service remains stopped
until explicitly started later.

Upgrade preflight reads the same `state/ledger` records written by the agent,
including orphaned started records and records whose receipts were published
incompletely. Corrupt execution records and durable uncertain browser intents
block upgrades even when the request queue or outbox appears empty. Legacy
`state/requests` records remain conservative blockers until reconciled.

The reports are also saved as `session_reports/latest.md` and
`session_reports/latest.json` after a `do-again stop`. Stopping one
project does not stop other projects; closing a ChatGPT tab does not stop
the Do Again service or produce a report.

Attended mode without a background service:

~~~bash
do-again setup --no-service
do-again run
~~~

Git/local mode without ChatGPT browser delivery:

~~~bash
do-again setup --no-browser
~~~

## Request and receipt safety

Requests live under automation/do_again/requests/<request-id>.json and receipts under automation/do_again/receipts/<request-id>.json.

Core invariants:

- request IDs are fingerprinted against their content;
- changed content under a reused request ID is a conflict;
- Git-backed claims prevent two agents from both executing one request;
- a durable local started ledger is written before execution;
- ambiguous started state is never silently replayed;
- ambiguous replay becomes blocked_ambiguous_replay;
- terminal receipts can be republished from the local ledger without re-execution.

### Operator acknowledgement and goal progress

Requests may include continuation metadata with acknowledged_receipts, goal_state, optional goal_id, and summary. Acknowledged receipt IDs must already exist durably.

This deliberately separates three states:

1. the browser delivered a receipt to ChatGPT;
2. the operator acknowledged that receipt;
3. the overall development goal is in_progress, completed, or blocked.

A sent browser message is therefore not treated as proof that a task is complete.

## Diagnostics

Recent history:

~~~bash
do-again history
do-again history --limit 50
~~~

Trace one request across request, claim, local ledger, receipt, and conflicts:

~~~bash
do-again trace <request-id>
~~~

Read or follow service logs:

~~~bash
do-again logs
do-again logs --stream stderr
do-again logs --follow
~~~

Repository-aware health checks:

~~~bash
do-again doctor
do-again doctor --fix
~~~

doctor --fix is conservative. It may repair deterministic local state such as missing runtime directories or a missing clean control worktree. It does not silently reinstall or restart services, overwrite the copied runtime, switch browser modes, or bypass ChatGPT authentication.

## Safe cancel and retry

Cancel:

~~~bash
do-again cancel <request-id>
~~~

Cancellation is allowed only while the request is demonstrably unclaimed, not started, and non-terminal. A durable cancellation tombstone is written before execution. Once a claim, started ledger, or receipt exists, cancellation refuses rather than pretending an in-flight mutation was stopped.

Retry:

~~~bash
do-again retry <request-id>
~~~

Retry always creates a fresh request ID, refreshes timestamps, preserves operation arguments and expectations, and links the new request to the prior terminal receipt.

Successful requests are not retryable. blocked_ambiguous_replay is not auto-retried; inspect it with trace and create a newly scoped request after determining whether the original mutation may have executed.

## Browser runtime

Do Again uses a dedicated automation profile rather than the user's normal Chrome profile. CDP is loopback-only.

Default browser mode is auto:

1. initial authentication may be visible;
2. the saved profile is tested in modern Chromium headless mode;
3. if that authenticated session is unreliable headless, Do Again falls back to a real background browser process;
4. later operation reuses the verified mode without normal foreground interaction.

Useful controls:

~~~bash
do-again browser status
do-again browser test
do-again browser login
do-again browser stop
~~~

Browser receipt delivery uses a durable outbox. Failed sends remain queued instead of losing the local receipt.

## Transactional conversation rollover

Do Again can proactively move a project to a fresh ChatGPT conversation before excessive conversation length, while retaining ChatGPT's explicit context-limit warning as a hard fallback.

Rollover sequence:

1. build a durable checkpoint grounded in Git branch and HEAD, request IDs, receipt states, and receipt hashes;
2. create a unique handoff token;
3. create a background successor chat;
4. send a bounded checkpoint transfer view that includes the durable checkpoint path;
5. require the exact response DO_AGAIN_HANDOFF_READY <token>;
6. record the successor as owned;
7. update the active project binding only after acknowledgement;
8. queue the predecessor for archival only after the new binding is durable.

If successor creation or acknowledgement fails, the predecessor remains active. Archive failure does not undo a successful handoff; it becomes retryable cleanup state.

## Safe chat cleanup

Do Again never deletes conversations.

Automatic archival requires strong ownership provenance:

- the chat was created by Do Again and is recorded in the project's durable owned-chat registry; or
- a historical chat is independently verified by its project bootstrap content and corroborating local request/receipt evidence, a durable project rollover/checkpoint reference, or a URL from Do Again's dedicated Chrome-profile history. Both initial readiness and acknowledged rollover-handoff conversations are supported.

Every currently bound project chat is globally excluded from archival.

~~~bash
do-again chats discover
do-again chats verify --candidate <conversation-id>
do-again chats cleanup
do-again chats cleanup --apply
do-again chats status
do-again chats run
~~~

`chats discover` automatically surfaces predecessors from the saved project binding, rollover transaction, durable checkpoints, and read-only history of Do Again's dedicated Chrome profile without treating any URL alone as proof of ownership. Use `chats verify --candidate <conversation-id>` to confirm a historical candidate against its actual authenticated chat content before it becomes eligible. Cleanup is dry-run by default. Archival retries are idempotent. Unowned or active chats are blocked before archive UI interaction.

## Service lifecycle

Do Again uses launchd on macOS, a systemd user service on Linux, and Task Scheduler on Windows.

~~~bash
do-again start
do-again restart
do-again stop
~~~

Each project gets an isolated runtime, control worktree, state directory, policy copy, and service label under ~/.do_again. The background service runs from a copied runtime so it remains stable after the invoking shell exits.

## Continuous development and stall reporting

A healthy daemon is not proof that the agent is progressing. Continuous development is opt-in per project. For an ongoing goal, add these values under [do_again] in do-again.toml:

~~~toml
continuous = true
idle_seconds = 1800
recovery_seconds = 900
report_stalls = true
stall_issue_repo = "YOUR_ORG/do-again"
~~~

With continuous mode enabled, Do Again evaluates project liveness after browser receipt delivery is drained. It waits during active model generation or pending local requests, performs bounded durable continuation for an idle unfinished goal, and escalates repeated no-progress or stuck states instead of prompting forever.

Issue reporting is optional and requires an authenticated GitHub CLI with permission to create issues in the configured destination. Reports are sanitized and deduplicated; browser content, credentials, prompts, candidate/customer data, and raw private logs are excluded. If GitHub reporting is unavailable, local liveness state remains visible through do-again status.

Continuous mode does not make browser submission equivalent to acknowledged progress, does not replay uncertain non-idempotent claims, and does not change application/submission authorization. Disable continuous mode for intentionally completed or paused projects.

## Security model

Do Again is built around explicit operation, binary, and path policy; dedicated control-branch validation; bounded TTLs; request fingerprints; local exact-once ledgers; Git-backed distributed claims; project-scoped runtime state; a dedicated browser profile; localhost-only CDP; no stored ChatGPT credentials; no automatic replay of ambiguous mutations; and no archival without verified ownership provenance.

It should not be configured as unrestricted shell access.

## Validation and resilience

Full unit and regression suite:

~~~bash
PYTHONPATH=src python -m unittest discover -s tests -v
~~~

Disposable browser smoke:

~~~bash
python tools/browser_smoke.py
~~~

Isolated local-agent soak benchmark:

~~~bash
PYTHONPATH=src python tools/agent_soak.py --requests 250
~~~

The soak benchmark measures local Agent.process_path processing only. It excludes Git remote, browser, ChatGPT, and network latency.

A representative 250-request hardening run measured:

~~~text
requests:          250
executions:        250
duplicate replays: 250
restart replays:   250
exact once:        true

median: 5.624 ms
p95:   11.314 ms
max:   19.860 ms
~~~

These are empirical local-machine measurements, not an end-to-end SLA.

## Development

~~~bash
git clone https://github.com/Tran-Steven/do-again.git
cd do-again
python -m pip install .
PYTHONPATH=src python -m unittest discover -s tests -v
python tools/browser_smoke.py
PYTHONPATH=src python tools/agent_soak.py --requests 250
do-again doctor
~~~

CI covers Python 3.11, 3.12, and 3.13 across Linux, macOS, and Windows, plus browser-smoke, npm-package, and artifact-install validation.

## License

MIT

Rollout ignores only children positively identified as exited zombies. Live children and children with unavailable process state still block cutover. Process filtering does not override started-ledger or uncertain-delivery blockers.

Browser submission uses one Enter gesture. Missing composer acceptance and all exceptions after that gesture are uncertain and prohibit a fallback click or automatic resend. Pre-dispatch composer failures remain distinguishable. A durable pre-dispatch intent journal and separate acknowledgment are still required before M3 acceptance.
### Operator authority journal (integration foundation)

`AuthorityRegistry` stores explicit `active`, `paused`, `maintenance`, and
`stopped` intent, an accepted goal revision, and a monotonically increasing
authority epoch in a separate SQLite journal. Missing, corrupt, unsupported,
symlinked, or unregistered authority denies admission. Initialization grants
no project authority. Admission and pause use the same transaction lock:
operator pause waits for an effect's initiation boundary, then prevents new
admissions while admitted execution may drain. Project identity comes from
the trusted repository connection, never a request payload.

This module is an integration foundation, not a deployed security boundary.
Privileged entrypoints still need integration, and native confinement must
protect the journal before arbitrary scripts can run under its authority.
Do not deploy or claim M2 acceptance from these registry tests alone.

### Scoped local Git transactions (v1 integration foundation)

The dedicated-identity broker's local Git transaction primitives copy only Git
object, index and reference data into request scratch space. Repository hooks,
configuration includes, filters, alternates and replacement references are not
imported. A transaction binds an exact starting commit and a supervisor-reserved
`do-again/` branch; commits rebuild the index and accept explicit relative paths.
Candidate metadata is validated and copied to a fresh sealed directory before
any future authoritative promotion. Git must execute through the native
confinement boundary, never as the root supervisor.

The macOS bundle preparer seals an Apple-signed Git executable alongside Python.
Installation qualification now requires a real confined edit-to-commit transaction
in synthetic scratch space, including exact-path and unchanged-authority checks.
A missing native Git binary blocks qualification without host-tool fallback.

These primitives are not yet exposed by the installed broker. Guarded metadata
promotion, durable effect admission, repository publication, approved dependency
retrieval and immutable worker integration remain release blockers. Their fixture
tests do not qualify native execution or the autonomous development loop. Both
projects remain in maintenance until installed end-to-end evidence passes.

Native qualification retains escape-test evidence in its original scratch
space and allocates fresh roots for the following Git transaction. Forbidden
aliases remain rejected; proof sequencing does not relax confinement.
