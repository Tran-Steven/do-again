from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Any

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


def _drain_browser_outbox_locked(repo: Path, state_dir: Path) -> int:
    activate_project(repo)
    ensure_browser_running(verify_auth=True)

    paths = _pending_outbox(state_dir)
    if not paths:
        _record_browser_state(state_dir, "ready")
        return 0

    batch_paths = paths[:20]
    receipts = [_read_outbox(path) for path in batch_paths]
    try:
        notify_receipts(repo, receipts)
    except BrowserError as exc:
        if "ChatGPT is still generating; retry delivery later" not in str(exc):
            raise
        _record_browser_state(state_dir, "recovering", error=str(exc))
        return 0

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
) -> None:
    while not stop_event.is_set():
        try:
            delivered = _drain_browser_outbox(repo, state_dir)
            if not delivered and not _pending_outbox(state_dir):
                with _file_lock(state_dir / "browser_delivery.lock", timeout=600.0):
                    check_liveness(repo, runtime_layout(repo).control_worktree, state_dir)
            delay = 3.0 if delivered else 30.0
        except BrowserAuthRequired as exc:
            _record_browser_state(state_dir, "auth_required", error=str(exc))
            delay = 60.0
        except BrowserError as exc:
            _record_browser_state(state_dir, "recovering", error=str(exc))
            delay = 15.0
        except Exception as exc:
            _record_browser_state(
                state_dir,
                "recovering",
                error=f"{type(exc).__name__}: {exc}",
            )
            delay = 15.0

        work_event.wait(delay)
        work_event.clear()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
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
    )

    if browser_enabled:
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
                deactivate_project(repo)
                stop_if_unused()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
