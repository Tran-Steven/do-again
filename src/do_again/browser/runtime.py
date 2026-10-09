from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..platforms.process import pid_alive
from . import cdp
from .errors import BrowserAuthRequired, BrowserError, BrowserSubmissionUncertain


CHATGPT_URL = "https://chatgpt.com/"


@dataclass(frozen=True)
class BrowserPaths:
    root: Path
    profile: Path
    config: Path
    state: Path
    log: Path
    projects: Path
    lock: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def browser_paths() -> BrowserPaths:
    home = Path(
        os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))
    ).expanduser().resolve()
    root = home / "browser"
    return BrowserPaths(
        root=root,
        profile=root / "profile",
        config=root / "config.json",
        state=root / "state.json",
        log=root / "browser.log",
        projects=root / "projects",
        lock=root / "startup.lock",
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserError(f"invalid browser state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BrowserError(f"browser state file must contain an object: {path}")
    return value


def _default_config() -> dict[str, Any]:
    paths = browser_paths()
    return {
        "schema_version": 1,
        "browser_binary": None,
        "profile_dir": str(paths.profile),
        "port": 9223,
        "preferred_mode": "auto",
        "resolved_mode": None,
        "authenticated": False,
        "last_authenticated_utc": None,
        "last_headless_verified_utc": None,
        "rollover_char_threshold": 180000,
    }


def load_config() -> dict[str, Any]:
    paths = browser_paths()
    value = _default_config()
    if paths.config.is_file():
        value.update(_read_json(paths.config))
    return value


def save_config(config: dict[str, Any]) -> None:
    _atomic_json(browser_paths().config, config)


def load_state() -> dict[str, Any]:
    return _read_json(browser_paths().state)


def save_state(state: dict[str, Any]) -> None:
    _atomic_json(browser_paths().state, state)


def _pid_alive(pid: int | None) -> bool:
    return pid_alive(pid)


def discover_browser(config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    candidates: list[str] = []
    env = os.environ.get("DO_AGAIN_BROWSER")
    if env:
        candidates.append(env)
    configured = config.get("browser_binary")
    if configured:
        candidates.append(str(configured))

    if sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            ]
        )
    elif os.name == "nt":
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
        ]
        for root in roots:
            if not root:
                continue
            candidates.extend(
                [
                    str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(root) / "Chromium/Application/chrome.exe"),
                ]
            )
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()

    raise BrowserError(
        "Chrome/Chromium was not found. Install Google Chrome or Chromium, "
        "or set DO_AGAIN_BROWSER to the browser executable."
    )


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _select_port(config: dict[str, Any]) -> int:
    preferred = int(config.get("port") or 9223)
    if _port_free(preferred):
        return preferred
    for port in range(9224, 9324):
        if _port_free(port):
            return port
    raise BrowserError("no free localhost CDP port was found")


def _browser_command(
    binary: Path,
    profile: Path,
    port: int,
    mode: str,
    *,
    initial_url: str | None = None,
) -> list[str]:
    argv = [
        str(binary),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-allow-origins=http://127.0.0.1:{port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-default-apps",
    ]
    if mode == "headless":
        argv.extend(["--headless=new", "--window-size=1280,900"])
    elif mode == "background":
        argv.extend(["--no-startup-window", "--start-minimized"])
    elif mode != "visible":
        raise BrowserError(f"unsupported browser mode: {mode!r}")

    if initial_url:
        argv.append(initial_url)
    elif mode != "background":
        argv.append("about:blank")
    return argv


_lock_depth = threading.local()


@contextmanager
def _file_lock(path: Path, timeout: float = 120.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    held = getattr(_lock_depth, "held", None)
    if held is None:
        held = _lock_depth.held = set()
    key = str(path.resolve())
    if key in held:
        yield
        return
    deadline = time.monotonic() + timeout
    token = uuid.uuid4().hex
    while True:
        try:
            path.mkdir()
            _atomic_json(
                path / "owner.json",
                {"pid": os.getpid(), "token": token, "created_utc": _utc_now()},
            )
            break
        except FileExistsError:
            try:
                owner = _read_json(path / "owner.json")
                age = time.time() - path.stat().st_mtime
            except (OSError, BrowserError):
                owner = {}
                age = 0
            owner_pid = int(owner.get("pid") or 0)
            if (owner_pid and not _pid_alive(owner_pid)) or (not owner_pid and age > 10):
                shutil.rmtree(path, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise BrowserError(f"timed out waiting for browser lock {path.name}")
            time.sleep(0.2)
    held.add(key)
    try:
        yield
    finally:
        held.remove(key)
        if _read_json(path / "owner.json").get("token") == token:
            shutil.rmtree(path, ignore_errors=True)


def _startup_lock(timeout: float = 120.0):
    return _file_lock(browser_paths().lock, timeout)


def _shared_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _startup_lock():
            return function(*args, **kwargs)
    return wrapped


def _project_operation(function):
    @wraps(function)
    def wrapped(repo, *args, **kwargs):
        path = browser_paths().root / "locks" / f"{_project_key(repo)}.lock"
        with _file_lock(path, timeout=600.0):
            return function(repo, *args, **kwargs)
    return wrapped


def _owns_process(state: dict[str, Any]) -> bool:
    pid = int(state.get("pid") or 0)
    if not _pid_alive(pid):
        return False
    profile = str(browser_paths().profile)
    if str(state.get("profile_dir") or "") != profile:
        return False
    try:
        if os.name == "nt":
            command = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"],
                text=True, timeout=5.0,
            )
        else:
            command = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="], text=True, timeout=5.0,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    profile_matches = f"--user-data-dir={profile}" in command or f'--user-data-dir="{profile}"' in command
    return profile_matches and f"--remote-debugging-port={int(state.get('port') or 0)}" in command


def _owns_endpoint(state: dict[str, Any]) -> bool:
    port = int(state.get("port") or 0)
    if not port:
        return False
    expected = state.get("browser_websocket_url")
    if expected:
        try:
            return cdp.browser_websocket_url(port) == expected
        except BrowserError:
            return False
    return _owns_process(state) and cdp.endpoint_ready(port)


def _wait_endpoint(port: int, process: subprocess.Popen[Any], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserError(
                f"automation browser exited during startup with rc={process.returncode}; "
                f"see {browser_paths().log}"
            )
        if cdp.endpoint_ready(port):
            return
        time.sleep(0.25)
    raise BrowserError(
        f"automation browser did not expose CDP on 127.0.0.1:{port}; "
        f"see {browser_paths().log}"
    )


@_shared_operation
def launch_browser(
    mode: str,
    *,
    initial_url: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    paths = browser_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.profile.mkdir(parents=True, exist_ok=True)
    existing = load_state()
    if _owns_endpoint(existing):
        return existing
    if _owns_process(existing):
        stop_browser(force=True)
    binary = discover_browser(config)
    port = _select_port(config)

    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))

    log_handle = open(paths.log, "ab", buffering=0)
    try:
        process = subprocess.Popen(
            _browser_command(binary, paths.profile, port, mode, initial_url=initial_url),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=start_new_session,
            creationflags=creationflags,
            close_fds=os.name != "nt",
        )
    finally:
        log_handle.close()

    try:
        _wait_endpoint(port, process)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    state = {
        "schema_version": 1,
        "pid": process.pid,
        "port": port,
        "mode": mode,
        "browser_binary": str(binary),
        "profile_dir": str(paths.profile),
        "started_utc": _utc_now(),
        "browser_websocket_url": cdp.browser_websocket_url(port),
    }
    save_state(state)
    config["browser_binary"] = str(binary)
    config["profile_dir"] = str(paths.profile)
    config["port"] = port
    save_config(config)
    return state


@_shared_operation
def stop_browser(*, force: bool = False) -> None:
    state = load_state()
    port = int(state.get("port") or load_config().get("port") or 9223)
    pid = int(state.get("pid") or 0)

    owns_process = _owns_process(state)
    if _owns_endpoint(state):
        cdp.close_browser(port)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and cdp.endpoint_ready(port):
            time.sleep(0.1)

    if owns_process and _owns_process(state):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if force and _owns_process(state):
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    state["pid"] = None
    state.pop("browser_websocket_url", None)
    state["stopped_utc"] = _utc_now()
    save_state(state)


def _find_chatgpt_target(port: int, chat_url: str | None = None) -> cdp.Target | None:
    rows = [item for item in cdp.targets(port) if item.url.startswith("https://chatgpt.com/")]
    if chat_url:
        exact = [item for item in rows if item.url == chat_url]
        if exact:
            return exact[0]
        if "/c/" in chat_url:
            chat_id = chat_url.split("/c/", 1)[1].split("?", 1)[0].split("#", 1)[0]
            matched = [item for item in rows if f"/c/{chat_id}" in item.url]
            if matched:
                return matched[0]
        return None
    return rows[0] if rows else None


def _prompt_probe(target: cdp.Target) -> dict[str, Any]:
    expression = r"""(() => {
const body = (document.body && document.body.innerText ? document.body.innerText : '').toLowerCase();
const prompt = document.querySelector('#prompt-textarea,[data-testid="prompt-textarea"],textarea,div[contenteditable="true"]');
const challenge = document.querySelector('iframe[src*="challenges.cloudflare.com"],.cf-turnstile');
const login = Array.from(document.querySelectorAll('button,a')).some(el => /^(log in|sign up|create an account)$/i.test((el.innerText || '').trim()));
return {
  url: location.href,
  title: document.title,
  prompt: !!prompt,
  challenge: !!challenge || /just a moment|verify you are human|checking your browser/i.test(document.title),
  login,
  readyState: document.readyState
};
})()"""
    value = cdp.evaluate(target, expression, timeout=15.0)
    return value if isinstance(value, dict) else {}


def _ensure_chatgpt_target(port: int, chat_url: str | None = None) -> cdp.Target:
    target = _find_chatgpt_target(port, chat_url)
    if target is not None:
        return target
    return cdp.create_target(port, chat_url or CHATGPT_URL, background=True)


def wait_for_authenticated(
    port: int,
    *,
    chat_url: str | None = None,
    timeout: float = 30.0,
    target: cdp.Target | None = None,
) -> tuple[cdp.Target, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    target = target or _ensure_chatgpt_target(port, chat_url)
    while time.monotonic() < deadline:
        try:
            current = next(
                (item for item in cdp.targets(port) if item.id == target.id),
                target,
            )
            last = _prompt_probe(current)
            if last.get("prompt") and not last.get("challenge") and not last.get("login"):
                return current, last
            target = current
        except Exception:
            if not cdp.endpoint_ready(port):
                raise BrowserError(
                    "the dedicated automation browser closed before ChatGPT "
                    "authentication completed"
                )
        time.sleep(0.5)

    if last.get("challenge"):
        raise BrowserAuthRequired(
            "ChatGPT is showing a human-verification challenge. "
            "Run do-again setup and complete it in the dedicated browser window."
        )
    if not last.get("login"):
        raise BrowserError("ChatGPT did not load a usable composer; check connectivity and retry")
    raise BrowserAuthRequired(
        "ChatGPT authentication is not ready in the dedicated Do Again profile. "
        "Run do-again setup to sign in interactively."
    )


def browser_status(*, verify_session: bool = False) -> dict[str, Any]:
    config = load_config()
    state = load_state()
    port = int(state.get("port") or config.get("port") or 9223)
    pid = int(state.get("pid") or 0)
    running = _pid_alive(pid) and _owns_endpoint(state)
    result: dict[str, Any] = {
        "configured": browser_paths().config.is_file(),
        "running": running,
        "pid": pid if running else None,
        "port": port,
        "mode": state.get("mode") if running else config.get("resolved_mode"),
        "profile_dir": str(browser_paths().profile),
        "browser_binary": config.get("browser_binary"),
        "authenticated": bool(config.get("authenticated")),
        "session_ready": None,
        "auth_required": bool(config.get("auth_required")),
    }
    if running and verify_session:
        try:
            _, probe = wait_for_authenticated(port, chat_url=CHATGPT_URL, timeout=8.0)
            result["session_ready"] = True
            result["chat_url"] = probe.get("url")
        except BrowserAuthRequired as exc:
            result["session_ready"] = False
            result["auth_required"] = True
            result["error"] = str(exc)
        except Exception as exc:
            result["session_ready"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _normal_mode(config: dict[str, Any]) -> str:
    preferred = str(config.get("preferred_mode") or "auto")
    resolved = str(config.get("resolved_mode") or "")
    if preferred == "headless":
        return "headless"
    if preferred == "background":
        return "background"
    if resolved in {"headless", "background"}:
        return resolved
    return "headless"


@_shared_operation
def ensure_browser_running(*, verify_auth: bool = True) -> dict[str, Any]:
    with _startup_lock():
        status = browser_status(verify_session=False)
        config = load_config()
        if verify_auth and config.get("auth_required"):
            raise BrowserAuthRequired("ChatGPT authentication requires interaction; run do-again setup")
        preferred = config.get("preferred_mode")
        if status["running"] and preferred in {"headless", "background"} and status.get("mode") != preferred:
            stop_browser(force=True)
            status["running"] = False
        if not status["running"]:
            mode = _normal_mode(config)
            try:
                launch_browser(mode, config=config)
            except BrowserError:
                if mode != "headless" or config.get("preferred_mode") != "auto":
                    raise
                stop_browser(force=True)
                launch_browser("background", config=config)
                config["resolved_mode"] = "background"
                save_config(config)
        status = browser_status(verify_session=False)

    if not verify_auth:
        return status

    port = int(status["port"])
    try:
        _, probe = wait_for_authenticated(port, chat_url=CHATGPT_URL, timeout=20.0)
    except BrowserError as headless_exc:
        config = load_config()
        current_mode = str(status.get("mode") or "")
        preferred = str(config.get("preferred_mode") or "auto")

        if current_mode == "headless" and preferred == "auto":
            # Some ChatGPT/Cloudflare sessions work in real Chrome but not in
            # true headless. Fall back automatically without involving the
            # user's normal browser profile or foreground applications.
            stop_browser(force=True)
            with _startup_lock():
                fallback = launch_browser("background", config=config)
            try:
                _, probe = wait_for_authenticated(
                    int(fallback["port"]),
                    chat_url=CHATGPT_URL,
                    timeout=30.0,
                )
            except BrowserAuthRequired as background_exc:
                config["authenticated"] = False
                config["auth_required"] = True
                save_config(config)
                raise background_exc from headless_exc
            config["resolved_mode"] = "background"
            save_config(config)
            status = browser_status(verify_session=False)
        else:
            if isinstance(headless_exc, BrowserAuthRequired):
                config["authenticated"] = False
                config["auth_required"] = True
                save_config(config)
            raise

    config = load_config()
    config["authenticated"] = True
    config["auth_required"] = False
    config["resolved_mode"] = status.get("mode")
    if status.get("mode") == "headless":
        config["last_headless_verified_utc"] = _utc_now()
    config["last_authenticated_utc"] = _utc_now()
    save_config(config)
    status["authenticated"] = True
    status["session_ready"] = True
    status["chat_url"] = probe.get("url")
    return status


@_shared_operation
def use_background_fallback() -> dict[str, Any]:
    config = load_config()
    if config.get("preferred_mode") != "auto":
        raise BrowserError("automatic browser fallback requires auto mode")
    stop_browser(force=True)
    config["resolved_mode"] = "background"
    save_config(config)
    return ensure_browser_running(verify_auth=True)


@_shared_operation
def setup_browser(
    *,
    mode: str = "auto",
    run_iteration_test: bool = True,
) -> dict[str, Any]:
    if mode not in {"auto", "headless", "background"}:
        raise BrowserError(f"invalid browser mode: {mode}")

    config = load_config()
    config["preferred_mode"] = mode
    config["auth_required"] = False
    config["browser_binary"] = str(discover_browser(config))
    config["profile_dir"] = str(browser_paths().profile)
    save_config(config)

    # Idempotent fast path: no visible browser after the first successful setup.
    if config.get("authenticated"):
        try:
            status = ensure_browser_running(verify_auth=True)
        except BrowserAuthRequired:
            stop_browser(force=True)
        else:
            if run_iteration_test:
                browser_self_test()
            return status

    stop_browser(force=True)
    visible = launch_browser("visible", initial_url=CHATGPT_URL, config=config)
    port = int(visible["port"])

    try:
        try:
            wait_for_authenticated(port, timeout=6.0)
        except BrowserAuthRequired:
            print("")
            print("Do Again needs one-time ChatGPT authentication.")
            print("A dedicated automation Chrome profile is open.")
            print("Sign into ChatGPT and complete any human verification there.")
            print("Do Again does not read or store your username or password.")
            print("Waiting for ChatGPT to become ready; no terminal action is required.")
            wait_for_authenticated(port, timeout=600.0)

        config = load_config()
        config["authenticated"] = True
        config["last_authenticated_utc"] = _utc_now()
        save_config(config)
    finally:
        stop_browser(force=True)

    resolved = "background"
    if mode in {"auto", "headless"}:
        try:
            headless = launch_browser("headless", config=load_config())
            wait_for_authenticated(int(headless["port"]), timeout=30.0)
            resolved = "headless"
            config = load_config()
            config["last_headless_verified_utc"] = _utc_now()
            save_config(config)
        except Exception as exc:
            stop_browser(force=True)
            if mode == "headless":
                raise BrowserError(
                    "true headless Chrome could not preserve a usable ChatGPT session: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            resolved = "background"

    config = load_config()
    config["resolved_mode"] = resolved
    config["authenticated"] = True
    save_config(config)

    stop_browser(force=True)
    final_state = launch_browser(resolved, config=config)
    target, _ = wait_for_authenticated(int(final_state["port"]), timeout=30.0)

    if run_iteration_test:
        result = browser_self_test(target=target)
        if "DO_AGAIN_BROWSER_OK" not in result.get("response", ""):
            raise BrowserError("ChatGPT browser self-test returned an unexpected response")

    status = browser_status(verify_session=True)
    status["resolved_mode"] = resolved
    return status


_MESSAGE_NODES_JS = r"""
const messageNodes = role => {
  const selector = role
    ? `[data-message-author-role="${role}"],[data-conversation-role="${role}"]`
    : '[data-message-author-role],[data-conversation-role]';
  const nodes = Array.from(new Set(Array.from(document.querySelectorAll(selector)).map(el =>
    el.hasAttribute('data-conversation-role') ? el.parentElement : el
  )));
  for (const heading of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    const label = (heading.innerText || heading.textContent || '').trim();
    const headingRole = label === 'You said:' ? 'user' : label === 'ChatGPT said:' ? 'assistant' : '';
    if (!headingRole || (role && headingRole !== role)) continue;
    const unit = heading.closest('[data-testid^="conversation-turn-"]') || heading.parentElement;
    if (!unit || unit.querySelector('[data-message-author-role],[data-conversation-role]')) continue;
    if (heading.closest('[data-message-author-role],[data-conversation-role]')) continue;
    nodes.push(unit);
  }
  return Array.from(new Set(nodes));
};
"""


def _normalize_assistant_text(value: Any) -> str:
    text = str(value or "").strip()
    prefix = "ChatGPT said:"
    if text.startswith(prefix):
        remainder = text[len(prefix):]
        if remainder.startswith("\n") or remainder.startswith("\r"):
            return remainder.lstrip("\r\n").strip()
    return text


def _assistant_snapshot(target: cdp.Target) -> dict[str, Any]:
    expression = r"""(() => {""" + _MESSAGE_NODES_JS + r"""
const assistant = messageNodes('assistant');
const latest = assistant.length ? (assistant[assistant.length-1].innerText || assistant[assistant.length-1].textContent || '').trim() : '';
const stop = document.querySelector('[data-testid="stop-button"],button[aria-label="Stop generating"],button[aria-label="Stop response"],button[aria-label="Stop"]');
return {count: assistant.length, latest, busy: !!(stop && !stop.disabled), url: location.href};
})()"""
    value = cdp.evaluate(target, expression, timeout=15.0)
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    result["latest"] = _normalize_assistant_text(result.get("latest"))
    return result


def send_message(
    target: cdp.Target,
    text: str,
    *,
    timeout: float = 180.0,
    wait_for_response: bool = True,
) -> dict[str, Any]:
    baseline = _assistant_snapshot(target)
    if baseline.get("busy"):
        raise BrowserError("ChatGPT is still generating; retry delivery later")
    if _context_limit_warning(target):
        raise BrowserError("ChatGPT conversation reached its context limit")
    prep = r"""(() => {
const visible = (el) => {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
};
const nodes = Array.from(document.querySelectorAll('#prompt-textarea,[data-testid="prompt-textarea"],textarea,div[contenteditable="true"]'));
const el = nodes.find(visible) || nodes[0];
if (!el) return 'no_prompt';
el.focus({preventScroll:true});
if ('value' in el) {
  el.value = '';
  el.dispatchEvent(new Event('input', {bubbles:true}));
} else {
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges();
  sel.addRange(range);
  document.execCommand('delete');
}
return 'ready';
})()"""
    if cdp.evaluate(target, prep, timeout=15.0, user_gesture=True) != "ready":
        raise BrowserError("ChatGPT composer was not available")
    dispatch_started = False
    try:
        cdp.insert_text(target, text)
        time.sleep(0.15)
        # Exactly one gesture. From this point every exception is uncertain;
        # absence of composer clearing never permits a fallback submission.
        dispatch_started = True
        cdp.press_enter(target)
        accepted = False
        for _ in range(20):
            time.sleep(0.1)
            accepted = bool(cdp.evaluate(target, r"""(() => {
    const el = document.querySelector('#prompt-textarea,[data-testid="prompt-textarea"],textarea,div[contenteditable="true"]');
    return el && !(el.value || el.innerText || '').trim();
    })()""", timeout=10.0))
            if accepted:
                break
        if not accepted:
            raise BrowserSubmissionUncertain(
                "ChatGPT did not confirm submission after the single Enter gesture; "
                "delivery outcome must be reconciled before another submission"
            )

        if not wait_for_response:
            return {
                "response": "submitted",
                "chat_url": target.url,
            }

        deadline = time.monotonic() + timeout
        last = baseline
        stable = 0
        while time.monotonic() < deadline:
            time.sleep(1.0)
            current = _assistant_snapshot(target)
            if _context_limit_warning(target):
                raise BrowserError("ChatGPT conversation reached its context limit")
            if (
                int(current.get("count") or 0) > int(baseline.get("count") or 0)
                and not current.get("busy")
                and str(current.get("latest") or "").strip()
            ):
                if current.get("latest") == last.get("latest"):
                    stable += 1
                else:
                    stable = 0
                if stable >= 1 and "/c/local-" not in str(current.get("url") or ""):
                    return {
                        "response": str(current.get("latest") or ""),
                        "chat_url": str(current.get("url") or ""),
                    }
            last = current
        raise BrowserError("timed out waiting for ChatGPT to finish the browser iteration")

    except Exception as exc:
        if isinstance(exc, BrowserSubmissionUncertain):
            raise
        if dispatch_started:
            raise BrowserSubmissionUncertain(
                "ChatGPT submission outcome is uncertain after dispatch; "
                "inspect the exact bound conversation before retrying"
            ) from exc
        if isinstance(exc, cdp.CdpTimeoutError):
            raise BrowserError("ChatGPT composer preparation failed before submission") from exc
        raise

def browser_self_test(*, target: cdp.Target | None = None) -> dict[str, Any]:
    status = ensure_browser_running(verify_auth=True)
    port = int(status["port"])
    if target is None:
        target = cdp.create_target(port, CHATGPT_URL, background=True)
        target, _ = wait_for_authenticated(port, chat_url=target.url, timeout=30.0, target=target)
    return send_message(
        target,
        "Reply with exactly DO_AGAIN_BROWSER_OK and nothing else.",
        timeout=120.0,
    )



def _page_contains(target: cdp.Target, text: str) -> bool:
    needle = json.dumps(text)
    expression = (
        "(() => {" + _MESSAGE_NODES_JS + r"""
const needle = """ + needle + r""";
const users = messageNodes('user');
if (users.some(el => (el.innerText || el.textContent || '').includes(needle))) return true;
const conversation = document.querySelector('[aria-label="Conversation"]') || document.body;
if (!conversation) return false;
const raw = String(conversation.innerText || conversation.textContent || '');
const lines = raw.split(String.fromCharCode(10)).map(line => line.trim()).filter(Boolean);
return lines.some((line, index) =>
  line === 'You said:' && (lines[index + 1] || '').startsWith(needle)
);
})()"""
    )
    return bool(cdp.evaluate(target, expression, timeout=10.0))


def _context_limit_warning(target: cdp.Target) -> str:
    expression = r"""(() => {""" + _MESSAGE_NODES_JS + r"""
const phrases = [
  "you've reached the maximum length for this conversation",
  "you’ve reached the maximum length for this conversation",
  "maximum length for this conversation",
  "maximum conversation length",
  "conversation is too long",
  "keep talking by starting a new chat",
  "start a new chat to continue"
];
const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
const matches = value => {
  const text = norm(value);
  return phrases.some(p => text.includes(p));
};
if (!matches(document.body ? document.body.innerText : '')) return '';
const visible = el => {
  if (!el || !(el instanceof Element)) return false;
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
};
const candidates = Array.from(document.querySelectorAll('body *'));
const units = messageNodes();
const hits = [];
for (const el of candidates) {
  if (!visible(el)) continue;
  if (el.closest('#prompt-textarea,[data-testid="prompt-textarea"],textarea,[contenteditable="true"],pre,code')) continue;
  const systemAlert = el.matches('[role="alert"],[data-testid*="error"]');
  if (!systemAlert && units.some(unit => unit.contains(el) || el.contains(unit))) continue;
  const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  if (!text || text.length > 900 || !matches(text)) continue;
  const turn = el.closest('[data-testid^="conversation-turn-"]');
  if (turn && turn.querySelector('[data-message-author-role],[data-conversation-role]') && !systemAlert) continue;
  hits.push(text);
}
hits.sort((a,b) => a.length - b.length);
return hits[0] || '';
})()"""
    value = cdp.evaluate(target, expression, timeout=10.0)
    return str(value or "").strip()



def _chat_id(chat_url: str) -> str:
    if "/c/" not in chat_url:
        return ""
    return chat_url.split("/c/", 1)[1].split("?", 1)[0].split("#", 1)[0]


def _project_browser_dir(repo: Path) -> Path:
    path = browser_paths().root / "project_state" / _project_key(repo)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owned_chat_registry_path(repo: Path) -> Path:
    return _project_browser_dir(repo) / "owned_chats.json"


def _rollover_transaction_path(repo: Path) -> Path:
    return _project_browser_dir(repo) / "rollover_transaction.json"


def _checkpoint_path(repo: Path, token: str) -> Path:
    return _project_browser_dir(repo) / "checkpoints" / f"{token}.json"


def _record_owned_chat(
    repo: Path,
    chat_url: str,
    *,
    created_reason: str,
    handoff_token: str | None = None,
) -> dict[str, Any]:
    registry = _read_json(_owned_chat_registry_path(repo))
    chats = registry.get("chats")
    if not isinstance(chats, list):
        chats = []
    chat_id = _chat_id(chat_url)
    if not chat_id:
        raise BrowserError("cannot register owned chat without a conversation id")
    existing = next((row for row in chats if row.get("chat_id") == chat_id), None)
    if existing is None:
        existing = {
            "chat_id": chat_id,
            "chat_url": chat_url,
            "repo": str(repo.resolve()),
            "project_key": _project_key(repo),
            "created_reason": created_reason,
            "created_utc": _utc_now(),
            "archive_state": "active",
        }
        if handoff_token:
            existing["handoff_token"] = handoff_token
        chats.append(existing)
    registry = {"schema_version": 1, "chats": chats, "updated_utc": _utc_now()}
    _atomic_json(_owned_chat_registry_path(repo), registry)
    return existing


def _archive_queue_path(repo: Path) -> Path:
    return _project_browser_dir(repo) / "archive_queue.json"


def _all_bound_chat_ids() -> set[str]:
    bound: set[str] = set()
    projects = browser_paths().projects
    if not projects.is_dir():
        return bound
    for path in projects.glob("*.json"):
        try:
            record = _read_json(path)
        except BrowserError:
            continue
        chat_id = _chat_id(str(record.get("chat_url") or ""))
        if chat_id:
            bound.add(chat_id)
    return bound


def _candidate_receipt_evidence(repo: Path, chat_id: str) -> list[dict[str, Any]]:
    control = _control_worktree_for_repo(repo)
    receipts_dir = control / "automation/do_again/receipts"
    evidence: list[dict[str, Any]] = []
    if not receipts_dir.is_dir():
        return evidence
    for path in sorted(receipts_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if chat_id not in raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        evidence.append(
            {
                "receipt": path.name,
                "request_id": payload.get("request_id"),
                "state": payload.get("state"),
                "operation": payload.get("operation"),
                "sha256": _sha256_file(path),
            }
        )
    return evidence


def _dedicated_browser_history_chat_ids() -> list[str]:
    history = browser_paths().profile / "Default" / "History"
    if not history.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{history.as_uri()}?immutable=1", uri=True)) as connection:
            urls = connection.execute(
                "SELECT url FROM urls WHERE url LIKE ? ORDER BY last_visit_time DESC LIMIT 5000",
                ("https://chatgpt.com/c/%",),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    return list(dict.fromkeys(
        chat_id for (url,) in urls if (chat_id := _chat_id(str(url)))
    ))


def _historical_chat_evidence(repo: Path) -> dict[str, list[str]]:
    repo = repo.resolve()
    evidence: dict[str, list[str]] = {}

    def record(url: object, source: str) -> None:
        chat_id = _chat_id(str(url or ""))
        if chat_id and source not in evidence.setdefault(chat_id, []):
            evidence[chat_id].append(source)

    record(project_record(repo).get("previous_chat_url"), "project_previous_chat")
    record(
        _read_json(_rollover_transaction_path(repo)).get("predecessor_chat_url"),
        "rollover_predecessor",
    )
    for path in sorted((_project_browser_dir(repo) / "checkpoints").glob("*.json")):
        try:
            checkpoint = _read_json(path)
        except BrowserError:
            continue
        if checkpoint.get("repo") == str(repo):
            record(checkpoint.get("active_chat_url"), f"checkpoint:{path.name}")
    for chat_id in _dedicated_browser_history_chat_ids():
        record(f"https://chatgpt.com/c/{chat_id}", "dedicated_browser_history")
    return evidence


def chat_cleanup_inventory(
    repo: Path,
    *,
    candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    registry = _read_json(_owned_chat_registry_path(repo))
    rows: list[dict[str, Any]] = []
    bound = _all_bound_chat_ids()
    seen: set[str] = set()
    historical = _historical_chat_evidence(repo)

    chats = registry.get("chats")
    if isinstance(chats, list):
        for chat in chats:
            chat_id = str(chat.get("chat_id") or "")
            if not chat_id:
                continue
            seen.add(chat_id)
            active = chat_id in bound
            archive_state = str(chat.get("archive_state") or "active")
            rows.append(
                {
                    "chat_id": chat_id,
                    "chat_url": chat.get("chat_url"),
                    "provenance": "owned_registry",
                    "ownership_verified": True,
                    "active_bound": active,
                    "archive_state": archive_state,
                    "eligible": (not active and archive_state != "archived"),
                    "reason": (
                        "currently_bound"
                        if active
                        else "already_archived"
                        if archive_state == "archived"
                        else "verified_owned_inactive"
                    ),
                }
            )

    for chat_id in dict.fromkeys([*historical, *(candidate_ids or [])]):
        chat_id = str(chat_id).strip()
        if not chat_id or chat_id in seen:
            continue
        evidence = _candidate_receipt_evidence(repo, chat_id)
        history = historical.get(chat_id, [])
        active = chat_id in bound
        rows.append(
            {
                "chat_id": chat_id,
                "chat_url": f"https://chatgpt.com/c/{chat_id}",
                "provenance": "historical_candidate",
                "ownership_verified": False,
                "active_bound": active,
                "archive_state": "unverified",
                "eligible": False,
                "reason": (
                    "currently_bound"
                    if active
                    else "needs_content_verification"
                    if evidence or history
                    else "no_corroborating_receipts"
                ),
                "evidence": evidence,
                "historical_evidence": history,
            }
        )
    return {
        "schema_version": 1,
        "repo": str(repo),
        "active_bound_chat_ids": sorted(bound),
        "candidates": rows,
        "generated_utc": _utc_now(),
    }


def archive_queue_status(repo: Path) -> dict[str, Any]:
    value = _read_json(_archive_queue_path(repo))
    items = value.get("items")
    if not isinstance(items, list):
        items = []
    return {
        "schema_version": 1,
        "repo": str(repo.resolve()),
        "items": items,
        "updated_utc": value.get("updated_utc"),
    }


def queue_verified_archives(
    repo: Path,
    *,
    candidate_ids: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    inventory = chat_cleanup_inventory(repo, candidate_ids=candidate_ids)
    eligible = [row for row in inventory["candidates"] if row.get("eligible")]
    queue = archive_queue_status(repo)
    items = list(queue["items"])
    existing = {str(item.get("chat_id") or ""): item for item in items}
    queued: list[str] = []

    if apply:
        for row in eligible:
            chat_id = str(row["chat_id"])
            prior = existing.get(chat_id)
            if prior and prior.get("state") in {"pending", "archived"}:
                continue
            item = {
                "chat_id": chat_id,
                "chat_url": row.get("chat_url"),
                "project_key": _project_key(repo),
                "state": "pending",
                "attempts": 0,
                "queued_utc": _utc_now(),
                "last_error": None,
            }
            if prior:
                items[items.index(prior)] = item
            else:
                items.append(item)
            existing[chat_id] = item
            queued.append(chat_id)
        _atomic_json(
            _archive_queue_path(repo),
            {"schema_version": 1, "items": items, "updated_utc": _utc_now()},
        )

    return {
        "dry_run": not apply,
        "eligible_chat_ids": [str(row["chat_id"]) for row in eligible],
        "queued_chat_ids": queued,
        "inventory": inventory,
        "queue": archive_queue_status(repo),
    }

def _chat_has_bootstrap_markers(
    target: cdp.Target,
    *,
    repo: Path,
    remote_url: str,
    control_branch: str,
) -> bool:
    required = [
        "Do Again automation workspace bootstrap.",
        f"Project: {repo.name}",
        f"Repository: {remote_url}",
        f"Control branch: {control_branch}",
    ]
    if not all(_page_contains(target, marker) for marker in required):
        return False
    return _page_contains(target, "DO_AGAIN_PROJECT_READY") or _page_contains(
        target, "DO_AGAIN_HANDOFF_READY"
    )


def _verify_historical_chat_ownership(
    repo: Path,
    chat_url: str,
    *,
    port: int,
) -> dict[str, Any]:
    record = project_record(repo)
    remote_url = str(record.get("remote_url") or "")
    control_branch = str(record.get("control_branch") or "operator-control")
    if not remote_url:
        raise BrowserError("project browser record is missing remote_url")
    target = _find_chatgpt_target(port, chat_url)
    created = False
    if target is None:
        target = cdp.create_target(port, chat_url, background=True)
        created = True
    try:
        target, _ = wait_for_authenticated(
            port, chat_url=chat_url, timeout=30.0, target=target
        )
        owned = _chat_has_bootstrap_markers(
            target,
            repo=repo,
            remote_url=remote_url,
            control_branch=control_branch,
        )
        return {
            "owned": owned,
            "chat_url": chat_url,
            "evidence": "bootstrap_markers" if owned else "bootstrap_markers_missing",
        }
    finally:
        if created:
            cdp.close_target(port, target.id)


def verify_candidate_chat(
    repo: Path,
    chat_id: str,
) -> dict[str, Any]:
    repo = repo.resolve()
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise BrowserError("chat id is required")
    if chat_id in _all_bound_chat_ids():
        return {
            "chat_id": chat_id,
            "verified": False,
            "reason": "currently_bound",
        }
    evidence = _candidate_receipt_evidence(repo, chat_id)
    history = _historical_chat_evidence(repo).get(chat_id, [])
    if not evidence and not history:
        return {
            "chat_id": chat_id,
            "verified": False,
            "reason": "no_corroborating_receipts_or_history",
        }
    status = ensure_browser_running(verify_auth=True)
    result = _verify_historical_chat_ownership(
        repo,
        f"https://chatgpt.com/c/{chat_id}",
        port=int(status["port"]),
    )
    if not result.get("owned"):
        return {
            "chat_id": chat_id,
            "verified": False,
            "reason": "bootstrap_markers_missing",
            "receipt_evidence": evidence,
            "historical_evidence": history,
        }
    _record_owned_chat(
        repo,
        f"https://chatgpt.com/c/{chat_id}",
        created_reason="historical_verified",
    )
    registry = _read_json(_owned_chat_registry_path(repo))
    chats = registry.get("chats") if isinstance(registry.get("chats"), list) else []
    for row in chats:
        if str(row.get("chat_id") or "") == chat_id:
            row["verified_from"] = (
                "bootstrap_markers_and_receipts" if evidence
                else "bootstrap_markers_and_history"
            )
            row["historical_evidence"] = history
            row["verified_utc"] = _utc_now()
            break
    registry["updated_utc"] = _utc_now()
    _atomic_json(_owned_chat_registry_path(repo), registry)
    return {
        "chat_id": chat_id,
        "verified": True,
        "reason": (
            "bootstrap_markers_and_receipts" if evidence
            else "bootstrap_markers_and_history"
        ),
        "receipt_evidence": evidence,
        "historical_evidence": history,
    }


_ARCHIVE_CHAT_JS = r"""(() => {
const visible = el => {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
};
const text = el => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();

const archiveButtons = Array.from(document.querySelectorAll('button,[role="menuitem"]'))
  .filter(visible)
  .filter(el => /^archive( chat| conversation)?$/i.test(text(el)));
if (archiveButtons.length === 1) {
  archiveButtons[0].click();
  return 'clicked_archive';
}
if (archiveButtons.length > 1) return 'ambiguous_archive';

const menuButtons = Array.from(document.querySelectorAll('button'))
  .filter(visible)
  .filter(el => {
    const label = text(el);
    return /^(more|more options|conversation options)$/i.test(label) ||
      /more options|conversation options/i.test(el.getAttribute('aria-label') || '');
  });
if (menuButtons.length !== 1) return menuButtons.length ? 'ambiguous_menu' : 'no_menu';
menuButtons[0].click();
return 'opened_menu';
})()"""


def _archive_chat_via_ui(target: cdp.Target) -> str:
    first = str(cdp.evaluate(target, _ARCHIVE_CHAT_JS, timeout=10.0, user_gesture=True) or "")
    if first == "clicked_archive":
        return first
    if first != "opened_menu":
        raise BrowserError(f"archive control unavailable: {first or 'unknown'}")
    time.sleep(0.2)
    second = str(cdp.evaluate(target, _ARCHIVE_CHAT_JS, timeout=10.0, user_gesture=True) or "")
    if second != "clicked_archive":
        raise BrowserError(f"archive action unavailable after opening menu: {second or 'unknown'}")
    return second


def process_archive_queue(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    queue = archive_queue_status(repo)
    items = list(queue["items"])
    if not items:
        return queue

    browser = ensure_browser_running(verify_auth=True)
    port = int(browser["port"])
    bound = _all_bound_chat_ids()
    registry = _read_json(_owned_chat_registry_path(repo))
    chats = registry.get("chats") if isinstance(registry.get("chats"), list) else []
    owned = {str(row.get("chat_id") or ""): row for row in chats}

    for item in items:
        if item.get("state") == "archived":
            continue
        chat_id = str(item.get("chat_id") or "")
        row = owned.get(chat_id)
        if not row:
            item["state"] = "blocked_unowned"
            item["last_error"] = "chat is not in the verified owned-chat registry"
            continue
        if chat_id in bound:
            item["state"] = "blocked_active"
            item["last_error"] = "chat is currently bound to an active project"
            continue

        target = None
        try:
            item["attempts"] = int(item.get("attempts") or 0) + 1
            target = cdp.create_target(
                port,
                str(item.get("chat_url") or row.get("chat_url") or ""),
                background=True,
            )
            target, _ = wait_for_authenticated(
                port, chat_url=target.url, timeout=30.0, target=target
            )
            if chat_id in _all_bound_chat_ids():
                raise BrowserError("chat became active before archive")
            outcome = _archive_chat_via_ui(target)
            if outcome != "clicked_archive":
                raise BrowserError(f"archive did not complete: {outcome}")
            item["state"] = "archived"
            item["archived_utc"] = _utc_now()
            item["last_error"] = None
            row["archive_state"] = "archived"
            row["archived_utc"] = item["archived_utc"]
        except Exception as exc:
            if item.get("state") not in {"blocked_active", "blocked_unowned"}:
                item["state"] = "retry"
                item["last_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if target is not None:
                cdp.close_target(port, target.id)

    _atomic_json(
        _archive_queue_path(repo),
        {"schema_version": 1, "items": items, "updated_utc": _utc_now()},
    )
    registry["chats"] = chats
    registry["updated_utc"] = _utc_now()
    _atomic_json(_owned_chat_registry_path(repo), registry)
    return archive_queue_status(repo)




def _control_worktree_for_repo(repo: Path) -> Path:
    home = Path(
        os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))
    ).expanduser().resolve()
    return home / "projects" / _project_key(repo) / "control"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_rollover_checkpoint(
    repo: Path,
    record: dict[str, Any],
    *,
    token: str,
    objective: str = "Continue the existing Do Again automation goal from Git and receipts.",
) -> dict[str, Any]:
    repo = repo.resolve()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True, capture_output=True,
    )
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        text=True, capture_output=True,
    )
    control = _control_worktree_for_repo(repo)
    requests_dir = control / "automation/do_again/requests"
    receipts_dir = control / "automation/do_again/receipts"
    receipts: dict[str, dict[str, Any]] = {}
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            request_id = str(payload.get("request_id") or path.stem)
            receipts[request_id] = {
                "state": payload.get("state"),
                "sha256": _sha256_file(path),
            }
    request_ids = sorted(path.stem for path in requests_dir.glob("*.json")) if requests_dir.is_dir() else []
    completed = sorted(request_id for request_id in request_ids if request_id in receipts)
    pending = sorted(request_id for request_id in request_ids if request_id not in receipts)

    request_rows: list[tuple[str, dict[str, Any]]] = []
    if requests_dir.is_dir():
        for path in requests_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                request_rows.append((str(payload.get("issued_at_utc") or ""), payload))
    request_rows.sort(key=lambda item: item[0])

    latest_continuation = None
    for _, payload in reversed(request_rows):
        continuation = payload.get("continuation")
        if isinstance(continuation, dict):
            latest_continuation = continuation
            break

    if latest_continuation and latest_continuation.get("summary"):
        objective = str(latest_continuation.get("summary"))
    unresolved = [
        {"request_id": request_id, "state": row.get("state")}
        for request_id, row in sorted(receipts.items())
        if str(row.get("state") or "") not in {"succeeded", "cancelled"}
    ]
    if pending:
        next_action = f"Inspect and complete pending request {pending[-1]}, then continue the existing goal once."
    elif unresolved:
        next_action = f"Inspect unresolved receipt {unresolved[-1]['request_id']} and continue with a newly scoped request if needed."
    else:
        next_action = "Continue the existing goal from the latest acknowledged receipt and Git state."

    checkpoint = {
        "schema_version": 1,
        "handoff_token": token,
        "project": repo.name,
        "repo": str(repo),
        "objective": objective,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "control_branch": record.get("control_branch"),
        "active_chat_url": record.get("chat_url"),
        "completed_request_ids": completed,
        "pending_request_ids": pending,
        "receipts": {key: receipts[key] for key in completed},
        "latest_operator_progress": latest_continuation,
        "unresolved_issues": unresolved,
        "next_action": next_action,
        "created_utc": _utc_now(),
    }
    _atomic_json(_checkpoint_path(repo, token), checkpoint)
    return checkpoint


def _checkpoint_transfer_view(checkpoint: dict[str, Any], *, max_completed: int = 40, max_pending: int = 40) -> dict[str, Any]:
    completed = list(checkpoint.get("completed_request_ids") or [])
    pending = list(checkpoint.get("pending_request_ids") or [])
    receipt_rows = checkpoint.get("receipts") if isinstance(checkpoint.get("receipts"), dict) else {}
    selected_completed = completed[-max_completed:]
    selected_pending = pending[-max_pending:]
    return {
        "schema_version": checkpoint.get("schema_version"),
        "handoff_token": checkpoint.get("handoff_token"),
        "project": checkpoint.get("project"),
        "repo": checkpoint.get("repo"),
        "objective": checkpoint.get("objective"),
        "branch": checkpoint.get("branch"),
        "head": checkpoint.get("head"),
        "control_branch": checkpoint.get("control_branch"),
        "active_chat_url": checkpoint.get("active_chat_url"),
        "completed_request_ids": selected_completed,
        "completed_request_count": len(completed),
        "pending_request_ids": selected_pending,
        "pending_request_count": len(pending),
        "receipts": {key: receipt_rows[key] for key in selected_completed if key in receipt_rows},
        "latest_operator_progress": checkpoint.get("latest_operator_progress"),
        "unresolved_issues": list(checkpoint.get("unresolved_issues") or [])[-20:],
        "next_action": checkpoint.get("next_action"),
        "created_utc": checkpoint.get("created_utc"),
        "durable_checkpoint_path": checkpoint.get("durable_checkpoint_path"),
    }


def _conversation_pressure_chars(target: cdp.Target) -> int:
    expression = "(() => {" + _MESSAGE_NODES_JS + r"""
const nodes = messageNodes();
return nodes.reduce((sum, el) => sum + String(el.innerText || el.textContent || '').length, 0);
})()"""
    try:
        value = cdp.evaluate(target, expression, timeout=10.0)
    except Exception:
        # Proactive rollover is best-effort. A transient metrics read must not
        # block receipt delivery; the context-limit warning remains the hard fallback.
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _rollover_needed(target: cdp.Target) -> bool:
    warning = _context_limit_warning(target)
    if warning:
        return True
    config = load_config()
    threshold = int(config.get("rollover_char_threshold") or 180000)
    if threshold < 20000:
        threshold = 20000
    return _conversation_pressure_chars(target) >= threshold


def _rollover_project_chat(
    repo: Path,
    record: dict[str, Any],
    *,
    port: int,
    old_target: cdp.Target | None = None,
) -> tuple[cdp.Target, dict[str, Any]]:
    remote_url = str(record.get("remote_url") or "")
    control_branch = str(record.get("control_branch") or "operator-control")
    previous_url = str(record.get("chat_url") or "")
    if not remote_url:
        raise BrowserError("project browser record is missing remote_url")

    token = secrets.token_hex(16)
    checkpoint = _build_rollover_checkpoint(repo, record, token=token)
    checkpoint["durable_checkpoint_path"] = str(_checkpoint_path(repo, token))
    _atomic_json(_checkpoint_path(repo, token), checkpoint)
    transfer_checkpoint = _checkpoint_transfer_view(checkpoint)
    transaction = {
        "schema_version": 1,
        "handoff_token": token,
        "state": "prepared",
        "predecessor_chat_url": previous_url or None,
        "successor_chat_url": None,
        "checkpoint_path": str(_checkpoint_path(repo, token)),
        "created_utc": _utc_now(),
        "updated_utc": _utc_now(),
        "archive_state": "not_started",
    }
    _atomic_json(_rollover_transaction_path(repo), transaction)

    prompt = _bootstrap_prompt(repo, remote_url, control_branch, readiness_marker=None) + (
        "\n\nThis is a transactional Do Again rollover. Git, requests, receipts, and the "
        "structured checkpoint below are the source of truth. Do not invent missing state."
        "\nHandoff token: " + token
        + "\nCheckpoint JSON:\n" + json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
        + "\n\nAfter verifying the checkpoint, reply exactly: DO_AGAIN_HANDOFF_READY " + token
    )

    target = cdp.create_target(port, CHATGPT_URL, background=True)
    try:
        target, _ = wait_for_authenticated(port, chat_url=target.url, timeout=30.0, target=target)
        result = send_message(target, prompt, timeout=180.0)
        new_url = str(result.get("chat_url") or "")
        transaction["successor_chat_url"] = new_url or None
        transaction["state"] = "successor_created"
        transaction["updated_utc"] = _utc_now()
        _atomic_json(_rollover_transaction_path(repo), transaction)

        expected = "DO_AGAIN_HANDOFF_READY " + token
        if str(result.get("response") or "").strip() != expected:
            raise BrowserError("successor chat did not acknowledge the exact rollover handoff token")
        if "/c/" not in new_url:
            raise BrowserError("successor chat did not bind a conversation URL")

        transaction["state"] = "acknowledged"
        transaction["updated_utc"] = _utc_now()
        _atomic_json(_rollover_transaction_path(repo), transaction)
        _record_owned_chat(
            repo,
            new_url,
            created_reason="rollover",
            handoff_token=token,
        )

        updated = register_project(
            repo,
            remote_url=remote_url,
            control_branch=control_branch,
            chat_url=new_url,
        )
        updated["previous_chat_url"] = previous_url or None
        updated["rollover_count"] = int(record.get("rollover_count") or 0) + 1
        updated["last_rollover_utc"] = _utc_now()
        updated["last_handoff_token"] = token
        updated["last_checkpoint_path"] = str(_checkpoint_path(repo, token))
        _atomic_json(_project_record_path(repo), updated)

        transaction["state"] = "bound"
        transaction["archive_state"] = "pending" if previous_url else "not_applicable"
        transaction["updated_utc"] = _utc_now()
        _atomic_json(_rollover_transaction_path(repo), transaction)

        if previous_url:
            previous_id = _chat_id(previous_url)
            registry = _read_json(_owned_chat_registry_path(repo))
            chats = registry.get("chats") if isinstance(registry.get("chats"), list) else []
            predecessor = next(
                (row for row in chats if str(row.get("chat_id") or "") == previous_id),
                None,
            )
            if predecessor is not None:
                queue_verified_archives(repo, candidate_ids=[previous_id], apply=True)
                try:
                    archive_result = process_archive_queue(repo)
                    archived = next(
                        (
                            row for row in archive_result.get("items", [])
                            if str(row.get("chat_id") or "") == previous_id
                        ),
                        None,
                    )
                    transaction["archive_state"] = str(
                        (archived or {}).get("state") or "pending"
                    )
                    if archived and archived.get("last_error"):
                        transaction["archive_error"] = archived.get("last_error")
                except Exception as archive_exc:
                    transaction["archive_state"] = "retry"
                    transaction["archive_error"] = f"{type(archive_exc).__name__}: {archive_exc}"
                transaction["updated_utc"] = _utc_now()
                _atomic_json(_rollover_transaction_path(repo), transaction)
            else:
                transaction["archive_state"] = "blocked_unowned"
                transaction["archive_error"] = "predecessor is not in the verified owned-chat registry"
                transaction["updated_utc"] = _utc_now()
                _atomic_json(_rollover_transaction_path(repo), transaction)

        if old_target is not None and old_target.id != target.id:
            cdp.close_target(port, old_target.id)
        return target, updated
    except Exception as exc:
        transaction["state"] = "failed"
        transaction["error"] = f"{type(exc).__name__}: {exc}"
        transaction["updated_utc"] = _utc_now()
        _atomic_json(_rollover_transaction_path(repo), transaction)
        cdp.close_target(port, target.id)
        raise


def _project_key(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]


def _project_record_path(repo: Path) -> Path:
    paths = browser_paths()
    paths.projects.mkdir(parents=True, exist_ok=True)
    return paths.projects / f"{_project_key(repo)}.json"


def project_record(repo: Path) -> dict[str, Any]:
    return _read_json(_project_record_path(repo))


@_project_operation
def register_project(
    repo: Path,
    *,
    remote_url: str | None = None,
    control_branch: str | None = None,
    chat_url: str | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    value = project_record(repo)
    value.update(
        {
            "schema_version": 1,
            "key": _project_key(repo),
            "repo": str(repo),
            "updated_utc": _utc_now(),
        }
    )
    if remote_url is not None:
        value["remote_url"] = remote_url
    if control_branch is not None:
        value["control_branch"] = control_branch
    if chat_url is not None:
        value["chat_url"] = chat_url
    _atomic_json(_project_record_path(repo), value)
    return value


def _bootstrap_prompt(
    repo: Path,
    remote_url: str,
    control_branch: str,
    *,
    readiness_marker: str | None = "DO_AGAIN_PROJECT_READY",
) -> str:
    prompt = f"""Do Again automation workspace bootstrap.

Project: {repo.name}
Repository: {remote_url}
Control branch: {control_branch}

When development work is requested in this conversation, use the Do Again request/receipt loop for local execution. Submit scoped request JSON files under automation/do_again/requests on the control branch, read matching receipts under automation/do_again/receipts, inspect failures, and iterate until the goal is actually complete. Do not claim a local action succeeded without a receipt."""
    if readiness_marker:
        prompt += f"\n\nReply exactly: {readiness_marker}"
    return prompt


def _bootstrap_new_chat(port: int, prompt: str) -> tuple[cdp.Target, dict[str, Any]]:
    target = cdp.create_target(port, CHATGPT_URL, background=True)
    try:
        target, _ = wait_for_authenticated(port, chat_url=target.url, timeout=30.0, target=target)
        result = send_message(target, prompt, timeout=180.0)
        if "DO_AGAIN_PROJECT_READY" not in result.get("response", ""):
            raise BrowserError("ChatGPT bootstrap did not return the expected readiness marker")
        if "/c/" not in str(result.get("chat_url") or ""):
            raise BrowserError("ChatGPT bootstrap did not bind a conversation URL")
        return target, result
    except Exception:
        cdp.close_target(port, target.id)
        raise


@_project_operation
def ensure_project_chat(
    repo: Path,
    *,
    remote_url: str,
    control_branch: str,
) -> dict[str, Any]:
    repo = repo.resolve()
    status = ensure_browser_running(verify_auth=True)
    port = int(status["port"])
    record = register_project(repo, remote_url=remote_url, control_branch=control_branch)
    chat_url = str(record.get("chat_url") or "")
    target = _find_chatgpt_target(port, chat_url or None)

    if chat_url and target is None:
        target = cdp.create_target(port, chat_url, background=True)
    if target is not None and chat_url:
        wait_for_authenticated(port, chat_url=chat_url, timeout=30.0, target=target)
        return record

    _, result = _bootstrap_new_chat(port, _bootstrap_prompt(repo, remote_url, control_branch))
    chat_url = result["chat_url"]
    _record_owned_chat(repo, chat_url, created_reason="bootstrap")
    return register_project(
        repo,
        remote_url=remote_url,
        control_branch=control_branch,
        chat_url=chat_url,
    )


def activate_project(repo: Path, *, daemon_pid: int | None = None) -> dict[str, Any]:
    value = register_project(repo)
    value["active"] = True
    value["daemon_pid"] = daemon_pid or os.getpid()
    value["activated_utc"] = _utc_now()
    with _startup_lock():
        path = browser_paths().root / "leases" / f"{_project_key(repo)}.json"
        _atomic_json(path, value)
    return value


def deactivate_project(repo: Path) -> dict[str, Any]:
    with _startup_lock():
        path = browser_paths().root / "leases" / f"{_project_key(repo)}.json"
        value = _read_json(path)
        value["active"] = False
        value["daemon_pid"] = None
        value["deactivated_utc"] = _utc_now()
        _atomic_json(path, value)
    return value


@_shared_operation
def active_projects() -> list[dict[str, Any]]:
    paths = browser_paths()
    if not paths.projects.is_dir():
        return []
    active: list[dict[str, Any]] = []
    lease_paths = list((paths.root / "leases").glob("*.json"))
    seen = {path.name for path in lease_paths}
    lease_paths.extend(path for path in paths.projects.glob("*.json") if path.name not in seen)
    for path in sorted(lease_paths):
        value = _read_json(path)
        if not value.get("active"):
            continue
        pid = int(value.get("daemon_pid") or 0)
        if not _pid_alive(pid):
            value["active"] = False
            value["daemon_pid"] = None
            _atomic_json(path, value)
            continue
        active.append(value)
    return active


@_shared_operation
def stop_if_unused() -> bool:
    if active_projects():
        return False
    stop_browser(force=True)
    return True


@_project_operation
def notify_receipts(repo: Path, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    if not receipts:
        return {"response": "nothing_to_deliver", "chat_url": ""}

    record = project_record(repo)
    chat_url = str(record.get("chat_url") or "")
    if not chat_url:
        raise BrowserError("project has no bound automation chat; run do-again setup")

    rows: list[str] = []
    ids: list[str] = []
    for receipt in receipts:
        request_id = str(receipt.get("request_id") or "")
        state = str(receipt.get("state") or "")
        ids.append(request_id)
        rows.append(
            f"- {request_id} state={state} "
            f"path=automation/do_again/receipts/{request_id}.json"
        )

    batch_marker = "DO_AGAIN_RECEIPTS_READY request_ids=" + ",".join(ids)
    newline = chr(10)
    message = (
        batch_marker
        + ". Inspect these receipts on "
        + str(record.get("control_branch", "operator-control"))
        + " in order, continue the existing goal once across the batch, and use another scoped "
        + "Do Again request only if more local work is needed:"
        + newline
        + newline.join(rows)
    )

    status = ensure_browser_running(verify_auth=True)
    port = int(status["port"])
    target = _find_chatgpt_target(port, chat_url)
    if target is None:
        target = cdp.create_target(port, chat_url, background=True)

    if _rollover_needed(target):
        if _assistant_snapshot(target).get("busy"):
            raise BrowserError(
                "rollover needed while ChatGPT is still busy; retrying later"
            )
        target, record = _rollover_project_chat(
            repo,
            record,
            port=port,
            old_target=target,
        )

    target, _ = wait_for_authenticated(
        port,
        chat_url=record["chat_url"],
        timeout=30.0,
        target=target,
    )
    if _assistant_snapshot(target).get("busy"):
        raise BrowserError("ChatGPT is still generating; retry delivery later")

    if _page_contains(target, batch_marker):
        return {"response": "already_delivered", "chat_url": target.url}

    try:
        return send_message(
            target,
            message,
            timeout=180.0,
            wait_for_response=False,
        )
    except BrowserSubmissionUncertain:
        # Never create a new chat or resend after an unverified CDP submit.
        raise
    except BrowserError:
        warning = _context_limit_warning(target)
        if not warning or _assistant_snapshot(target).get("busy"):
            raise
        target, _ = _rollover_project_chat(
            repo,
            project_record(repo),
            port=port,
            old_target=target,
        )
        if _page_contains(target, batch_marker):
            return {"response": "already_delivered", "chat_url": target.url}
        return send_message(
            target,
            message,
            timeout=180.0,
            wait_for_response=False,
        )


@_project_operation
def notify_receipt(repo: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    record = project_record(repo)
    chat_url = str(record.get("chat_url") or "")
    if not chat_url:
        raise BrowserError("project has no bound automation chat; run do-again setup")

    request_id = str(receipt.get("request_id") or "")
    state = str(receipt.get("state") or "")
    marker = f"DO_AGAIN_RECEIPT_READY request_id={request_id}"
    message = (
        f"{marker} state={state}. "
        f"Inspect automation/do_again/receipts/{request_id}.json on "
        f"{record.get('control_branch', 'operator-control')}, continue the existing goal, "
        "and use another scoped Do Again request if more local work is needed."
    )

    status = ensure_browser_running(verify_auth=True)
    port = int(status["port"])
    target = _find_chatgpt_target(port, chat_url)
    if target is None:
        target = cdp.create_target(port, chat_url, background=True)
    warning = _context_limit_warning(target)
    if warning:
        if _assistant_snapshot(target).get("busy"):
            raise BrowserError(
                "context limit detected while ChatGPT is still busy; retrying later"
            )
        target, record = _rollover_project_chat(
            repo,
            record,
            port=port,
            old_target=target,
        )

    target, _ = wait_for_authenticated(port, chat_url=record["chat_url"], timeout=30.0, target=target)
    if _assistant_snapshot(target).get("busy"):
        raise BrowserError("ChatGPT is still generating; retry delivery later")
    if _page_contains(target, marker + " state="):
        return {"response": "already_delivered", "chat_url": target.url}

    try:
        return send_message(
            target,
            message,
            timeout=180.0,
            wait_for_response=False,
        )
    except BrowserSubmissionUncertain:
        # Preserve the durable outbox for DOM/receipt reconciliation.
        raise
    except BrowserError:
        # A conversation can cross the limit exactly when the continuation is
        # submitted. Detect that case, roll over once, and retry safely.
        warning = _context_limit_warning(target)
        if not warning or _assistant_snapshot(target).get("busy"):
            raise
        target, _ = _rollover_project_chat(
            repo,
            project_record(repo),
            port=port,
            old_target=target,
        )
        if _page_contains(target, marker + " state="):
            return {"response": "already_delivered", "chat_url": target.url}
        return send_message(
            target,
            message,
            timeout=180.0,
            wait_for_response=False,
        )
