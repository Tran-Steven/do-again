# Do Again Roadmap

Do Again is a developer-alpha local execution bridge. Near-term work emphasizes reliability, recovery, and operator clarity before broader integration.

## P0 reliable core

- [x] One-command project onboarding
- [x] Project-specific policy
- [x] Dedicated control-branch validation
- [x] Custom Git remote support
- [x] Future-dated request rejection
- [x] Request fingerprint and ID-collision detection
- [x] Durable local request locks
- [x] Distributed Git-backed claims
- [x] Durable pre-execution started ledger
- [x] Ambiguous replay blocking
- [x] Observable malformed-request records
- [x] macOS launchd lifecycle
- [x] Linux systemd user-service lifecycle
- [x] Windows Task Scheduler lifecycle

## P1 browser and ChatGPT reliability

- [x] Dedicated persistent browser profile
- [x] One-time visible authentication flow
- [x] Real composer/session verification over localhost CDP
- [x] Headless operation when reliable
- [x] Background-headed fallback
- [x] Per-project automation conversation binding
- [x] Shared browser runtime across concurrent projects
- [x] Browser crash recovery
- [x] Durable browser receipt outbox
- [x] Explicit auth-required state
- [x] Proactive rollover threshold plus hard context-limit fallback
- [x] Transactional successor acknowledgement before binding update
- [x] Durable Git/receipt-grounded rollover checkpoints
- [x] Bounded checkpoint transfer view
- [x] Verified-owned predecessor archival only after successful handoff
- [x] Retryable archive failure without invalidating the new binding
- [x] No chat deletion

## P1 acknowledgement and progress

- [x] Durable acknowledgement of prior receipts through continuation metadata
- [x] Explicit goal state: in-progress, completed, blocked
- [x] Receipt echo of operator progress
- [x] Separate browser delivery, operator acknowledgement, and goal completion
- [ ] Optional long-term progress index beyond request-file history

## P1 diagnostics and recovery

- [x] Repository-aware doctor
- [x] Conservative doctor --fix
- [x] history
- [x] trace <request-id>
- [x] logs --follow
- [x] Safe pre-execution cancellation
- [x] Fresh-ID retry for terminal failed or blocked requests
- [x] Explicit refusal to auto-retry ambiguous replay
- [x] Browser delivery status and pending-receipt count
- [x] End-to-end verify command proving ChatGPT -> Git -> local -> receipt
- [ ] Explicit safe service-runtime reinstall/repair command distinct from doctor --fix
- [ ] Upgrade-path tests from older releases

## P1 cleanup and conversation lifecycle

- [x] Durable owned-chat registry
- [x] Global exclusion of all active bound project chats
- [x] Dry-run cleanup inventory
- [x] Historical verification requiring bootstrap markers plus receipt evidence
- [x] Idempotent archive queue
- [x] Active, unowned, duplicate, and archive-failure tests
- [x] Automatic archive attempt after verified rollover
- [ ] Live disposable-chat integration coverage for archive UI

## P1 resilience and measurement

- [x] Request fingerprint/replay regression coverage
- [x] Concurrent-agent claim coverage
- [x] Browser crash/auth/delivery recovery coverage
- [x] Partial rollover and archive-retry coverage
- [x] Measured isolated local-agent soak benchmark
- [x] Duplicate-delivery and restart-replay soak assertions
- [ ] Longer unattended soak runs in CI
- [ ] End-to-end latency instrumentation split by Git transport, local execution, browser delivery, and ChatGPT response
- [ ] Controlled fault injection for Git push/rebase, browser, and network failures

## P2 broader integration

- [ ] Pluggable transports beyond Git-backed control branches
- [ ] More real-host Linux and Windows lifecycle tests
- [ ] Stable versioned machine-readable CLI output
- [ ] Signed release/install verification guidance
- [ ] Optional structured metrics export

## Merge baseline

Before merging major reliability work:

- full unit/regression suite green;
- browser-hardening suites green;
- isolated local-agent soak green;
- branch clean;
- development branch pushed;
- pull request CI green;
- active installed runtime not self-overwritten during self-dogfood development.
