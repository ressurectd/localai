# Development

## Setup

```powershell
git clone <repo> localai
cd localai
python tasks.py setup
```

Creates `.venv`, installs the package editable with dev tools.

### Direct PowerShell equivalents

Every task has a plain equivalent, so nothing is hidden behind the runner.

| Task | Direct command |
|---|---|
| `setup` | `python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ".[dev]"` |
| `install` | `.\.venv\Scripts\python.exe -m pip install -e .` |
| `run` | `.\.venv\Scripts\python.exe -m localai` |
| `sandbox` | `.\.venv\Scripts\python.exe -m localai dev sandbox` |
| `test` | `.\.venv\Scripts\python.exe -m pytest tests` |
| `test-unit` | `.\.venv\Scripts\python.exe -m pytest tests\unit` |
| `test-integration` | `.\.venv\Scripts\python.exe -m pytest tests\integration` |
| `test-security` | `.\.venv\Scripts\python.exe -m pytest -m security -v` |
| `test-one` | `.\.venv\Scripts\python.exe -m pytest tests\unit\test_tools.py::test_x -v` |
| `lint` | `.\.venv\Scripts\python.exe -m ruff check src tests` |
| `format` | `.\.venv\Scripts\python.exe -m ruff format src tests` |
| `typecheck` | `.\.venv\Scripts\python.exe -m mypy` |
| `migrate` | `.\.venv\Scripts\python.exe -m localai migrations apply` |
| `doctor` | `.\.venv\Scripts\python.exe -m localai doctor` |
| `build` | `.\.venv\Scripts\python.exe -m build` |
| `schemas` | `.\.venv\Scripts\python.exe scripts\generate_schemas.py` |
| `seed-test-data` | `.\.venv\Scripts\python.exe scripts\seed_test_data.py` |

## The gate

```powershell
python tasks.py check
```

Format, lint, typecheck, tests, documentation validation. Run before every commit.
This is the only command you need to remember.

## Layout

```
src/localai/
  app.py            composition root
  config/           typed config, atomic save, path resolution
  domain/           conversation primitives, no I/O
  providers/        ollama, mock, and the Protocol they satisfy
  permissions/      engine (the decision point) + audit
  safety/           pathsafe, sensitive, injection, backup, recycle
  tools/            base, registry, runner, and the tools
  agent/            loop + structured fallback
  storage/          db, migrations, conversations, usage, audit_store
  cli/              main, output, doctor
  ui/               Textual app, screens, slash commands, theme
tests/
  unit/             fast, isolated
  integration/      CLI contracts, end-to-end, headless TUI
scripts/            schema generation, test-data seeding
schemas/            generated JSON Schemas
docs/               this
```

## Environment variables

These are **all** of them. There are no undocumented ones — `ENV_OVERRIDES` in
`config/manager.py` is the single source of truth, and a test asserts each is
documented here.

| Variable | Config key | Notes |
|---|---|---|
| `LOCALAI_HOME` | — | Override the whole state directory. Used by every test. |
| `LOCALAI_PORTABLE` | — | Keep state beside the install. |
| `LOCALAI_MODEL` | `default_model` | |
| `LOCALAI_PROFILE` | `default_profile` | |
| `LOCALAI_OLLAMA_HOST` | `ollama.host` | |
| `LOCALAI_OLLAMA_PORT` | `ollama.port` | |
| `LOCALAI_PERMISSION_MODE` | `permissions.mode` | |
| `LOCALAI_KILL_SWITCH` | `permissions.kill_switch` | |
| `LOCALAI_READ_ONLY` | `safety.read_only` | |
| `LOCALAI_DRY_RUN` | `safety.dry_run` | |
| `LOCALAI_NETWORK_DISABLED` | `privacy.network_disabled` | |
| `LOCALAI_THEME` | `ui.theme` | |

Precedence: defaults < `config.toml` < environment < CLI flags.
`localai doctor` reports which are active.

## Developing without Ollama

Everything works offline against the deterministic mock provider:

```powershell
localai --mock models list
localai --mock chat --model mock-tools:8b --prompt "hi" --json
python tasks.py sandbox
```

`MockProvider` is scripted, not random:

```python
provider = MockProvider()
provider.queue_tool_call("read_file", {"path": "notes.txt"})
provider.queue_text("The file says hello.")
```

It ships three models: `mock-tools:8b` (tools + thinking), `mock-plain:3b` (no native
tool calling, exercises the fallback path) and `mock-vision:7b`.

## Migrations

Add `src/localai/storage/migrations/NNN_description.sql`, numbered contiguously from
001. Never edit a released migration — ship a correction as a new file.

**Transaction control goes inside the script.** `sqlite3.executescript` commits any
open transaction before it runs, so an outer `BEGIN`/`COMMIT` has nothing to close.
The runner wraps each migration's SQL in `BEGIN; … COMMIT;` for this reason, which also
handles the semicolons inside `CREATE TRIGGER` bodies that statement-splitting would
break.

See `database-schema.md`.

## Code style

Enforced by ruff and mypy; `pyproject.toml` is authoritative. What the tools cannot
check:

- **Comments explain why, not what.** If a comment restates the code, delete it. If
  removing it makes the code unclear, rename something.
- **Names make type annotations redundant.** A variable called `data` has failed.
- **The error case is part of the design.** What happens on empty input, a missing
  file, a permission error, a disconnected drive? Those are the cases, not edge cases.
- **Error messages tell the reader what to do next.** Especially tool errors — the
  model reads them and acts on them.

Security-sensitive modules are held to stricter mypy settings. Loosening those settings
is itself a security-relevant change.

## Common failure modes

**Tests writing to real user data.** They cannot: `conftest.py` sets `LOCALAI_HOME` to
a temporary directory autouse and unconditionally. If you add a fixture that resolves
paths, route it through `AppPaths`.

**A tool that forgets `affected_paths`.** The engine then never sees the path. A test
catches this for common argument names; if your tool uses an unusual one, add a case.

**Widening permissions to make something pass.** If a boundary test fails, the test is
right. Run `python tasks.py changed-security` to see what you are near.

**Assuming an ordered enum compares correctly.** See AGENTS.md §10.

## Adding a dependency

Raise it first if it has security, privacy or maintenance implications. The runtime
list is deliberately short. Prefer stdlib or forty lines of reviewed code over a
dependency for something contained — this is why Recycle Bin deletion uses `ctypes`
rather than `send2trash`.
