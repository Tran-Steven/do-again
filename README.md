# Do Again

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
- a historical chat is independently verified by its project bootstrap content and corroborating local request/receipt evidence, or by the same content plus a durable project rollover/checkpoint reference. Both initial readiness and acknowledged rollover-handoff conversations are supported.

Every currently bound project chat is globally excluded from archival.

~~~bash
do-again chats discover
do-again chats verify --candidate <conversation-id>
do-again chats cleanup
do-again chats cleanup --apply
do-again chats status
do-again chats run
~~~

`chats discover` automatically surfaces predecessors from the saved project binding, rollover transaction, and durable checkpoints without treating those references alone as proof of ownership. Use `chats verify --candidate <conversation-id>` to confirm a historical candidate against its actual authenticated chat content before it becomes eligible. Cleanup is dry-run by default. Archival retries are idempotent. Unowned or active chats are blocked before archive UI interaction.

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
