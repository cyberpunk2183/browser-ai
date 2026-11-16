#!/usr/bin/env bash
# browser-ai installer for Linux, macOS and WSL.
#
#   ./install.sh            install what is missing, then browser-ai
#   ./install.sh --check    only report what is missing, change nothing
#
# Nothing is installed with sudo without asking you first.
set -u

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${WEBAI_PORT:-9222}"
PROFILE="${WEBAI_PROFILE:-$HOME/.browser-ai-profile}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ok  ] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
bad()  { printf '[FAIL] %s\n' "$*"; }

ask() {  # ask "command to run"  -> runs it after a y/n, unless --check
    say ""
    say "    $1"
    say ""
    if [ "$CHECK_ONLY" = "1" ]; then
        warn "skipped (--check). Run the command above yourself."
        return 1
    fi
    printf "Run it now? [y/N] "
    read -r reply </dev/tty || return 1
    case "$reply" in
        [yY]*) eval "$1" ;;
        *) warn "skipped. Run the command above when you are ready."; return 1 ;;
    esac
}

# --- which system is this? ---------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
    SYSTEM=macos
else
    SYSTEM=linux
fi
say "browser-ai installer - detected: $SYSTEM"
say ""

# --- Python ------------------------------------------------------------------
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -n "$PY" ]; then
    ok "Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
else
    bad "Python 3.9+ not found"
    case "$SYSTEM" in
        macos)  ask "brew install python" ;;
        linux)
            if   command -v apt    >/dev/null 2>&1; then ask "sudo apt install -y python3 python3-pip"
            elif command -v dnf    >/dev/null 2>&1; then ask "sudo dnf install -y python3 python3-pip"
            elif command -v pacman >/dev/null 2>&1; then ask "sudo pacman -S --noconfirm python python-pip"
            else say "Install Python 3.9+ with your package manager, then run this again."
            fi ;;
    esac
    command -v python3 >/dev/null 2>&1 && PY=python3
    [ -z "$PY" ] && { bad "still no Python - stopping here."; exit 1; }
fi

# --- a Chromium browser (the one real requirement) ---------------------------
# browser-ai drives whatever Chromium-based browser you start with a debug port.
BROWSER=""
for candidate in google-chrome google-chrome-stable chromium chromium-browser brave-browser microsoft-edge; do
    if command -v "$candidate" >/dev/null 2>&1; then BROWSER="$candidate"; break; fi
done
# macOS app bundles are not on PATH; check the usual spot.
if [ -z "$BROWSER" ] && [ "$SYSTEM" = macos ] \
   && [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    BROWSER="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fi

if [ -n "$BROWSER" ]; then
    ok "browser found ($BROWSER)"
else
    bad "no Chromium-based browser found - this is the one real requirement"
    case "$SYSTEM" in
        macos)  ask "brew install --cask google-chrome" ;;
        linux)
            if   command -v apt    >/dev/null 2>&1; then ask "sudo apt install -y chromium || sudo apt install -y chromium-browser"
            elif command -v dnf    >/dev/null 2>&1; then ask "sudo dnf install -y chromium"
            elif command -v pacman >/dev/null 2>&1; then ask "sudo pacman -S --noconfirm chromium"
            else say "Install Google Chrome or Chromium, then run this again."
            fi ;;
    esac
    for candidate in google-chrome chromium chromium-browser; do
        command -v "$candidate" >/dev/null 2>&1 && BROWSER="$candidate" && break
    done
    [ -z "$BROWSER" ] && warn "still no browser - browser-ai will have nothing to drive yet."
fi

# --- browser-ai --------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
    say ""
    say "--check: nothing was installed."
    exit 0
fi

say ""
say "Installing browser-ai..."
if "$PY" -m pip install --user "$HERE" ; then
    ok "installed"
    RUN="web-ai"
    command -v web-ai >/dev/null 2>&1 || RUN="$PY -m webai"
else
    warn "pip install failed - you can still use it from this folder with: $PY -m webai"
    RUN="$PY -m webai"
fi

# --- offer to register with Codex -------------------------------------------
CODEX_CFG="$HOME/.codex/config.toml"
if [ -d "$HOME/.codex" ] && ! grep -q "mcp_servers.browser-ai" "$CODEX_CFG" 2>/dev/null; then
    say ""
    say "Codex detected but browser-ai is not registered. Add it to $CODEX_CFG?"
    if ask "cat >> '$CODEX_CFG' <<'TOML'

[mcp_servers.browser-ai]
command = \"$PY\"
args = [\"-m\", \"webai\", \"mcp\"]
env = { PYTHONPATH = \"$HERE\" }
TOML"; then
        ok "registered with Codex (restart Codex to pick it up)"
    fi
fi

# --- how to start the browser -----------------------------------------------
say ""
say "To let the agent drive the browser, start it with a debug port and a"
say "dedicated profile (keeps automation out of your everyday browser):"
say ""
say "    ${BROWSER:-google-chrome} --remote-debugging-port=$PORT \\"
say "        --user-data-dir=\"$PROFILE\""
say ""

# --- doctor ------------------------------------------------------------------
command -v web-ai >/dev/null 2>&1 && web-ai doctor || "$PY" -m webai doctor
