# Do Again

[![CI](https://github.com/Tran-Steven/do-again/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Tran-Steven/do-again/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/do-again.svg?cacheSeconds=300)](https://pypi.org/project/do-again/)
[![npm](https://img.shields.io/npm/v/do-again.svg?cacheSeconds=300)](https://www.npmjs.com/package/do-again)
[![Python](https://img.shields.io/pypi/pyversions/do-again.svg?cacheSeconds=300)](https://pypi.org/project/do-again/)
[![License](https://img.shields.io/github/license/Tran-Steven/do-again.svg?cacheSeconds=300)](LICENSE)

Do Again gives AI coding agents a policy-controlled way to execute work on a local development machine, return auditable receipts and artifacts, and keep iterating when another pass is needed.

## How it works

```text
agent
  |
  | request
  v
operator-control branch
  |
  v
Do Again -> policy validation -> local command / file / test / git action
  ^                                         |
  |                                         |
  +----------- receipt / artifact -----------+
```

The agent can inspect the result, decide what still needs work, and submit another scoped action instead of stopping at the first attempt.

## Install

### Python / PyPI

```bash
pip install do-again
```

### Node / npm

```bash
npm install -g do-again
```

or run it without a global install:

```bash
npx do-again setup
```

Both distributions expose the same command:

```bash
do-again
```

The npm package ships the same Python runtime from this repository behind a small Node launcher. It requires Node.js 18+ and Python 3.11+.

## Quick start

From the Git repository you want an agent to work on:

```bash
pip install do-again
cd my-project
do-again setup
do-again start
do-again status
```

On the first browser-enabled setup, Do Again creates a dedicated automation Chrome/Chromium profile and opens it visibly so you can sign into ChatGPT and complete any human verification. Do Again watches for the real composer and continues automatically when sign-in is complete; you do not need to return to the terminal and confirm it. Do Again does not ask for or store your ChatGPT username or password.

After that one-time authentication step, setup automatically:

- verifies the real ChatGPT composer through CDP;
- tests the saved session with Chrome's current `--headless=new` mode;
- uses true headless mode when it is reliable, otherwise falls back to a real background Chrome instance;
- creates and binds a dedicated automation conversation for the project;
- creates the Git control branch and project policy;
- installs the native user-level background service; and
- verifies the local runtime.

Normal use is intentionally small:

```bash
do-again start
do-again status
do-again stop
do-again list
```

`setup` already starts the project, so `start` is mainly for bringing it back later. Multiple projects can run at the same time; they share one dedicated authenticated browser runtime while keeping separate project conversations, service state, control worktrees, policies, and receipt queues.

If you do not want ChatGPT browser automation for a project:

```bash
do-again setup --no-browser
```

For attended use without installing a native background service:

```bash
do-again setup --no-service
do-again run
```

## Browser runtime

The browser is an implementation detail during normal operation. Do Again never attaches to your everyday Chrome/Chromium profile and does not touch your normal tabs, cookies, extensions, Firefox session, or browser history.

The shared automation profile lives under `~/.do_again/browser/profile` by default. CDP listens only on loopback, and Do Again chooses another local port if its preferred port is occupied.

The default browser mode is `auto`:

1. first authentication is visible and interactive;
2. Do Again restarts the same persistent profile with `--headless=new`;
3. if the authenticated ChatGPT session is not reliable in true headless mode, it automatically falls back to a real Chrome process running without a startup window;
4. later starts reuse the verified mode without opening a foreground window.

The daemon monitors the browser and restarts it after crashes. Browser delivery uses a durable per-project outbox, so a receipt is retried after browser/network failures instead of being lost. Receipt markers make retries idempotent. If ChatGPT reports that a conversation reached its maximum length, Do Again creates a fresh background conversation, carries over recent conversation excerpts, bootstraps it from the Git control state, rebinds the project, and continues there.

If headless authentication fails, Do Again first checks the background browser with the same profile. If both modes fail, Do Again reports `auth_required` and waits for interactive setup. It does not repeatedly launch browsers or bypass verification. Local request execution continues, and browser receipts stay queued. Re-run `do-again setup` to reopen only the dedicated automation profile for human interaction.

Advanced/debug controls remain available when needed:

```bash
do-again browser status
do-again browser test
do-again browser login
do-again browser stop
```

You can force a browser mode during setup with `--browser-mode headless` or `--browser-mode background`; `auto` is recommended.

## Service lifecycle

Do Again installs a per-repository background agent using the native user-level service manager:

```bash
do-again start
do-again status
do-again restart
do-again stop
```

Low-level `install`, `uninstall`, `init`, and foreground `run` commands remain available for advanced use and backward compatibility, but they are intentionally omitted from the primary help surface.

Each repository gets an isolated runtime, control worktree, state directory, policy copy, and service label under `~/.do_again`. The installed service runs from a copied runtime so installs made through either PyPI or npm remain stable after the invoking shell exits. `do-again init` also creates `do-again-policy.json` for project-specific operation, binary, root, timeout, and execution controls. The control branch must be dedicated and cannot be `main`, `master`, `trunk`, or the currently checked-out branch.

## Continuous development and stall reporting

A healthy daemon is not proof that the agent is progressing. Continuous development
is **opt-in** per project. For an ongoing goal, add the following to that project's
`do-again.toml` under `[do_again]`:

```toml
continuous = true
idle_seconds = 1800
recovery_seconds = 900
report_stalls = true
stall_issue_repo = "YOUR_ORG/do-again"
```

With this enabled, Do Again checks the dedicated project conversation and request
ledger after the receipt queue is drained. It waits during active model generation
or pending requests, retries an idle goal at most twice with durable cooldowns,
and classifies repeated no-progress, stuck generation, and stale requests as
stalls. After a verified new receipt, the recovery budget resets. Disabling
`continuous` is the explicit way to stop a finished project's auto-continuation.

Issue reporting requires an authenticated GitHub CLI (`gh auth login`) with
permission to create issues in the configured destination. Reported issues
contain only a sanitized project slug and failure category, never browser content,
candidate data, credentials, raw logs, or prompts. Issues are deduplicated by
failure class and project and reporting failures are bounded. If reporting is
unavailable, `do-again status` still exposes the local stall state.

An idle model can be asked to continue but cannot be forced to deliver useful
engineering work. The watchdog avoids infinite prompt loops; use independent
acceptance checks and periodic product reviews before interpreting receipt volume
as progress. `continuous` does not change submission/approval authorization.

After editing configuration or upgrading the package, reinstall the project's
background runtime with `do-again install /path/to/project`.

## Why Do Again

- Explicit policy-based command permissions
- GitHub-backed request and receipt transport
- Project-scoped filesystem and command access
- Deterministic audit trail and recovery
- No agent API keys embedded in the project
- Dedicated ChatGPT browser profile with headless-first operation
- Durable retry/recovery when the browser crashes or a send fails
- One shared browser runtime for multiple concurrent projects
- A shared platform interface for macOS, Linux, and Windows
- CI-tested on Python 3.11, 3.12, and 3.13 across all three platforms

## Platform status

| Platform | Service backend | Browser runtime |
| --- | --- | --- |
| macOS | launchd | Chrome/Chromium CDP, headless-first |
| Linux | systemd user service | Chrome/Chromium CDP, headless-first |
| Windows | per-user Task Scheduler | Chrome/Chromium CDP, headless-first |

Do Again is currently alpha software. The foreground runner, per-repository service lifecycle, browser discovery, persistent profile model, and CDP runtime are implemented behind the same interface on macOS, Linux, and Windows. Browser availability still depends on a compatible local Chrome/Chromium installation and ChatGPT authentication.

## Development

```bash
git clone https://github.com/Tran-Steven/do-again.git
cd do-again
python -m pip install .
python -m unittest discover -s tests -v
python tools/browser_smoke.py
do-again doctor
```

Disposable-profile smoke tests verify real Chrome CDP, profile persistence, and crash recovery without signing into ChatGPT. Authenticated ChatGPT round trips are a separate manual integration check; mocked tests cannot establish that a real session works in headless mode. Linux background Chrome requires a graphical session or virtual display.

## Release model

Releases use semantic versioning. Pushing a version tag validates the shared release version, publishes the Python distribution to PyPI and the Node launcher to npm through Trusted Publishing, and creates the matching GitHub Release.

PyPI distribution name: `do-again`

npm package name: `do-again`

CLI command: `do-again`

Python package: `do_again`

## Security

Do Again is designed around explicit allowlists, scoped filesystem roots, bounded execution, and auditable request/receipt records. Browser cookies and session data remain in the dedicated local automation profile; credentials are never requested or stored by Do Again, and CDP is bound to localhost. It should not be configured as unrestricted shell access.

## License

MIT
