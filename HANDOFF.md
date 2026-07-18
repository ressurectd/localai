# HANDOFF

For whoever picks this up next — human or agent. The machine-readable version is
`.project/status.json`; this is the prose that gives it meaning.

**Last updated:** 2026-07-18 · **Phase 1 complete** · 389 tests passing

---

## Where things stand

Phase 1 is done and works end to end. It has been run against a real Ollama daemon
(0.32.0) with `qwen3:8b` on Windows 11: the model requested a tool, the permissions
engine evaluated it, the tool executed, the result went back, and the model answered —
at ~43 tok/s with exact token counts from Ollama.

The parts you can rely on:

- **Permissions engine** — 11-stage evaluation, four modes, fully tested. This is the
  most carefully built part of the project and the part most worth understanding first.
- **Tool runner** — the single execution path. Permission check, confirmation, timeout,
  truncation, injection screening and audit happen here for every tool, and cannot be
  skipped by a caller.
- **Agent loop** — streaming, cancellation with partial output preserved, three guard
  rails, and a structured fallback for models without native tool calling.
- **Storage** — migrations, conversations with fork/export/search, usage with honest
  provenance, audit log.
- **CLI** — versioned JSON contracts with golden tests.
- **Provider discovery** — `providers/discovery.py` reports what is installed and how
  completely each model can be driven. `doctor`, `providers scan` and the TUI's
  first-run model choice all classify through the same `classify_model`, so they
  cannot disagree.
- **TUI** — driven headlessly in tests, so UI changes are verifiable.

## What to do first

```bash
cat AGENTS.md                  # architecture, invariants, traps
cat .project/status.json       # machine-readable status
localai doctor                 # is the environment sane?
python tasks.py test           # green baseline BEFORE you change anything
localai capabilities --json    # what is built vs. planned
```

Do not skip the baseline test run. If something is already failing, you want to know
that before you start attributing it to your change.

## What is deliberately not built

These report `false` from `localai capabilities --json`. That honesty is load-bearing —
do not flip a flag without building the thing.

| Feature | Phase | Notes |
|---|---|---|
| Document extraction | 3 | Extractor Protocol sketched in `docs/architecture.md`. Optional deps already declared in `pyproject.toml` under `[extract]`, and `doctor` already reports which are missing. |
| Semantic index / RAG | 3 | FTS5 is in place for conversations; document indexing is separate. |
| Investigation mode | 3 | Read-only registry already exists (`register_builtins(include_mutating=False)`). |
| Persistent memory | 3 | `memories` table exists and is empty by design. |
| Modelfile management | 3 | |
| Local HTTP API | 4 | Must reuse `PermissionEngine`. No second permission path. |
| MCP server | 4 | Same requirement. `Interface.MCP` and `Caller.client_id` already flow through the engine and audit log. |
| LoRA / QLoRA | 5 | Optional module. |

## Suggested next steps, in order

**1. Document extraction (Phase 3).** The highest-value next thing: it turns "read text
files" into "read the documents I actually have". The interface is designed, the
optional dependencies are declared, `doctor` already reports them. Extractor output is
untrusted content and must go through the existing fencing — that is already handled if
you set `returns_untrusted_content = True`.

**2. Investigation mode.** Mostly composition of parts that exist: read-only registry,
bounded walks, caching in SQLite, resumable indexing. The novel work is the pause/resume
index state and the classifier for "probably important document".

**3. MCP server (Phase 4).** Likely what a user asks for next, since it makes localai's
tools available to Claude Code and Codex. **The single hard requirement:** it calls
`PermissionEngine.evaluate` like everything else. A client identity may select a *more
restrictive* profile; it must never grant permission.
`test_external_agent_identity_confers_no_authority` already asserts the principle.

**4. Model-assisted `/compact`.** Currently a deterministic truncation. Summarising with
the model is better, but must be clearly marked as a summary so the user is never
misled about what was actually said.

## Things you should know before editing

Read AGENTS.md §10 for the full list. The ones that will actually cost you time:

- **`RiskLevel` defines all four comparison operators on purpose.** It is a `StrEnum`;
  removing one silently reintroduces alphabetical comparison and every severity gate
  breaks quietly. There is a regression test.
- **Never use `(st_dev, st_ino)` for cycle detection on Windows.** `DirEntry.stat()`
  returns 0 for both.
- **Migrations carry their own `BEGIN`/`COMMIT`.** `executescript` commits first.
- **`PromptArea` exists because `TextArea` eats keys** before the App sees them.
- **`Message` means the conversation message.** Textual's is `TextualMessage`.

## Invariants you must not break

Full list in AGENTS.md §4. The three that matter most:

1. Every tool call goes through `ToolRunner.execute`. No bypass parameter. Do not add one.
2. Every decision comes from `PermissionEngine.evaluate`. No interface has its own logic.
3. Stages 1–3 (protected paths, kill switch, read-only) cannot be overridden by any
   rule, mode or grant.

If a security test fails, **the test is right**.

## Open questions

- **Energy estimation** is currently assumed-watts × wall-time, which is crude. Worth
  either improving (per-model calibration) or removing. It is off by default and
  labelled an estimate everywhere, so it is honest — just not very useful.
- **`num_ctx` and model context length.** We read the model's own context length but do
  not currently set `num_ctx` to match. For long conversations on models whose default
  is lower than their maximum, setting it explicitly would help.
- **Windows Credential Manager** integration is designed but unused, because nothing
  stores a secret yet. It becomes real with the local API's auth token.

## Environment this was built and verified on

Windows 11 (26200), Python 3.14.3, Ollama 0.32.0, Git 2.53.0, Windows PowerShell 5.1.
Models present at time of writing: `qwen3.6:27b` (tools, 262k context),
`qwen3:8b` (tools + thinking), `qwen2.5:7b` (tools),
`entity12208/editorai:v3-7b` (tools), `gemma3:12b` (no native tool calling).
Run `localai providers scan` for the current picture.

Note Python 3.14 — newer than the 3.11 floor in `pyproject.toml`. All dependencies have
wheels for it. If you hit a packaging problem on 3.14, that is the likely cause and
3.12 is a safe fallback.

## Handoff checklist for your own work

- [ ] `python tasks.py check` passes
- [ ] `python tasks.py test-security` passes if you touched a boundary
- [ ] `localai capabilities --json` still tells the truth
- [ ] Docs updated (`validate-docs` enforces the mechanical part)
- [ ] `.project/status.json` and this file updated
- [ ] `CHANGELOG.md` has a real entry
