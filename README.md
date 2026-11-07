```
        ██████   ██
       ██    ██  ▀▀
       ████████  ██
       ██    ██  ██
```
local models · your files · your machine

ai is a terminal interface for local Ollama models. It can read files, run commands and make changes on your machine, with permission checks around every tool call. No hosted model or account required.

License: MIT Python 3.11+ Windows CI

Tests Security boundaries Ruff mypy Textual

No telemetry Loopback only No account

Install · Quick tour · Permissions · Safety · Privacy · For coding agents · Docs

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

I use Ollama, but a chat prompt only goes so far when the work is sitting in files and terminals. I wanted to give local models access to that work without handing over the whole machine.

ai adds file and shell tools, permission checks, an audit log and a terminal interface. It talks to Ollama running on your machine.

## What it does

Installed Ollama models are detected automatically, and you can switch between them without restarting. ai providers scan checks whether they support native tool calling or need the text fallback.

The model gets 19 tools for reading, searching and modifying files; checking metadata and hashes; finding duplicates; running PowerShell or Python; and inspecting Git or system information.

Four permission modes cover anything from approving every read to running without prompts. Actions remain visible and are written to an append-only audit log.

Deletes go to the Recycle Bin. Edits get a backup and a diff, and dry-run simulates changes without making them.

Usage reports separate actual token counts from estimates. Ollama does not report thinking tokens separately, so those numbers are estimated.

There is also a non-interactive CLI with --json output, meaningful exit codes and versioned schemas for scripts or coding agents.

## Install

Download ai-setup-\<version\>.exe from Releases and run the installer. It installs per user and does not need administrator access.

The installer includes Python. You only need Ollama and a model:

```
winget install Ollama.Ollama
ollama pull qwen3:8b
```

Open a new terminal and run:

```
ai
```

Install from source instead · The executable is unsigned

## Quick tour

```
ai                       # the terminal interface
ai doctor                # check the environment; every problem comes with a fix
ai providers scan        # what is installed, and how well each model can be driven
ai models list           # installed models with capabilities and memory
ai chat --model qwen3:8b --prompt "what is in this folder?" --tools
ai usage report --period week
```

Type / for anything

The command menu opens above the prompt and filters as you type. Use ↑↓ to move, Tab to complete and Enter to run.

```
 ❯ /model     model picker
   /models    model picker
   /mode      permission mode picker
   ↑↓ choose · Tab complete · Enter run · Esc dismiss
```

There are 28 commands. /help shows the full list and keyboard shortcuts.

## Watch it think

```
 ◐ Qwen is thinking  ▃▄▅▆▇█▇▆▅▄▃▂▁▂  3.4s
   considering whether the budget figure appears twice...
```

While a model is reasoning, the interface shows elapsed time and a short caption. The caption updates a phrase at a time instead of flickering with every token. Once the answer starts, the indicator collapses to ▸ thought for 3.4s.

## Know which model you are talking to

Model families have their own colours and marks. DeepSeek uses blue, Qwen uses jade, and the top bar, prompt border and model messages change together.

```
   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄       ░▒▓████████▓▒░          ▄██████▄
   ██████████████          ▄██████▄           ▄██████████▄
   ███  ████  ███        ▄████████████▄       ████████████
   ███  ████  ███        ▀████████████▀        ▀████████▀
   ██████████████       ░▒▓████████▓▒░           ▀████▀
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀
        Qwen                 DeepSeek               Gemma
```

It also makes model switches harder to miss. A small model and a 27B model with a 262k context need different expectations, especially when tools are enabled.

/theme has 14 options, including tokyo-night, gruvbox, catppuccin-mocha, synthwave and matrix.

## Permission modes

| Mode | Behaviour |
|---|---|
| manual | Confirms every action, including reading a file. |
| auto | Default. Reads run freely; changes and commands ask first. |
| workspace | Free rein inside directories you have trusted; asks everywhere else. |
| bypass | No prompts. Everything still shown and logged. |

Bypass requires a typed confirmation phrase; there is no keyboard shortcut for it. It skips approval prompts, but some safeguards remain:

- every action is displayed and written to an append-only audit log
- ai's own config, database and audit log stay unwritable by any tool
- Ctrl+Q stops everything instantly and engages a global kill switch
- deletions still go to the Recycle Bin

Permission decisions live in PermissionEngine.evaluate. The TUI and CLI use that path, and the planned local API and MCP server are designed around it too. Tests check that the same request gets the same decision.

To inspect a permission decision:

```
ai permissions explain --action request.json --json
```

→ docs/permissions-engine.md

## Safety

Model input is treated as untrusted, including files read through tools. Suspected prompt injections — instructions in documents, hidden Unicode or fake system messages — are marked in the interface, wrapped as untrusted content and logged.

```
 ☣ This content tried to instruct the model (injection:instruction_override).
   It is marked as untrusted data.
```

The detector is heuristic; it can miss things. Permission checks are the main safeguard against unapproved changes.

Workspace boundaries are checked against resolved paths, not just the path the model supplied. That covers .. traversal and Windows junctions. A regression test creates a junction pointing outside the workspace and checks that access is denied.

→ docs/security-model.md, including what is not defended against.

## Privacy

By default, ai connects only to Ollama at 127.0.0.1:11434.

- No account, telemetry, analytics, crash reporting or update checks.
- privacy.telemetry is typed Literal[False]; changing the config cannot enable it.
- The HTTP client uses trust_env=False, so it does not pick up proxy settings from the environment.
- /context shows what goes into the model request. There is no hidden memory or preamble.

→ docs/privacy.md

## For AI coding agents

The repo is set up for development with Claude Code, Codex, Cursor and other coding agents.

Read AGENTS.md first. The useful entry points are:

```
ai capabilities --json     # what is built vs. planned — honest, not aspirational
ai architecture --json     # layers, dependencies, invariants
python tasks.py check      # the one gate: format, lint, typecheck, test, docs
```

Unbuilt features appear as false in ai capabilities --json instead of disappearing from the output. The architecture and capability commands are there so tools can inspect the project without scraping the UI.

## Documentation

| File | Contents |
|---|---|
| AGENTS.md | Canonical guide for coding agents; fastest human orientation |
| docs/user-guide.md | Using the interface |
| docs/installation.md | Install, upgrade, uninstall |
| docs/permissions-engine.md | How access is decided |
| docs/security-model.md | Threat model, and what is not defended |
| docs/privacy.md | What is stored, what never leaves |
| docs/architecture.md | Design, and why |
| docs/tool-api.md | Writing a tool |
| docs/development.md | Working on ai |
| docs/testing.md | Test strategy |
| docs/database-schema.md | Schema and migrations |
| docs/training.md | Prompting vs. memory vs. RAG vs. fine-tuning |
| docs/release-process.md | Versioning and building a release |
| docs/troubleshooting.md | When something breaks |

## Status

Phase 1 is complete: 408 tests, including 137 security-boundary tests.

### Built

- Model detection, switching, provider discovery
- Streaming chat with live reasoning display
- Terminal UI: command menu, pickers, 14 themes
- 19 tools behind one execution path
- Permissions engine, 4 modes, audit log
- Conversation storage, fork, export, search
- Usage tracking with honest provenance
- Non-interactive CLI with versioned JSON
- Installer and standalone executable

### Not built yet

- Document extraction (PDF, Word, Excel…)
- Semantic indexing / RAG
- Investigation mode for old drives
- Persistent memory
- Modelfile management
- Local HTTP API
- MCP server
- LoRA / QLoRA fine-tuning

Planned features are marked false in ai capabilities --json until they are implemented.

The machine-readable status is in .project/status.json. HANDOFF.md has the current development handoff.

## Known limitations

- /compact currently drops older context rather than generating a summary. It is predictable, but can discard useful information.
- list_processes and Recycle Bin deletion work on Windows only.
- Prompt-injection detection is heuristic. The permissions engine is the main security boundary.

## Contributing

```
python tasks.py setup
python tasks.py check
```

Read CONTRIBUTING.md before opening a PR. For changes covered by CODEOWNERS.md, run python tasks.py test-security. A failing boundary test needs investigation, not a workaround.

## Built with

Textual · httpx · pydantic · Rich · Ollama

Five runtime dependencies. Recycle Bin deletion uses about 40 lines of ctypes rather than adding another package; details are in the dependency policy.

---

MIT · Local models, local tools, local control.
