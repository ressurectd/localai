# Privacy

## The short version

Nothing leaves your computer. There is no account, no telemetry, no analytics, no crash
reporting, no update check, and no usage data collection.

The only network traffic localai generates by default is HTTP to `127.0.0.1:11434` —
your own Ollama daemon, on loopback.

## What is stored, and where

Everything is under `%LOCALAPPDATA%\localai\` (`localai config path` prints it):

| Path | Contents |
|---|---|
| `config.toml` | Your settings. |
| `data\localai.db` | Conversations, messages, usage history, audit log, rules. |
| `logs\audit.jsonl` | Append-only record of every action. |
| `logs\localai.log` | Application log. |
| `backups\` | Copies of files taken before modification. |
| `data\tool-output\` | Full output of truncated tool results. |

Plain files on your disk. Delete any of them whenever you like. Nothing is synced,
uploaded or phoned home.

## How this is guaranteed, not just promised

- **No telemetry code exists.** `privacy.telemetry` is typed `Literal[False]` in the
  configuration model, so it cannot be set to true even by editing the file.
- **The HTTP client sets `trust_env=False`**, so a system proxy cannot silently route
  local traffic off-device.
- **Network-touching tools are classified and gated.** `privacy.network_disabled`
  blocks them outright, and commands that look like network requests are flagged before
  execution.
- **`localai doctor` warns if `ollama.host` is not loopback** — the one configuration
  that would send your prompts to another machine.

## Reading your own data

```powershell
localai history list
localai history export <id> --format md
localai logs --limit 100
localai usage report --period all --json
```

The database is ordinary SQLite; open it with any tool you like.

## What the model can see

Only what you send it, and `/context` shows you exactly that: every message, in order,
with its estimated size. There is no hidden preamble and no invisible memory.

Persistent memory is not implemented in this phase. When it ships it will be opt-in,
stored in a table you can read, and inspectable and deletable from the interface.
**localai will never create hidden long-term memory.**

## What tools can reach

Whatever your permission mode allows. In `auto` (the default) the model can read files
without asking, so:

- Reads of browser profiles, SSH keys, credential stores, password databases, crypto
  wallets and `.env` files **always** require confirmation, even in auto mode.
- Trusted workspaces let you confine free access to specific directories.
- `/mode readonly on` prevents all modification.
- The audit log records everything, so you can always check afterwards.

## Optional features that would involve the network

None are enabled, and none are implemented in this phase. Any that arrive will be
explicitly labelled, off by default, and shown in the UI while active:

- A local HTTP API (Phase 4) — loopback-bound by default, authenticated, with a clear
  warning if bound more widely.
- An MCP server (Phase 4) — stdio transport first.
- Embedding models for semantic search (Phase 3) — local models preferred; indexed
  material is never sent anywhere.

## Sharing diagnostics

`localai doctor --json` includes file paths from your machine, and `localai.log` may
contain paths you accessed. Review before sharing. Conversations and audit logs are
never included automatically in anything.

## If you delete localai

`uninstall.ps1` removes the program and **keeps your data** unless you explicitly ask
otherwise. Your Ollama models are never touched.
