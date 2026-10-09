from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        modern = self.path.startswith("/modern")
        legacy = self.path.startswith("/legacy")
        labeled = self.path.startswith("/labeled")
        body = b"<!doctype html><title>Do Again Browser Smoke</title><div>ready</div>"
        if modern or legacy or labeled:
            body = ("""<!doctype html><title>Do Again Browser Smoke</title>
<main id="messages"></main>
<div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="width:600px;min-height:30px"></div>
<button aria-label="Send">Send</button>
<script>
const modern = """ + str(modern).lower() + """;
const labeled = """ + str(labeled).lower() + """;
const editor = document.querySelector('[contenteditable]');
const addMessage = (role, text) => {
  const node = document.createElement('div');
  if (labeled) {
    const heading = document.createElement('h4');
    heading.textContent = role === 'user' ? 'You said:' : 'ChatGPT said:';
    node.append(heading);
  } else if (modern) {
    const heading = document.createElement('h4');
    heading.dataset.conversationRole = role;
    heading.textContent = role === 'user' ? 'You said:' : 'ChatGPT said:';
    node.append(heading);
  } else {
    node.dataset.messageAuthorRole = role;
  }
  const content = document.createElement('p');
  content.textContent = text;
  node.append(content);
  document.querySelector('#messages').append(node);
};
editor.addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey) return;
  event.preventDefault();
  const text = editor.innerText.trim();
  if (!text) return;
  addMessage('user', text);
  editor.innerText = '';
  setTimeout(() => addMessage('assistant', 'DO_AGAIN_BROWSER_OK'), 100);
});
</script>""").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def wait_target(cdp_module, port: int, url: str):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for target in cdp_module.targets(port):
            if target.url.startswith(url):
                return target
        time.sleep(0.1)
    raise RuntimeError(f"browser target did not appear: {url}")


def wait_title(cdp_module, target, expected: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = cdp_module.evaluate(target, "document.title")
            if last == expected:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(
        f"browser page did not become ready: expected title {expected!r}, got {last!r}"
    )


def kill_browser(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            text=True,
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--background",
        action="store_true",
        help="Also exercise the non-headless background fallback.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="do-again-browser-smoke-") as tmp:
        os.environ["DO_AGAIN_HOME"] = str(Path(tmp) / "home")

        from do_again.browser import cdp
        from do_again.browser.runtime import (
            _context_limit_warning,
            _page_contains,
            activate_project,
            deactivate_project,
            ensure_browser_running,
            launch_browser,
            load_config,
            save_config,
            send_message,
            stop_browser,
            stop_if_unused,
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/"

        try:
            config = load_config()
            config["resolved_mode"] = "headless"
            config["preferred_mode"] = "auto"
            config["port"] = server.server_port
            save_config(config)

            first = launch_browser("headless", initial_url=url, config=config)
            if int(first["port"]) == server.server_port:
                raise RuntimeError("browser reused an occupied port")
            target = wait_target(cdp, int(first["port"]), url)
            wait_title(cdp, target, "Do Again Browser Smoke")
            persisted = cdp.evaluate(
                target,
                "localStorage.setItem('do_again_smoke','yes'); "
                "localStorage.getItem('do_again_smoke')",
            )
            if persisted != "yes":
                raise RuntimeError("could not write persistent browser storage")

            for variant in ("legacy", "modern", "labeled"):
                fixture = cdp.create_target(int(first["port"]), url + variant, background=True)
                wait_title(cdp, fixture, "Do Again Browser Smoke")
                prompt = "DO_AGAIN_RECEIPT_READY request_id=smoke state=succeeded. Quoted: maximum length for this conversation."
                result = send_message(fixture, prompt, timeout=15.0)
                if result.get("response") != "DO_AGAIN_BROWSER_OK":
                    raise RuntimeError(f"{variant} composer/response fixture failed: {result!r}")
                if not _page_contains(fixture, "DO_AGAIN_RECEIPT_READY request_id=smoke state="):
                    raise RuntimeError(f"{variant} receipt acknowledgement was not detected")
                if _page_contains(fixture, "request_id=not-posted state="):
                    raise RuntimeError(f"{variant} reported an unposted receipt")
                if _context_limit_warning(fixture):
                    raise RuntimeError(f"{variant} quoted limit text caused a false rollover")
                cdp.evaluate(fixture, "const alert = document.createElement('div'); alert.setAttribute('role','alert'); alert.textContent='maximum length for this conversation'; document.body.append(alert)")
                if not _context_limit_warning(fixture):
                    raise RuntimeError(f"{variant} limit alert was not detected")
                cdp.close_target(int(first["port"]), fixture.id)
            project_a = Path(tmp) / "project-a"
            project_b = Path(tmp) / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            activate_project(project_a)
            activate_project(project_b)
            deactivate_project(project_a)
            if stop_if_unused() or not cdp.endpoint_ready(int(first["port"])):
                raise RuntimeError("stopping one project terminated the shared browser")
            deactivate_project(project_b)
            if not stop_if_unused() or cdp.endpoint_ready(int(first["port"])):
                raise RuntimeError("last project stop did not close the shared browser")

            second = launch_browser("headless", initial_url=url, config=load_config())
            target = wait_target(cdp, int(second["port"]), url)
            wait_title(cdp, target, "Do Again Browser Smoke")
            if cdp.evaluate(target, "localStorage.getItem('do_again_smoke')") != "yes":
                raise RuntimeError("browser profile state did not persist across restart")

            old_pid = int(second["pid"])
            old_port = int(second["port"])
            kill_browser(old_pid)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and cdp.endpoint_ready(old_port):
                time.sleep(0.1)
            if cdp.endpoint_ready(old_port):
                raise RuntimeError("forced browser crash did not terminate CDP")

            recovered = ensure_browser_running(verify_auth=False)
            if not recovered.get("running"):
                raise RuntimeError("browser runtime did not recover after forced crash")
            if int(recovered.get("pid") or 0) == old_pid:
                raise RuntimeError("browser recovery did not start a new process")
            stop_browser(force=True)

            if args.background:
                background = launch_browser("background", config=load_config())
                target = cdp.create_target(int(background["port"]), url, background=True)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        if cdp.evaluate(target, "document.title") == "Do Again Browser Smoke":
                            break
                    except Exception:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("background fallback target did not become ready")
                stop_browser(force=True)

            print("BROWSER_SMOKE_OK")
            return 0
        except Exception as exc:
            # Preserve synthetic startup evidence before temporary profile removal.
            log = Path(tmp) / "home/browser/browser.log"
            if log.is_file():
                tail = log.read_bytes()[-32768:].decode("utf-8", errors="replace")
                tail = "".join(char for char in tail if char in "\n\t" or ord(char) >= 32)
                exc.add_note("Synthetic browser log (last 32 KiB):\n" + tail)
            raise
        finally:
            try:
                stop_browser(force=True)
            except Exception:
                pass
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
