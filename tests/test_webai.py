"""Tests that need no real browser: MCP plumbing, WS framing, handlers via a fake session."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webai import cdp, mcp_server  # noqa: E402


# --- MCP protocol plumbing --------------------------------------------------

def test_initialize_reports_server():
    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["serverInfo"]["name"] == "browser-ai"
    assert resp["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_initialized_notification_has_no_reply():
    assert mcp_server._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_all_handlers():
    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == set(mcp_server.HANDLERS)


def test_unknown_tool_is_an_error():
    resp = mcp_server._handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert resp["error"]["code"] == -32601


# --- WebSocket framing (mask/unmask round-trip) -----------------------------

def test_ws_mask_unmask_roundtrip():
    # A client frame is masked; unmasking with the same key restores the bytes.
    payload = "olá, mundo 🌎".encode("utf-8")
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    back = bytes(b ^ mask[i % 4] for i, b in enumerate(masked))
    assert back == payload


# --- handlers with a fake CDP session ---------------------------------------

class FakeSession:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []
        self.closed = False

    def evaluate(self, expression, await_promise=True):
        self.calls.append(expression)
        for key, val in self.values.items():
            if key in expression:
                return val
        return self.values.get("*")

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return {}

    def screenshot_png_b64(self):
        return "ZmFrZQ=="  # "fake" in base64

    def close(self):
        self.closed = True


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(mcp_server.cdp, "session_for",
                        lambda target_id=None, **kw: (session, {"id": "T1"}))


def test_read_returns_text_title_url(monkeypatch):
    sess = FakeSession({"innerText": "corpo da página", "document.title": "Título",
                        "location.href": "https://ex.com"})
    _patch_session(monkeypatch, sess)
    out = mcp_server.tool_read({})
    data = json.loads(out["content"][0]["text"])
    assert data["text"] == "corpo da página"
    assert data["title"] == "Título"
    assert sess.closed


def test_click_by_selector_ok(monkeypatch):
    sess = FakeSession({"querySelector": "ok"})
    _patch_session(monkeypatch, sess)
    out = mcp_server.tool_click({"selector": "#botao"})
    assert "clicked" in out["content"][0]["text"]


def test_click_not_found_raises(monkeypatch):
    sess = FakeSession({"*": "not found"})
    _patch_session(monkeypatch, sess)
    try:
        mcp_server.tool_click({"selector": "#x"})
        assert False, "should have raised"
    except cdp.CDPError:
        pass


def test_type_requires_match(monkeypatch):
    sess = FakeSession({"querySelector": "ok"})
    _patch_session(monkeypatch, sess)
    out = mcp_server.tool_type({"selector": "#q", "text": "oi", "submit": True})
    assert "submitted" in out["content"][0]["text"]


def test_eval_disabled(monkeypatch):
    monkeypatch.setenv("WEBAI_NO_EVAL", "1")
    sess = FakeSession({"*": 42})
    _patch_session(monkeypatch, sess)
    try:
        mcp_server.tool_eval({"expression": "1+1"})
        assert False, "should be blocked"
    except cdp.CDPError:
        pass


def test_screenshot_returns_image(monkeypatch):
    sess = FakeSession()
    _patch_session(monkeypatch, sess)
    out = mcp_server.tool_screenshot({})
    assert out["content"][0]["type"] == "image"
    assert out["content"][0]["mimeType"] == "image/png"
