# Security model

## What this application is

A tool that gives a local language model controlled access to your computer. That is
inherently a security-relevant thing to build, so the threat model is stated plainly
rather than implied.

## What is defended against

### 1. The model doing something you did not intend

Local models are small and make mistakes. Defences, in order of strength:

1. **Permissions engine** — the real guarantee. Consequential actions require
   confirmation unless you configured otherwise. See `permissions-engine.md`.
2. **Reversibility** — deletes go to the Recycle Bin; edits are backed up first; diffs
   are shown; dry-run simulates.
3. **Bounds** — timeouts, output caps, scan depth and entry limits, a tool-iteration
   cap, a repeated-call detector and a wall-clock budget per turn.
4. **Visibility** — every action displayed with its exact arguments, and audited.

### 2. Prompt injection from file content

Anything a tool reads is untrusted input that lands in the model's context. A document
can try to issue instructions.

**Structural defence (always on):** every tool result derived from file or command
content is wrapped in an `<untrusted-content>` envelope stating that it is data, not
instruction. This cannot be disabled.

**Heuristic defence (optional):** `safety/injection.py` scans for instruction
overrides, ChatML role markers, permission-escalation requests, exfiltration attempts
and hidden Unicode. Findings are named in the envelope, flagged in the UI and recorded
in the audit log.

**This scanner is not the security boundary.** It raises the cost of an attack and
makes it visible. The guarantee is that a successful injection still cannot make a
consequential change without your confirmation — the model has no more authority than
you granted it.

### 3. Escaping a trusted workspace

Containment is decided on resolved real paths. `..` traversal, junctions, symlinks and
reserved device names are all handled. See `permissions-engine.md`, stage 7.

`tests/unit/test_path_safety.py` creates a real junction pointing outside the workspace
and asserts the escape is detected.

### 4. Command injection

We never build a command by interpolating model-supplied text into a template. The
command a user approves is the exact string handed to PowerShell, passed as a single
argv element with `-NoProfile -NonInteractive`. The `git` tool takes an *enum* of
subcommands rather than an argument string, so a model cannot reach `git push --force`
through a tool advertised as read-only.

### 5. The model tampering with its own oversight

`config.toml`, the database and the audit log are denied to mutating tools at stage 1,
before any policy is consulted. No mode, rule or grant re-opens this.

### 6. Silent data exfiltration

Default operation is loopback-only. `privacy.network_disabled` blocks networked tools
outright. Commands that appear to make network requests are classified and flagged
before execution. There is no telemetry code in the project; `privacy.telemetry` is
typed `Literal[False]` so it cannot be enabled.

## What is *not* defended against

Stated honestly, because a security model that overclaims is worse than none.

- **A user who chooses bypass mode and does not read what scrolls past.** Bypass is
  deliberately hard to enable and impossible to reach by accident, but it is real.
- **A malicious model.** A deliberately backdoored local model acting within its
  granted permissions is not detectable here.
- **Kernel or OS-level compromise.** This runs with your user privileges and is not a
  sandbox.
- **A sufficiently novel prompt injection.** The scanner is pattern-based.
- **Physical access to the state directory.** Conversations and audit logs are stored
  unencrypted on disk, protected only by filesystem permissions.

## Trust boundaries

| Boundary | Trust |
|---|---|
| You, at the terminal | Trusted. Can change any setting. |
| The local model | **Untrusted.** Every action passes the engine. |
| File and command content | **Untrusted.** Fenced and scanned. |
| External agents (MCP/API) | **Untrusted.** Identity grants nothing. |
| A developer editing the repo | Trusted for source, **not** for runtime permission. |

That last row matters: an external coding agent working inside this repository does not
thereby gain elevated application permissions. Developer access and runtime tool
permission are separate concepts.

## Secrets

The project stores no secrets today. When the local API gains authentication (Phase 4),
its token goes to Windows Credential Manager via DPAPI, never to `config.toml`.

## Dependency policy

Runtime dependencies are deliberately few: `httpx`, `textual`, `rich`, `pydantic`,
`tomli-w`. All are widely used and actively maintained.

Recycle Bin deletion is implemented with `ctypes` against `SHFileOperationW` rather
than adding `send2trash`. The call is about forty lines with no transitive
dependencies, and it keeps a security-relevant behaviour inside code reviewed here.

New dependencies with security, privacy or maintenance implications must be raised
before being added.

## Security-sensitive modules

See `../CODEOWNERS.md`. These are held to stricter mypy settings; `python tasks.py
changed-security` warns when a diff touches them.

## Reporting a problem

This is a local-first tool with no network service by default, so the attack surface is
mostly the permissions engine. If you find a way to make a tool act outside its
declared risk, escape a workspace, or bypass the audit log, that is a genuine
vulnerability — please report it with a reproducing `permissions explain` request.
