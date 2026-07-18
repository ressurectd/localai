<#
.SYNOPSIS
    Uninstall localai.

.DESCRIPTION
    Removes the virtual environment, the launcher and the PATH entry.

    Your data is NOT removed unless you explicitly ask. Conversations, usage history,
    audit logs, backups and configuration are yours; this script will tell you exactly
    where they are and what they contain, then ask.

    Ollama and your downloaded models are never touched by this script.

.PARAMETER RemoveData
    Also delete conversations, usage history, audit logs and configuration.
    You will still be asked to confirm.

.PARAMETER Yes
    Skip confirmation prompts. Combined with -RemoveData this deletes data without
    asking, so use it only in automation you control.

.EXAMPLE
    .\uninstall.ps1

.EXAMPLE
    .\uninstall.ps1 -RemoveData
#>
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv'
$binDir = Join-Path $root 'bin'

function Write-Step { param($Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param($Message) Write-Host "  [ok] $Message" -ForegroundColor Green }
function Write-Warn { param($Message) Write-Host "  [!]  $Message" -ForegroundColor Yellow }

function Get-DataDirectory {
    if ($env:LOCALAI_HOME) { return $env:LOCALAI_HOME }
    $portable = Join-Path $root 'localai-data'
    if (Test-Path $portable) { return $portable }
    return Join-Path $env:LOCALAPPDATA 'localai'
}

Write-Host 'localai uninstaller' -ForegroundColor Cyan
Write-Host '-------------------'

$dataDir = Get-DataDirectory

# --- Report what exists before removing anything ----------------------------
Write-Step 'What will be removed'

Write-Host '  Program files:'
foreach ($path in @($venv, $binDir)) {
    if (Test-Path $path) { Write-Host "    $path" } else { Write-Host "    $path (not present)" -ForegroundColor DarkGray }
}

Write-Host "`n  Your data:  $dataDir"
if (Test-Path $dataDir) {
    $db = Join-Path $dataDir 'data\localai.db'
    $conversationCount = 'unknown'
    if (Test-Path $db) {
        $venvPython = Join-Path $venv 'Scripts\python.exe'
        if (Test-Path $venvPython) {
            try {
                $conversationCount = & $venvPython -c @"
import sqlite3,sys
try:
    c=sqlite3.connect(r'$db')
    print(c.execute('SELECT COUNT(*) FROM conversations').fetchone()[0])
except Exception:
    print('unknown')
"@ 2>$null
            } catch { $conversationCount = 'unknown' }
        }
    }
    $sizeMb = [math]::Round(((Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host "    $conversationCount saved conversation(s), $sizeMb MB total"
    Write-Host '    includes: configuration, usage history, audit log, file backups'
} else {
    Write-Host '    (no data directory found)' -ForegroundColor DarkGray
}

Write-Host "`n  NOT touched by this script:" -ForegroundColor Green
Write-Host '    Ollama itself'
Write-Host '    Your downloaded models (ollama list)'
Write-Host '    Any file localai read or wrote in your own folders'

# --- Confirm ----------------------------------------------------------------
if (-not $Yes) {
    Write-Host ''
    $answer = Read-Host 'Remove the localai program files? [y/N]'
    if ($answer -notmatch '^[Yy]') {
        Write-Host 'Cancelled. Nothing was changed.' -ForegroundColor Yellow
        exit 0
    }
}

# --- Remove program files ---------------------------------------------------
Write-Step 'Removing program files'

foreach ($path in @($venv, $binDir)) {
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
        Write-Ok "Removed $path"
    }
}

foreach ($cache in @('.pytest_cache', '.mypy_cache', '.ruff_cache', 'build', 'dist')) {
    $path = Join-Path $root $cache
    if (Test-Path $path) { Remove-Item -Recurse -Force $path; Write-Ok "Removed $cache" }
}

# --- PATH -------------------------------------------------------------------
Write-Step 'Cleaning PATH'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -and $userPath.Split(';') -contains $binDir) {
    $cleaned = ($userPath.Split(';') | Where-Object { $_ -ne $binDir }) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $cleaned, 'User')
    Write-Ok "Removed the launcher directory from your user PATH ('ai' and 'localai' will stop resolving)"
} else {
    Write-Ok 'Nothing to remove from PATH'
}

# --- Data -------------------------------------------------------------------
if (Test-Path $dataDir) {
    Write-Step 'Your data'
    $deleteData = $RemoveData
    if ($RemoveData -and -not $Yes) {
        Write-Warn 'This permanently deletes your conversations, usage history and audit log.'
        $confirm = Read-Host "Type DELETE to confirm removing $dataDir"
        $deleteData = ($confirm -ceq 'DELETE')
        if (-not $deleteData) { Write-Host '  Not confirmed; data kept.' -ForegroundColor Yellow }
    }

    if ($deleteData) {
        Remove-Item -Recurse -Force $dataDir
        Write-Ok "Deleted $dataDir"
    } else {
        Write-Host "  Kept: $dataDir" -ForegroundColor Green
        Write-Host '  Delete it yourself at any time, or re-run with -RemoveData.'
    }
}

Write-Host "`nUninstall complete." -ForegroundColor Green
Write-Host 'The source folder itself was left in place; delete it if you no longer want it.'
