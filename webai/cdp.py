"""Chrome DevTools Protocol client over nothing but the standard library.

It talks straight to a Chrome that was started with ``--remote-debugging-port``
and drives the page. No extension is installed. The HTTP endpoints (``/json``)
list and open tabs;
the actual commands ride a WebSocket, which we hand-roll here so the package
stays dependency-free (``pip install`` pulls nothing).

Start Chrome like this (any Chromium-based browser works):

    google-chrome --remote-debugging-port=9222 \\
        --user-data-dir="$HOME/.browser-ai-profile"

A dedicated ``--user-data-dir`` keeps automation out of your everyday profile.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.request
from typing import Any

DEFAULT_HOST = os.environ.get("WEBAI_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WEBAI_PORT", "9222"))


class CDPError(RuntimeError):
    """Something the browser (or the connection to it) refused to do."""


# --- HTTP endpoints: list / open / close tabs -------------------------------

def _base_url(host: str | None = None, port: int | None = None) -> str:
    return f"http://{host or DEFAULT_HOST}:{port or DEFAULT_PORT}"


def _http_get(path: str, host=None, port=None, timeout=10, method="GET") -> Any:
    url = _base_url(host, port) + path
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise CDPError(
            f"Could not reach Chrome at {_base_url(host, port)} ({exc}). "
            "Start it with --remote-debugging-port and try again."
        ) from exc
    body = body.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def list_targets(host=None, port=None) -> list[dict[str, Any]]:
    """Open pages/tabs, each with an id, title, url and webSocketDebuggerUrl."""
    data = _http_get("/json", host, port) or []
    return [t for t in data if t.get("type") == "page"]


def new_tab(url: str = "about:blank", host=None, port=None) -> dict[str, Any]:
    # Chrome 111+ requires PUT on /json/new (GET returns 405).
    return _http_get("/json/new?" + url, host, port, method="PUT")


def activate_tab(target_id: str, host=None, port=None) -> None:
    _http_get("/json/activate/" + target_id, host, port)


def close_tab(target_id: str, host=None, port=None) -> None:
    _http_get("/json/close/" + target_id, host, port)


# --- minimal WebSocket client (RFC 6455, client side) -----------------------

class _WS:
    """Just enough WebSocket to carry CDP's JSON back and forth.

    Client frames are masked (the spec requires it); server frames are not.
    We handle text, continuation, ping (auto-pong) and close. That is all CDP
    ever sends us.
    """

    def __init__(self, ws_url: str, timeout: float = 30.0):
        if not ws_url.startswith("ws://"):
            raise CDPError(f"Unexpected WebSocket URL: {ws_url}")
        rest = ws_url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        resp = self._read_until(b"\r\n\r\n")
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise CDPError("WebSocket handshake failed: " + resp.decode("latin1")[:200])
        self._buf = b""

    def _read_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _recv_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise CDPError("WebSocket closed mid-frame")
            out += chunk
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        """Return the next complete text message, answering pings along the way."""
        message = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length) if length else b""
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x9:  # ping -> pong
                self._send_control(0xA, data)
                continue
            if opcode == 0x8:  # close
                raise CDPError("WebSocket closed by browser")
            if opcode in (0x0, 0x1, 0x2):
                message += data
                if fin:
                    return message.decode("utf-8", "replace")

    def _send_control(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        header = bytes([0x80 | opcode, 0x80 | len(data)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + masked)

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --- a session bound to one tab ---------------------------------------------

class Session:
    """A CDP connection to a single page, with request/response by message id."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self._ws = _WS(ws_url, timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        msg_id = self._id
        self._ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        # Skip unsolicited events; return the response that carries our id.
        while True:
            reply = json.loads(self._ws.recv_text())
            if reply.get("id") != msg_id:
                continue  # an event, not our answer
            if "error" in reply:
                raise CDPError(f"{method}: {reply['error'].get('message', reply['error'])}")
            return reply.get("result", {})

    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        """Run JS in the page and return the value (by value)."""
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        })
        if result.get("exceptionDetails"):
            exc = result["exceptionDetails"]
            text = exc.get("exception", {}).get("description") or exc.get("text") or "JS error"
            raise CDPError(text)
        return result.get("result", {}).get("value")

    def navigate(self, url: str) -> dict:
        return self.call("Page.navigate", {"url": url})

    def screenshot_png_b64(self) -> str:
        result = self.call("Page.captureScreenshot", {"format": "png"})
        return result.get("data", "")

    def print_pdf_b64(self) -> str:
        # printToPDF streams base64; printBackground keeps colours/logos.
        result = self.call("Page.printToPDF", {"printBackground": True}, timeout=60.0)
        return result.get("data", "")

    def bring_to_front(self) -> None:
        self.call("Page.bringToFront")

    def close(self) -> None:
        self._ws.close()


# --- pick a tab / open a session --------------------------------------------

def session_for(target_id: str | None = None, host=None, port=None,
                timeout: float = 30.0) -> tuple[Session, dict[str, Any]]:
    """Open a Session for the given tab id, or the first tab if none is given.

    Returns ``(session, target)`` so callers can report which tab they hit.
    """
    targets = list_targets(host, port)
    if not targets:
        raise CDPError("No open tabs. Open one with web_new_tab first.")
    if target_id:
        target = next((t for t in targets if t.get("id") == target_id), None)
        if target is None:
            raise CDPError(f"No tab with id {target_id}. Use web_tabs to list them.")
    else:
        target = targets[0]
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise CDPError("Tab has no debugger URL (is another client attached?).")
    return Session(ws_url, timeout=timeout), target
