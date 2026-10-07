from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import timezone
from pathlib import Path
from typing import Any

from .executor import MacOperatorExecutor
from .schema import (
    OperatorError,
    atomic_json,
    read_json,
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
    ):
        self.repo = repo.resolve()
        self.control_worktree = control_worktree.resolve()
        self.branch = branch
        self.policy_path = policy_path.resolve()
        self.state_dir = state_dir.resolve()
        self.policy = read_json(self.policy_path)
        self.executor = MacOperatorExecutor(
            repo=self.repo,
            policy_path=self.policy_path,
            state_dir=self.state_dir,
        )
        self.requests_dir = (
            self.control_worktree / "automation/agent_relay/requests"
        )
        self.receipts_dir = (
            self.control_worktree / "automation/agent_relay/receipts"
        )
        self.ledger_dir = self.state_dir / "ledger"
        self.stop_requested = False
        self.started = utc_now()
        self.instance_id = f"{socket.gethostname()}:{os.getpid()}:{int(self.started.timestamp())}"
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
        self.require_git("fetch", "--quiet", "origin", self.branch)
        self.require_git("rebase", f"origin/{self.branch}")
        push = self.git("push", "origin", f"HEAD:{self.branch}", timeout=90)
        if push.returncode != 0:
            self.require_git("fetch", "--quiet", "origin", self.branch)
            self.require_git("rebase", f"origin/{self.branch}")
            self.require_git("push", "origin", f"HEAD:{self.branch}", timeout=90)

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
            "repo": str(self.repo),
            "control_worktree": str(self.control_worktree),
            **extra,
        }

    def publish_status(self, state: str, **extra: Any) -> None:
        self.publish_json(
            Path("automation/agent_relay/agent_status.json"),
            self.status_payload(state, **extra),
            f"Mac operator {state}",
        )

    def ledger_path(self, request_id: str) -> Path:
        return self.ledger_dir / f"{request_id}.json"

    def receipt_relative(self, request_id: str) -> Path:
        return Path(f"automation/agent_relay/receipts/{request_id}.json")

    def receipt_exists(self, request_id: str) -> bool:
        return (self.control_worktree / self.receipt_relative(request_id)).is_file()

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
            f"Mac operator receipt {request_id}: {receipt['state']}",
        )

    def schedule_self_restart(self) -> None:
        label = str(
            self.policy.get("agent_launchd_label", "com.steventran.mac-operator")
        ).strip()
        if not label.startswith("com.steventran."):
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
            return False
        if not isinstance(raw, dict):
            return False
        request_id = str(raw.get("request_id") or "")
        if not request_id or path.stem != request_id:
            return False
        if self.receipt_exists(request_id):
            return False

        ledger = self.local_ledger(request_id)
        if ledger:
            ledger_state = ledger.get("state")
            stored_receipt = ledger.get("receipt")
            if ledger_state == "terminal" and isinstance(stored_receipt, dict):
                self.publish_receipt(stored_receipt)
                return True
            if ledger_state == "started":
                started_at = str(ledger.get("started_at_utc") or utc_now().isoformat())
                receipt = self.make_receipt(
                    request=raw,
                    state="blocked_ambiguous_replay",
                    started_at=started_at,
                    error=(
                        "A prior agent instance began this request but did not durably record "
                        "its completion. Refusing automatic replay."
                    ),
                )
                self.write_ledger(
                    request_id,
                    {
                        "state": "terminal",
                        "request_fingerprint": request_fingerprint(raw),
                        "receipt": receipt,
                    },
                )
                self.publish_receipt(receipt)
                return True

        started_at = utc_now().isoformat()
        try:
            request = validate_request(
                raw,
                max_ttl_seconds=int(self.policy.get("max_request_ttl_seconds", 3600)),
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
            receipt_state = "succeeded" if self.result_succeeded(payload) else "failed"
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
                "request_fingerprint": request_fingerprint(raw),
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

    def request_paths(self) -> list[Path]:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        values = []
        for path in self.requests_dir.glob("*.json"):
            try:
                value = read_json(path)
            except Exception:
                continue
            issued = str(value.get("issued_at_utc") or "") if isinstance(value, dict) else ""
            values.append((issued, path.name, path))
        values.sort()
        return [path for _, _, path in values]

    def run(self, once: bool = False) -> int:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.publish_status("ready")
        except Exception as exc:
            print(f"mac-operator initial status publish failed: {exc}", file=sys.stderr, flush=True)
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
                print(f"mac-operator loop error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
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
