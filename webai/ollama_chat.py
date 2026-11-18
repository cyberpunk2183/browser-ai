"""`web-ai chat`: drive Chrome with a local model served by Ollama.

No MCP client needed: this is a small agent loop that sends the browser tools
to Ollama's /api/chat, runs the tool calls the model asks for (through the same
handlers the MCP server uses) and feeds the results back until the model
answers in plain text. Standard library only.

Settings (flags win over environment):
  --model / WEBAI_MODEL        model name, default "qwen3"
  --host  / OLLAMA_HOST        default http://localhost:11434
  WEBAI_NUM_CTX                context window to request, default 16384
  WEBAI_MAX_STEPS              tool calls allowed per request, default 30
  WEBAI_THINK                  1 lets thinking models (qwen3…) reason first;
                               off by default: on a CPU it costs minutes per step
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import cdp, mcp_server

DEFAULT_MODEL = "qwen3"
DEFAULT_HOST = "http://localhost:11434"
# Local models have small context windows; one web_read can be 40k chars.
MAX_RESULT_CHARS = 8000
# Screenshots are base64 images a text model cannot use; web_read covers it.
SKIPPED_TOOLS = {"web_screenshot"}

SYSTEM_PROMPT = """You control a real Chrome browser through tools.
How to work:
- Start with web_tabs or web_navigate, then web_read to see the page.
- To act, use web_click (by visible text or CSS selector) and web_type.
- After navigating or clicking, use web_read again to check what happened.
- Prefer web_read over web_html; use web_html only to find a selector.
- One step at a time. Never invent page content: read it.
- When the task is done, answer briefly in the same language the user wrote in."""


class OllamaError(RuntimeError):
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def ollama_tools() -> list[dict[str, Any]]:
    """The MCP tool list, in the function-calling shape Ollama expects."""
    tools = []
    for tool in mcp_server.TOOLS:
        if tool["name"] in SKIPPED_TOOLS:
            continue
        if tool["name"] == "web_eval" and not mcp_server._eval_enabled():
            continue
        tools.append({"type": "function", "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["inputSchema"],
        }})
    return tools


def _http_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 600) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", body)
        except (ValueError, AttributeError):
            pass
        raise OllamaError(f"Ollama answered {exc.code}: {body}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from None


def run_tool(name: str, arguments: Any) -> str:
    """Run one tool call through the MCP handlers and return its text."""
    if isinstance(arguments, str):  # some models send JSON as a string
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return f"Error: arguments for {name} are not valid JSON: {arguments!r}"
    if not isinstance(arguments, dict):
        arguments = {}
    if name in SKIPPED_TOOLS:
        return f"Error: {name} is not available here. Use web_read instead."
    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
    if "error" in resp:
        return "Error: " + resp["error"]["message"]
    result = resp["result"]
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    text = "\n".join(parts)
    if result.get("isError"):
        text = "Error: " + text
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[truncated, {len(text) - MAX_RESULT_CHARS} more chars]"
    return text


class Agent:
    """Holds the conversation; `ask` runs one user request to completion."""

    def __init__(self, model: str, host: str,
                 post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.post = post or _http_json
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.tools = ollama_tools()
        self.max_steps = _env_int("WEBAI_MAX_STEPS", 30)
        self.options = {"num_ctx": _env_int("WEBAI_NUM_CTX", 16384)}
        self.think = os.environ.get("WEBAI_THINK", "").strip().lower() in {"1", "true", "yes", "on"}
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def _chat(self) -> dict[str, Any]:
        started = time.monotonic()
        reply = self.post(self.host + "/api/chat", {
            "model": self.model, "messages": self.messages, "tools": self.tools,
            "stream": False, "options": self.options, "think": self.think,
        })
        self.log(f"  · model answered in {time.monotonic() - started:.1f}s")
        return reply.get("message") or {}

    def ask(self, prompt: str) -> str:
        self.messages.append({"role": "user", "content": prompt})
        for _ in range(self.max_steps):
            message = self._chat()
            calls = message.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": message.get("content", ""),
                                  **({"tool_calls": calls} if calls else {})})
            if not calls:
                return (message.get("content") or "").strip()
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                self.log(f"  → {name} {json.dumps(args, ensure_ascii=False)[:160]}")
                output = run_tool(name, args)
                self.messages.append({"role": "tool", "tool_name": name, "content": output})
        return (f"(Stopped after {self.max_steps} tool calls without a final answer. "
                "Try a smaller step, or raise WEBAI_MAX_STEPS.)")


def preflight(model: str, host: str) -> str | None:
    """Return a human-readable problem, or None when everything is ready."""
    try:
        tags = _http_json(host.rstrip("/") + "/api/tags", timeout=5)
    except OllamaError:
        return (f"✗ Ollama is not answering at {host}.\n"
                "  Install it from https://ollama.com and make sure it is running.")
    names = {m.get("name", "") for m in tags.get("models", [])}
    wanted = model if ":" in model else model + ":latest"
    if model not in names and wanted not in names:
        return (f"✗ Model '{model}' is not downloaded.\n"
                f"  Run: ollama pull {model}\n"
                "  It must support tool calling (qwen3, llama3.1, mistral-nemo, …).")
    try:
        cdp.list_targets()
    except cdp.CDPError:
        return "✗ Chrome is not controllable. Run `web-ai start` first (then `web-ai doctor`)."
    return None


def _parse(argv: list[str]) -> tuple[str, str, str]:
    model = os.environ.get("WEBAI_MODEL", DEFAULT_MODEL)
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    if not host.startswith(("http://", "https://")):
        host = "http://" + host  # OLLAMA_HOST is often just host:port
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg in {"--model", "-m"}:
            model = next(it, model)
        elif arg.startswith("--model="):
            model = arg.split("=", 1)[1]
        elif arg == "--host":
            host = next(it, host)
        elif arg.startswith("--host="):
            host = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return model, host, " ".join(rest).strip()


def main(argv: list[str]) -> int:
    model, host, task = _parse(argv)
    problem = preflight(model, host)
    if problem:
        print(problem)
        return 1
    agent = Agent(model, host)
    try:
        if task:  # one-shot: web-ai chat "do something"
            print(agent.ask(task))
            return 0
        print(f"browser-ai chat · model {model} · type 'exit' to quit")
        while True:
            try:
                prompt = input("\nyou › ").strip()
            except EOFError:
                print()
                return 0
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit", "sair", "/exit", "/sair"}:
                return 0
            print("\n" + agent.ask(prompt))
    except KeyboardInterrupt:
        print()
        return 130
    except OllamaError as exc:
        print("✗ " + str(exc))
        return 1
