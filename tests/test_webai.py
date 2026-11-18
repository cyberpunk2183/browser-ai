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

    def print_pdf_b64(self):
        # Minimal valid-ish PDF bytes, base64-encoded.
        import base64
        return base64.b64encode(b"%PDF-1.4 fake pdf").decode("ascii")

    def bring_to_front(self):
        self.calls.append("bring_to_front")

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


def test_download_pdf_writes_file(monkeypatch, tmp_path):
    sess = FakeSession({"document.title": "Peticao Inicial"})
    _patch_session(monkeypatch, sess)
    out = mcp_server.tool_download_pdf({"path": str(tmp_path)})
    data = json.loads(out["content"][0]["text"])
    assert data["saved"].endswith(".pdf")
    assert "Peticao Inicial" in data["saved"]
    assert os.path.isfile(data["saved"])
    assert data["bytes"] > 0


def test_download_pdf_default_dir(monkeypatch, tmp_path):
    sess = FakeSession({"document.title": "Contrato"})
    _patch_session(monkeypatch, sess)
    monkeypatch.setenv("WEBAI_DOWNLOAD_DIR", str(tmp_path))
    out = mcp_server.tool_download_pdf({})
    data = json.loads(out["content"][0]["text"])
    assert data["saved"] == os.path.join(str(tmp_path), "Contrato.pdf")


def test_switch_tab_activates(monkeypatch):
    calls = {}
    monkeypatch.setattr(mcp_server.cdp, "activate_tab",
                        lambda tid, **kw: calls.setdefault("activated", tid))
    sess = FakeSession()
    monkeypatch.setattr(mcp_server.cdp, "session_for",
                        lambda target_id=None, **kw: (sess, {"id": target_id, "title": "T", "url": "u"}))
    out = mcp_server.tool_switch_tab({"target_id": "ABC"})
    data = json.loads(out["content"][0]["text"])
    assert data["active"] == "ABC"
    assert calls["activated"] == "ABC"
    assert "bring_to_front" in sess.calls


class SeqSession:
    """Session whose location.href changes after a history.go(), to model navigation."""
    def __init__(self):
        self.href = "https://b.com"
        self.calls = []
        self.closed = False

    def evaluate(self, expression, await_promise=True):
        self.calls.append(expression)
        if "history.go" in expression:
            self.href = "https://a.com"  # navigation happened
            return None
        if "location.href" in expression:
            return self.href
        if "document.title" in expression:
            return "Página A"
        return None

    def close(self):
        self.closed = True


def test_back_reports_new_url(monkeypatch):
    sess = SeqSession()
    monkeypatch.setattr(mcp_server.cdp, "session_for", lambda target_id=None, **kw: (sess, {"id": "T"}))
    out = mcp_server.tool_back({})
    data = json.loads(out["content"][0]["text"])
    assert data["back"] is True and data["url"] == "https://a.com"
    assert any("history.go(-1)" in c for c in sess.calls)


def test_forward_uses_positive_delta(monkeypatch):
    sess = SeqSession()
    monkeypatch.setattr(mcp_server.cdp, "session_for", lambda target_id=None, **kw: (sess, {"id": "T"}))
    mcp_server.tool_forward({})
    assert any("history.go(1)" in c for c in sess.calls)


def test_back_when_no_history(monkeypatch):
    sess = FakeSession({"location.href": "https://same.com"})  # href never changes
    _patch_session(monkeypatch, sess)
    out = mcp_server.tool_back({})
    assert "no page to go back" in out["content"][0]["text"]


def test_filename_is_sanitised():
    bad = 'Proc: 123/2026 <auto> "réu"?'
    safe = mcp_server._safe_filename(bad)
    assert "/" not in safe and "<" not in safe and '"' not in safe and "?" not in safe


# --- web-ai chat (Ollama agent loop) ----------------------------------------

from webai import cli, ollama_chat  # noqa: E402


def test_cli_imports_os_for_start():
    # Regression: `web-ai start` crashed with NameError: name 'os' is not defined.
    assert hasattr(cli, "os")


def test_ollama_tools_shape_and_skips_screenshot():
    tools = ollama_chat.ollama_tools()
    names = {t["function"]["name"] for t in tools}
    assert "web_screenshot" not in names
    assert "web_read" in names and "web_click" in names
    for t in tools:
        assert t["type"] == "function"
        assert t["function"]["parameters"]["type"] == "object"


def test_ollama_tools_hide_eval_when_disabled(monkeypatch):
    monkeypatch.setenv("WEBAI_NO_EVAL", "1")
    names = {t["function"]["name"] for t in ollama_chat.ollama_tools()}
    assert "web_eval" not in names


def _fake_tabs(monkeypatch):
    monkeypatch.setattr(cdp, "list_targets",
                        lambda *a, **k: [{"id": "T1", "title": "Tribunal", "url": "https://x.jus.br"}])


def test_run_tool_accepts_string_arguments(monkeypatch):
    _fake_tabs(monkeypatch)
    out = ollama_chat.run_tool("web_tabs", "{}")
    assert "Tribunal" in out


def test_run_tool_bad_json_and_unknown_tool():
    assert ollama_chat.run_tool("web_tabs", "{nope").startswith("Error:")
    assert ollama_chat.run_tool("web_nope", {}).startswith("Error:")
    assert ollama_chat.run_tool("web_screenshot", {}).startswith("Error:")


def test_run_tool_truncates_long_results(monkeypatch):
    monkeypatch.setattr(cdp, "list_targets",
                        lambda *a, **k: [{"id": str(i), "title": "x" * 500, "url": ""} for i in range(50)])
    out = ollama_chat.run_tool("web_tabs", {})
    assert len(out) < ollama_chat.MAX_RESULT_CHARS + 100
    assert "truncated" in out


def test_agent_runs_tool_then_answers(monkeypatch):
    _fake_tabs(monkeypatch)
    replies = iter([
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "web_tabs", "arguments": {}}}]}},
        {"message": {"role": "assistant", "content": "Uma aba: Tribunal."}},
    ])
    sent = []

    def post(url, payload):
        sent.append(json.loads(json.dumps(payload)))
        return next(replies)

    agent = ollama_chat.Agent("qwen3", "http://localhost:11434/", post=post, log=lambda _: None)
    assert agent.ask("quais abas?") == "Uma aba: Tribunal."
    assert sent[0]["model"] == "qwen3" and sent[0]["stream"] is False
    assert sent[0]["tools"]
    assert sent[0]["think"] is False
    tool_msgs = [m for m in sent[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and "Tribunal" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_name"] == "web_tabs"


def test_agent_stops_after_max_steps(monkeypatch):
    _fake_tabs(monkeypatch)
    monkeypatch.setenv("WEBAI_MAX_STEPS", "3")
    loop = {"message": {"content": "",
                        "tool_calls": [{"function": {"name": "web_tabs", "arguments": {}}}]}}
    calls = []
    agent = ollama_chat.Agent("m", "http://h", post=lambda u, p: calls.append(1) or loop,
                              log=lambda _: None)
    assert "Stopped after 3" in agent.ask("loop")
    assert len(calls) == 3


def test_parse_flags_and_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5:11434")
    monkeypatch.delenv("WEBAI_MODEL", raising=False)
    model, host, task = ollama_chat._parse(["--model", "llama3.1", "abra", "o", "google"])
    assert (model, host, task) == ("llama3.1", "http://10.0.0.5:11434", "abra o google")


def test_preflight_reports_missing_ollama(monkeypatch):
    def boom(*a, **k):
        raise ollama_chat.OllamaError("down")
    monkeypatch.setattr(ollama_chat, "_http_json", boom)
    assert "not answering" in ollama_chat.preflight("qwen3", "http://h")


def test_preflight_reports_missing_model(monkeypatch):
    monkeypatch.setattr(ollama_chat, "_http_json", lambda *a, **k: {"models": [{"name": "llama3.1:latest"}]})
    assert "ollama pull qwen3" in ollama_chat.preflight("qwen3", "http://h")
