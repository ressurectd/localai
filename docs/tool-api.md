# Tool API

A tool is a capability the model can invoke. This document is the contract.

## The 19 built-in tools

`!` marks a mutating tool. Risk drives the permission decision — see
`permissions-engine.md`.

| Tool | Category | Risk | Purpose |
|---|---|---|---|
| `list_directory` | filesystem | read | List one directory, non-recursively. |
| `find_files` | filesystem | read | Find files by name pattern, recursively. |
| `search_file_contents` | filesystem | read | Regex search inside text files. |
| `read_file` | filesystem | read | Read a text file, with line-range paging. |
| `inspect_metadata` | filesystem | read | Size, timestamps, attributes, link target. |
| `file_hash` | filesystem | read | md5/sha1/sha256/sha512 of a file. |
| `find_duplicates` | filesystem | read | Duplicate detection by content hash. |
| `system_info` | system | read | OS, CPU, memory, Python, hostname. |
| `disk_info` | system | read | Drive capacity and free space. |
| `list_processes` | system | read | Running processes (Windows only). |
| `git` | system | read | Read-only Git: status, log, diff, branch, show, remote, blame. |
| `write_file` ! | filesystem | write | Create or replace a file. Backs up, returns a diff. |
| `edit_file` ! | filesystem | write | Replace exact text. Refuses ambiguous matches. |
| `create_directory` ! | filesystem | write | Create a directory and parents. |
| `move_path` ! | filesystem | write | Move or rename. |
| `copy_path` ! | filesystem | write | Copy a file or tree. |
| `delete_path` ! | filesystem | **destructive** | Delete, to the Recycle Bin by default. |
| `run_powershell` ! | shell | **execute** | Run a PowerShell command. |
| `run_python` ! | shell | **execute** | Run a Python script in a subprocess. |

Machine-readable: `localai tools list --json`. One tool's schema:
`localai tools schema read_file`.

## The contract

```python
class Tool(ABC):
    name: str                          # stable identifier, never renamed silently
    description: str                   # written FOR THE MODEL
    risk: RiskLevel                    # read | write | destructive | execute | privileged
    mutating: bool                     # does it change state on disk?
    network: bool                      # does it make a network request?
    returns_untrusted_content: bool    # is the output file/command content?
    parameters: dict                   # JSON Schema for the arguments object
    category: str                      # filesystem | shell | system | general

    async def run(self, arguments, context) -> ToolResult: ...
    def affected_paths(self, arguments) -> list[Path]: ...
    def describe_call(self, arguments) -> str: ...
```

### `risk`

Drives everything. Be honest — under-declaring is a security bug.

| Level | Use for |
|---|---|
| `read` | Reads nothing but existing state. |
| `write` | Creates or modifies, recoverably. |
| `destructive` | Deletes or irreversibly overwrites. |
| `execute` | Runs arbitrary code or commands. |
| `privileged` | Requires or changes elevated privilege. |

`RiskLevel` defines all four comparison operators explicitly. It subclasses `StrEnum`,
which inherits `str`'s comparisons — omitting one silently reverts to alphabetical
ordering. See `tests/unit/test_risk_ordering.py`.

### `description`

Written for the model, not a human reader. State what it does, when to use it, and what
it returns. The model chooses tools from these strings alone.

Good: *"Search inside text files for a regular expression, returning matching lines with
their file path and line number. Use this to find where something is mentioned across
many files."*

Poor: *"Searches files."*

### `affected_paths` — a security requirement

Return every path the call would touch. **A path the engine never sees is a path it
cannot contain.** Omitting one means no workspace check, no sensitive-path
classification, no junction detection for that path.

`tests/unit/test_tools.py::test_path_taking_tools_declare_their_paths` asserts that
every tool accepting `path`, `root`, `source` or `destination` declares them. Use the
`path_from` helper:

```python
def affected_paths(self, arguments):
    return path_from(arguments, "source", "destination")
```

### `describe_call`

The exact string the user is asked to authorise. It must reflect what will actually
happen — paraphrasing defeats the point of asking.

`run_powershell` shows the command **verbatim** plus annotations:

```
powershell: Remove-Item C:\Temp\old -Recurse -Force  [DESTRUCTIVE: recursive forced delete]
```

### `run`

```python
async def run(self, arguments, context: ToolContext) -> ToolResult:
```

`arguments` is already validated against your schema, with defaults applied.

`context` gives you `config`, `paths`, `cwd`, `workspaces`, `conversation_id`,
`dry_run` and `cancel`. It deliberately does **not** give you the permissions engine:
the decision has already been made.

Rules:

- **Return `ToolResult.failure(...)` for expected failures.** The model reads that text
  and can recover. Write errors that tell it what to do next — `read_file` on a missing
  path says "Use find_files to locate it".
- **Raise only for genuine bugs.** The runner catches, audits and reports them.
- **Honour `context.dry_run`** if you mutate. Report what *would* happen; change nothing.
- **Honour `context.cancelled()`** in any loop.
- **Use `asyncio.to_thread`** for blocking I/O, so streaming and cancellation stay live.
- **Bound everything.** Depth, entries, bytes, time. An unbounded scan of a 4 TB drive
  is a denial of service against the user's own machine.

You do not need to handle: permission checks, timeouts, output truncation, injection
scanning or auditing. The runner does all of it, for every tool, unconditionally.

## Adding a tool

1. Subclass `Tool` in the right module under `src/localai/tools/`.
2. Set the class attributes.
3. Implement `run`, `affected_paths` and `describe_call`.
4. Register in `register_builtins()` in `tools/builtin.py`.
5. Add the name to `BUILTIN_TOOL_NAMES`.
6. Add a row to the table above — `python tasks.py validate-docs` fails otherwise.
7. Add tests to `tests/unit/test_tools.py`.

### Worked example

```python
class CountLines(Tool):
    name = "count_lines"
    description = (
        "Count the lines in a text file without reading its contents. Use this to "
        "judge whether a file is worth reading in full."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to count."}},
        "required": ["path"],
    }

    def affected_paths(self, arguments):
        return path_from(arguments, "path")

    def describe_call(self, arguments):
        return f"count lines in {arguments.get('path')}"

    async def run(self, arguments, context):
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = context.cwd / path
        if not path.is_file():
            return ToolResult.failure(f"{path} is not a file")

        def count() -> int:
            # Streamed, not slurped: the file may be enormous.
            with path.open("rb") as handle:
                return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))

        total = await asyncio.to_thread(count)
        return ToolResult(
            content=f"{path.name}: {total:,} lines",
            metadata={"path": str(path), "lines": total},
        )
```

## Testing a tool

```bash
localai test-tool count_lines --args '{"path":"README.md"}' --json
localai test-tool count_lines --args-file request.json --yes
```

Shows the permission decision and the preview before running, then the result. `--yes`
approves confirmations; without it, a non-interactive caller is treated as declining.

## Argument validation

The registry implements the JSON Schema subset the tools use: types, `required`,
`enum`, `minimum`/`maximum`, `maxLength` and `default`.

It is forgiving where a model's mistake is unambiguous and strict where it is not:

- `"50"` becomes `50` for an integer field.
- `"true"` becomes `True` for a boolean.
- A bare string becomes a one-element list for an array field.
- A boolean is **rejected** where an integer is expected — `True` is an `int` in Python
  and accepting it would silently mean `1`.
- Unknown keys are **dropped, not rejected**: a model inventing a plausible option
  should not fail an otherwise valid call.
- A missing required argument produces an error naming the field and showing the
  expected signature, which goes back to the model so it can retry correctly.

## Untrusted content

If your tool returns file or command content, set `returns_untrusted_content = True`
(or `result.untrusted = True`). The runner then wraps it in an `<untrusted-content>`
envelope and scans it for injection attempts.

Fencing is unconditional; only the scanner is configurable. See
`security-model.md`.

## Plugins

Schema: `schemas/plugin-manifest.schema.json`. A plugin declares its tools and their
risk levels in the manifest, so a user can review what it can do before loading it.
There is no implicit scanning — loading is always explicit.

The loader ships in Phase 3; the manifest format is stable now.
