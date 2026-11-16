"""An MCP server over stdio for driving Chrome, standard library only.

Hand-rolled JSON-RPC over stdin/stdout so ``pip install browser-ai`` pulls no
dependencies. It speaks the Chrome DevTools Protocol (see ``cdp.py``) to
whatever Chrome you started with ``--remote-debugging-port``.

Wire it into an MCP client (Codex, Claude Code, Cursor) with:

    {"mcpServers": {"browser-ai": {"command": "web-ai", "args": ["mcp"]}}}
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable

from . import __version__, cdp

PROTOCOL_VERSION = "2024-11-05"

# Arbitrary JS (web_eval) is on by default: it is the escape hatch that makes
# the agent genuinely useful. Turn it off on a shared or sensitive machine with
# WEBAI_NO_EVAL=1 (or WEBAI_ALLOW_EVAL=0).
_OFF = {"0", "false", "no", "off"}


def _eval_enabled() -> bool:
    if os.environ.get("WEBAI_NO_EVAL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    allow = os.environ.get("WEBAI_ALLOW_EVAL")
    if allow is None:
        return True
    return allow.strip().lower() not in _OFF


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def _data(payload: object) -> dict[str, Any]:
    return _text(json.dumps(payload, indent=2, ensure_ascii=False))


def _session(args: dict[str, Any]):
    return cdp.session_for(args.get("target_id"))


# --- tool implementations ---------------------------------------------------

def tool_tabs(args: dict[str, Any]) -> dict[str, Any]:
    tabs = cdp.list_targets()
    return _data([
        {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
        for t in tabs
    ])


def tool_new_tab(args: dict[str, Any]) -> dict[str, Any]:
    t = cdp.new_tab(args.get("url", "about:blank"))
    return _data({"id": t.get("id"), "url": t.get("url")})


def tool_close_tab(args: dict[str, Any]) -> dict[str, Any]:
    cdp.close_tab(args["target_id"])
    return _text("closed")


def tool_navigate(args: dict[str, Any]) -> dict[str, Any]:
    sess, target = _session(args)
    try:
        sess.navigate(args["url"])
        return _text(f"navigating {target.get('id')} to {args['url']}")
    finally:
        sess.close()


def tool_read(args: dict[str, Any]) -> dict[str, Any]:
    max_chars = int(args.get("max_chars") or 40000)
    sess, target = _session(args)
    try:
        text = sess.evaluate("document.body ? document.body.innerText : ''") or ""
        title = sess.evaluate("document.title") or ""
        url = sess.evaluate("location.href") or ""
    finally:
        sess.close()
    truncated = len(text) > max_chars
    return _data({
        "title": title,
        "url": url,
        "text": text[:max_chars],
        "truncated": truncated,
    })


def tool_html(args: dict[str, Any]) -> dict[str, Any]:
    max_chars = int(args.get("max_chars") or 40000)
    selector = args.get("selector")
    sess, _ = _session(args)
    try:
        if selector:
            expr = (
                "(() => { const el = document.querySelector(%s);"
                " return el ? el.outerHTML : null; })()" % json.dumps(selector)
            )
        else:
            expr = "document.documentElement.outerHTML"
        html = sess.evaluate(expr)
    finally:
        sess.close()
    if html is None:
        raise cdp.CDPError(f"No element matched selector {selector!r}")
    truncated = len(html) > max_chars
    return _data({"html": html[:max_chars], "truncated": truncated})


def tool_click(args: dict[str, Any]) -> dict[str, Any]:
    selector = args.get("selector")
    text = args.get("text")
    if not selector and not text:
        raise ValueError("Provide 'selector' or 'text'.")
    sess, _ = _session(args)
    try:
        if selector:
            expr = (
                "(() => { const el = document.querySelector(%s);"
                " if (!el) return 'not found';"
                " el.scrollIntoView({block:'center'}); el.click(); return 'ok'; })()"
                % json.dumps(selector)
            )
        else:
            # Click the first element whose visible text matches (clickable-ish).
            expr = (
                "(() => { const needle = %s.trim().toLowerCase();"
                " const els = [...document.querySelectorAll("
                "'a,button,[role=button],input[type=submit],input[type=button],summary,label')];"
                " const el = els.find(e => (e.innerText||e.value||'').trim().toLowerCase().includes(needle));"
                " if (!el) return 'not found';"
                " el.scrollIntoView({block:'center'}); el.click(); return 'ok'; })()"
                % json.dumps(text)
            )
        result = sess.evaluate(expr)
    finally:
        sess.close()
    if result == "not found":
        raise cdp.CDPError("Nothing matched to click.")
    return _text("clicked")


def tool_type(args: dict[str, Any]) -> dict[str, Any]:
    selector = args["selector"]
    value = args.get("text", "")
    submit = bool(args.get("submit"))
    sess, _ = _session(args)
    try:
        expr = (
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return 'not found';"
            " el.focus(); el.value = %s;"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
            " %s"
            " return 'ok'; })()"
            % (
                json.dumps(selector),
                json.dumps(value),
                "if (el.form) el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit();"
                if submit else "",
            )
        )
        result = sess.evaluate(expr)
    finally:
        sess.close()
    if result == "not found":
        raise cdp.CDPError(f"No element matched selector {selector!r}")
    return _text("typed" + (" and submitted" if submit else ""))


def tool_wait(args: dict[str, Any]) -> dict[str, Any]:
    import time
    selector = args["selector"]
    timeout = float(args.get("timeout_seconds") or 10)
    deadline = time.time() + timeout
    expr = "!!document.querySelector(%s)" % json.dumps(selector)
    while time.time() < deadline:
        sess, _ = _session(args)
        try:
            if sess.evaluate(expr):
                return _text("present")
        finally:
            sess.close()
        time.sleep(0.4)
    raise cdp.CDPError(f"Timed out after {timeout}s waiting for {selector!r}")


def tool_eval(args: dict[str, Any]) -> dict[str, Any]:
    if not _eval_enabled():
        raise cdp.CDPError("web_eval is disabled (WEBAI_NO_EVAL). Ask the operator to enable it.")
    sess, _ = _session(args)
    try:
        value = sess.evaluate(args["expression"])
    finally:
        sess.close()
    return _data({"value": value})


def tool_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    sess, _ = _session(args)
    try:
        b64 = sess.screenshot_png_b64()
    finally:
        sess.close()
    if not b64:
        raise cdp.CDPError("Screenshot failed.")
    return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]}


def _download_dir() -> str:
    """Where PDFs land by default: WEBAI_DOWNLOAD_DIR, else ~/Downloads, else ~."""
    env = os.environ.get("WEBAI_DOWNLOAD_DIR")
    if env:
        return os.path.expanduser(env)
    downloads = os.path.expanduser("~/Downloads")
    return downloads if os.path.isdir(downloads) else os.path.expanduser("~")


def _safe_filename(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_.()[]" else "_" for c in name).strip()
    return (keep or "pagina")[:120]


def tool_download_pdf(args: dict[str, Any]) -> dict[str, Any]:
    """Save the current page as a PDF and return the file path."""
    import base64
    sess, target = _session(args)
    try:
        title = sess.evaluate("document.title") or "pagina"
        b64 = sess.print_pdf_b64()
    finally:
        sess.close()
    if not b64:
        raise cdp.CDPError("PDF generation failed.")

    path = args.get("path")
    if path:
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            path = os.path.join(path, _safe_filename(str(title)) + ".pdf")
    else:
        path = os.path.join(_download_dir(), _safe_filename(str(title)) + ".pdf")
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return _data({"saved": path, "bytes": os.path.getsize(path),
                  "title": title, "url": target.get("url")})


def _history_go(args: dict[str, Any], delta: int, word: str) -> dict[str, Any]:
    sess, _ = _session(args)
    try:
        before = sess.evaluate("location.href")
        sess.evaluate(f"history.go({delta})")
        import time
        time.sleep(0.4)  # history navigation is async; let it settle
        after = sess.evaluate("location.href")
        title = sess.evaluate("document.title")
    finally:
        sess.close()
    if before == after:
        return _text(f"no page to go {word} to (history unchanged: {after})")
    return _data({word: True, "url": after, "title": title})


def tool_back(args: dict[str, Any]) -> dict[str, Any]:
    return _history_go(args, -1, "back")


def tool_forward(args: dict[str, Any]) -> dict[str, Any]:
    return _history_go(args, 1, "forward")


def tool_switch_tab(args: dict[str, Any]) -> dict[str, Any]:
    """Bring a tab to the front (activate it)."""
    target_id = args["target_id"]
    cdp.activate_tab(target_id)  # HTTP /json/activate
    # Also nudge the page itself to the foreground.
    sess, target = cdp.session_for(target_id)
    try:
        sess.bring_to_front()
    except cdp.CDPError:
        pass
    finally:
        sess.close()
    return _data({"active": target_id, "title": target.get("title"), "url": target.get("url")})


# --- tool registry ----------------------------------------------------------

HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "web_tabs": tool_tabs,
    "web_new_tab": tool_new_tab,
    "web_close_tab": tool_close_tab,
    "web_navigate": tool_navigate,
    "web_read": tool_read,
    "web_html": tool_html,
    "web_click": tool_click,
    "web_type": tool_type,
    "web_wait": tool_wait,
    "web_eval": tool_eval,
    "web_screenshot": tool_screenshot,
    "web_download_pdf": tool_download_pdf,
    "web_switch_tab": tool_switch_tab,
    "web_back": tool_back,
    "web_forward": tool_forward,
}

_TARGET = {"target_id": {"type": "string",
                         "description": "Tab id from web_tabs. Omit to use the first/active tab."}}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": {**_TARGET, **properties}, "required": required or []}


TOOLS: list[dict[str, Any]] = [
    {"name": "web_tabs",
     "description": "List the open browser tabs (id, title, url).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "web_new_tab",
     "description": "Open a new tab, optionally at a URL. Returns its id.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
    {"name": "web_close_tab",
     "description": "Close a tab by id.",
     "inputSchema": {"type": "object", "properties": {"target_id": {"type": "string"}},
                     "required": ["target_id"]}},
    {"name": "web_navigate",
     "description": "Navigate a tab to a URL.",
     "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "web_read",
     "description": "Read the visible text of the page (document.body.innerText), with title and URL.",
     "inputSchema": _schema({"max_chars": {"type": "integer", "description": "Default 40000."}})},
    {"name": "web_html",
     "description": "Get outerHTML of the page, or of the element matching 'selector'.",
     "inputSchema": _schema({"selector": {"type": "string"},
                             "max_chars": {"type": "integer"}})},
    {"name": "web_click",
     "description": "Click an element by CSS 'selector', or the first clickable element whose visible text contains 'text'.",
     "inputSchema": _schema({"selector": {"type": "string"}, "text": {"type": "string"}})},
    {"name": "web_type",
     "description": "Type 'text' into the input/textarea matching 'selector'. Set submit=true to submit the form.",
     "inputSchema": _schema({"selector": {"type": "string"}, "text": {"type": "string"},
                             "submit": {"type": "boolean"}}, ["selector"])},
    {"name": "web_wait",
     "description": "Wait until an element matching 'selector' appears (polls), up to timeout_seconds.",
     "inputSchema": _schema({"selector": {"type": "string"},
                             "timeout_seconds": {"type": "number"}}, ["selector"])},
    {"name": "web_eval",
     "description": "Run arbitrary JavaScript in the page and return the result. The power tool; "
                    "can be disabled with WEBAI_NO_EVAL.",
     "inputSchema": _schema({"expression": {"type": "string"}}, ["expression"])},
    {"name": "web_screenshot",
     "description": "Capture a PNG screenshot of the tab.",
     "inputSchema": _schema({})},
    {"name": "web_download_pdf",
     "description": "Save the current page as a PDF and return the file path. Optional 'path' "
                    "(a file or a folder); defaults to ~/Downloads named after the page title.",
     "inputSchema": _schema({"path": {"type": "string"}})},
    {"name": "web_switch_tab",
     "description": "Bring a tab to the front / make it active, by id.",
     "inputSchema": {"type": "object", "properties": {"target_id": {"type": "string"}},
                     "required": ["target_id"]}},
    {"name": "web_back",
     "description": "Go back one entry in the tab's history (like the browser's Back button).",
     "inputSchema": _schema({})},
    {"name": "web_forward",
     "description": "Go forward one entry in the tab's history (like the browser's Forward button).",
     "inputSchema": _schema({})},
]


# --- JSON-RPC / MCP plumbing ------------------------------------------------

def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "browser-ai", "version": __version__},
            },
        }

    if method in {"notifications/initialized", "initialized"}:
        return None  # notification, no reply

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
        try:
            result = handler(params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:  # surface as a tool error, not a crash
            msg = str(exc) or exc.__class__.__name__
            if os.environ.get("WEBAI_DEBUG"):
                msg += "\n" + traceback.format_exc()
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": msg}]}}

    if req_id is not None:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}
    return None


def main(argv: list[str] | None = None) -> int:
    """Read JSON-RPC requests line by line from stdin, answer on stdout."""
    stdin = sys.stdin
    stdout = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(request)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
