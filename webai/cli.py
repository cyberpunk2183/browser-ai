"""Tiny command line: `web-ai mcp` runs the server; `web-ai tabs` is a smoke test."""

from __future__ import annotations

import json
import sys

from . import __version__, cdp, mcp_server


USAGE = """web-ai — drive Chrome from an AI agent (MCP over CDP)

Usage:
  web-ai mcp            Run the MCP server over stdio (wire this into Codex).
  web-ai tabs           List open tabs (quick check that Chrome is reachable).
  web-ai doctor         Check the connection to Chrome's debug port.
  web-ai version

First start Chrome with a debug port, e.g.:
  google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.browser-ai-profile"
"""


def _doctor() -> int:
    try:
        tabs = cdp.list_targets()
    except cdp.CDPError as exc:
        print("✗ " + str(exc))
        return 1
    print(f"✓ Chrome reachable at {cdp._base_url()} — {len(tabs)} tab(s) open.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "mcp"

    if cmd in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    if cmd in {"version", "--version", "-v"}:
        print(__version__)
        return 0
    if cmd == "mcp":
        return mcp_server.main()
    if cmd == "doctor":
        return _doctor()
    if cmd == "tabs":
        try:
            tabs = cdp.list_targets()
        except cdp.CDPError as exc:
            print("✗ " + str(exc))
            return 1
        print(json.dumps(
            [{"id": t.get("id"), "title": t.get("title"), "url": t.get("url")} for t in tabs],
            indent=2, ensure_ascii=False,
        ))
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
