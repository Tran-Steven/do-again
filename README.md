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
npx do-again doctor
```

Both distributions expose the same command:

```bash
do-again
```

The npm package ships the same Python runtime from this repository behind a small Node launcher. It requires Node.js 18+ and Python 3.11+.

## Quick start

```bash
do-again doctor
do-again init
do-again install
do-again status
```

## Service lifecycle

On macOS, Do Again can install a per-repository launchd agent:

    do-again install
    do-again status
    do-again restart
    do-again stop
    do-again uninstall

For attended or cross-platform use, run the agent in the foreground:

    do-again run
    do-again run --once

Each repository gets an isolated runtime, control worktree, state directory, policy copy, and service label under ~/.do_again. The installed service runs from a copied runtime so installs made through either PyPI or npm remain stable after the invoking shell exits.

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
| Linux | Foreground runner working; service adapter scaffolded | systemd user service |
| Windows | Foreground runner working; service adapter scaffolded | Windows service |

Do Again is currently alpha software. The foreground runner works across the supported Python platforms; macOS is the first platform with an installable background service while Linux and Windows service integration are being completed behind the same interface.

## Development

```bash
git clone https://github.com/Tran-Steven/do-again.git
cd do-again
python -m pip install .
python -m unittest discover -s tests -v
do-again doctor
```

## Release model

Releases use semantic versioning. Pushing a version tag such as `v0.2.0` validates the shared release version, publishes the Python distribution to PyPI and the Node launcher to npm through Trusted Publishing, and creates the matching GitHub Release.

PyPI distribution name: `do-again`

npm package name: `do-again`

CLI command: `do-again`

Python package: `do_again`

## Security

Do Again is designed around explicit allowlists, scoped filesystem roots, bounded execution, and auditable request/receipt records. It should not be configured as unrestricted shell access.

## License

MIT
