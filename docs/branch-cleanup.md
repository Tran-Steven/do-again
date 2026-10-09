# Read-only branch inventory

Issue #47 tracks the reduction of stale remote branches without interrupting any active Do Again project, local worktree, agent, or Git control transport.

Run from a clone that has GitHub CLI authentication:

```sh
python tools/branch_inventory.py --repo Tran-Steven/do-again --local-repo /absolute/path/to/do-again > /private/path/branch-inventory.json
```

Omit `--local-repo` when the local checkout is unavailable. Missing worktree inspection is always an exclusion, not evidence of inactivity.

The report includes each remote branch's current head, GitHub protection flag, relative ahead/behind status, same-repository pull requests, merged PR head and merge commit, local worktree locations (if inspected), exclusions and a review-candidate flag. All pages of branches and PRs are fetched. Fork-origin PRs cannot authorize deletion of a same-named local branch.

`candidate_for_operator_review` means that a merged PR's head exactly matches the present remote tip and the other read-only checks found no blockers. **It does not authorize deletion.** Every entry has `eligible_for_deletion=false` and `deletion_enabled=false`. The tool never writes to GitHub, invokes a delete endpoint, or modifies any local ref.

Before deletion can ever be added, a separate guarded phase must provide authoritative agent/session leases and local worktree ownership, re-read the latest remote ref and PR status immediately before the effect, prove the exact expected SHA, enforce protected/default/control ref exclusions, persist a durable audit record, and abort on uncertainty or permission failures. Squash merges and post-merge tip changes must remain protected unless there is independent exact evidence.

Do not run a remote branch cleanup concurrently with native capability qualification, Sol's in-progress work, or uncertain executor state. Do not delete the `operator-control` transport branch.
