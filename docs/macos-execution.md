# Dedicated macOS execution users (maintenance preview)

This boundary is an installation and enforcement preview for the authorized Do Again and jobpipe projects. It does **not** migrate the existing agent, services, browser delivery, rollout, or Git publication paths. Keep legacy engineering daemons held. Production resume fails closed while the sealed `production_ready` flag is false. Passing these probes does not complete M2 or start acceptance windows.

The supervisor runs as root from an immutable package under `/Library/Application Support/DoAgainSupervisor/current`. Each project receives its own hidden, password-disabled, non-login account (`_doagain_da` or `_doagain_jp`) and collision-checked UID/GID in 400–499. Execution drops UID, GID, supplemental groups, inherited descriptors and environment before entering native Seatbelt confinement. Detached descendants are identified by kernel UID and birth time and drained before terminal evidence. No unsupported-platform or unsandboxed fallback exists.

Writes are confined to the allocated engineering worktree and request scratch/cache. Git metadata remains operator-owned and denied by the profile. Network, signals, service creation, supervisor sockets and host credentials are unavailable to scripts. Privileged Git/dependency operations are not yet implemented. The helper authenticates operator connections using `getpeereid`; project identity comes from its socket. Administrative intent uses a separate operator socket. Pause shares a file fence with the final spawn decision and permits already admitted work to drain.

The root-owned SQLite journal stores operator intent and execution fingerprints. Started operations without terminal evidence remain ambiguous across restart; they cannot replay. An installation requires maintenance intent and the same accepted goal revision in the original operator journal. Originals, dirty primary checkouts, receipts, outboxes, browser sessions, and Sonary records are retained.

## Prepare and install

Use a clean, committed and CI-validated source checkout. Preparation requires no administrator access:

```sh
python3 tools/prepare_macos_supervisor.py --do-again-repo /absolute/do-again --jobpipe-repo /absolute/jobpipe --output /absolute/private-bundle
```

Preparation exports committed package objects and Git snapshots, selects unoccupied identities, packages and relocates a private Python runtime excluding host site packages, hashes every payload file, and writes the installer and native authentication script. Review the resulting manifest/configuration before invoking `osascript /absolute/private-bundle/authenticate.applescript`. The native dialog handles authentication; never supply passwords through command arguments or chat.

The authenticated step verifies the wrapper checksum, copies and seals a root-owned stage, checks all payload hashes before executing installer code, rechecks maintenance and identity collisions, creates the two accounts, provisions separate worktrees under `/private/var/do-again-execution`, and loads only `io.github.tran-steven.do-again.supervisor` in the system domain. Existing helper upgrades are deferred while loaded. No old project service is restarted. Partial account creation is journaled and requires reconciliation rather than blind retry. A maintenance recovery bundle reuses the sealed account identities and original engineering snapshot commits. Only an operator-owned partial tree containing Git metadata alone may finish materialization; edited partial trees require reconciliation. Exact snapshot objects are explicitly fetched even when a bundle advertises only remote-tracking references.

## Enforcement proof and recovery

Run `PYTHONPATH=src python3 tools/macos_supervisor_operator.py status --repo /absolute/project`, then `probe` for each project. Probes create unique synthetic operator-domain service fixtures and protected sentinels. They verify allowed work, Python/Bash denial of protected writes, alias attempts, credentials/privilege elevation, signals, raw CDP networking, authenticated sockets, existing-service kickstart/disable/bootout, and detached descendant cleanup. A failed or unavailable proof remains blocked. Successful evidence binds source, OS and interpreter/sandbox hashes; runtime drift invalidates admission.

`pause`, `maintenance` and `stop` are scoped operator actions. `resume` is rejected until the production migration and verified enforcement gates are satisfied. The private Python runtime is sealed with the package; the helper never imports writable source or host site packages. Synthetic proof evidence is distinct from live acceptance. Do not replace an effect journal during rollback; leave maintenance in place when compatibility or ambiguity prevents recovery.

Local tests cover protocol rejection, authority races, PID reuse, output bounds, durable replay suppression and platform failure. Actual privileged UID/service isolation is **not measured** until post-install probes pass. Linux and Windows production execution through this boundary remain unsupported and blocked.
