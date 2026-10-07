# Do Again

[![CI](https://github.com/Tran-Steven/do-again/actions/workflows/ci.yml/badge.svg)](https://github.com/Tran-Steven/do-again/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/do-again.svg)](https://pypi.org/project/do-again/)
[![Python](https://img.shields.io/pypi/pyversions/do-again.svg)](https://pypi.org/project/do-again/)
[![License](https://img.shields.io/github/license/Tran-Steven/do-again.svg)](LICENSE)

Do Again is a policy-controlled local execution layer for agent-driven development workflows. It lets an agent submit scoped work to a developer machine, receive auditable receipts and artifacts, inspect the result, and continue working when another pass is needed.

## Install

```bash
pip install do-again
```

The installed command is:

```bash
do-again
```

## Quick start

```bash
do-again doctor
do-again init
do-again status
```

## Why Do Again

- Explicit policy-based command permissions
- GitHub-backed request and receipt transport
- Persistent local background execution architecture
- Project-scoped filesystem and command access
- Deterministic audit trail and recovery
- No agent API keys embedded in the project
- A shared platform interface for macOS, Linux, and Windows

## Platform status

| Platform | Status | Service backend |
| --- | --- | --- |
| macOS | Core working | launchd |
| Linux | Adapter scaffolded | systemd user service |
| Windows | Adapter scaffolded | Windows service |

Do Again is currently alpha software. macOS is the first active platform while Linux and Windows service integration are being completed behind the same interface.

## Development

```bash
git clone https://github.com/Tran-Steven/do-again.git
cd do-again
python -m pip install -e .
python -m unittest discover -s tests -v
do-again doctor
```

## Release model

Releases use semantic versioning. Pushing a version tag such as `v0.1.0` builds a wheel and source distribution, validates both artifacts, and publishes them to PyPI through Trusted Publishing.

PyPI distribution name: `do-again`

CLI command: `do-again`

Python package: `do_again`

## Security

Do Again is designed around explicit allowlists, scoped filesystem roots, bounded execution, and auditable request/receipt records. It should not be configured as unrestricted shell access.

## License

MIT
