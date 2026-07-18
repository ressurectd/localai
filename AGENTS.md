# AGENTS.md

Canonical guide for AI coding agents (Claude Code, Codex, Cursor, OpenCode, and
others) working in this repository. Humans should read it too — it is the fastest
orientation available.

**Read this first, then `.project/status.json` and `HANDOFF.md`.**

---

## 1. What this project is

`localai` is a local-first, agentic terminal application for talking to models
installed in [Ollama](https://ollama.com). It gives a local model controlled access
to the filesystem and shell, behind a permissions engine modelled on coding agents
like Claude Code.

Three properties define it. If a change compromises one of them, the change is wrong:

1. **Local only.** No cloud account, no telemetry, no analytics, no upload. The only
   network traffic by default is HTTP to `127.0.0.1:11434`.
2. **Every action is visible and reversible.** Even in bypass mode, every tool call is
   displayed and written to an append-only audit log. Deletes go to the Recycle Bin;
   edits are backed up first.
3. **Honest about uncertainty.** Token counts are labelled `reported` or `estimated`
   and never conflated. Unbuilt features report themselves as unbuilt.

---

## 2. Architecture in one page

Data flows one way. The UI never evaluates a permission; a tool never opens the
database; the agent loop never renders anything.

```
  TUI (Textual)          CLI (argparse)         [API / MCP - Phase 4]
        \                      |                        /
         \                     |                       /
          +---------- Application (app.py) -----------+     composition root:
                              |                              builds and injects
              +---------------+---------------+              every dependency
              |               |               |
         AgentLoop       ToolRunner       storage/
         (agent/)        (tools/)         (SQLite)
              |               |
         ModelProvider   PermissionEngine  <-- the single decision point
         (providers/)    (permissions/)
                              |
                          safety/
              (path containment, sensitive-data
               classification, injection detection,
               backups, Recycle Bin)
```

Full detail, including a Mermaid diagram: `docs/architecture.md`.

---

## 3. Repository map

| Path | What lives there |
|---|---|
| `src/localai/app.py` | **Composition root.** Everything is constructed and injected here. |
| `src/localai/config/` | Typed configuration (`models.py`), atomic persistence (`manager.py`), path resolution (`paths.py`). |
| `src/localai/domain/` | Provider-neutral conversation primitives. No I/O. |
| `src/localai/providers/` | `base.py` (Protocol), `ollama.py` (real), `mock.py` (deterministic test double), `discovery.py` (what is installed here). |
| `src/localai/permissions/` | **Security-sensitive.** `engine.py` is the only place a decision is made; `audit.py` records it. |
| `src/localai/safety/` | **Security-sensitive.** Path containment, credential classification, prompt-injection detection, backups, Recycle Bin. |
| `src/localai/tools/` | `base.py` (contract), `registry.py` (validation), `runner.py` (**the single execution path**), then the tools themselves. |
| `src/localai/agent/` | The tool-call loop and the structured fallback for models without native tool calling. |
| `src/localai/storage/` | SQLite access and migrations. **No other module opens sqlite3.** |
| `src/localai/cli/` | Non-interactive interface. This is the surface you should prefer. |
| `src/localai/ui/` | Textual TUI. Presentation only. |
| `schemas/` | JSON Schemas for config, tools, results, permission rules, profiles, plugins. |
| `tests/unit/`, `tests/integration/` | Tests, mapped to the modules they cover. |
| `tasks.py` | The task runner. Every dev command lives here. |

---

## 4. Invariants

These are enforced by tests in `tests/unit/test_permission_boundaries.py` and
`tests/unit/test_path_safety.py`. **Do not weaken one to make something else pass.**
If one of these tests fails, the test is right and the change is wrong.

1. **Every tool call goes through `ToolRunner.execute`.** There is no parameter that
   skips permission evaluation, confirmation or auditing. Do not add one.
2. **Every permission decision comes from `PermissionEngine.evaluate`.** No interface
   has its own logic. TUI, CLI, API and MCP produce identical verdicts for identical
   requests.
3. **Stages 1–3 of the evaluation order cannot be overridden.** Protected application
   paths, the kill switch and read-only mode ignore every rule, mode and grant.
   `Decision.overridable` is `False` for these, and a UI must not offer a prompt.
4. **The model cannot rewrite policy or erase history.** `config.toml`, the database
   and the audit log are in `AppPaths.protected_paths()`; mutation is denied at
   stage 1.
5. **Caller identity confers no authority.** An MCP client naming itself `claude-code`
   gets exactly the permissions a rule grants it and nothing more.
6. **Tools never receive the permissions engine.** `ToolContext` deliberately omits it,
   so a tool cannot consult or second-guess a decision already made.
7. **Logs go to stderr; JSON goes to stdout.** `localai x --json | jq` must never break.
8. **No estimate is presented as exact.** Every token figure carries a `TokenSource`.
9. **Untrusted content is always fenced.** Tool output from files or commands is wrapped
   in an `<untrusted-content>` envelope. Detection is optional; fencing is not.
10. **Ordered enums define all four comparison operators.** See §10.

---

## 5. Commands

All of these work in PowerShell, cmd and bash. Use `python tasks.py <name>`; the raw
equivalents are in `docs/development.md` if you need them.

```
python tasks.py setup              # venv + install with dev tools
python tasks.py check              # THE GATE: format, lint, typecheck, test, docs
python tasks.py test               # full suite
python tasks.py test-unit
python tasks.py test-integration
python tasks.py test-security      # permission and path-safety boundaries only
python tasks.py test-one tests/unit/test_tools.py::test_read_file_refuses_binary
python tasks.py lint
python tasks.py format
python tasks.py typecheck
python tasks.py run                # launch the TUI
python tasks.py sandbox            # TUI against synthetic data, mutations disabled
python tasks.py doctor
python tasks.py migrate
python tasks.py seed-test-data
python tasks.py schemas            # regenerate schemas/ from the pydantic models
python tasks.py validate-docs      # fail if docs disagree with the code
python tasks.py changed-security   # list changed files that touch a boundary
python tasks.py secret-scan        # check for credentials before committing
python tasks.py build / package / clean
```

**Run one test:** `python tasks.py test-one <path>::<test_name>`

**Before you finish any change:** `python tasks.py check`

---

## 6. Inspect the app without reading the source

Prefer these over grepping. They are versioned contracts, not incidental output.

```
localai capabilities --json      # what is built vs. planned - check this before assuming
localai architecture --json      # layers, dependencies, invariants
localai providers scan --json    # which providers and models are installed, and how
                                 # completely each model can be driven
localai providers best           # the model best suited to agentic use
localai tools list --json
localai tools schema read_file
localai config schema
localai permissions explain --action request.json
localai version --json           # every interface's schema version
localai migrations status --json
localai doctor --json
```

`localai capabilities --json` is the honest answer to "does this project do X yet".
Features not built report `false` rather than being omitted.

### `permissions explain`

The most useful command for understanding a denial. Write a JSON file:

```json
{
  "tool": "delete_path",
  "risk": "destructive",
  "mutating": true,
  "paths": ["D:/Archive/old.txt"],
  "caller": { "interface": "mcp", "client_id": "my-agent" }
}
```

`localai permissions explain --action request.json --json` returns the requested
action, matched rule, decision, whether confirmation is required, risk classification,
workspace boundary and the reason. Exit code 77 means denied.

---

## 7. How to add things

### Add a tool

1. Subclass `Tool` in the right module under `src/localai/tools/`.
2. Set `name`, `description` (written *for the model*), `risk`, `mutating`, `network`,
   `returns_untrusted_content`, and `parameters` (JSON Schema).
3. Implement `run`. Return `ToolResult.failure(...)` for expected failures — the model
   reads that text and can recover. Raise only for genuine bugs.
4. **Implement `affected_paths`** if it touches the filesystem. A path the engine never
   sees is a path it cannot contain. This is a security requirement, not a nicety.
5. Implement `describe_call` — this exact string is what the user authorises.
6. Register it in `register_builtins()` in `tools/builtin.py` and add it to
   `BUILTIN_TOOL_NAMES`.
7. Document it in the table in `docs/tool-api.md` (`validate-docs` enforces this).
8. Add tests in `tests/unit/test_tools.py`.

Full walkthrough with a worked example: `docs/tool-api.md`.

### Add a model provider

Implement the `ModelProvider` Protocol in `src/localai/providers/`. Note `chat` is a
non-async `def` returning `AsyncIterator` — it is an async generator. Wire it in
`Application.create`. See `docs/architecture.md#adding-a-model-provider`.

### Add a document extractor

Phase 3. The interface is designed in `docs/architecture.md#extractors`; extractors are
optional dependencies and `doctor` reports which are missing.

### Change a permission rule safely

1. Read `docs/permissions-engine.md` in full, especially the evaluation order.
2. Never reorder stages 1–3.
3. Add tests to `tests/unit/test_permission_boundaries.py` **before** the change.
4. Run `python tasks.py test-security`.
5. Run `python tasks.py changed-security` and address what it prints.

### Database migration

Add `src/localai/storage/migrations/NNN_description.sql`, numbered contiguously.
Never edit a released migration — ship a correction as a new file. Transaction control
belongs *inside* the script (see `002_message_search.sql`); the runner cannot wrap
`executescript` from outside. Details: `docs/database-schema.md`.

---

## 8. Security-sensitive modules

Changes here need extra tests and a note in the commit message. `python tasks.py
changed-security` prints this list for your diff. Also in `CODEOWNERS.md`.

- `src/localai/permissions/engine.py` — the decision point
- `src/localai/permissions/audit.py` — the audit trail
- `src/localai/safety/pathsafe.py` — containment and junction escapes
- `src/localai/safety/sensitive.py` — credential classification
- `src/localai/safety/injection.py` — prompt-injection detection
- `src/localai/safety/recycle.py` — Win32 deletion
- `src/localai/tools/runner.py` — the single execution path
- `src/localai/tools/shell.py` — command execution
- `src/localai/tools/fs_write.py` — filesystem mutation
- `src/localai/config/paths.py` — defines the protected paths

These are held to stricter mypy settings (see `[[tool.mypy.overrides]]` in
`pyproject.toml`). Loosening those settings is itself a security-relevant change.

---

## 9. Files you must not edit automatically

- `schemas/*.json` — **generated.** Run `python tasks.py schemas` instead.
- `src/localai/storage/migrations/*.sql` — **immutable once released.** Add a new one.
- `.venv/`, `dist/`, `build/`, `*.egg-info/` — build output.
- Anything under the user's localai home directory (`%LOCALAPPDATA%\localai`) — that
  is user data, not project data. Tests never touch it; `conftest.py` redirects
  `LOCALAI_HOME` to a temporary directory for every test.

Update `.project/status.json` and `HANDOFF.md` as part of significant work. They are
handoff aids, **not** a substitute for reading the code or running the tests.

---

## 10. Traps in this codebase

Real bugs found during development, kept here so they are not reintroduced.

**Ordered `StrEnum`s need all four comparison operators.** `RiskLevel` subclasses
`StrEnum`, which inherits `str`'s comparisons. Defining only `__lt__` and `__le__` left
`>=` doing *alphabetical* comparison: `"read" >= "execute"` is True because `r` sorts
after `e`. Every severity gate silently compared spelling instead of severity. See
`tests/unit/test_risk_ordering.py`.

**`DirEntry.stat()` on Windows returns `st_ino == 0`.** It reads cached `FindFirstFile`
data. Using `(st_dev, st_ino)` as a cycle-detection key made every directory look
identical and collapsed recursive walks to one level. `_walk` in `tools/fs_read.py` uses
resolved paths instead.

**`sqlite3.executescript` commits before it runs.** Wrapping it in an outer
`BEGIN`/`COMMIT` leaves nothing to commit. Transaction control goes inside the script.

**`TextArea` consumes keys before they reach the App.** An App-level `on_key` handler
for Enter is dead code. `PromptArea` in `ui/app.py` overrides `_on_key`.

**`Message` is ambiguous.** In this project it means a conversation message
(`localai.domain.messages.Message`). Textual's is imported as `TextualMessage`.

**`ConversationStore.list` was renamed to `recent`.** A method named `list` shadows the
builtin inside its own class body, breaking every `-> list[...]` annotation after it.

---

## 11. Recommended workflow

1. Read this file.
2. Read `.project/status.json` and `HANDOFF.md`.
3. Run `localai doctor --json` — confirm the environment is sane.
4. Run `python tasks.py test` — establish a green baseline **before** changing anything.
5. Propose a change plan.
6. Run `python tasks.py changed-security` to see which boundaries you are near.
7. Implement in small steps.
8. Run focused tests: `python tasks.py test-one ...`
9. Run `python tasks.py check`.
10. Update `docs/`, `.project/status.json`, `HANDOFF.md` and `CHANGELOG.md`.
11. Write a concise handoff summary.

**Do not claim a feature works unless you have run it.** `localai capabilities --json`
must stay truthful.

---

## 12. Current status

Phase 1 is complete and tested. See `.project/status.json` for the machine-readable
version, which is authoritative.

**Built and tested:** model and provider discovery, model switching, streaming chat, the Textual UI,
conversation storage with fork/export/search, usage tracking with provenance, 19 tools
(filesystem read/write, PowerShell, Python, system, Git), the permissions engine with
four modes, audit logging, cancellation, configuration, the CLI, the installer.

**Not built yet** (and reported as `false` by `localai capabilities --json`): document
extraction, semantic indexing, investigation mode, persistent memory, Modelfile
management, fine-tuning integration, the local HTTP API, the MCP server.

**Known limitations:** `/compact` truncates deterministically rather than summarising
with the model. `list_processes` is Windows-only. Prompt-injection detection is a
heuristic — the real guarantee is the permissions engine.
