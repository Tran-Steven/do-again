from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .executor import LocalExecutor
from .schema import (
    OperatorError,
    atomic_json,
    read_json,
    parse_utc,
    request_fingerprint,
    utc_now,
    validate_request,
)


class Agent:
    def __init__(
        self,
        *,
        repo: Path,
        control_worktree: Path,
        branch: str,
        policy_path: Path,
        state_dir: Path,
        remote: str = "origin",
    ):
        self.repo = repo.resolve()
        self.control_worktree = control_worktree.resolve()
        self.branch = branch
        self.remote = remote
        self.policy_path = policy_path.resolve()
        self.state_dir = state_dir.resolve()
        self.policy = read_json(self.policy_path)
        self.executor = LocalExecutor(
            repo=self.repo,
            policy_path=self.policy_path,
            state_dir=self.state_dir,
        )
        self.requests_dir = (
            self.control_worktree / "automation/do_again/requests"
        )
        self.receipts_dir = (
            self.control_worktree / "automation/do_again/receipts"
        )
        self.invalid_dir = self.control_worktree / "automation/do_again/invalid"
        self.conflicts_dir = self.control_worktree / "automation/do_again/conflicts"
        self.claims_dir = self.control_worktree / "automation/do_again/claims"
        self.ledger_dir = self.state_dir / "ledger"
        self.locks_dir = self.state_dir / "locks"
        self.stop_requested = False
        self.started = utc_now()
        self.instance_id = f"{socket.gethostname()}:{os.getpid()}:{int(self.started.timestamp())}:{uuid.uuid4().hex[:12]}"
        self.poll_seconds = max(1.0, float(self.policy.get("poll_seconds", 3)))

    def git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.control_worktree), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def require_git(self, *args: str, timeout: float = 60) -> str:
        proc = self.git(*args, timeout=timeout)
        if proc.returncode != 0:
            raise OperatorError(
                f"git {' '.join(args)} failed rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return proc.stdout

    def sync(self) -> None:
        status = self.require_git("status", "--porcelain")
        if status.strip():
            raise OperatorError("operator control worktree is dirty")
        self.require_git("fetch", "--quiet", self.remote, self.branch)
        self.require_git("rebase", "FETCH_HEAD")
        push = self.git("push", self.remote, f"HEAD:{self.branch}", timeout=90)
        if push.returncode != 0:
            self.require_git("fetch", "--quiet", self.remote, self.branch)
            self.require_git("rebase", "FETCH_HEAD")
            self.require_git(
                "push", self.remote, f"HEAD:{self.branch}", timeout=90
            )

    def publish_json(self, relative: Path, value: dict[str, Any], message: str) -> None:
        for attempt in range(4):
            try:
                self.sync()
                target = self.control_worktree / relative
                atomic_json(target, value)
                self.require_git("add", str(relative))
                commit = self.git("commit", "-m", message, timeout=60)
                if commit.returncode != 0:
                    combined = f"{commit.stdout}\n{commit.stderr}".lower()
                    if "nothing to commit" not in combined:
                        raise OperatorError(
                            f"git commit failed rc={commit.returncode}: "
                            f"{(commit.stderr or commit.stdout).strip()}"
                        )
                self.require_git("push", "origin", f"HEAD:{self.branch}", timeout=90)
                return
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))

    def status_payload(self, state: str, **extra: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state": state,
            "instance_id": self.instance_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started_at_utc": self.started.astimezone(timezone.utc).isoformat(),
            "updated_at_utc": utc_now().isoformat(),
            "control_branch": self.branch,
            "remote": self.remote,
            "repo": str(self.repo),
            "control_worktree": str(self.control_worktree),
            **extra,
        }

    def publish_status(self, state: str, **extra: Any) -> None:
        self.publish_json(
            Path("automation/do_again/agent_status.json"),
            self.status_payload(state, **extra),
            f"Do Again {state}",
        )

    def ledger_path(self, request_id: str) -> Path:
        return self.ledger_dir / f"{request_id}.json"

    def receipt_relative(self, request_id: str) -> Path:
        return Path(f"automation/do_again/receipts/{request_id}.json")

    def receipt_exists(self, request_id: str) -> bool:
        return (self.control_worktree / self.receipt_relative(request_id)).is_file()

    def receipt_payload(self, request_id: str) -> dict[str, Any] | None:
        path = self.control_worktree / self.receipt_relative(request_id)
        if not path.is_file():
            return None
        try:
            value = read_json(path)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def claim_relative(self, request_id: str) -> Path:
        return Path(f"automation/do_again/claims/{request_id}.json")

    def claim_payload(self, request_id: str) -> dict[str, Any] | None:
        path = self.control_worktree / self.claim_relative(request_id)
        if not path.is_file():
            return None
        try:
            value = read_json(path)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def acquire_remote_claim(self, request: dict[str, Any]) -> bool:
        request_id = str(request["request_id"])
        fingerprint = request_fingerprint(request)
        relative = self.claim_relative(request_id)

        for attempt in range(5):
            self.sync()
            existing = self.claim_payload(request_id)
            if existing is not None:
                existing_fingerprint = str(
                    existing.get("request_fingerprint") or ""
                )
                if existing_fingerprint != fingerprint:
                    self.publish_conflict(
                        request=request,
                        reason="request_id conflicts with a durable remote claim",
                        existing_fingerprint=existing_fingerprint or None,
                    )
                    return False
                return existing.get("agent_instance_id") == self.instance_id

            payload = {
                "schema_version": 1,
                "request_id": request_id,
                "request_fingerprint": fingerprint,
                "agent_instance_id": self.instance_id,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "claimed_at_utc": utc_now().isoformat(),
            }
            target = self.control_worktree / relative
            atomic_json(target, payload)
            self.require_git("add", str(relative))
            commit = self.git(
                "commit",
                "-m",
                f"Do Again claim {request_id}",
                timeout=60,
            )
            if commit.returncode != 0:
                combined = f"{commit.stdout}\n{commit.stderr}".lower()
                if "nothing to commit" not in combined:
                    raise OperatorError(
                        f"git commit failed rc={commit.returncode}: "
                        f"{(commit.stderr or commit.stdout).strip()}"
                    )

            push = self.git(
                "push",
                self.remote,
                f"HEAD:{self.branch}",
                timeout=90,
            )
            if push.returncode == 0:
                return True

            # Another agent may have won the race. Discard only our unpushed
            # claim commit and inspect the authoritative remote branch.
            self.require_git("fetch", "--quiet", self.remote, self.branch)
            self.require_git("reset", "--hard", "FETCH_HEAD")
            time.sleep(0.1 * (attempt + 1))

        raise OperatorError(
            f"unable to acquire durable remote claim for request {request_id}"
        )

    def conflict_relative(self, request_id: str, fingerprint: str) -> Path:
        return Path(
            f"automation/do_again/conflicts/{request_id}/{fingerprint[:16]}.json"
        )

    def publish_conflict(
        self,
        *,
        request: dict[str, Any],
        reason: str,
        existing_fingerprint: str | None,
    ) -> None:
        fingerprint = request_fingerprint(request)
        payload = {
            "schema_version": 1,
            "state": "blocked_request_id_reuse",
            "request_id": request.get("request_id"),
            "request_fingerprint": fingerprint,
            "existing_fingerprint": existing_fingerprint,
            "reason": reason,
            "observed_at_utc": utc_now().isoformat(),
            "agent_instance_id": self.instance_id,
        }
        self.publish_json(
            self.conflict_relative(str(request.get("request_id")), fingerprint),
            payload,
            f"Do Again conflict {request.get('request_id')}",
        )

    def invalid_relative(self, path: Path) -> Path:
        return Path(f"automation/do_again/invalid/{path.name}.json")

    def publish_invalid_request(self, path: Path, error: Exception | str) -> bool:
        try:
            data = path.read_bytes()
        except OSError:
            return False
        digest = hashlib.sha256(data).hexdigest()
        relative = self.invalid_relative(path)
        existing_path = self.control_worktree / relative
        if existing_path.is_file():
            try:
                existing = read_json(existing_path)
            except Exception:
                existing = None
            if isinstance(existing, dict) and existing.get("sha256") == digest:
                return False
        payload = {
            "schema_version": 1,
            "state": "invalid_request",
            "request_file": path.name,
            "sha256": digest,
            "size_bytes": len(data),
            "error": (
                f"{type(error).__name__}: {error}"
                if isinstance(error, Exception)
                else str(error)
            ),
            "observed_at_utc": utc_now().isoformat(),
            "agent_instance_id": self.instance_id,
        }
        self.publish_json(relative, payload, f"Do Again invalid request {path.name}")
        return True

    def request_lock_path(self, request_id: str) -> Path:
        return self.locks_dir / f"{request_id}.lock"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def acquire_request_lock(self, request_id: str) -> bool:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        lock = self.request_lock_path(request_id)
        for _ in range(2):
            try:
                lock.mkdir()
            except FileExistsError:
                owner_path = lock / "owner.json"
                owner: dict[str, Any] | None = None
                try:
                    value = read_json(owner_path)
                    owner = value if isinstance(value, dict) else None
                except Exception:
                    owner = None

                stale = False
                if owner:
                    owner_host = str(owner.get("host") or "")
                    try:
                        owner_pid = int(owner.get("pid") or 0)
                    except (TypeError, ValueError):
                        owner_pid = 0
                    if owner_host == socket.gethostname():
                        stale = not self._pid_alive(owner_pid)
                    else:
                        try:
                            age = max(0.0, time.time() - lock.stat().st_mtime)
                        except OSError:
                            age = 0.0
                        stale = age > float(
                            self.policy.get("request_lock_stale_seconds", 7200)
                        )
                else:
                    try:
                        age = max(0.0, time.time() - lock.stat().st_mtime)
                    except OSError:
                        age = 0.0
                    stale = age > 30.0

                if not stale:
                    return False
                try:
                    owner_path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    lock.rmdir()
                except OSError:
                    return False
                continue

            atomic_json(
                lock / "owner.json",
                {
                    "instance_id": self.instance_id,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "request_id": request_id,
                    "acquired_at_utc": utc_now().isoformat(),
                },
            )
            return True
        return False

    def release_request_lock(self, request_id: str) -> None:
        lock = self.request_lock_path(request_id)
        try:
            (lock / "owner.json").unlink()
        except FileNotFoundError:
            pass
        try:
            lock.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def local_ledger(self, request_id: str) -> dict[str, Any] | None:
        path = self.ledger_path(request_id)
        if not path.is_file():
            return None
        value = read_json(path)
        return value if isinstance(value, dict) else None

    def write_ledger(self, request_id: str, value: dict[str, Any]) -> None:
        atomic_json(self.ledger_path(request_id), value)

    def make_receipt(
        self,
        *,
        request: dict[str, Any],
        state: str,
        started_at: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        traceback_text: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": request.get("request_id"),
            "request_fingerprint": request_fingerprint(request),
            "operation": request.get("operation"),
            "state": state,
            "started_at_utc": started_at,
            "finished_at_utc": utc_now().isoformat(),
            "agent_instance_id": self.instance_id,
            "host": socket.gethostname(),
            "result": result,
            "error": error,
            "traceback": traceback_text,
        }

    def result_succeeded(self, payload: dict[str, Any]) -> bool:
        result = payload.get("result")
        if not isinstance(result, dict):
            return True
        if "returncode" in result:
            return result.get("returncode") == 0 and not result.get("timed_out")
        if "retarget" in result:
            retarget = result.get("retarget")
            build = result.get("build")
            return (
                isinstance(build, dict)
                and build.get("returncode") == 0
                and isinstance(retarget, dict)
                and retarget.get("returncode") == 0
            )
        return True

    def publish_receipt(self, receipt: dict[str, Any]) -> None:
        request_id = str(receipt["request_id"])
        self.publish_json(
            self.receipt_relative(request_id),
            receipt,
            f"Do Again receipt {request_id}: {receipt['state']}",
        )

    def schedule_self_restart(self) -> None:
        label = str(
            self.policy.get("agent_launchd_label", "io.github.tran-steven.do-again")
        ).strip()
        prefixes = tuple(str(value) for value in self.policy.get("launchctl_label_prefixes", []))
        if not prefixes or not label.startswith(prefixes):
            raise OperatorError("agent launchd label is not approved")
        target = f"gui/{os.getuid()}/{label}"
        subprocess.Popen(
            ["/bin/launchctl", "kickstart", "-k", target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def process_path(self, path: Path) -> bool:
        try:
            raw = read_json(path)
        except Exception as exc:
            return self.publish_invalid_request(path, exc)
        if not isinstance(raw, dict):
            return self.publish_invalid_request(path, "request must be an object")

        request_id = str(raw.get("request_id") or "")
        if not request_id or path.stem != request_id:
            return self.publish_invalid_request(
                path,
                "request_id is missing or does not match the request filename",
            )

        fingerprint = request_fingerprint(raw)
        existing_receipt = self.receipt_payload(request_id)
        if existing_receipt is not None:
            existing_fingerprint = str(
                existing_receipt.get("request_fingerprint") or ""
            )
            if existing_fingerprint == fingerprint:
                return False
            self.publish_conflict(
                request=raw,
                reason="request_id already has a receipt for different request content",
                existing_fingerprint=existing_fingerprint or None,
            )
            return True

        if not self.acquire_request_lock(request_id):
            return False

        try:
            existing_receipt = self.receipt_payload(request_id)
            if existing_receipt is not None:
                existing_fingerprint = str(
                    existing_receipt.get("request_fingerprint") or ""
                )
                if existing_fingerprint == fingerprint:
                    return False
                self.publish_conflict(
                    request=raw,
                    reason="request_id already has a receipt for different request content",
                    existing_fingerprint=existing_fingerprint or None,
                )
                return True

            ledger = self.local_ledger(request_id)
            if ledger:
                ledger_fingerprint = str(
                    ledger.get("request_fingerprint") or ""
                )
                if ledger_fingerprint and ledger_fingerprint != fingerprint:
                    self.publish_conflict(
                        request=raw,
                        reason="request_id conflicts with durable local ledger content",
                        existing_fingerprint=ledger_fingerprint,
                    )
                    return True

                ledger_state = ledger.get("state")
                stored_receipt = ledger.get("receipt")
                if ledger_state == "terminal" and isinstance(stored_receipt, dict):
                    self.publish_receipt(stored_receipt)
                    return True
                if ledger_state == "started":
                    started_at = str(
                        ledger.get("started_at_utc") or utc_now().isoformat()
                    )
                    receipt = self.make_receipt(
                        request=raw,
                        state="blocked_ambiguous_replay",
                        started_at=started_at,
                        error=(
                            "A prior agent instance began this request but did not "
                            "durably record its completion. Refusing automatic replay."
                        ),
                    )
                    self.write_ledger(
                        request_id,
                        {
                            "state": "terminal",
                            "request_fingerprint": fingerprint,
                            "receipt": receipt,
                        },
                    )
                    self.publish_receipt(receipt)
                    return True

            if not self.acquire_remote_claim(raw):
                return False

            started_at = utc_now().isoformat()
            try:
                request = validate_request(
                    raw,
                    max_ttl_seconds=int(
                        self.policy.get("max_request_ttl_seconds", 3600)
                    ),
                    max_future_skew_seconds=int(
                        self.policy.get("max_future_skew_seconds", 300)
                    ),
                )
                self.write_ledger(
                    request_id,
                    {
                        "state": "started",
                        "request_fingerprint": request_fingerprint(request),
                        "started_at_utc": started_at,
                        "agent_instance_id": self.instance_id,
                    },
                )
                payload = self.executor.execute(request)
                receipt_state = (
                    "succeeded" if self.result_succeeded(payload) else "failed"
                )
                receipt = self.make_receipt(
                    request=request,
                    state=receipt_state,
                    started_at=started_at,
                    result=payload,
                )
            except OperatorError as exc:
                receipt = self.make_receipt(
                    request=raw,
                    state="blocked",
                    started_at=started_at,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as exc:
                receipt = self.make_receipt(
                    request=raw,
                    state="error",
                    started_at=started_at,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback_text=traceback.format_exc()[-12000:],
                )

            self.write_ledger(
                request_id,
                {
                    "state": "terminal",
                    "request_fingerprint": fingerprint,
                    "receipt": receipt,
                },
            )
            self.publish_receipt(receipt)
            try:
                self.publish_status(
                    "ready",
                    last_request_id=request_id,
                    last_request_state=receipt["state"],
                )
            except Exception:
                pass
            payload_result = receipt.get("result")
            if (
                receipt.get("state") == "succeeded"
                and isinstance(payload_result, dict)
                and isinstance(payload_result.get("result"), dict)
                and payload_result["result"].get("restart_after_receipt") is True
            ):
                self.schedule_self_restart()
            return True
        finally:
            self.release_request_lock(request_id)

    def request_paths(self) -> list[Path]:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.invalid_dir.mkdir(parents=True, exist_ok=True)
        self.conflicts_dir.mkdir(parents=True, exist_ok=True)
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        values = []
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        for path in self.requests_dir.glob("*.json"):
            try:
                value = read_json(path)
                issued_raw = (
                    str(value.get("issued_at_utc") or "")
                    if isinstance(value, dict)
                    else ""
                )
                issued = parse_utc(issued_raw) if issued_raw else far_future
            except Exception:
                issued = far_future
            values.append((issued, path.name, path))
        values.sort()
        return [path for _, _, path in values]

    def run(self, once: bool = False) -> int:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.publish_status("ready")
        except Exception as exc:
            print(f"do-again initial status publish failed: {exc}", file=sys.stderr, flush=True)
        while not self.stop_requested:
            try:
                self.sync()
                did_work = False
                for path in self.request_paths():
                    if self.stop_requested:
                        break
                    if self.process_path(path):
                        did_work = True
                if once:
                    return 0
                if not did_work:
                    time.sleep(self.poll_seconds)
            except Exception as exc:
                print(f"do-again loop error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                if once:
                    return 1
                time.sleep(max(3.0, self.poll_seconds))
        try:
            self.publish_status("stopped")
        except Exception:
            pass
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--control-worktree", required=True)
    parser.add_argument("--branch", default="operator-control")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agent = Agent(
        repo=Path(args.repo),
        control_worktree=Path(args.control_worktree),
        branch=args.branch,
        remote=args.remote,
        policy_path=Path(args.policy),
        state_dir=Path(args.state_dir),
    )

    def stop_handler(signum: int, frame: Any) -> None:
        agent.stop_requested = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    return agent.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
