# Local storage audit (read-only)

Do Again can accumulate browser state, Git control worktrees, offline execution snapshots, broker journals, request scratch areas, receipts, and development build artifacts. The current native macOS supervisor is not yet fully qualified, so an apparent old file is not evidence that it is safe to delete.

Run from the Do Again repository:

```sh
python3 tools/storage_inventory.py
python3 tools/storage_inventory.py --root "$HOME/.do_again" --root "$HOME/Documents/Codex/do-again" --json
```

Replace the example source-checkout path with the real one. Nonexistent paths fail rather than being interpreted as empty. The default root is `~/.do_again`, not your entire home directory. Every run is read-only. The script never deletes files, follows directory symlinks, or modifies the supervisor. It returns aggregate sizes, largest immediate directories and files, and reviews Python caches, build outputs, virtual environments, and Node dependencies. The `older_than_min_age` indicator is based on filesystem modification times and is not evidence of non-use. Access-denied or incomplete scans are marked; treat resulting sizes as incomplete.

Leave these untouched until their ownership and lifetime can be verified against the live supervisor: `/Library/Application Support/DoAgainSupervisor/current`, `/private/var/do-again-execution`, `~/.do_again/projects/*/control`, `~/.do_again/projects/*/runtime`, broker state and sockets, protected project snapshots, Git worktrees, uncommitted checkouts, receipts, outboxes, journals, and evidence. Do not remove `operator-control` Git branches. Do not run branch/worktree cleanup while an execution or native qualification is in progress.

Review old cache folders first, but deletion is deliberately not implemented. A later cleanup phase must establish supervisor liveness, ownership and leases, positive path allowlists, live-object exclusions, explicit confirmation, and audited deletion before any removals are safe.
