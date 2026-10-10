# GitHub-to-Mac connectivity without a public self-hosted runner

**Do not register a self-hosted runner directly with the public
`Tran-Steven/do-again` repository.** GitHub warns that public PR workflows can
run untrusted code on a self-hosted machine. This document instead uses a
separate **private** repo and a fixed read-only diagnostic workflow.

## One-time human bootstrap on the Mac mini

1. Sign in to GitHub as the repository owner. Create a **private** personal
   repository named `Tran-Steven/do-again-mac-control` (or create it with
   `gh repo create Tran-Steven/do-again-mac-control --private`).
   Do not invite third-party collaborators or enable public forks.
2. Copy the reviewed template
   `examples/private-mac-control/mac-health.yml` from this Do Again PR
   into **the private repo only** as
   `.github/workflows/mac-health.yml` on its default `main` branch.
   It contains no `actions/checkout`, untrusted PR trigger, remote
   command input, or privileged installer invocation.
3. In that private repository choose
   **Settings → Actions → Runners → New self-hosted runner → macOS → ARM64**.
   On the Mac mini, run the commands **shown by GitHub for that registration**
   in an isolated, nonadministrator account, with the custom runner label
   `do-again-control` (keep `self-hosted`, `macOS`, `ARM64` defaults).
   Never paste the expiring registration token into a chat or public repo.
   Use the official macOS service setup to keep the runner connected if
   desired; don't elevate the GitHub job to root.
4. In the private repository, use
   **Actions → Mac maintenance health (read only) → Run workflow** for the
   initial handshake. Its fixed task reads only the installed supervisor's
   source SHA, production flag, and sanitized maintenance status. It
   cannot install, run a shell command supplied in a request, enable
   production, or touch jobpipe or Sonary.

## GitHub-only control from ChatGPT, once the runner is online

The workflow also has a narrow `push` trigger, restricted to
`requests/health-*.json` files on that **private** repo's `main` branch.
There is no checkout: the file content does not become commands, arguments,
or environment variables. GitHub's repository connector can create a uniquely
named inert JSON file such as `requests/health-20261010-001.json` to cause
a single fixed status check, then read the workflow's result/logs. This
requires no inbound open port and does not consume Desktop Commander usage.

**Important:** this is initially a **health-only** connection, not production
Do Again access. A dedicated runner login intentionally differs from the
operator UID, so the protected broker rejects its Unix-socket calls. To perform
native confinement probes and controlled canary work we must separately
install a **fixed, allowlisted operator-side bridge** with explicit local
authorization. Do not fix this by using `sudo`, changing socket permissions,
sharing the operator's Chrome profile, or executing arbitrary GitHub workflow
contents as the operator.

The existing protected Do Again parent and jobpipe remain in maintenance,
`production_ready=false`; previous uncertain browser and GitHub operations
must never be replayed. Native tests require the actual Mac mini, while
GitHub-hosted macOS CI validates only portable source code.

## If using a private runner under the operator account

Running the private GitHub runner as the existing operator UID would allow its
jobs to reach files and authenticated browser data owned by that account.
This materially expands trust from local Do Again to GitHub workflow authors.
Do not use this shortcut for the initial handshake; review the security
boundary and implement a scoped bridge instead.

References:
- https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners
- https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application?platform=mac
- https://docs.github.com/en/actions/reference/security/secure-use
