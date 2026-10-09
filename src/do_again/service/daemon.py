from __future__ import annotations

import argparse
import json
import hashlib
import re
import signal
import time
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from ..browser import (
    BrowserAuthRequired,
    BrowserError,
    activate_project,
    deactivate_project,
    ensure_browser_running,
    notify_receipt,
    notify_receipts,
    stop_if_unused,
)
from ..core.agent import Agent
from ..core.schema import atomic_json, utc_now
from ..browser import cdp
from ..browser.errors import BrowserSubmissionUncertain
from ..browser import runtime as browser_runtime
from ..browser.runtime import _file_lock
from .runtime import runtime_layout
from .liveness import check_liveness


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--control-worktree", required=True)
    parser.add_argument("--branch", default="operator-control")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def _browser_state_file(state_dir: Path) -> Path:
    return state_dir / "browser_status.json"


def _browser_outbox_dir(state_dir: Path) -> Path:
    return state_dir / "browser_outbox"


def _record_browser_state(
    state_dir: Path,
    state: str,
    *,
    error: str | None = None,
) -> None:
    outbox = _browser_outbox_dir(state_dir)
    pending = len(list(outbox.glob("*.json"))) if outbox.is_dir() else 0
    atomic_json(
        _browser_state_file(state_dir),
        {
            "state": state,
            "error": error,
            "pending_receipts": pending,
            "recorded_at_utc": utc_now().isoformat(),
        },
    )


def _queue_receipt(state_dir: Path, receipt: dict[str, Any]) -> Path:
    request_id = str(receipt.get("request_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", request_id) or request_id in {".", ".."}:
        raise BrowserError("cannot queue browser receipt with invalid request_id")
    outbox = _browser_outbox_dir(state_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{request_id}.json"
    atomic_json(path, receipt)
    return path


def _pending_outbox(state_dir: Path) -> list[Path]:
    outbox = _browser_outbox_dir(state_dir)
    if not outbox.is_dir():
        return []
    return sorted(outbox.glob("*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))


def _read_outbox(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserError(f"invalid browser outbox item {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BrowserError(f"browser outbox item must be an object: {path}")
    return value


def _drain_browser_outbox(repo: Path, state_dir: Path) -> int:
    with _file_lock(state_dir / "browser_delivery.lock", timeout=600.0):
        return _drain_browser_outbox_locked(repo, state_dir)


def _uncertain_delivery_path(state_dir: Path) -> Path:
    return state_dir / "browser_submission_uncertain.json"


def _read_uncertain_delivery(state_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_uncertain_delivery_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("request_ids"), list):
        raise BrowserError("invalid durable browser submission uncertainty record")
    return value


def _acknowledge_uncertain_batch(state_dir: Path, paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
    _uncertain_delivery_path(state_dir).unlink(missing_ok=True)
    _record_browser_state(state_dir, "queued" if _pending_outbox(state_dir) else "ready")


def _reconcile_uncertain_delivery(repo: Path, state_dir: Path, state: dict[str, Any]) -> int:
    """A possibly-sent message must never be automatically submitted twice."""
    ids = [str(x) for x in state["request_ids"]]
    paths = [_browser_outbox_dir(state_dir) / f"{rid}.json" for rid in ids]
    if any(not p.exists() for p in paths):
        raise BrowserSubmissionUncertain(
            "Uncertain browser delivery was modified outside reconciliation; inspect the bound chat and outbox"
        )
    record = browser_runtime.project_record(repo)
    chat_url = str(record.get("chat_url") or "")
    if chat_url != str(state.get("chat_url") or ""):
        raise BrowserSubmissionUncertain(
            "Uncertain browser receipt is bound to a different conversation; manual reconciliation required"
        )
    session = ensure_browser_running(verify_auth=True)
    target = browser_runtime._find_chatgpt_target(int(session["port"]), chat_url)
    if target is not None and browser_runtime._page_contains(target, str(state["batch_marker"])):
        # Exact original user-message evidence; never resubmit it.
        _acknowledge_uncertain_batch(state_dir, paths)
        return len(paths)
    age = time.time() - float(state.get("first_seen_epoch", time.time()))
    ack_token = str(state.get("ack_probe_token") or "")
    if ack_token:
        # A separately named, read-only reconciliation probe is allowed once,
        # even if the original receipt send was uncertain. Its only action is
        # to ask the model to acknowledge the durable receipt, not execute.
        if target is not None:
            snapshot = browser_runtime._assistant_snapshot(target)
            if str(snapshot.get("latest") or "").strip() == ack_token:
                _acknowledge_uncertain_batch(state_dir, paths)
                return len(paths)
    elif age >= 300 and target is not None:
        snapshot = browser_runtime._assistant_snapshot(target)
        if not snapshot.get("busy"):
            # Persist a *new* immutable acknowledgment intent before CDP.
            # This is not a replay of the original task/receipt notification.
            digest = hashlib.sha256(
                (chat_url + str(state["batch_marker"]) + str(state.get("first_seen_epoch"))).encode("utf-8")
            ).hexdigest()[:24]
            ack_token = "DO_AGAIN_RECEIPT_ACK token=" + digest
            state = dict(state)
            state["ack_probe_token"] = ack_token
            state["ack_probe_at"] = time.time()
            state["ack_probe_phase"] = "uncertain"
            atomic_json(_uncertain_delivery_path(state_dir), state)
            prompt = (
                "READ-ONLY DELIVERY RECONCILIATION. A previously attempted receipt "
                "notification may or may not have been delivered. Do not repeat any "
                "prior actions, create Do Again requests, submit job applications, "
                "or modify files in this turn. Inspect the durable receipt(s) on "
                "operator-control: " + ", ".join(ids) + ". "
                "Acknowledge that you have checked their current terminal states "
                "by replying with exactly " + ack_token + " and nothing else. "
                "The daemon will then independently continue its existing goal."
            )
            try:
                browser_runtime.send_message(target, prompt, wait_for_response=False)
            except Exception as exc:
                state["ack_probe_error"] = type(exc).__name__
                atomic_json(_uncertain_delivery_path(state_dir), state)
                raise BrowserSubmissionUncertain(
                    "Read-only receipt reconciliation probe outcome is uncertain; never replay"
                ) from exc
            state["ack_probe_phase"] = "submitted_unverified"
            atomic_json(_uncertain_delivery_path(state_dir), state)
            raise BrowserSubmissionUncertain(
                "Awaiting one-time read-only receipt acknowledgment, without original notification replay"
            )
    if age >= 300:
        incident = state_dir / "incidents" / "browser-submission-unacknowledged.json"
        incident.parent.mkdir(parents=True, exist_ok=True)
        if not incident.exists():
            atomic_json(incident, {
                "schema_version": 1,
                "kind": "browser_submission_unacknowledged",
                "first_seen_epoch": state.get("first_seen_epoch"),
                "request_ids": ids,
                "action": "Inspect the exact bound ChatGPT conversation; do not replay until delivery is conclusively resolved",
            })
    raise BrowserSubmissionUncertain(
        "Browser receipt submission remains uncertain; waiting for exact user-message marker in bound chat"
    )


def _drain_browser_outbox_locked(repo: Path, state_dir: Path) -> int:
    activate_project(repo)
    ensure_browser_running(verify_auth=True)

    uncertain = _read_uncertain_delivery(state_dir)
    if uncertain:
        return _reconcile_uncertain_delivery(repo, state_dir, uncertain)
    paths = _pending_outbox(state_dir)
    if not paths:
        _record_browser_state(state_dir, "ready")
        return 0

    batch_paths = paths[:20]
    receipts = [_read_outbox(path) for path in batch_paths]
    ids = [str(r.get("request_id") or "") for r in receipts]
    record = browser_runtime.project_record(repo)
    # Crash-safe intent: if the process dies during a browser click, a later
    # instance must reconcile rather than blindly send the same payload.
    atomic_json(_uncertain_delivery_path(state_dir), {
        "schema_version": 1,
        "request_ids": ids,
        "batch_marker": "DO_AGAIN_RECEIPTS_READY request_ids=" + ",".join(ids),
        "chat_url": str(record.get("chat_url") or ""),
        "first_seen_epoch": time.time(),
        "state": "preparing",
    })
    try:
        outcome = notify_receipts(repo, receipts)
        if not isinstance(outcome, dict) or outcome.get("response") != "already_delivered":
            # A browser submitted result is just a click, not in-chat proof.
            raise BrowserSubmissionUncertain(
                "Browser submitted receipt batch but its user-message marker is not yet verified"
            )
    except BrowserError as exc:
        if isinstance(exc, BrowserAuthRequired) or "ChatGPT is still generating; retry delivery later" in str(exc):
            # These known pre-submit checks guarantee the page was not clicked.
            _uncertain_delivery_path(state_dir).unlink(missing_ok=True)
            _record_browser_state(state_dir, "recovering", error=str(exc))
            if isinstance(exc, BrowserAuthRequired):
                raise
            return 0
        raise

    _uncertain_delivery_path(state_dir).unlink(missing_ok=True)
    delivered = 0
    for path in batch_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        delivered += 1

    remaining = len(_pending_outbox(state_dir))
    _record_browser_state(state_dir, "queued" if remaining else "ready")
    return delivered


def _browser_monitor(
    *,
    repo: Path,
    state_dir: Path,
    stop_event: threading.Event,
    work_event: threading.Event,
    admission_check: Callable[[], Any] | None = None,
) -> None:
    while not stop_event.is_set():
        try:
            if admission_check is not None:
                admission_check()
            delivered = _drain_browser_outbox(repo, state_dir)
            if not delivered and not _pending_outbox(state_dir):
                with _file_lock(state_dir / "browser_delivery.lock", timeout=600.0):
                    check_liveness(repo, runtime_layout(repo).control_worktree, state_dir)
            delay = 3.0 if delivered else 30.0
        except BrowserSubmissionUncertain as exc:
            _record_browser_state(state_dir, "submission_uncertain", error=str(exc))
            delay = 15.0
        except cdp.CdpTimeoutError as exc:
            _record_browser_state(state_dir, "browser_unresponsive", error=str(exc))
            delay = 15.0
        except BrowserAuthRequired as exc:
            _record_browser_state(state_dir, "auth_required", error=str(exc))
            delay = 60.0
        except BrowserError as exc:
            _record_browser_state(state_dir, "recovering", error=str(exc))
            delay = 15.0
        except Exception as exc:
            from ..supervisor.authority import AuthorityDenied
            if isinstance(exc,AuthorityDenied):
                _record_browser_state(state_dir,'paused',error=str(exc))
                stop_event.set()
                return
            _record_browser_state(
                state_dir,
                "recovering",
                error=f"{type(exc).__name__}: {exc}",
            )
            delay = 15.0

        work_event.wait(delay)
        work_event.clear()


def main(argv: list[str] | None = None, *, admission_check: Callable[[], Any] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    from ..supervisor.admission import require_active
    require_active(repo)
    if admission_check is not None:
        admission_check()
    state_dir = Path(args.state_dir).resolve()
    layout = runtime_layout(repo)
    browser_enabled = layout.browser_enabled

    browser_monitor_stop = threading.Event()
    browser_work = threading.Event()
    browser_thread: threading.Thread | None = None

    def receipt_callback(receipt: dict[str, Any]) -> None:
        if not browser_enabled:
            return
        _queue_receipt(state_dir, receipt)
        _record_browser_state(state_dir, "queued")
        browser_work.set()

    agent = Agent(
        repo=repo,
        control_worktree=Path(args.control_worktree),
        branch=args.branch,
        remote=args.remote,
        policy_path=Path(args.policy),
        state_dir=state_dir,
        receipt_callback=receipt_callback if browser_enabled else None,
        admission_check=admission_check,
    )

    if browser_enabled:
        if admission_check is not None:
            admission_check()
        activate_project(repo)
        _record_browser_state(state_dir, "starting")
        try:
            ensure_browser_running(verify_auth=True)
            _record_browser_state(state_dir, "ready")
        except BrowserAuthRequired as exc:
            _record_browser_state(state_dir, "auth_required", error=str(exc))
            print(
                "do-again browser authentication requires interaction; "
                "run do-again setup in the project to re-authenticate.",
                file=sys.stderr,
                flush=True,
            )
        except BrowserError as exc:
            _record_browser_state(state_dir, "recovering", error=str(exc))
            print(
                f"do-again browser unavailable; background recovery will continue: {exc}",
                file=sys.stderr,
                flush=True,
            )

        browser_thread = threading.Thread(
            target=_browser_monitor,
            kwargs={
                "repo": repo,
                "state_dir": state_dir,
                "stop_event": browser_monitor_stop,
                "work_event": browser_work,
                "admission_check": admission_check,
            },
            name="do-again-browser-monitor",
            daemon=True,
        )
        if not args.once:
            browser_thread.start()
        else:
            browser_thread = None

    def stop_handler(signum: int, frame: Any) -> None:
        agent.stop_requested = True
        browser_monitor_stop.set()
        browser_work.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    try:
        result = agent.run(once=args.once)
        if browser_enabled and args.once and _pending_outbox(state_dir):
            try:
                if admission_check is not None:
                    admission_check()
                _drain_browser_outbox(repo, state_dir)
            except Exception:
                pass
        return result
    finally:
        browser_monitor_stop.set()
        browser_work.set()
        if browser_thread is not None:
            browser_thread.join(timeout=2.0)
        if browser_enabled:
            try:
                if admission_check is not None:
                    admission_check()
                deactivate_project(repo)
                stop_if_unused()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
