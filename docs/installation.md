# Installation

## Requirements

- **Windows 10/11** (primary target) or **Linux**. Every tool works on Linux: deletes
  go to the freedesktop.org Trash, `run_shell` replaces PowerShell (which remains
  available if `pwsh` is installed), and `list_processes` reads `/proc`. On macOS the
  core works, but deletion requires `permanent=true`.
- **Python 3.11+**
- **Ollama** with at least one model
- Windows Terminal recommended; classic PowerShell and cmd work

## Linux

```bash
git clone <repo> localai && cd localai
./install.sh
```

`install.sh` mirrors `install.ps1`: it checks for Python 3.11+ and Ollama, creates an
isolated virtualenv in the folder, installs localai, writes the `ai` and `localai`
launchers, and finishes by running `localai doctor`. It needs no root and installs
nothing system-wide. It is POSIX `sh`, so it runs under bash, dash and busybox.

```bash
./install.sh --with-extractors   # + PDF/Word/Excel/PowerPoint/RTF/HTML (~40 MB)
./install.sh --portable          # state beside the install, for a USB stick
./install.sh --no-path-update    # do not create launchers in ~/.local/bin
./install.sh --yes               # unattended: accept every default
```

**PATH.** Rather than editing your shell startup files, it offers to put two small
wrapper scripts in `~/.local/bin`, the per-user directory most distributions already
have on `PATH`. If it is not on yours, the installer prints the one line to add. It
never replaces an existing `~/.local/bin/ai` that it did not create; `ai` is a common
command name, so in that case use `localai`.

**Debian and Ubuntu** need the `python3-venv` package; the installer checks for it
and names the package if it is missing.

For development, use `python3 tasks.py setup` instead, which also installs the test
and lint tools.

Configuration and data live in `$LOCALAI_HOME`, else `$XDG_DATA_HOME/localai`
(normally `~/.local/share/localai`). Deleted files go to `~/.local/share/Trash`, where
your file manager or `gio trash --list` can restore them.

## Quick install — the installer

Download `ai-setup-<version>.exe` from the release and run it. Standard Windows
wizard; installs per-user, so there is no UAC prompt and no admin rights needed.

Tick **"Add ai to my PATH"** (default) and you can type `ai` in any new terminal.

Python is **not** required — the installer bundles everything. You still need Ollama
and at least one model.

## Install from source

```powershell
git clone <repo> localai
cd localai
.\install.ps1
```

The installer checks Python and Ollama, creates an isolated virtualenv, installs the
package, writes a launcher, optionally adds it to your PATH, and finishes by running
`localai doctor` so you know immediately whether it worked.

If PowerShell blocks the script:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### Options

```powershell
.\install.ps1 -WithExtractors    # + PDF/Word/Excel/PowerPoint/RTF/HTML (~40 MB)
.\install.ps1 -Portable          # state beside the install, for a USB stick
.\install.ps1 -NoPathUpdate      # do not offer to modify PATH
```

## Manual install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m localai doctor
```

## Prerequisites

### Python

```powershell
winget install Python.Python.3.12
```

or python.org. Verify with `py -3 --version`.

### Ollama

```powershell
winget install Ollama.Ollama
```

or ollama.com/download. Then pull a model:

```powershell
ollama pull qwen3:8b       # 5 GB, tools + thinking. Good default.
ollama pull qwen2.5:7b     # 4.4 GB, tools
ollama pull llama3.2:3b    # 2 GB, small and fast
```

For the agentic features you want a model with the `tools` capability.
`localai models list` shows which of yours have it, and `localai providers scan`
(see below) reports what is installed and how well it is supported.

Confirm Ollama is running:

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

## Verify

```powershell
localai doctor
```

Checks Python, SQLite and FTS5, Ollama and its version, installed models, database
integrity and migrations, configuration, permissions, PowerShell, Git, disk space,
optional extractors, privacy posture and active environment overrides. Every non-`ok`
line carries a remediation.

Exit 0 means nothing failed; warnings alone do not fail it.

## Where things go

```
%LOCALAPPDATA%\localai\
  config.toml              your settings
  data\localai.db          conversations, usage, audit, rules
  logs\audit.jsonl         append-only audit trail
  logs\localai.log         application log
  backups\                 pre-modification file copies
  profiles\                saved model profiles
```

`localai config path` prints the exact locations. Portable mode puts all of it in
`localai-data` beside the install instead.

## First run

Open a **new** terminal (a PATH change does not reach shells that are already open),
then:

```powershell
ai
```

`ai` is the short name; `localai` is the same program. Both are created by the
installer in `bin\` and resolve the virtualenv relative to their own location, so you
can move the folder without reinstalling.

If `ai` is not found:

```powershell
$env:Path -split ';' | Select-String localai   # did PATH pick it up?
C:\Users\you\localai\bin\ai.cmd                # or run it directly
```

Press **F1** for help. Permission mode starts at `auto`: reads run freely, changes ask
first.

A good first move is to define a trusted workspace so the model can work freely in one
place:

```
/workspace add D:\Projects\something
/mode workspace
```

## Upgrading

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -e .
localai migrations apply
localai doctor
```

Migrations also run automatically at startup. Your conversations and settings are
preserved.

## Uninstall

```powershell
.\uninstall.ps1
```

On Linux:

```bash
./uninstall.sh                   # program only; your data is kept
./uninstall.sh --remove-data     # also data, after you type DELETE
```

`uninstall.sh` removes the virtualenv and only the `~/.local/bin` launchers that
`install.sh` created (each carries a marker line). Items localai moved to your Trash
stay there.

Removes the virtualenv, launcher and PATH entry. **Your data is kept** unless you pass
`-RemoveData`, and even then it tells you how many conversations you are about to lose
and requires you to type `DELETE`.

**Ollama and your models are never touched.**

## Troubleshooting

See `troubleshooting.md`.
