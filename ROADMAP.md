# Do Again Roadmap

Do Again should be easy on the first run and still expose lower-level controls for advanced users.

## P0 — reliable core

- [x] One-command local onboarding with `do-again setup`
- [x] Project-specific policy file created by `do-again init` / `setup`
- [x] Safe dedicated control-branch validation
- [x] Custom Git remote support
- [x] Future-dated request rejection
- [x] Request-ID collision detection
- [x] Durable local request locks
- [x] Distributed Git-backed request claims
- [x] Observable malformed-request records
- [x] Chronological request ordering across timezone offsets
- [x] macOS launchd lifecycle
- [x] Linux systemd user-service lifecycle
- [x] Windows per-user Task Scheduler lifecycle

## P1 — frictionless ChatGPT connection

- [x] Dedicated persistent automation Chrome/Chromium profile
- [x] One-time visible ChatGPT sign-in / human-verification flow
- [x] Real composer/session verification over localhost CDP
- [x] True `--headless=new` operation when the authenticated session supports it
- [x] Automatic background-headed fallback when true headless is unreliable
- [x] No normal-profile attachment and no post-setup foreground focus requirement
- [x] Per-project automation conversation binding
- [x] Shared browser runtime across concurrent projects
- [x] Browser crash detection and automatic restart
- [x] Durable browser-delivery outbox with idempotent receipt retries
- [x] Explicit `auth_required` state instead of challenge/auth bypass loops
- [x] Automatic safe new-chat rollover on conversation-length limits
- [x] Browser state remains local; credentials are never requested or stored
- [x] Git-backed request/receipt transport remains independent of the browser layer

## P1 — diagnostics and recovery

- [x] Make `doctor` repository-aware and report browser/runtime repair information.
- [x] Surface browser delivery failures and pending receipt count through `status`.
- [ ] Surface recent core request/receipt failures without requiring users to inspect the control branch manually.
- [ ] Add safe repair/reinstall behavior for damaged service definitions and runtime copies.
- [ ] Add upgrade-path tests from older Do Again releases.

## P2 — broader integration

- [ ] Pluggable transports beyond Git-backed control branches.
- [ ] More end-to-end service tests on real Linux and Windows hosts, not only CI-level mocks/contracts.
- [ ] Stable machine-readable CLI output for external agent integrations.
- [ ] Signed release/install verification guidance.

## 0.3.0 release validation

- [x] Unit and regression tests for shared startup, stale PID/port protection, auth recovery, rollover, and durable delivery.
- [x] Real macOS Chrome smoke tests with disposable profiles, persistence, forced crash recovery, and background launch.
- [x] Clean wheel, sdist, and npm installation verification.
- [x] Browser hardening tests executed through a real Do Again request/receipt loop.
- [x] Real Chrome smoke tests pass in Linux and Windows CI.
- [x] Authenticated ChatGPT message/response succeeds after human verification and runtime restart (macOS background fallback).
- [ ] Publish 0.3.0 through Trusted Publishing after validation passes.
