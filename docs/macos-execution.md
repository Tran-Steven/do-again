# Dedicated macOS execution users (maintenance preview)

This boundary is an installation and enforcement preview for the authorized Do Again and jobpipe projects. It does **not** migrate the existing agent, services, browser delivery, rollout, or Git publication paths. Keep legacy engineering daemons held. Production resume fails closed while the sealed `production_ready` flag is false. Passing these probes does not complete M2 or start acceptance windows.

The supervisor runs as root from an immutable package under `/Library/Application Support/DoAgainSupervisor/current`. Each project receives its own hidden, password-disabled, non-login account (`_doagain_da` or `_doagain_jp`) and collision-checked UID/GID in 400–499. Execution drops UID, GID, supplemental groups, inherited descriptors and environment before entering native Seatbelt confinement. The immutable child shim then clears the task bootstrap and registered-port mapping, zeros the cached bootstrap port, and destroys its inherited send right before executing untrusted code. Dedicated UID separation alone did not prevent starting an existing synthetic operator service; this capability removal is mandatory. The child attests effective groups through Darwin’s legacy POSIX `getgroups` symbol. [Python’s macOS directory-backed group access list](https://docs.python.org/3.13/library/os.html#os.getgroups) is not used, and no directory-service IPC is granted. Detached descendants are identified by kernel UID and birth time and drained before terminal evidence. No unsupported-platform or unsandboxed fallback exists.

Writes are confined to the allocated engineering worktree and request scratch/cache. Git metadata is denied by the profile; successful broker promotion seals its replacement as root-owned read-only data. Network, signals, service creation, supervisor sockets and host credentials are unavailable to scripts. A typed local Git commit capability is implemented; scoped GitHub publication and approved wheel installation are implemented in source. The helper authenticates operator connections using `getpeereid`; project identity comes from its socket. Administrative intent uses a separate operator socket. Pause shares a file fence with the final spawn decision and permits already admitted work to drain.

The root-owned SQLite journal stores operator intent and execution fingerprints. Started operations without terminal evidence remain ambiguous across restart; they cannot replay. An installation requires maintenance intent and the same accepted goal revision in the original operator journal. Originals, dirty primary checkouts, receipts, outboxes, browser sessions, and Sonary records are retained.

## Prepare and install

Use a clean, committed and CI-validated source checkout. Preparation requires no administrator access:

```sh
python3 tools/prepare_macos_supervisor.py --do-again-repo /absolute/do-again --jobpipe-repo /absolute/jobpipe --output /absolute/private-bundle
```

Preparation exports committed package objects and Git snapshots, selects unoccupied identities, packages and relocates a private Python runtime excluding host site packages, hashes every payload file, and writes the installer and native authentication script. Review the resulting manifest/configuration before invoking `osascript /absolute/private-bundle/authenticate.applescript`. The native dialog handles authentication; never supply passwords through command arguments or chat.

The authenticated step verifies the wrapper checksum, copies and seals a root-owned stage, checks all payload hashes before executing installer code, rechecks maintenance and identity collisions, creates the two accounts, provisions separate worktrees under `/private/var/do-again-execution`, and loads only `io.github.tran-steven.do-again.supervisor` in the system domain. A loaded preview helper may upgrade only with the production gate false, unchanged project identities/snapshots, closed intents, no started execution records, no dedicated processes, and both admission fences held across service stop and package cutover. Production helpers are deferred to the later guarded rollout procedure. No old project service is restarted. Partial account creation is journaled and requires reconciliation rather than blind retry. A maintenance recovery bundle reuses the sealed account identities and original engineering snapshot commits. Only an operator-owned partial tree containing Git metadata alone may finish materialization; edited partial trees require reconciliation. Exact snapshot objects are explicitly fetched even when a bundle advertises only remote-tracking references.

## Enforcement proof and recovery

Run `PYTHONPATH=src python3 tools/macos_supervisor_operator.py status --repo /absolute/project`, then `probe` for each project. Probes create unique synthetic operator-domain service fixtures and protected sentinels. They verify allowed work, Python/Bash denial of protected writes, alias attempts, credentials/privilege elevation, signals, raw CDP networking, authenticated sockets, existing-service kickstart/disable/bootout, and detached descendant cleanup. A failed or unavailable proof remains blocked. Sanitized bounded failure evidence is persisted in root-owned state and exposed through authenticated status, keeping uncertainty explicit across restart. Successful evidence binds source, OS and interpreter/sandbox hashes; runtime drift invalidates admission.

`pause`, `maintenance` and `stop` are scoped operator actions. `resume` is rejected until the production migration and verified enforcement gates are satisfied. The private Python runtime is sealed with the package; the helper never imports writable source or host site packages. Synthetic proof evidence is distinct from live acceptance. Do not replace an effect journal during rollback; leave maintenance in place when compatibility or ambiguity prevents recovery.

Local tests cover protocol rejection, authority races, PID reuse, output bounds, durable replay suppression and platform failure. Actual privileged UID/service isolation is **not measured** until post-install probes pass. Linux and Windows production execution through this boundary remain unsupported and blocked.

### Agent integration and legacy entrypoint hold

The production Agent now selects `BrokerExecutor`. Scratch Python/Bash, repository
scripts, unit tests and approved extended commands go to the authenticated
project socket and use supervisor-resolved interpreters in the assigned
engineering tree. They have no local-execution fallback. Script-supplied
environment variables and unsupported host administrative operations are blocked.
Original request fingerprints remain attached to broker packets; the assigned
HEAD is rechecked under the final pause/spawn fence. The root helper reads Git
identity files directly rather than executing writable repository configuration.
Dirty-state or legacy control-plane fences cannot yet be attested and therefore
block rather than being silently ignored.

Foreground run, install, restart and runtime preparation reject before source
copying, service effects or browser preparation. Legacy runtime installation
remains disabled even if a future supervisor configuration admits execution.
Daemon startup and Agent claiming require active, production-ready, verified
admission. Cross-platform packaging/tests continue; Linux/Windows production
execution has no admitted native boundary and fails closed.

This is partial production integration. The sealed production gate remains false.
Immutable engineering-daemon deployment, atomic
claim/publication admission and browser delivery/rollover/archive admission are
still required. These source changes do not upgrade a legacy installed daemon.
Legacy protocol regression fixtures explicitly inject their executor dependency;
no configuration or environment switch enables that dependency in production.
Native installed isolation proofs and unit protocol fixtures are separate evidence.


### Local Git commit capability

`git_commit` accepts only an original request identity/fingerprint, exact starting
HEAD and authority epoch, exact relative file paths, and a bounded commit message.
The authenticated socket fixes the project; the supervisor derives the branch
from the accepted goal revision. Neither scripts nor model requests select a
repository, branch, executable, arbitrary Git arguments, or credentials. Operator
policy must explicitly allow this capability before Agent requests can use it.

One durable execution reservation covers candidate creation and promotion. Each
fixed Git command runs with the dedicated identity through the same native
confinement and pause fence as script execution, using the sealed Apple Git.
Candidate metadata excludes hooks, host configuration, alternate objects,
replacement references and remote access. The index starts from the exact base
commit; the verified commit must have that single parent and change only selected
files. A no-op is a failed receipt rather than useful progress.

After descendants drain, the broker rechecks admission and exchanges sealed
metadata with the assigned `.git` using Darwin's atomic directory exchange.
There is no paired-rename fallback. A crash or lost receipt after the exchange
leaves a started reservation visible in status; that request cannot replay.
Completed requests return their original receipt without creating another commit.
Original source files and excluded edits are preserved.

Fixture regressions exercise actual Git data with a substituted execution backend;
they do not qualify the installed privileged RPC. Production remains disabled.
Before enabling it, qualification must exercise the broker under its immutable
installed runtime, and guarded upgrades must accept proven broker-owned metadata
and its audited new HEAD instead of assuming the initial operator-owned snapshot.
This migration, immutable worker integration, publication/dependencies and live
acceptance remain release gates.


The installed enforcement probe additionally exercises this broker against its
own synthetic repository and private proof journal. It verifies atomic promotion,
root-owned metadata, identical receipt replay and unchanged live HEAD/excluded
edits. A root-created fixture authority admits only that synthetic transaction;
the real maintenance fence guards every command and promotion. The live sealed
production flag is never changed. This proves the installed implementation's
local commit boundary; it does not resume the worker, qualify publication or
start a production acceptance window.


### Approved dependency capability

`dependency_install` accepts an artifact identifier and exact request/head/epoch
fences. Its project-bound approval must be sealed into the installation using
`--dependency-lock`: a JSON object keyed only by `do-again` and/or `jobpipe`, whose
arrays contain `{id, name, version, url, sha256}` records. No artifacts are approved
by default. Duplicate identities, other projects, incomplete hashes and arbitrary
origins fail closed. Agent policy must separately permit the operation.

The trusted broker retrieves only the exact HTTPS wheel on
`files.pythonhosted.org`, with certificate validation, no redirects/proxies and
bounded response/time budgets. Scripts receive no network capability. Hash,
package/version and pure-Python wheel metadata must match. Paths, links, duplicate
members, archive expansion, startup `.pth` hooks and wheel installer scripts are
checked before extraction. This first slice deliberately excludes native wheels
and source/build downloads.

The immutable extraction module executes under the project UID and native
confinement into a fresh request cache, returning its `site_packages` path for
confined tests/scripts to import explicitly. The privileged worker never imports
these packages. A conclusive pause before installation produces a failed terminal
receipt. Uncertain admission/crashes retain started state; terminal replay cannot
install twice. Root journals and original workspace files are preserved.

Local real-extraction fixtures and malicious archive/network regressions validate
the source capability. Installed dependency qualification is not yet measured;
the installed source remains the PR #52 milestone. Production and live acceptance
remain closed until the remaining installed qualification, worker and release gates pass.


### Scoped GitHub publication

`git_publish` accepts only bounded PR title/body and original request/head/epoch
identity. The sealed project configuration binds `Tran-Steven/do-again` or
`Tran-Steven/jobpipe`; the accepted goal derives the `do-again/task-…` branch.
No packet selects a remote, ref, executable, credential, force flag or API path.
The confined immutable exporter reads a bounded canonical unsigned single-parent
commit, including binary blobs and deletions. Oversized changes and unavailable
remote parents require an explicit synchronization task.

Only the trusted supervisor sends HTTPS Git-data requests. It verifies remote
blob/tree/commit identities before creating or advancing the reserved branch,
uses non-force updates, and requires independent branch and PR read-back. Main,
repository administration and unrelated endpoints are excluded. Existing matching
PRs receive the requested title/body; new PRs start as drafts. API requests never
execute Git with credentials or expose tokens to execution users. Supported API
contracts: https://docs.github.com/en/rest/git/commits and
https://docs.github.com/en/rest/git/refs.

An operator can enroll the existing authenticated GitHub CLI credential using
`tools/macos_supervisor_operator.py enroll-github --repo /absolute/project`.
Enrollment requires maintenance and the authenticated operator socket. The token
is kept only in root-owned private supervisor state, excluded from journals,
command arguments, model input, receipts and status. No credential is enrolled by
default. This operator tool is not an untrusted-script administrative interface.

Publication intent and execution reservation commit in one SQLite transaction.
Uncertain network/ref/PR effects remain started and cannot replay. The separate
`git_publication_reconcile` operation reads only the original repository, ref,
head and exact PR payload. Positive independent evidence can finish the original
receipt while maintenance or pause remains in force. Missing/changed evidence
stays uncertain; reconciliation never sends a mutation. Additive intent records
preserve existing authority, execution and compatibility journals.

Source fixtures exercise real Git export, scoped remote effects, exact payload
read-back, pause, changed tips, receipt replay and restart/lost-response recovery.
Installed publication/network qualification, immutable worker integration and
live acceptance remain pending. No production-ready flag is changed by this work.


Broker HTTPS uses the OS certificate roots copied into the sealed build manifest.
It does not use host site packages, relocated-interpreter CA defaults or
`SSL_CERT_FILE`/`SSL_CERT_DIR` to select trust. Native enforcement identity also
binds the complete installed manifest, so replacing a runtime, Git binary or trust
bundle under the same source commit invalidates an earlier proof. The native
probe verifies immutable extraction/export module imports under confinement.
