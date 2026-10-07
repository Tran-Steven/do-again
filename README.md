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
do-again setup
```

That one command checks the repository and Git remote, creates the project config and policy if they do not exist, creates the dedicated control branch, installs the user-level background agent, and verifies that it is running.

Then:

```bash
do-again status
```

For attended use or advanced setup without a background service:

```bash
do-again setup --no-service
do-again run
```

## Service lifecycle

Do Again can install a per-repository background agent using the native user-level service manager:

    do-again install
    do-again status
    do-again restart
    do-again stop
    do-again uninstall

For attended use, or when a native service manager is unavailable, run the agent in the foreground:

    do-again run
    do-again run --once

Each repository gets an isolated runtime, control worktree, state directory, policy copy, and service label under ~/.do_again. The installed service runs from a copied runtime so installs made through either PyPI or npm remain stable after the invoking shell exits. do-again init also creates do-again-policy.json for project-specific operation, binary, root, timeout, and execution controls. The control branch must be dedicated and cannot be main, master, trunk, or the currently checked-out branch.

## ChatGPT browser bridge

The public package currently sets up the local execution runtime and Git-backed request/receipt transport. It does **not** yet install or automate a ChatGPT browser session.

A private predecessor has a working dedicated-Chrome/CDP flow for one-time ChatGPT sign-in, browser verification, conversation binding, and safe chat rollover. That code is being generalized before it is exposed publicly so Do Again does not inherit project-specific assumptions or foreground-focus behavior.

The target experience is still one-command onboarding: browser automation will be optional and layered on top of `do-again setup`, not a required pile of extra commands. See `ROADMAP.md`.

## Why Do Again

- Explicit policy-based command permissions
- GitHub-backed request and receipt transport
- Project-scoped filesystem and command access
- Deterministic audit trail and recovery
- No agent API keys embedded in the project
- A shared platform interface for macOS, Linux, and Windows
- CI-tested on Python 3.11, 3.12, and 3.13 across all three platforms

## Platform status

| Platform | Status | Service backend |
| --- | --- | --- |
| macOS | Service lifecycle working | launchd |
| Linux | Service lifecycle working | systemd user service |
| Windows | Service lifecycle working | Task Scheduler |

Do Again is currently alpha software. The foreground runner and per-repository background service lifecycle are implemented on macOS, Linux, and Windows. Linux uses a systemd user service; Windows uses a per-user Task Scheduler task so administrator privileges are not required.

## Development

```bash
git clone https://github.com/Tran-Steven/do-again.git
cd do-again
python -m pip install .
python -m unittest discover -s tests -v
do-again doctor
```

## Release model

Releases use semantic versioning. Pushing a version tag such as `v0.2.1` validates the shared release version, publishes the Python distribution to PyPI and the Node launcher to npm through Trusted Publishing, and creates the matching GitHub Release.

PyPI distribution name: `do-again`

npm package name: `do-again`

CLI command: `do-again`

Python package: `do_again`

## Security

Do Again is designed around explicit allowlists, scoped filesystem roots, bounded execution, and auditable request/receipt records. It should not be configured as unrestricted shell access.

## License

MIT
