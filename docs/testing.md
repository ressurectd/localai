# Testing

```powershell
python tasks.py test               # everything
python tasks.py test-unit
python tasks.py test-integration
python tasks.py test-security      # boundary tests only
python tasks.py test-one tests/unit/test_tools.py::test_read_file_refuses_binary
python tasks.py coverage
```

## Hermetic by construction

No test touches your real configuration, database, conversations or Ollama daemon.

`tests/conftest.py` has an **autouse, unconditional** fixture that points
`LOCALAI_HOME` at a temporary directory and clears every `LOCALAI_*` override. It is
autouse precisely because an opt-in version would eventually be forgotten, and the
consequence would be a test writing to real user data.

`MockProvider` replaces Ollama. It is scripted, not random:

```python
provider = MockProvider()
provider.queue_tool_call("read_file", {"path": "notes.txt"})
provider.queue_text("The file says hello.")
```

It records every call in `provider.calls`, so you can assert on what the agent loop
actually sent — which messages, which tool schemas, which options.

## Fixtures

| Fixture | What it gives you |
|---|---|
| `isolated_home` | Temporary `LOCALAI_HOME` (autouse). |
| `workspace` | Synthetic filesystem: nested tree, Unicode filenames, a duplicate pair, a binary file, and a document containing a prompt-injection attempt. |
| `config` | Auto mode, one trusted workspace, backups on. |
| `database`, `paths`, `audit`, `engine`, `registry`, `runner`, `context` | Wired components. |
| `provider` | `MockProvider`. |
| `app` | A fully wired `Application` against mocks. |
| `windows_only` | Skips on non-Windows. |

## Markers

```powershell
python -m pytest -m security
python -m pytest -m integration
```

**`security`-marked tests must never be skipped or weakened.** If one fails, the
failure is the finding, not an inconvenience.

## What is covered

| Area | File | What it asserts |
|---|---|---|
| Permission boundaries | `unit/test_permission_boundaries.py` | The full evaluation order, all four modes, unconditional stages, rules, grants, rate limiting, and that external-agent identity confers nothing. |
| Path safety | `unit/test_path_safety.py` | `..` traversal, containment, reserved device names, long paths, Unicode, and **real junction escapes** created with `mklink /J`. |
| Risk ordering | `unit/test_risk_ordering.py` | All four comparison operators on `RiskLevel`. Regression test for a real bug. |
| Tools | `unit/test_tools.py` | Every tool's behaviour, bounds, dry-run, backups, and that each declares its `affected_paths`. |
| Agent loop | `unit/test_agent_loop.py` | Streaming, tool round-trips, all three guard rails, cancellation with partial output preserved, and the structured fallback parser. |
| Safety | `unit/test_safety.py` | Injection detection and false-positive resistance, sensitive classification, backups, diffs. |
| Storage and config | `unit/test_storage_and_config.py` | Migrations including rollback, conversations, fork, search, export, usage provenance, config validation. |
| CLI contracts | `integration/test_cli_contracts.py` | Golden tests: every `--json` schema, stdout purity, exit codes, deterministic output. |
| End to end | `integration/test_end_to_end.py` | Real wiring plus a **headless drive of the TUI** via Textual's pilot. |

## Testing the TUI

Textual's `run_test` drives the real application against a virtual terminal, so UI
behaviour is testable without a human:

```python
async with tui_app.run_test() as pilot:
    await pilot.pause()
    tui_app.query_one("#prompt").text = "hi there"
    await pilot.press("enter")
    await pilot.pause()
    assert tui_app.state.conversation_id is not None
```

This caught a real bug: `TextArea` consumes key events before they reach the App, so an
App-level Enter handler was dead code and Enter inserted a newline instead of sending.

## Writing a good test here

- **Name the behaviour, not the function.** `test_edit_file_refuses_an_ambiguous_match`
  beats `test_edit_file_2`.
- **One reason to fail.** If the assertion message would not tell you what broke, split
  it.
- **Assert on the message, not just the status,** for anything a model or a user reads.
  A tool error that fails to say what to do next is a defect even if `ok` is correct.
- **Test the boundary, not the implementation.** Boundary tests should survive a
  refactor of the engine's internals.

## Before committing

```powershell
python tasks.py check
```

If you touched a security-sensitive module, also:

```powershell
python tasks.py changed-security
python tasks.py test-security
```
