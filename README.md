# Agent Relay

[![CI](https://github.com/Tran-Steven/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/Tran-Steven/agent-relay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-relay-bridge.svg)](https://pypi.org/project/agent-relay-bridge/)
[![Python](https://img.shields.io/pypi/pyversions/agent-relay-bridge.svg)](https://pypi.org/project/agent-relay-bridge/)
[![License](https://img.shields.io/github/license/Tran-Steven/agent-relay.svg)](LICENSE)

Agent Relay is a cross-platform relay for securely executing agent-driven development tasks on a local machine.

It provides a policy-controlled bridge between an agent workflow and a local development environment, with GitHub-backed requests and receipts, auditable execution, and project-scoped permissions.

## Install

```bash
pip install agent-relay-bridge
```

The installed command is:

```bash
agent-relay
```

## Quick start

```bash
agent-relay doctor
agent-relay init
agent-relay status
```

## Why Agent Relay

- One control protocol across macOS, Linux, and Windows
- Explicit policy-based command permissions
- GitHub-backed request and receipt transport
- Persistent local background agent architecture
- Project-scoped filesystem and command access
- Deterministic audit trail and recovery
- No agent API keys embedded in the project

## Platform status

| Platform | Status | Service backend |
| --- | --- | --- |
| macOS | Core working | launchd |
| Linux | Adapter scaffolded | systemd user service |
| Windows | Adapter scaffolded | Windows service |

Agent Relay is currently alpha software. macOS is the first active platform while Linux and Windows service integration are being completed behind the same interface.

## Development

```bash
git clone https://github.com/Tran-Steven/agent-relay.git
cd agent-relay
python -m pip install -e .
python -m unittest discover -s tests -v
agent-relay doctor
```

## Release model

Releases use semantic versioning. Pushing a version tag such as `v0.1.0` builds a wheel and source distribution, validates both artifacts, and publishes them to PyPI through Trusted Publishing.

PyPI distribution name: `agent-relay-bridge`

CLI command: `agent-relay`

Python package: `agent_relay`

## Security

Agent Relay is designed around explicit allowlists, scoped filesystem roots, bounded execution, and auditable request/receipt records. It should not be configured as unrestricted shell access.

## License

MIT
