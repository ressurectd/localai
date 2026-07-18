# User guide

## Starting

```powershell
ai
```

`ai` and `localai` are the same command. It picks the best installed model unless you
have configured one — `ai providers scan` shows what that means. Run `ai doctor` if
anything looks wrong.

Useful variants:

```powershell
ai run --model qwen3:8b     # pick a specific model
ai dev sandbox              # synthetic data, mutations disabled, opens instantly
```

## The screen

```
qwen3:8b   auto   D:\Work\project        ######...... ~4,210/40,960   <- model, mode, cwd, context
                                                                       
 > summarise the notes in this folder                                  <- you
                                                                       
 -> list D:\Work\project matching *.md                                 <- tool request
    ok  7 entries                                                      <- tool result
 -> read D:\Work\project\notes.md                                      
    ok  84 lines                                                       
                                                                       
 The notes cover three areas...                                        <- the model
                                                                       
 19 tools | 2 calls | 43 tok/s | local only | Enter send, Shift+Enter  <- status
```

Colour is consistent: cyan is the model, magenta is a tool *request*, green is a
success, yellow is awaiting you, red is denied or failed, dim is reasoning and
metadata. **A tool request and a tool result never look like model prose** — you can
always tell what was suggested from what was done.

## Keys

| Key | Action |
|---|---|
| `Enter` | Send |
| `Shift+Enter` | New line |
| `Esc` | Cancel the current generation |
| **`Ctrl+Q`** | **Emergency stop** — cancels and engages the kill switch |
| `Ctrl+P` | Command palette |
| `Ctrl+M` | Model selector |
| `Ctrl+G` | Cycle permission mode (never reaches bypass) |
| `Ctrl+O` | Permission mode picker |
| `Ctrl+T` | Colour theme picker |
| `Ctrl+F` | Search history |
| `Ctrl+L` | Clear the view |
| `F1` | Help |

**Ctrl+Q** is deliberately heavy-handed: it stops what is running *and* blocks the next
mutating action until you run `/permissions killswitch off`. It is the control you
reach for when something is going wrong.

## Typing a command

Press `/` and a menu rises above the prompt, filtering as you type:

```
┌──────────────────────────────────────────────┐
│ ❯ /model     same picker; /model <name> too   │
│   /models    model picker                     │
│   /mode      permission mode picker           │
│   ↑↓ choose · Tab complete · Enter run · Esc  │
└──────────────────────────────────────────────┘
```

`↑`/`↓` move, `Tab` completes, `Enter` runs the highlighted one, `Esc` dismisses.
`Ctrl+P` opens it directly.

## Watching it think

When a model reasons before answering, an indicator appears above the prompt:

```
◐ Qwen is thinking  ▃▄▅▆▇█▇▆▅▄▃▂▁▂  3.4s
  considering whether the budget figure appears twice...
```

Three things at a glance: that it is working, how long it has been, and roughly what
about. The caption shows the last complete phrase of the reasoning rather than a
stream of half-words — updating on every token strobes and is harder to read than
nothing.

When the answer starts, the indicator folds away and leaves a note:
`▸ thought for 4.2s`. Trivial pauses are not reported.

## Knowing which model you are talking to

Each model family has its own colour and sigil. Switch to DeepSeek and the interface
goes abyssal blue; switch to Qwen and it turns jade. The accent colour of the top bar,
the prompt border and the model name all move together.

That is not only decoration — a 27B model with a 262k context behaves very differently
from a 3B one, and the permissions you are comfortable granting may differ too.
Colour carries that faster than reading a name does.

Qwen, DeepSeek, Gemma, Llama, Mistral, Phi, Granite and Command-R have their own
identity. Anything else gets a neutral one — unlisted is not a downgrade.

## Themes

`/theme` or `Ctrl+T`. Fourteen options including `tokyo-night`, `dracula`, `gruvbox`,
`nord`, `catppuccin-mocha`, plus two custom: **synthwave** (hot magenta and cyan on
deep violet) and **matrix** (green phosphor on black).

Set a default in `config.toml`:

```toml
[ui]
theme = "synthwave"
```

## Permission modes

| Mode | What happens |
|---|---|
| `manual` | Confirms every action, including reads. |
| `auto` | **Default.** Reads run freely; changes and commands ask first. |
| `workspace` | Anything inside a trusted workspace runs freely; outside asks. |
| `bypass` | No prompts. Everything still shown and logged. |

`/mode auto`, `/mode workspace`, or `Ctrl+G` to cycle. Bypass requires typing a
confirmation phrase into a dialog that explains what you are agreeing to.

When asked to approve something you get four choices: **Once**, **Session** (this tool
for the rest of the session), **Always** (this tool, no more prompts), or **Deny**.

## Slash commands

| Command | What it does |
|---|---|
| `/help` | Commands and keys. `/help <command>` for one. |
| `/models` | Model picker, ranked by how well each can be driven. |
| `/model` | Same picker. `/model qwen3:8b` switches directly. |
| `/settings` | Where the config file is and what you can change live. |
| `/theme` | Colour theme picker — 14 themes including synthwave and matrix. |
| `/permissions` | Show policy. `/permissions killswitch on|off`, `/permissions clear`. |
| `/mode` | Permission mode picker. Also `/mode readonly on`, `/mode dry-run on`, `/mode network off`. |
| `/workspace` | Set the working directory, or `add`/`remove` a trusted workspace. |
| `/tools` | List tools. `/tools <name>` for detail. |
| `/context` | **Exactly** what will be sent to the model, message by message. |
| `/usage` | Tokens and time. `/usage today|7d|week|month|year|all`. |
| `/history` | Browse conversations. `/history <query>` to search. |
| `/resume` | Resume a saved conversation. |
| `/new` | Start fresh. |
| `/fork` | Branch from an earlier message (numbers from `/context`). |
| `/export` | Export as `md` or `json`. |
| `/index` | Document indexing — Phase 3, not built yet. |
| `/search` | Search past conversations. |
| `/memory` | Persistent memory — Phase 3, not built yet. |
| `/profile` | List or apply a saved model profile. |
| `/system` | Show or set the system prompt. |
| `/think` / `/nothink` | Reasoning on/off, where the model supports it. |
| `/clear` | Clear the view (history is still saved). |
| `/compact` | Free context by dropping older messages. |
| `/logs` | The audit log. |
| `/doctor` | Diagnostics. |
| `/quit` | Exit. |

`/index` and `/memory` tell you plainly that they are not implemented yet rather than
pretending.

## Models

`Ctrl+M` lists every installed model with parameter count, quantisation, context length,
estimated memory and capabilities. Switching mid-conversation works — no restart.

`~` before a memory figure means estimated (from file size). A loaded model reports its
true resident size.

Not every model supports tools or thinking. If yours has no native tool calling,
localai falls back to a structured text protocol and tells you so — it works, but less
reliably.

To see what you have and how well each model can be driven:

```powershell
localai providers scan
```

```
+ ollama 0.32.0  5 model(s), 4 with native tool calling
    binary     C:\Users\you\AppData\Local\Programs\Ollama\ollama.EXE
    endpoint   http://127.0.0.1:11434
    models in  C:\Users\you\.ollama\models

    full   qwen2.5:7b                        4.4 GB  ctx  32,768
  * full   qwen3.6:27b                      16.2 GB  ctx 262,144
    full   qwen3:8b                          4.9 GB  ctx  40,960
    text   gemma3:12b                        7.6 GB  ctx       ?
```

`full` means native tool calling — the agent loop works as designed. `text` means the
fallback protocol will be used. `*` marks the model best suited to agentic use, which
is what localai picks by default on a fresh start. `localai providers best` prints just
that name, which is handy in a script.

It also shows where Ollama keeps its models, which is usually the answer to "where has
my disk space gone".

## Context

`/context` shows exactly what goes to the model: every message, in order, with its
estimated token count. **Nothing is sent that is not listed there.** There is no hidden
memory and no invisible preamble.

The meter in the top bar turns yellow at 70% and red at 90%. When it fills, `/compact`
drops older messages from context — they stay in the saved conversation and remain
searchable. `/new` starts clean.

`/compact` currently truncates deterministically rather than summarising with the model.
That is a deliberate limitation: a mechanical drop is predictable, whereas a generated
summary can quietly invent things you said.

## Usage

```
/usage today
```

```
Usage - today  [exact (reported by Ollama)]
  prompt              12,430
  completion           3,120
  thinking            ~1,024  (always estimated)
  total               15,550
  generations             18
  tok/s                 43.2
```

The label matters. `exact` means Ollama reported the counts. `estimated` or `mixed`
means at least some were derived by us (roughly 4 characters per token) and are shown
with `~`. Thinking tokens are **always** an estimate — Ollama counts them inside
completion tokens and does not report them separately.

If energy estimation is on, it is labelled an estimate everywhere it appears, because
it is assumed system draw multiplied by generation time, not a measurement.

## Safety

Always on:

- Deletions go to the **Recycle Bin** unless you explicitly request permanent.
- Files are **backed up** before being modified — see `%LOCALAPPDATA%\localai\backups`.
- Edits show a **diff**.
- Every action is written to an **audit log** you can read with `/logs`.
- localai's own config, database and audit log **cannot be modified by any tool**, in
  any mode.

Optional:

- `/mode dry-run on` — simulate changes without making them.
- `/mode readonly on` — refuse every mutating tool.
- `/mode network off` — block anything that would reach the network.

## Untrusted documents

If a file tries to instruct the model — "ignore previous instructions", a hidden
payload, a fake system prompt — you will see:

```
   ! This content tried to instruct the model (injection:instruction_override).
     It has been marked as untrusted data.
```

The content is passed to the model inside an envelope stating it is data. Detection is
a heuristic; the actual protection is that the model still cannot make a consequential
change without your approval.

Be cautious combining bypass mode with documents you did not write.

## Conversations

Saved automatically, locally, in SQLite. `/history` browses, `/history <query>`
searches full text, `/resume` reopens, `/fork <n>` branches from message *n*, `/export`
writes Markdown or JSON.

Nothing leaves your machine.

## Configuration

`%LOCALAPPDATA%\localai\config.toml`. `localai config path` prints the exact locations.

```toml
default_model = "qwen3:8b"
system_prompt = "You are a careful assistant working on a Windows machine."

[permissions]
mode = "workspace"

[[permissions.workspaces]]
path = "D:/Work"
allow_write = true
allow_execute = false

[safety]
use_recycle_bin = true
backup_before_modify = true
shell_timeout_s = 120

[profiles.careful]
model = "qwen3:8b"
description = "Slow and precise"
system_prompt = "Think carefully. Prefer reading before writing."
think = true

[profiles.careful.options]
temperature = 0.3
seed = 42
```

Validate with `localai config validate`. Full schema: `localai config schema`.

## When something goes wrong

`localai doctor` first. Then `troubleshooting.md`.
