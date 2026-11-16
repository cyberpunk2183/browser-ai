# browser-ai installer for Windows (PowerShell 5+).
#
#   .\install.ps1            install what is missing, then browser-ai
#   .\install.ps1 -Check     only report what is missing, change nothing
#
# If Windows blocks the script, run PowerShell once with:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

param([switch]$Check)

function Say($m)  { Write-Host $m }
function Ok($m)   { Write-Host "[ok  ] $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "[warn] $m"   -ForegroundColor Yellow }
function Bad($m)  { Write-Host "[FAIL] $m"   -ForegroundColor Red }

function Invoke-Step($command) {
    Say ""
    Say "    $command"
    Say ""
    if ($Check) { Warn "skipped (-Check). Run the command above yourself."; return $false }
    $reply = Read-Host "Run it now? [y/N]"
    if ($reply -match '^[yY]') { Invoke-Expression $command; return $true }
    Warn "skipped. Run the command above when you are ready."
    return $false
}

$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port    = if ($env:WEBAI_PORT) { $env:WEBAI_PORT } else { "9222" }
$Profile = if ($env:WEBAI_PROFILE) { $env:WEBAI_PROFILE } else { "$env:USERPROFILE\.browser-ai-profile" }

Say "browser-ai installer - Windows"
Say ""

# --- Python ------------------------------------------------------------------
$py = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    $exe, $flag = $candidate.Split(" ", 2)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $version = & $exe $flag -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.9") { $py = $candidate; break }
    }
}

if ($py) {
    Ok "Python $version ($py)"
} else {
    Bad "Python 3.9+ not found"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Invoke-Step "winget install -e --id Python.Python.3.12" | Out-Null
    } else {
        Say "Install Python from https://www.python.org/downloads/ and tick"
        Say "'Add python.exe to PATH' during setup, then run this again."
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
    if (-not $py) { Bad "still no Python - stopping here."; exit 1 }
}

# --- a Chromium browser (the one real requirement) ---------------------------
# browser-ai drives whatever Chromium-based browser you start with a debug port.
$browser = $null
$candidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
)
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $browser = $c; break } }
if (-not $browser) {
    foreach ($name in @("chrome", "msedge", "brave")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $browser = $cmd.Source; break }
    }
}

if ($browser) {
    Ok "browser found ($browser)"
} else {
    Bad "no Chromium-based browser found - this is the one real requirement"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Invoke-Step "winget install -e --id Google.Chrome" | Out-Null
    } else {
        Say "Install Google Chrome from https://www.google.com/chrome/ , then run this again."
    }
}

# --- browser-ai --------------------------------------------------------------
if ($Check) { Say ""; Say "-Check: nothing was installed."; exit 0 }

Say ""
Say "Installing browser-ai..."
$exe, $flag = $py.Split(" ", 2)
& $exe $flag -m pip install --user "$Here"
if ($LASTEXITCODE -eq 0) { Ok "installed" } else { Warn "pip install failed - you can still run it from this folder with: $py -m webai" }

# --- offer to register with Codex -------------------------------------------
$codexCfg = "$env:USERPROFILE\.codex\config.toml"
if ((Test-Path "$env:USERPROFILE\.codex") -and
    -not (Select-String -Path $codexCfg -Pattern "mcp_servers.browser-ai" -Quiet -ErrorAction SilentlyContinue)) {
    Say ""
    Say "Codex detected but browser-ai is not registered. Add it to $codexCfg?"
    $reply = Read-Host "Add it now? [y/N]"
    if ($reply -match '^[yY]') {
        $herePath = $Here -replace '\\', '\\'
        $block = @"

[mcp_servers.browser-ai]
command = "$exe"
args = ["-m", "webai", "mcp"]
env = { PYTHONPATH = "$herePath" }
"@
        Add-Content -Path $codexCfg -Value $block
        Ok "registered with Codex (restart Codex to pick it up)"
    }
}

# --- how to start the browser -----------------------------------------------
Say ""
Say "To let the agent drive the browser, start it with a debug port and a"
Say "dedicated profile (keeps automation out of your everyday browser):"
Say ""
Say "    `"$browser`" --remote-debugging-port=$Port --user-data-dir=`"$Profile`""
Say ""

# --- doctor ------------------------------------------------------------------
& $exe $flag -m webai doctor
