"""Tiny command line: `web-ai mcp` runs the server; `web-ai tabs` is a smoke test."""

from __future__ import annotations

import json
import os
import sys

from . import __version__, cdp, mcp_server, ollama_chat


USAGE = """web-ai — drive Chrome from an AI agent (MCP over CDP)

Usage:
  web-ai start          Launch Chrome correctly (debug port + dedicated profile).
  web-ai mcp            Run the MCP server over stdio (wire this into Codex).
  web-ai doctor         Check that the agent can actually control the browser.
  web-ai chat [task]    Drive the browser with a local Ollama model
                        (--model qwen3). No task = interactive.
  web-ai tabs           List open tabs.
  web-ai version

Easiest path: run `web-ai start` once, leave that window open, then use the agent.
"""

# Where Chrome/Chromium usually lives, per OS. First hit wins.
def _find_browser() -> str | None:
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "brave-browser", "microsoft-edge",
                 "chrome", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LocalAppData", "")
        candidates = [
            pf + r"\Google\Chrome\Application\chrome.exe",
            pfx86 + r"\Google\Chrome\Application\chrome.exe",
            local + r"\Google\Chrome\Application\chrome.exe",
            pf + r"\Microsoft\Edge\Application\msedge.exe",
            pfx86 + r"\Microsoft\Edge\Application\msedge.exe",
            pf + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _start() -> int:
    import subprocess
    browser = _find_browser()
    if not browser:
        print("✗ No Chromium-based browser found. Install Google Chrome and try again.")
        return 1
    profile = os.environ.get("WEBAI_PROFILE", os.path.expanduser("~/.browser-ai-profile"))
    port = cdp.DEFAULT_PORT
    # A dedicated --user-data-dir spawns a separate Chrome process, so the debug
    # port opens even if the person's everyday Chrome is already running.
    args = [browser, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check"]
    if os.environ.get("WEBAI_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}:
        args.append("--headless=new")  # for servers / no display
    args.append("about:blank")
    print(f"Starting {os.path.basename(browser)} with debug port {port}…")
    try:
        if os.name == "nt":
            subprocess.Popen(args, close_fds=True,
                             creationflags=0x00000008)  # DETACHED_PROCESS
        else:
            subprocess.Popen(args, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print("✗ Could not launch the browser: " + str(exc))
        return 1
    # Wait for the port to come up.
    import time
    for _ in range(20):
        try:
            cdp.list_targets()
            print(f"✓ Chrome is up and controllable on port {port}. Leave this window open.")
            print("  Now use your agent, or run `web-ai doctor` to confirm.")
            return 0
        except cdp.CDPError:
            time.sleep(0.5)
    print("… Browser launched but the debug port did not answer in time.")
    print("  If a Chrome was already open, close ALL Chrome windows and run `web-ai start` again.")
    return 1


def _doctor() -> int:
    base = cdp._base_url()
    # 1) Is the debug port actually open?
    try:
        tabs = cdp.list_targets()
    except cdp.CDPError as exc:
        print("✗ " + str(exc))
        print()
        print("  The debug port is NOT open. Most common cause on Windows/macOS:")
        print("  Chrome was ALREADY running, so --remote-debugging-port was ignored.")
        print("  Fix: close EVERY Chrome window first, then start it like this —")
        print(f"    chrome --remote-debugging-port={cdp.DEFAULT_PORT} "
              f'--user-data-dir="<a dedicated folder>"')
        print("  The separate --user-data-dir is what lets it open even if your")
        print("  normal Chrome is in use. Re-run `web-ai doctor` after that.")
        return 1
    print(f"✓ Debug port open at {base} — {len(tabs)} tab(s).")

    if not tabs:
        print("… No tabs yet. Open a page in that Chrome, then re-run doctor.")
        return 0

    # 2) Can we actually CONTROL a tab (the part that click/type needs)?
    try:
        sess, target = cdp.session_for(tabs[0].get("id"))
        try:
            answer = sess.evaluate("6*7")
            title = sess.evaluate("document.title")
        finally:
            sess.close()
    except cdp.CDPError as exc:
        print("✗ Reached the port, but could NOT control the tab: " + str(exc))
        print("  Another debugger may be attached (DevTools open? another client?).")
        print("  Close DevTools / other automation and try again.")
        return 1
    if answer != 42:
        print(f"✗ Tab responded oddly (expected 42, got {answer!r}).")
        return 1
    print(f"✓ Full control works — ran JS in tab “{title}”. click/type/read are good to go.")
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
    if cmd == "start":
        return _start()
    if cmd == "doctor":
        return _doctor()
    if cmd == "chat":
        return ollama_chat.main(argv[1:])
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
