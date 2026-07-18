# localai

A local-first, agentic terminal application for the models you already have in
[Ollama](https://ollama.com).

It gives a local model controlled access to your filesystem and shell, behind a
permissions engine modelled on coding agents like Claude Code — but everything stays on
your machine. No account, no cloud, no telemetry.

```
qwen3:8b   auto   D:\Work\project        ######...... ~4,210/40,960

 > summarise the notes in this folder

 -> list D:\Work\project matching *.md
    ok  7 entries
 -> read D:\Work\project\notes.md
    ok  84 lines

 The notes cover three areas: the budget revision, the September deadline,
 and two open questions about the vendor contract.

 19 tools | 2 calls | 43 tok/s | local only | Enter send, Shift+Enter newline
```

## What it does

- **Talks to your installed Ollama models** — detects them automatically, switches
  without restarting, shows capabilities, context length and memory use.
- **Gives the model real tools** — 19 of them: read and search files, inspect metadata,
  hash, find duplicates, write, edit, move, delete, run PowerShell and Python, inspect
  the system and Git.
- **Asks before it acts.** Four permission modes, from confirming everything to full
  autonomy. Even at full autonomy every action is displayed and logged.
- **Is reversible.** Deletes go to the Recycle Bin. Edits are backed up first and shown
  as a diff. Dry-run simulates.
- **Tells the truth about tokens.** Counts are labelled `reported` or `estimated` and
  never conflated. Thinking tokens are always marked as estimates, because Ollama does
  not report them separately.
- **Works for scripts and coding agents too** — a full non-interactive CLI with `--json`
  on everything, meaningful exit codes and versioned output schemas.

## Install

```powershell
git clone <repo> localai
cd localai
.\install.ps1
```

Then open a **new** terminal and type:

```powershell
ai
```

`ai` and `localai` are the same program — `ai` because it is what you actually want to
type, `localai` because the docs and scripts reference it.

Needs Python 3.11+, Ollama, and at least one model (`ollama pull qwen3:8b`).
Full guide: [docs/installation.md](docs/installation.md).

## Try it

```powershell
localai                                  # the terminal interface
localai doctor                           # check the environment
localai models list                      # what you have installed
localai providers scan                   # what is installed and how well supported
localai chat --model qwen3:8b --prompt "what is in this folder?" --tools
localai usage report --period week
```

## Permission modes

| Mode | Behaviour |
|---|---|
| `manual` | Confirms every action, including reads. |
| `auto` | **Default.** Reads run freely; changes and commands ask first. |
| `workspace` | Anything inside a trusted directory runs freely; outside asks. |
| `bypass` | No prompts. Everything still shown and logged. |

Bypass cannot be reached by a keystroke — it requires typing a confirmation phrase into
a dialog that explains the consequences. And even in bypass:

- every action is displayed and written to an append-only audit log
- localai's own config, database and audit log stay unwritable by any tool
- `Ctrl+Q` stops everything instantly and engages a global kill switch
- deletes still go to the Recycle Bin

See [docs/permissions-engine.md](docs/permissions-engine.md).

## Safety

Files a model reads are untrusted input. If a document tries to instruct the model —
"ignore previous instructions", a hidden Unicode payload, a fake system prompt — the
content is fenced in an explicit envelope, flagged in the interface and recorded in the
audit log.

That detection is a heuristic and is documented as one. The actual guarantee is the
permissions engine: a successful injection still cannot make a consequential change
without your approval.

Paths are contained on their **resolved real path**, so `..` traversal and Windows
junctions cannot smuggle an operation out of a trusted workspace. There is a test that
creates a real junction and asserts the escape is caught.

See [docs/security-model.md](docs/security-model.md).

## Privacy

The only network traffic by default is to `127.0.0.1:11434`. There is no telemetry code
in the project — `privacy.telemetry` is typed `Literal[False]`, so it cannot be enabled.
`/context` shows exactly what is sent to the model; there is no hidden memory.

See [docs/privacy.md](docs/privacy.md).

## For AI coding agents

This repository is built to be worked on by Claude Code, Codex, Cursor and similar.

**Start with [AGENTS.md](AGENTS.md).** Then:

```bash
localai capabilities --json     # what is built vs. planned - honest, not aspirational
localai architecture --json     # layers, dependencies, invariants
python tasks.py check           # the one gate: format, lint, typecheck, test, docs
```

You should not need to scrape the terminal UI or guess how to build anything.

## Documentation

| | |
|---|---|
| [AGENTS.md](AGENTS.md) | Canonical guide for coding agents and fast human orientation |
| [docs/user-guide.md](docs/user-guide.md) | Using the interface |
| [docs/installation.md](docs/installation.md) | Install and upgrade |
| [docs/permissions-engine.md](docs/permissions-engine.md) | How access is decided |
| [docs/security-model.md](docs/security-model.md) | Threat model, and what is *not* defended |
| [docs/privacy.md](docs/privacy.md) | What is stored and what never leaves |
| [docs/architecture.md](docs/architecture.md) | Design and why |
| [docs/tool-api.md](docs/tool-api.md) | Writing a tool |
| [docs/development.md](docs/development.md) | Working on localai |
| [docs/testing.md](docs/testing.md) | Test strategy |
| [docs/database-schema.md](docs/database-schema.md) | Schema and migrations |
| [docs/training.md](docs/training.md) | Prompting vs. memory vs. RAG vs. fine-tuning |
| [docs/troubleshooting.md](docs/troubleshooting.md) | When something breaks |

## Status

**Phase 1 is complete and tested** — 389 tests, all passing, including the security
boundary tests.

Built: model detection and switching, streaming chat, the terminal UI, conversation
storage with fork/export/search, usage tracking with provenance, 19 tools, the
permissions engine, audit logging, cancellation, configuration, the CLI, the installer.

Not built yet, and reported as `false` by `localai capabilities --json`: document
extraction, semantic indexing, investigation mode, persistent memory, Modelfile
management, fine-tuning integration, the local HTTP API, the MCP server.

See [.project/status.json](.project/status.json) for the machine-readable version and
[HANDOFF.md](HANDOFF.md) to pick up where this left off.

## Licence

MIT.
