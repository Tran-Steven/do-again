# Agent Relay

Agent Relay is a cross-platform relay for securely executing agent-driven development tasks on a local machine.

It is extracted from a production-style GitHub request/receipt automation loop originally developed for autonomous ChatGPT engineering workflows.

## Goals

- One control protocol across macOS, Linux, and Windows
- Explicit policy-based command permissions
- GitHub-backed request and receipt transport
- Persistent local background agent
- Project-scoped filesystem and command access
- Deterministic audit trail and recovery

## CLI

```bash
python -m pip install -e .
agent-relay doctor
agent-relay status
agent-relay init
```

## Platform status

- macOS: existing relay core available; launchd integration is the first supported service backend
- Linux: systemd adapter scaffolded
- Windows: Windows service adapter scaffolded

## Roadmap

1. Package the existing request/receipt loop behind the public CLI
2. Move macOS service installation behind a platform adapter
3. Implement systemd user service installation
4. Implement Windows service installation
5. Add cross-platform CI
6. Publish to PyPI
