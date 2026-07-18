<#
.SYNOPSIS
    Install localai: a local-first agentic terminal UI for Ollama models.

.DESCRIPTION
    Checks prerequisites, creates an isolated virtual environment, installs the
    package, and verifies the result by running the built-in diagnostics.

    Nothing is installed system-wide and no service is registered. Everything lives
    in this folder plus a state directory under %LOCALAPPDATA%.

.PARAMETER Portable
    Keep all state next to the installation rather than under %LOCALAPPDATA%.
    Suitable for a USB stick.

.PARAMETER WithExtractors
    Also install the optional document extractors (PDF, Word, Excel, PowerPoint,
    RTF, HTML). Adds roughly 40 MB.

.PARAMETER NoPathUpdate
    Do not offer to add the launcher directory to your user PATH.

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -WithExtractors -Portable
#>
[CmdletBinding()]
param(
    [switch]$Portable,
    [switch]$WithExtractors,
    [switch]$NoPathUpdate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

function Write-Step { param($Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param($Message) Write-Host "  [ok] $Message" -ForegroundColor Green }
function Write-Warn { param($Message) Write-Host "  [!]  $Message" -ForegroundColor Yellow }
function Write-Err  { param($Message) Write-Host "  [x]  $Message" -ForegroundColor Red }

Write-Host @"
+------------------------------------------------------------+
|  localai - local-first AI terminal for Ollama               |
|  No cloud account. No telemetry. Your files stay here.      |
+------------------------------------------------------------+
"@ -ForegroundColor Cyan

# --- 1. Python --------------------------------------------------------------
Write-Step 'Checking Python'

# Prefer the py launcher: it finds a suitable interpreter even when several are
# installed and PATH points at the Microsoft Store stub.
$python = $null
foreach ($candidate in @('py -3', 'python', 'python3')) {
    $parts = $candidate.Split(' ')
    $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    try {
        $versionText = & $parts[0] $parts[1..$parts.Length] --version 2>&1
        if ($versionText -match 'Python (\d+)\.(\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 11) {
                $python = $candidate
                Write-Ok "$versionText  ($($exe.Source))"
                break
            } else {
                Write-Warn "$versionText is too old (need 3.11+)"
            }
        }
    } catch { continue }
}

if (-not $python) {
    Write-Err 'Python 3.11 or newer was not found.'
    Write-Host ''
    Write-Host '  Install it with one of:' -ForegroundColor Yellow
    Write-Host '    winget install Python.Python.3.12'
    Write-Host '    https://www.python.org/downloads/'
    Write-Host ''
    Write-Host '  Then re-run this script.' -ForegroundColor Yellow
    exit 1
}

# --- 2. Ollama --------------------------------------------------------------
Write-Step 'Checking Ollama'

$ollamaInstalled = $null -ne (Get-Command ollama -ErrorAction SilentlyContinue)
$ollamaRunning = $false
$modelCount = 0

try {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
    $ollamaRunning = $true
    $modelCount = @($tags.models).Count
    Write-Ok "Ollama is running with $modelCount model(s) installed"
    foreach ($model in $tags.models | Select-Object -First 5) {
        $sizeGb = [math]::Round($model.size / 1GB, 1)
        $caps = if ($model.capabilities) { ($model.capabilities -join ',') } else { 'chat' }
        Write-Host "       $($model.name)  ${sizeGb} GB  [$caps]"
    }
} catch {
    if ($ollamaInstalled) {
        Write-Warn 'Ollama is installed but not responding on 127.0.0.1:11434'
        Write-Host '       Start it with:  ollama serve' -ForegroundColor Yellow
        Write-Host '       (or launch the Ollama desktop app)' -ForegroundColor Yellow
    } else {
        Write-Warn 'Ollama was not found.'
        Write-Host '       Install it from https://ollama.com/download' -ForegroundColor Yellow
        Write-Host '       or:  winget install Ollama.Ollama' -ForegroundColor Yellow
    }
    Write-Host '       Installation will continue; localai will report this at startup.'
}

if ($ollamaRunning -and $modelCount -eq 0) {
    Write-Warn 'No models installed. Pull one with:  ollama pull qwen3:8b'
}

# --- 3. Virtual environment -------------------------------------------------
Write-Step 'Creating the virtual environment'

if (Test-Path $venvPython) {
    Write-Ok 'Virtual environment already exists; reusing it'
} else {
    $parts = $python.Split(' ')
    & $parts[0] $parts[1..$parts.Length] -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Err 'Failed to create the virtual environment'; exit 1 }
    Write-Ok "Created $venv"
}

# --- 4. Install -------------------------------------------------------------
Write-Step 'Installing localai and its dependencies'

& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Write-Warn 'Could not upgrade pip; continuing' }

$target = if ($WithExtractors) { '.[extract]' } else { '.' }
Push-Location $root
try {
    & $venvPython -m pip install -e $target --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err 'Installation failed'; exit 1 }
} finally {
    Pop-Location
}
Write-Ok "Installed localai$(if ($WithExtractors) { ' with document extractors' })"

# --- 5. Launcher ------------------------------------------------------------
Write-Step 'Creating the launcher'

$binDir = Join-Path $root 'bin'
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

# Two names for the same thing: `ai` because it is what you actually want to type,
# `localai` because every document and script references it. Both resolve the venv
# relative to their own location, so the folder can be moved without reinstalling.
$portableSet = if ($Portable) { "set LOCALAI_PORTABLE=1" } else { "" }
$cmdLauncher = @"
@echo off
REM localai launcher - generated by install.ps1
setlocal
$portableSet
set "LOCALAI_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%LOCALAI_PY%" (
  echo localai: virtualenv not found at "%LOCALAI_PY%" 1>&2
  echo Re-run install.ps1 from the localai folder. 1>&2
  exit /b 78
)
"%LOCALAI_PY%" -m localai %*
"@

$shLauncher = @"
#!/bin/sh
# localai launcher for Git Bash / MSYS - generated by install.ps1
$(if ($Portable) { "export LOCALAI_PORTABLE=1" })
here="`$(cd "`$(dirname "`$0")/.." && pwd)"
py="`$here/.venv/Scripts/python.exe"
[ -x "`$py" ] || py="`$here/.venv/bin/python"
if [ ! -x "`$py" ]; then
  echo "localai: virtualenv not found under `$here/.venv" >&2
  exit 78
fi
exec "`$py" -m localai "`$@"
"@

foreach ($name in @('ai', 'localai')) {
    Set-Content -Path (Join-Path $binDir "$name.cmd") -Value $cmdLauncher -Encoding ASCII
    # No extension: Git Bash does not consult PATHEXT.
    Set-Content -Path (Join-Path $binDir $name) -Value ($shLauncher -replace "`r`n", "`n") -Encoding UTF8 -NoNewline
}
Write-Ok "Launchers 'ai' and 'localai' written to $binDir"

if ($Portable) {
    Write-Ok 'Portable mode: state will live in localai-data beside this folder'
}

# --- 6. PATH ----------------------------------------------------------------
if (-not $NoPathUpdate) {
    Write-Step 'Adding the launcher to your PATH'
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -and $userPath.Split(';') -contains $binDir) {
        Write-Ok 'Already on PATH'
    } else {
        Write-Host "  This lets you type 'ai' from any terminal." -ForegroundColor Cyan
        $answer = Read-Host "  Add '$binDir' to your user PATH? [Y/n]"
        if ($answer -eq '' -or $answer -match '^[Yy]') {
            # User scope only: never modify the machine PATH, which needs elevation
            # and affects other accounts.
            [Environment]::SetEnvironmentVariable('Path', "$userPath;$binDir", 'User')
            $env:Path = "$env:Path;$binDir"
            Write-Ok 'Added to PATH. Open a new terminal for it to take effect everywhere.'
        } else {
            Write-Host "  Skipped. Run it directly: $binDiri.cmd" -ForegroundColor Yellow
        }
    }
}

# --- 7. Verify --------------------------------------------------------------
Write-Step 'Verifying the installation'

& $venvPython -m localai config init | Out-Null
& $venvPython -m localai doctor
$doctorExit = $LASTEXITCODE

Write-Host ''
if ($doctorExit -eq 0) {
    Write-Host 'Installation complete.' -ForegroundColor Green
} else {
    Write-Host 'Installed, but the diagnostics reported a problem (see above).' -ForegroundColor Yellow
    Write-Host 'localai will still start; fix the reported items when convenient.' -ForegroundColor Yellow
}

Write-Host @"

Next steps  (open a NEW terminal first so PATH takes effect)
  ai                          launch the terminal interface
  ai doctor                   re-run diagnostics
  ai providers scan           see your models and how well each is supported
  ai --help                   every command

  'localai' works too - same program, longer name.

First run
  Press F1 for help, Ctrl+P for commands, Ctrl+M to pick a model.
  Permission mode starts at 'auto': reads run freely, changes ask first.

Docs
  README.md, docs/user-guide.md, docs/permissions-guide.md

Uninstall
  .\uninstall.ps1             (keeps your conversations unless you say otherwise)
"@ -ForegroundColor Cyan

exit $doctorExit
