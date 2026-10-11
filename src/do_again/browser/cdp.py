from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import BrowserError


class CdpError(BrowserError):
    pass


class CdpTimeoutError(CdpError):
    """A CDP command timed out; its side effects may be unknown."""


@dataclass(frozen=True)
class Target:
    id: str
    url: str
    title: str
    websocket_url: str


def http_json(port: int, path: str, *, timeout: float = 3.0) -> Any:
    url = f"http://127.0.0.1:{int(port)}{path}"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise CdpError(f"CDP HTTP request failed for {url}: {type(exc).__name__}: {exc}") from exc


def endpoint_ready(port: int) -> bool:
    try:
        value = http_json(port, "/json/version", timeout=1.0)
    except CdpError:
        return False
    return isinstance(value, dict) and bool(value.get("Browser"))


def browser_websocket_url(port: int) -> str:
    value = http_json(port, "/json/version", timeout=2.0)
    if not isinstance(value, dict):
        raise CdpError("CDP /json/version returned an unexpected payload")
    ws = str(value.get("webSocketDebuggerUrl") or "")
    if not ws:
        raise CdpError("CDP browser websocket endpoint is unavailable")
    return ws


def targets(port: int) -> list[Target]:
    value = http_json(port, "/json/list", timeout=2.0)
    if not isinstance(value, list):
        raise CdpError("CDP /json/list returned an unexpected payload")
    rows: list[Target] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        target_id = str(item.get("id") or "")
        ws = str(item.get("webSocketDebuggerUrl") or "")
        if not target_id or not ws:
            continue
        rows.append(
            Target(
                id=target_id,
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                websocket_url=ws,
            )
        )
    return rows


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise CdpError("CDP websocket closed unexpectedly")
        chunks.extend(chunk)
    return bytes(chunks)


class WebSocket:
    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise CdpError(f"unsupported websocket scheme: {parsed.scheme!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise CdpError("CDP websocket must be on loopback")
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise CdpError(f"CDP websocket connection failed: {exc}") from exc
        try:
            self._handshake(host, port, path, timeout)
        except OSError as exc:
            self.sock.close()
            raise CdpError(f"CDP websocket handshake failed: {exc}") from exc
        except Exception:
            self.sock.close()
            raise

    def _handshake(self, host: str, port: int, path: str, timeout: float) -> None:
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://{host}:{port}\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))

        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("CDP websocket handshake closed unexpectedly")
            header.extend(chunk)
            if len(header) > 65536:
                raise CdpError("CDP websocket handshake response is too large")
        head, remainder = bytes(header).split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise CdpError(f"CDP websocket handshake failed: {status.decode('latin1', 'replace')}")

        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        headers: dict[str, str] = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers[name.decode("latin1").lower()] = value.decode("latin1").strip()
        if headers.get("sec-websocket-accept") != expected:
            raise CdpError("CDP websocket handshake returned an invalid accept value")

        self._buffer = bytearray(remainder)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

    def _read(self, length: int) -> bytes:
        if len(self._buffer) >= length:
            data = bytes(self._buffer[:length])
            del self._buffer[:length]
            return data
        prefix = bytes(self._buffer)
        self._buffer.clear()
        return prefix + _recv_exact(self.sock, length - len(prefix))

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        length = len(payload)
        frame = bytearray([0x81])
        if length < 126:
            frame.append(0x80 | length)
        elif length <= 0xFFFF:
            frame.append(0x80 | 126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack("!Q", length))
        frame.extend(mask)
        frame.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(frame)

    def recv_text(self) -> str:
        chunks = bytearray()
        fragmented = False
        while True:
            first, second = self._read(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length + len(chunks) > 16 * 1024 * 1024:
                raise CdpError("CDP websocket message exceeds 16 MiB")
            mask = self._read(4) if masked else b""
            payload = self._read(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x8:
                raise CdpError("CDP websocket closed")
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                if fragmented:
                    raise CdpError("CDP websocket started a second fragmented message")
                chunks.extend(payload)
                fragmented = not final
            elif opcode == 0x0 and fragmented:
                chunks.extend(payload)
            else:
                raise CdpError(f"unexpected CDP websocket opcode: {opcode}")
            if final:
                return chunks.decode("utf-8")

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        frame = bytearray([0x80 | opcode, 0x80 | len(payload)])
        frame.extend(mask)
        frame.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(frame)


def call(url: str, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    try:
        ws = WebSocket(url, timeout=timeout)
    except TimeoutError as exc:
        raise CdpTimeoutError(f"CDP {method} connection timed out") from exc
    try:
        deadline = time.monotonic() + timeout
        message_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        ws.send_text(
            json.dumps(
                {
                    "id": message_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpTimeoutError(f"CDP {method} timed out")
            ws.sock.settimeout(remaining)
            payload = json.loads(ws.recv_text())
            if payload.get("id") != message_id:
                continue
            if "error" in payload:
                raise CdpError(f"CDP {method} failed: {payload['error']}")
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
    except TimeoutError as exc:
        raise CdpTimeoutError(f"CDP {method} response timed out") from exc
    finally:
        ws.close()


def browser_call(port: int, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    return call(browser_websocket_url(port), method, params, timeout=timeout)


def target_call(target: Target | str, method: str, params: dict[str, Any] | None = None, *, port: int | None = None, timeout: float = 30.0) -> dict[str, Any]:
    if isinstance(target, Target):
        url = target.websocket_url
    else:
        if port is None:
            raise CdpError("port is required when targeting by id")
        matches = [item for item in targets(port) if item.id == target]
        if not matches:
            raise CdpError(f"CDP target not found: {target}")
        url = matches[0].websocket_url
    return call(url, method, params, timeout=timeout)


def create_target(port: int, url: str, *, background: bool = True) -> Target:
    result = browser_call(
        port,
        "Target.createTarget",
        {"url": url, "background": bool(background)},
        timeout=10.0,
    )
    target_id = str(result.get("targetId") or "")
    if not target_id:
        raise CdpError("CDP Target.createTarget returned no targetId")
    import time

    for _ in range(50):
        for item in targets(port):
            if item.id == target_id:
                return item
        time.sleep(0.1)
    raise CdpError(f"CDP target did not appear: {target_id}")


def close_target(port: int, target_id: str) -> bool:
    try:
        result = browser_call(
            port,
            "Target.closeTarget",
            {"targetId": target_id},
            timeout=5.0,
        )
        return bool(result.get("success", True))
    except Exception:
        return False


def close_browser(port: int) -> None:
    try:
        browser_call(port, "Browser.close", timeout=5.0)
    except Exception:
        pass


def evaluate(target: Target, expression: str, *, timeout: float = 30.0, user_gesture: bool = False) -> Any:
    result = target_call(
        target,
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": bool(user_gesture),
        },
        timeout=timeout,
    )
    if result.get("exceptionDetails"):
        raise CdpError(f"CDP JavaScript exception: {result['exceptionDetails']}")
    remote = result.get("result")
    if not isinstance(remote, dict):
        return None
    return remote.get("value")


def insert_text(target: Target, text: str) -> None:
    target_call(target, "Input.insertText", {"text": text}, timeout=30.0)


def press_enter(target: Target) -> None:
    target_call(
        target,
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        },
        timeout=10.0,
    )
    target_call(
        target,
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        },
        timeout=10.0,
    )


def click_send(target: Target) -> None:
    """One trusted browser mouse click, never a JavaScript .click() or Enter fallback.

    Chromium may ignore synthetic DOM clicks in the composer. Selection is
    read-only; once the first CDP mouse event is attempted, the caller's
    pre-dispatch journal treats *any* exception as an uncertain submission.
    """
    point = evaluate(target, r"""(() => {
const visible = el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
  return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; };
const buttons=Array.from(document.querySelectorAll(
  'button[data-testid="send-button"],button[aria-label="Send"],button[aria-label="Send prompt"]'
)).filter(visible);
if(buttons.length!==1 || buttons[0].disabled || buttons[0].getAttribute('aria-disabled')==='true')
  return null;
const button=buttons[0],rect=button.getBoundingClientRect();
const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
if(!(x>=0 && y>=0 && x<innerWidth && y<innerHeight)) return null;
const hit=document.elementFromPoint(x,y);
if(!hit || !button.contains(hit)) return null;
return {x,y};
})()""", timeout=10.0)
    if (not isinstance(point, dict) or set(point) != {'x', 'y'}
            or any(type(point[k]) not in (int, float) or not 0 <= point[k] < 100000
                   for k in ('x', 'y'))):
        raise BrowserError('ChatGPT Send button was unavailable, obscured, or ambiguous')
    # One physical click consists of mouse down and up at the same verified
    # coordinate. A lost press/release response is never retried here.
    common = {'x': point['x'], 'y': point['y'], 'button': 'left', 'clickCount': 1}
    target_call(target, 'Input.dispatchMouseEvent',
                {'type': 'mousePressed', **common, 'buttons': 1}, timeout=10.0)
    target_call(target, 'Input.dispatchMouseEvent',
                {'type': 'mouseReleased', **common, 'buttons': 0}, timeout=10.0)
