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
from ..browser.errors import BrowserPreDispatchBlocked, BrowserSubmissionUncertain
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
    with _file_lock(state_dir/'browser_delivery.lock',timeout=5.0):
        return _queue_receipt_locked(state_dir,receipt)


def _queue_receipt_locked(state_dir: Path, receipt: dict[str, Any]) -> Path:
    request_id = str(receipt.get("request_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", request_id) or request_id in {".", ".."}:
        raise BrowserError("cannot queue browser receipt with invalid request_id")
    outbox = _browser_outbox_dir(state_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{request_id}.json"
    reservation=state_dir/'browser_reservations'/path.name
    if reservation.exists():
        reserved=_read_outbox(reservation)
        if reserved.get('item')!=receipt:raise BrowserSubmissionUncertain('receipt reservation payload conflicts')
        if reserved.get('state')=='reconciled':return reservation
        if not path.exists() and reserved.get('state')!='prepared':
            raise BrowserSubmissionUncertain('receipt reservation lacks outbox evidence; no automatic replay')
    if path.exists():
        if _read_outbox(path)!=receipt:raise BrowserSubmissionUncertain('receipt payload conflicts')
        return path
    atomic_json(reservation,{'item':receipt,'state':'prepared'})
    atomic_json(path, receipt)
    atomic_json(reservation,{'item':receipt,'state':'queued'})
    return path


def _queue_continuation(state_dir: Path, prompt: str, *, purpose: str, marker: str, binding: dict[str, Any]) -> Path:
    """Trusted scheduler reservation; the common outbox performs the only send."""
    if purpose not in {'ci_continuation', 'idle_continuation'} or not marker or not prompt:
        raise BrowserError('invalid scoped continuation intent')
    if not isinstance(binding,dict) or not browser_runtime._chat_id(str(binding.get('chat_url') or '')):
        raise BrowserError('continuation requires an exact original conversation binding')
    identity = hashlib.sha256((purpose + ':' + marker).encode()).hexdigest()
    item = {'request_id': 'continuation-' + identity, 'kind': 'continuation',
            'purpose': purpose, 'event_marker': marker, 'prompt': prompt,
            'chat_url':binding['chat_url'],'binding_identity':browser_runtime.binding_identity(binding)}
    with _file_lock(state_dir / 'browser_delivery.lock', timeout=5.0):
        path = _browser_outbox_dir(state_dir) / (item['request_id'] + '.json')
        reservation = state_dir / 'browser_reservations' / (item['request_id'] + '.json')
        if reservation.exists():
            reserved = _read_outbox(reservation)
            if reserved.get('item') != item:
                raise BrowserSubmissionUncertain('continuation reservation payload conflicts')
            if reserved.get('state') == 'reconciled':
                return reservation
            if not path.exists():
                raise BrowserSubmissionUncertain('continuation reservation lacks outbox evidence; no automatic replay')
        if path.exists():
            if _read_outbox(path) != item:
                raise BrowserSubmissionUncertain('continuation identity already binds a different payload')
            return path
        atomic_json(reservation, {'item': item, 'state': 'prepared'})
        result = _queue_receipt_locked(state_dir, item)
        atomic_json(reservation, {'item': item, 'state': 'queued'})
        return result


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


def _drain_browser_outbox(repo: Path, state_dir: Path, *, daemon_pid: int | None = None, binding_guard: dict | None = None) -> int:
    with _file_lock(state_dir / "browser_delivery.lock", timeout=600.0):
        return _drain_browser_outbox_locked(repo, state_dir, daemon_pid=daemon_pid, binding_guard=binding_guard)


def _uncertain_delivery_path(state_dir: Path) -> Path:
    return state_dir / "browser_submission_uncertain.json"


def _read_uncertain_delivery(state_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_uncertain_delivery_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if (not isinstance(value, dict) or not isinstance(value.get("request_ids"), list)
        or not value['request_ids']
        or any(not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,160}', rid)
               or rid in {'.', '..'} for rid in value['request_ids'])
        or len(set(value['request_ids'])) != len(value['request_ids'])):
        raise BrowserError("invalid durable browser submission uncertainty record")
    return value


def _delivery_evidence_path(state_dir: Path, state: dict[str, Any]) -> Path:
    identity = hashlib.sha256(json.dumps({key: state.get(key) for key in
        ('request_ids', 'binding_identity', 'payload_sha256', 'acknowledgment_token')},
        sort_keys=True).encode()).hexdigest()
    return state_dir / 'browser_delivery_evidence' / (identity + '.json')


def _acknowledge_uncertain_batch(state_dir: Path, paths: list[Path], state: dict[str, Any]) -> None:
    # Commit terminal evidence first; a crash during deletion resumes cleanup only.
    atomic_json(_delivery_evidence_path(state_dir, state), dict(state, state='reconciled',
        message_visible=True, assistant_acknowledged=True, reconciled_at_utc=utc_now().isoformat()))
    for path in paths:
        reservation = state_dir / 'browser_reservations' / path.name
        if reservation.exists():
            reserved = _read_outbox(reservation)
            atomic_json(reservation, dict(reserved, state='reconciled', evidence=str(_delivery_evidence_path(state_dir, state))))
        path.unlink(missing_ok=True)
    _uncertain_delivery_path(state_dir).unlink(missing_ok=True)
    _record_browser_state(state_dir, "queued" if _pending_outbox(state_dir) else "ready")


def _reconcile_uncertain_delivery(repo: Path, state_dir: Path, state: dict[str, Any]) -> int:
    """A possibly-sent message must never be automatically submitted twice."""
    ids = [str(x) for x in state["request_ids"]]
    paths = [_browser_outbox_dir(state_dir) / f"{rid}.json" for rid in ids]
    evidence_path = _delivery_evidence_path(state_dir, state)
    if evidence_path.exists():
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        if (evidence.get('state') != 'reconciled' or evidence.get('assistant_acknowledged') is not True
            or evidence.get('message_visible') is not True
            or any(evidence.get(key) != state.get(key) for key in
                ('request_ids', 'binding_identity', 'payload_sha256', 'acknowledgment_token'))):
            raise BrowserSubmissionUncertain('Invalid terminal delivery evidence; manual inspection required')
        _acknowledge_uncertain_batch(state_dir, paths, evidence)
        return len(paths)
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
    if state.get('binding_identity') and state['binding_identity']!=browser_runtime.binding_identity(record):
        raise BrowserSubmissionUncertain('Uncertain receipt binding generation changed; manual reconciliation required')
    session = ensure_browser_running(verify_auth=True)
    target = browser_runtime._find_chatgpt_target(int(session["port"]), chat_url)
    if target is not None and browser_runtime._page_contains(target, str(state["batch_marker"])):
        # Visibility is not acknowledgment. Legacy uncertain batches without a
        # causally bound token remain blocked; historical probe replies cannot clear them.
        state = dict(state, state='visible', message_visible=True)
        token = str(state.get('acknowledgment_token') or '')
        observation = browser_runtime.receipt_acknowledgment(target, str(state['batch_marker']), token)
        if observation.get('visible') and observation.get('acknowledged'):
            state = dict(state, state='acknowledged', assistant_acknowledged=True)
            atomic_json(_uncertain_delivery_path(state_dir), state)
            _acknowledge_uncertain_batch(state_dir, paths, state)
            return len(paths)
        atomic_json(_uncertain_delivery_path(state_dir), state)
    age = time.time() - float(state.get("first_seen_epoch", time.time()))
    ack_token = str(state.get("ack_probe_token") or "")
    if ack_token and target is not None:
        # Preserve historical probe evidence without another submission. Its
        # acknowledgment is separate from visibility of the original message.
        snapshot = browser_runtime._assistant_snapshot(target)
        if str(snapshot.get("latest") or "").strip() == ack_token:
            state = dict(state, historical_probe_acknowledged=True)
            atomic_json(_uncertain_delivery_path(state_dir), state)
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


def _drain_browser_outbox_locked(repo: Path, state_dir: Path, *, daemon_pid: int | None = None, binding_guard: dict | None = None) -> int:
    if binding_guard is not None:
        record=browser_runtime.project_record(repo)
        if record.get('chat_url')!=binding_guard['chat_url'] or browser_runtime.binding_identity(record)!=binding_guard['binding_identity']:
            raise BrowserSubmissionUncertain('sealed canary binding changed before browser work')
    activate_project(repo, daemon_pid=daemon_pid)
    ensure_browser_running(verify_auth=True)

    uncertain = _read_uncertain_delivery(state_dir)
    if uncertain:
        return _reconcile_uncertain_delivery(repo, state_dir, uncertain)
    paths = _pending_outbox(state_dir)
    if not paths:
        _record_browser_state(state_dir, "ready")
        return 0

    # A continuation reserves one event and never shares a receipt batch.
    batch_paths = []
    for candidate in paths[:20]:
        item = _read_outbox(candidate)
        if item.get('kind') == 'continuation':
            if not batch_paths:
                batch_paths.append(candidate)
            break
        batch_paths.append(candidate)
    receipts = [_read_outbox(path) for path in batch_paths]
    ids = [str(r.get("request_id") or "") for r in receipts]
    continuation = receipts[0] if len(receipts) == 1 and receipts[0].get('kind') == 'continuation' else None
    marker = str(continuation['event_marker']) if continuation else 'DO_AGAIN_RECEIPTS_READY request_ids=' + ','.join(ids)
    purpose = str(continuation['purpose']) if continuation else 'receipt_notification'
    record = browser_runtime.project_record(repo)
    if continuation and (continuation.get('chat_url')!=record.get('chat_url')
                         or continuation.get('binding_identity')!=browser_runtime.binding_identity(record)):
        raise BrowserSubmissionUncertain('queued continuation binding changed; no automatic replay')
    # Crash-safe intent: if the process dies during a browser click, a later
    # instance must reconcile rather than blindly send the same payload.
    atomic_json(_uncertain_delivery_path(state_dir), {
        "schema_version": 1,
        "request_ids": ids,
        "batch_marker": marker,
        "purpose": purpose,
        "chat_url": str(record.get("chat_url") or ""),
        'binding_identity':browser_runtime.binding_identity(record),
        "first_seen_epoch": time.time(),
        "state": "preparing",
    })
    def commit_dispatch(evidence):
        original=_read_uncertain_delivery(state_dir)
        if original.get('request_ids')!=ids or original.get('state')!='preparing':
            raise BrowserSubmissionUncertain('receipt intent changed before dispatch; no automatic replay')
        atomic_json(_uncertain_delivery_path(state_dir),dict(original,**evidence))
    try:
        options={'binding_guard':binding_guard} if binding_guard is not None else {}
        outcome = notify_receipts(repo, receipts,before_dispatch=commit_dispatch,**options)
        if not isinstance(outcome, dict) or outcome.get("response") != "already_delivered":
            # A browser submitted result is just a click, not in-chat proof.
            raise BrowserSubmissionUncertain(
                "Browser submitted receipt batch but its user-message marker is not yet verified"
            )
    except BrowserError as exc:
        if (_read_uncertain_delivery(state_dir).get('state') == 'preparing'
            and (isinstance(exc, (BrowserAuthRequired, BrowserPreDispatchBlocked))
                 or "ChatGPT is still generating; retry delivery later" in str(exc))):
            # Only a typed *pre-Send* refusal, explicit login blocker, or
            # read-only busy check proves no click. Keep the original outbox,
            # but retire the transient "preparing" intent so a later healthy
            # session may attempt delivery. Anything post-dispatch, unknown,
            # or unexpected still requires read-only reconciliation.
            _uncertain_delivery_path(state_dir).unlink(missing_ok=True)
            _record_browser_state(state_dir, "recovering", error=str(exc))
            if isinstance(exc, BrowserAuthRequired):
                raise
            return 0
        raise

    # The adapter's already-visible result proves no assistant acknowledgment.
    return _reconcile_uncertain_delivery(repo, state_dir, _read_uncertain_delivery(state_dir))


def _browser_monitor(
    *,
    repo: Path,
    state_dir: Path,
    stop_event: threading.Event,
    work_event: threading.Event,
    admission_check: Callable[[], Any] | None = None,
    browser_effect: Callable[[], Any] | None = None,
) -> None:
    while not stop_event.is_set():
        try:
            if admission_check is not None:
                admission_check()
            delivered = browser_effect()["delivered"] if browser_effect is not None else _drain_browser_outbox(repo, state_dir)
            if browser_effect is None and not delivered and not _pending_outbox(state_dir):
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


def main(argv: list[str] | None = None, *, admission_check: Callable[[], Any] | None = None, control_transport: Any | None = None, browser_effect: Callable[[], Any] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    from ..supervisor.admission import require_active
    require_active(repo)
    if admission_check is not None:
        admission_check()
    state_dir = Path(args.state_dir).resolve()
    layout = runtime_layout(repo)
    browser_enabled = layout.browser_enabled

    return _run_admitted_worker(args, repo, state_dir, browser_enabled,
        admission_check=admission_check, control_transport=control_transport, browser_effect=browser_effect)


def _run_admitted_worker(args, repo, state_dir, browser_enabled, *, admission_check=None,
                         control_transport=None, browser_effect=None, executor=None, receipt_observer=None):
    """Shared sealed engine; callers establish production or fixed fixture authority."""
    browser_monitor_stop = threading.Event()
    browser_work = threading.Event()
    browser_thread: threading.Thread | None = None

    def receipt_callback(receipt: dict[str, Any]) -> None:
        if browser_enabled:
            _queue_receipt(state_dir, receipt)
            _record_browser_state(state_dir, "queued")
            browser_work.set()
        if receipt_observer is not None and receipt_observer(receipt):
            agent.stop_requested = True

    agent = Agent(
        repo=repo,
        control_worktree=Path(args.control_worktree),
        branch=args.branch,
        remote=args.remote,
        policy_path=Path(args.policy),
        state_dir=state_dir,
        receipt_callback=receipt_callback if browser_enabled or receipt_observer is not None else None,
        executor=executor,
        admission_check=admission_check,
        control_transport=control_transport,
    )

    if browser_enabled and browser_effect is None:
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

    if browser_enabled and browser_effect is not None and not args.once:
        browser_thread=threading.Thread(target=_browser_monitor,kwargs={"repo":repo,"state_dir":state_dir,
            "stop_event":browser_monitor_stop,"work_event":browser_work,"admission_check":admission_check,
            "browser_effect":browser_effect},daemon=True)
        browser_thread.start()

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
                if browser_effect is not None:browser_effect()
                else:_drain_browser_outbox(repo, state_dir)
            except Exception:
                pass
        return result
    finally:
        browser_monitor_stop.set()
        browser_work.set()
        if browser_thread is not None:
            browser_thread.join(timeout=2.0)
        if browser_enabled and browser_effect is None:
            try:
                if admission_check is not None:
                    admission_check()
                deactivate_project(repo)
                stop_if_unused()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
