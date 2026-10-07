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

Build an optional public browser transport without coupling the core runtime to a browser.

Target UX:

    do-again setup
    # optional guided ChatGPT connection from setup

Requirements:

- Dedicated automation Chrome profile; never reuse the user's normal browser profile.
- One-time visible setup for Cloudflare/sign-in only when necessary.
- Verify the real ChatGPT composer before claiming setup success.
- Bind and validate the selected automation conversation.
- No foreground focus stealing after the one-time setup step.
- Safe new-chat rollover when a conversation reaches its context limit.
- Clear browser health/status diagnostics and recovery instructions.
- Keep browser state local; never publish cookies, tokens, or browser-profile data.
- Preserve Git-backed request/receipt transport so browser automation is replaceable.

The existing private bridge prototype is reference material only. It must be generalized and stripped of private repository, Vast, persona, and legacy control-plane assumptions before becoming public.

## P1 — diagnostics and recovery

- [ ] Make `doctor` repository-aware and explain exactly how to repair failed checks.
- [ ] Surface recent request/receipt failures without requiring users to inspect the control branch manually.
- [ ] Add safe repair/reinstall behavior for damaged service definitions and runtime copies.
- [ ] Add upgrade-path tests from older Do Again releases.

## P2 — broader integration

- [ ] Pluggable transports beyond Git-backed control branches.
- [ ] More end-to-end service tests on real Linux and Windows hosts, not only CI-level mocks/contracts.
- [ ] Stable machine-readable CLI output for external agent integrations.
- [ ] Signed release/install verification guidance.
