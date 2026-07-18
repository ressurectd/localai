# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). This project versions
each public interface separately — see [docs/release-process.md](docs/release-process.md).

## [Unreleased]

### Added
- Slash-command menu: press `/` and a filtered list rises above the prompt.
  Arrow keys navigate, Tab completes, Enter runs, Esc dismisses.
- Thinking indicator showing a pulse, an elapsed timer and a rolling caption of the
  model's reasoning. Folds away into a `thought for 4.2s` note when the answer starts.
- Model identity: each family (Qwen, DeepSeek, Gemma, Llama, Mistral, Phi, Granite,
  Command-R) has its own sigil and accent colour, and the whole interface takes on the
  colour of the active model. ASCII fallback for terminals without Unicode support.
- `/theme` and Ctrl+T: 14 colour themes, including two custom ones (synthwave, matrix).
- `/mode` and Ctrl+O open a permission-mode picker with consequences spelled out.
  Selecting bypass from the list still requires the typed confirmation phrase.
- `/model` with no argument opens the model picker rather than printing a line.
- `ai` launcher alongside `localai`.
- Window title is now `ai`.

### Fixed
- **Critical: the permission confirmation dialog crashed.** `push_screen_wait`
  requires a Textual worker context; it was called from ordinary async methods, so
  every tool call needing confirmation failed with `NoActiveWorker` instead of asking
  the user. `/models` crashed for the same reason.
- `PromptArea.Changed` shadowed `TextArea.Changed`, so TextArea's own message was
  built through our constructor and arrived carrying the widget instead of the text.
- Refreshing the top bar while a modal was open raised `NoMatches`, because
  `App.query_one` searches the active screen.
- `ThinkingIndicator.stop()` reported zero for turns that finished between timer ticks.

## [0.1.0] — 2026-07-18

First functional release. Phase 1 complete: 353 tests passing, including the security
boundary suite.

### Added

**Models**
- Automatic detection of installed Ollama models, with capabilities, context length,
  quantisation, parameter count and estimated memory.
- Switching models mid-conversation without restarting.
- Live load state from `/api/ps`, preload and unload with `keep_alive` control.
- `localai providers scan` — discovers installed providers and models, classifying each
  as `full` (native tool calling), `fallback` (text protocol), `chat_only` or
  `embedding`, and reporting the Ollama binary, endpoint and model directory.
- `localai providers best` — prints the model best suited to agentic use. The TUI uses
  the same ranking to choose a default on first run, rather than taking whichever model
  sorts first alphabetically.
- `doctor` reports the recommended model and each model's support level, classified by
  the same code as `providers scan`.
- Streaming chat with thinking-mode support where the model exposes it.
- Configurable temperature, top_p, top_k, seed, context length, system prompt and stop
  sequences; reusable model profiles.

**Interface**
- Textual terminal UI: conversation view, multiline input, command palette, model
  selector, permissions indicator, context/token meter, working directory, tool activity
  display, status bar, searchable history, progress indicators.
- 28 slash commands.
- Keyboard shortcuts including `Ctrl+Q` emergency stop.
- Visual separation of model text, tool requests, tool output and system messages.

**Tools** (19)
- Read: `list_directory`, `find_files`, `search_file_contents`, `read_file`,
  `inspect_metadata`, `file_hash`, `find_duplicates`.
- Write: `write_file`, `edit_file`, `create_directory`, `move_path`, `copy_path`,
  `delete_path`.
- Execute: `run_powershell`, `run_python`.
- System: `system_info`, `disk_info`, `list_processes`, `git`.

**Permissions**
- Central engine with an 11-stage evaluation order; one decision point for every
  interface.
- Four modes: manual, auto, trusted-workspace, bypass.
- Rules by tool, path, command pattern, risk ceiling, interface and expiry.
- Session grants: once, session, per-tool, per-command-pattern.
- Global kill switch, per-session and persistent rules, rate limiting on high-risk
  actions.
- `localai permissions explain` — full reasoning for any proposed action.

**Safety**
- Dry-run and read-only modes.
- Automatic backup before modification; Recycle Bin deletion by default.
- Diff preview on every edit.
- Timeouts, output caps, scan depth and entry limits.
- Junction and symlink escape detection on resolved real paths.
- Reserved DOS device name rejection; long-path support.
- Prompt-injection detection with unconditional content fencing.
- Warnings on browser profiles, credential stores, SSH keys, cloud credentials,
  password databases, crypto wallets and registry hives.

**Storage and usage**
- SQLite with versioned migrations and FTS5 conversation search.
- Conversations: save, resume, named sessions, fork, export to Markdown or JSON,
  full-text search.
- Usage tracking with explicit `reported`/`estimated`/`unknown` provenance on every
  figure, across eight reporting periods, grouped by model, workspace and conversation.
- Append-only audit log in JSONL and SQLite, recording interface and caller identity.

**Agent loop**
- Streaming with cancellation that preserves partial output.
- Guard rails: iteration cap, wall-clock budget, identical-call detection.
- Structured text fallback for models without native tool calling.
- Schema validation with forgiving coercion for malformed model output.

**CLI**
- Full non-interactive surface with `--json` everywhere, NDJSON streaming, meaningful
  exit codes, `--no-color`, quiet and verbose modes.
- Introspection: `capabilities`, `architecture`, `tools schema`, `config schema`,
  `permissions explain`, `version`, `migrations status`, `doctor`.
- Logs to stderr, data to stdout, always.

**Project**
- `tasks.py` runner working identically in PowerShell, cmd and bash.
- PowerShell installer and uninstaller; uninstall never removes user data or Ollama
  models without explicit confirmation.
- JSON Schemas for config, tool definitions, tool results, permission rules, model
  profiles and plugin manifests.
- AGENTS.md, CLAUDE.md, CODEOWNERS.md and twelve documents under `docs/`.
- `validate-docs` and `secret-scan` gates.

### Fixed during development

Found by the test suite and worth recording, since each was a real defect:

- **Risk levels compared alphabetically.** `RiskLevel` subclasses `StrEnum` and only
  `__lt__`/`__le__` were overridden, so `>=` fell back to string comparison —
  `"read" >= "execute"` is True. Every severity gate compared spelling rather than
  severity: the kill switch blocked ordinary reads, and workspace mode demanded
  `allow_execute` for a plain file write. All four operators are now defined, with a
  regression test.
- **Recursive search only descended one level on Windows.** `DirEntry.stat()` returns
  cached `FindFirstFile` data where `st_ino` and `st_dev` are always 0, so the
  `(dev, ino)` cycle-detection key matched every directory and skipped all of them
  after the first. Now keyed on resolved paths.
- **Migrations could not commit.** `sqlite3.executescript` commits any open transaction
  before running, so the outer `BEGIN`/`COMMIT` had nothing to close. Transaction
  control moved inside the script, which also handles semicolons in `CREATE TRIGGER`
  bodies.
- **Enter inserted a newline instead of sending.** `TextArea` consumes key events before
  they reach the App, making the App-level handler dead code. Fixed with a `PromptArea`
  subclass that intercepts in `_on_key`.
- **ChatML injection markers were not detected.** The role-marker pattern did not allow
  the `|` in `<|system|>` or `<|im_start|>`, the most common template-injection form for
  local models.
- **Tool-name suggestions never reached the model.** "Did you mean `read_file`?" lived in
  `remediation`, which the runner discarded when building the failure message.
- **`ConversationStore.list` shadowed the builtin** inside its own class body, breaking
  every `-> list[...]` annotation after it. Renamed to `recent`.

### Known limitations

- `/compact` truncates deterministically rather than summarising with the model. This is
  deliberate for now: a mechanical drop is predictable, whereas a generated summary can
  quietly invent things you said.
- `list_processes` and Recycle Bin deletion are Windows-only.
- Prompt-injection detection is heuristic. The guarantee is the permissions engine.
- Document extraction, semantic indexing, investigation mode, persistent memory,
  Modelfile management, fine-tuning, the local HTTP API and the MCP server are not
  implemented. `localai capabilities --json` reports them as `false`.

[Unreleased]: https://github.com/localai/localai/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/localai/localai/releases/tag/v0.1.0
