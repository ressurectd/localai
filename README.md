<div align="center">

```
        ██████   ██
       ██    ██  ▀▀
       ████████  ██
       ██    ██  ██
```

### local models · your files · your machine

An agentic terminal for the models you already have in [Ollama](https://ollama.com).
It gives a local model controlled access to your filesystem and shell, behind a
permissions engine modelled on coding agents like Claude Code — except nothing ever
leaves your computer.

[![License: MIT](https://img.shields.io/badge/License-MIT-black.svg?style=flat-square)](LICENSE.txt)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4.svg?style=flat-square&logo=windows&logoColor=white)](docs/installation.md)
[![CI](https://img.shields.io/github/actions/workflow/status/OWNER/REPO/ci.yml?branch=main&style=flat-square&label=CI)](../../actions)

[![Tests](https://img.shields.io/badge/tests-408%20passing-2dd4a7.svg?style=flat-square)](docs/testing.md)
[![Security boundaries](https://img.shields.io/badge/security%20boundaries-137%20tests-c74440.svg?style=flat-square)](docs/security-model.md)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64.svg?style=flat-square&logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![mypy](https://img.shields.io/badge/types-mypy%20strict-1f5a2f.svg?style=flat-square)](pyproject.toml)
[![Textual](https://img.shields.io/badge/TUI-Textual-b57bff.svg?style=flat-square)](https://textual.textualize.io/)

[![No telemetry](https://img.shields.io/badge/telemetry-none-2dd4a7.svg?style=flat-square)](docs/privacy.md)
[![Loopback only](https://img.shields.io/badge/network-loopback%20only-2dd4a7.svg?style=flat-square)](docs/privacy.md)
[![No account](https://img.shields.io/badge/account-not%20required-2dd4a7.svg?style=flat-square)](docs/privacy.md)

[Install](#install) · [Quick tour](#quick-tour) · [Permissions](#permission-modes) ·
[Safety](#safety) · [Privacy](#privacy) · [For coding agents](#for-ai-coding-agents) ·
[Docs](#documentation)

</div>

---

```
 ◆ qwen3:8b   ▸ D:\Work\project   ████░░░░░░░░ ~4,210/40,960

 ▎ summarise the notes in this folder

 ⏵ list D:\Work\project matching *.md
   ✓ 7 entries
 ⏵ read D:\Work\project\notes.md
   ✓ 84 lines

 ◆ The notes cover three areas: the budget revision, the September
   deadline, and two open questions about the vendor contract.

 ▸ thought for 3.4s

 ◐ auto   19 tools · 2 calls · 43 tok/s   /help
```

## Why

Cloud coding agents are excellent, and sending your files to one is a choice you
should get to make deliberately.

Ollama already runs capable models locally, but talking to them means a bare prompt
with no access to anything. The gap is not the model — it is everything around it:
tools, a permissions layer you can trust, a record of what happened, and an interface
you do not mind living in.

That is what this is.

## What it does

**Talks to your installed models.** Detects them automatically, switches without
restarting, and shows what each one can actually do — `ai providers scan` reports
which models support native tool calling and which fall back to a text protocol.

**Gives the model real tools.** Nineteen of them: read and search files, inspect
metadata, hash, find duplicates, write, edit, move, delete, run PowerShell and Python,
inspect the system and Git.

**Asks before it acts.** Four permission modes, from confirming everything to full
autonomy. Even at full autonomy every action is displayed and written to an
append-only audit log.

**Is reversible.** Deletes go to the Recycle Bin. Edits are backed up first and shown
as a diff. Dry-run simulates without touching anything.

**Tells the truth about tokens.** Counts are labelled `reported` or `estimated` and
never conflated. Thinking tokens are always marked estimates, because Ollama counts
them inside completion tokens and does not report them separately.

**Works for scripts and coding agents too.** A full non-interactive CLI with `--json`
on everything, meaningful exit codes and versioned output schemas.

## Install

Download `ai-setup-<version>.exe` from [Releases](../../releases) and run it. Standard
Windows wizard, per-user install, no admin rights.

**Python is not required** — the installer bundles everything. You do need Ollama and
at least one model:

```powershell
winget install Ollama.Ollama
ollama pull qwen3:8b
```

Then open a new terminal:

```powershell
ai
```

<details>
<summary><b>Install from source instead</b></summary>

<br>

```powershell
git clone <repo> localai
cd localai
.\install.ps1
```

Needs Python 3.11+. Creates an isolated virtualenv, writes `ai` and `localai`
launchers, optionally adds them to your PATH, and finishes by running diagnostics.
Full guide: [docs/installation.md](docs/installation.md).

</details>

<details>
<summary><b>The executable is unsigned</b></summary>

<br>

SmartScreen will show *"Windows protected your PC"* on first run — choose
**More info → Run anyway**. That is normal for unsigned software and does not indicate
anything is wrong. Verify the download against `SHA256SUMS.txt` if you would rather
check.

Signing requires a code-signing certificate (roughly £200–400/year). Worth it for wide
distribution; not for a handful of users.

</details>

## Quick tour

```powershell
ai                       # the terminal interface
ai doctor                # check the environment; every problem comes with a fix
ai providers scan        # what is installed, and how well each model can be driven
ai models list           # installed models with capabilities and memory
ai chat --model qwen3:8b --prompt "what is in this folder?" --tools
ai usage report --period week
```

### Type `/` for anything

A menu rises above the prompt and filters as you type. `↑↓` to choose, `Tab` to
complete, `Enter` to run.

```
 ❯ /model     model picker
   /models    model picker
   /mode      permission mode picker
   ↑↓ choose · Tab complete · Enter run · Esc dismiss
```

Twenty-eight commands. `/help` lists them all with every keyboard shortcut.

### Watch it think

```
 ◐ Qwen is thinking  ▃▄▅▆▇█▇▆▅▄▃▂▁▂  3.4s
   considering whether the budget figure appears twice...
```

Three things at a glance: that it is working, how long it has been, and roughly what
about. The caption shows the last complete *phrase* of the reasoning rather than a
stream of half-words — updating on every token strobes, and is harder to read than
nothing. When the answer starts, it folds away into `▸ thought for 3.4s`.

### Know which model you are talking to

Each family has its own colour and mark. Switch to DeepSeek and the interface turns
abyssal blue; switch to Qwen and it goes jade. The top bar, the prompt border and the
model's own messages all move together.

```
   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄       ░▒▓████████▓▒░          ▄██████▄
   ██████████████          ▄██████▄           ▄██████████▄
   ███  ████  ███        ▄████████████▄       ████████████
   ███  ████  ███        ▀████████████▀        ▀████████▀
   ██████████████       ░▒▓████████▓▒░           ▀████▀
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀
        Qwen                 DeepSeek               Gemma
```

That is not only decoration — a 27B model with a 262k context behaves very differently
from a 3B one, and the permissions you are comfortable granting may differ too. Colour
carries that faster than reading a name does.

Fourteen themes via `/theme`, including `tokyo-night`, `gruvbox`, `catppuccin-mocha`,
plus **synthwave** and **matrix**.

## Permission modes

| Mode | Behaviour |
|:--|:--|
| `manual` | Confirms every action, including reading a file. |
| **`auto`** | **Default.** Reads run freely; changes and commands ask first. |
| `workspace` | Free rein inside directories you have trusted; asks everywhere else. |
| `bypass` | No prompts. Everything still shown and logged. |

Bypass cannot be reached by a keystroke — it requires typing a confirmation phrase
into a dialog that spells out the consequences. And even in bypass:

- every action is displayed and written to an append-only audit log
- `ai`'s own config, database and audit log stay unwritable by any tool
- `Ctrl+Q` stops everything instantly and engages a global kill switch
- deletions still go to the Recycle Bin

Every interface — TUI, CLI, and later the local API and MCP server — goes through one
`PermissionEngine.evaluate` call. There is no second path, and a test asserts all four
produce identical verdicts for identical requests.

You can ask it why it decided something:

```powershell
ai permissions explain --action request.json --json
```

→ [docs/permissions-engine.md](docs/permissions-engine.md)

## Safety

Anything a model reads is untrusted input. If a document tries to instruct it —
*"ignore previous instructions"*, a hidden Unicode payload, a fake system prompt — the
content is fenced in an explicit envelope, flagged in the interface, and recorded in
the audit log.

```
 ☣ This content tried to instruct the model (injection:instruction_override).
   It is marked as untrusted data.
```

That detection is a heuristic and is documented as one. **The real guarantee is the
permissions engine:** a successful injection still cannot make a consequential change
without your approval.

Paths are contained on their **resolved real path**, so `..` traversal and Windows
junctions cannot smuggle an operation out of a trusted workspace. There is a test that
creates a real junction pointing outside the workspace and asserts the escape is
caught.

→ [docs/security-model.md](docs/security-model.md), including what is **not** defended
against.

## Privacy

The only network traffic by default is to `127.0.0.1:11434` — your own Ollama daemon.

- No account, no telemetry, no analytics, no crash reporting, no update check.
- `privacy.telemetry` is typed `Literal[False]`, so it **cannot be enabled** even by
  editing the config file.
- The HTTP client sets `trust_env=False`, so a system proxy cannot silently route
  local traffic off-device.
- `/context` shows exactly what is sent to the model. No hidden memory, no invisible
  preamble.

→ [docs/privacy.md](docs/privacy.md)

## For AI coding agents

This repository is built to be worked on by Claude Code, Codex, Cursor and similar.

**Start with [AGENTS.md](AGENTS.md).** Then:

```bash
ai capabilities --json     # what is built vs. planned — honest, not aspirational
ai architecture --json     # layers, dependencies, invariants
python tasks.py check      # the one gate: format, lint, typecheck, test, docs
```

`capabilities --json` reports unbuilt features as `false` rather than omitting them,
so you can trust it when deciding whether something needs building. You should never
need to scrape the terminal UI or guess how to build anything.

## Documentation

| | |
|:--|:--|
| [AGENTS.md](AGENTS.md) | Canonical guide for coding agents; fastest human orientation |
| [docs/user-guide.md](docs/user-guide.md) | Using the interface |
| [docs/installation.md](docs/installation.md) | Install, upgrade, uninstall |
| [docs/permissions-engine.md](docs/permissions-engine.md) | How access is decided |
| [docs/security-model.md](docs/security-model.md) | Threat model, and what is *not* defended |
| [docs/privacy.md](docs/privacy.md) | What is stored, what never leaves |
| [docs/architecture.md](docs/architecture.md) | Design, and why |
| [docs/tool-api.md](docs/tool-api.md) | Writing a tool |
| [docs/development.md](docs/development.md) | Working on `ai` |
| [docs/testing.md](docs/testing.md) | Test strategy |
| [docs/database-schema.md](docs/database-schema.md) | Schema and migrations |
| [docs/training.md](docs/training.md) | Prompting vs. memory vs. RAG vs. fine-tuning |
| [docs/release-process.md](docs/release-process.md) | Versioning and building a release |
| [docs/troubleshooting.md](docs/troubleshooting.md) | When something breaks |

## Status

**Phase 1 is complete and tested.** 408 tests, 137 of them security boundaries.

<table>
<tr>
<td valign="top" width="50%">

**Built**

- Model detection, switching, provider discovery
- Streaming chat with live reasoning display
- Terminal UI: command menu, pickers, 14 themes
- 19 tools behind one execution path
- Permissions engine, 4 modes, audit log
- Conversation storage, fork, export, search
- Usage tracking with honest provenance
- Non-interactive CLI with versioned JSON
- Installer and standalone executable

</td>
<td valign="top" width="50%">

**Not built yet**

- Document extraction (PDF, Word, Excel…)
- Semantic indexing / RAG
- Investigation mode for old drives
- Persistent memory
- Modelfile management
- Local HTTP API
- MCP server
- LoRA / QLoRA fine-tuning

</td>
</tr>
</table>

Everything in the right-hand column is reported as `false` by
`ai capabilities --json`. Nothing claims to work that has not been run.

See [.project/status.json](.project/status.json) for the machine-readable version, and
[HANDOFF.md](HANDOFF.md) to pick up where this left off.

### Known limitations

- `/compact` truncates deterministically rather than summarising with the model.
  Deliberate for now: a mechanical drop is predictable, whereas a generated summary
  can quietly invent things you said.
- `list_processes` and Recycle Bin deletion are Windows-only.
- Prompt-injection detection is heuristic; the guarantee is the permissions engine.

## Contributing

```powershell
python tasks.py setup
python tasks.py check
```

Read [CONTRIBUTING.md](CONTRIBUTING.md). If you touch anything listed in
[CODEOWNERS.md](CODEOWNERS.md), run `python tasks.py test-security` — and if a
boundary test fails, **the test is right**.

## Built with

[Textual](https://textual.textualize.io/) · [httpx](https://www.python-httpx.org/) ·
[pydantic](https://docs.pydantic.dev/) · [Rich](https://rich.readthedocs.io/) ·
[Ollama](https://ollama.com)

Five runtime dependencies, all widely used and actively maintained. Recycle Bin
deletion is forty lines of `ctypes` rather than a sixth — see the
[dependency policy](docs/security-model.md#dependency-policy).

<div align="center">
<br>

**[MIT](LICENSE.txt)** · Built for people who want the agent, not the cloud.

</div>
