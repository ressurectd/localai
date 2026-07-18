# Troubleshooting

**Start here:** `localai doctor`. It checks thirteen things, and every non-`ok` line
tells you what to do. Use `localai doctor --json` if you are scripting.

## Ollama

### "cannot connect to http://127.0.0.1:11434"

Ollama is not running.

```powershell
ollama serve                                          # or launch the desktop app
Invoke-RestMethod http://127.0.0.1:11434/api/tags     # confirm
```

If it runs on a different port, set it in `config.toml`:

```toml
[ollama]
host = "127.0.0.1"
port = 11500
```

### "no models installed"

```powershell
ollama pull qwen3:8b
```

### "model X is not installed"

`localai models list` shows what you actually have. Tags are exact — `qwen3` and
`qwen3:8b` are different names.

### The model ignores tools, or invents results

Check `localai models list` for the `tools` capability, or run `localai providers scan`
for a fuller report. Without native tool calling, localai uses a structured text
fallback that is genuinely less reliable. Prefer a tool-capable model such as
`qwen3:8b`.

### Generation is very slow

The model is probably too large for available memory and is swapping. Check
`localai models loaded` and the `system_info` tool. Use a smaller quantisation, or keep
the model resident:

```powershell
localai models preload qwen3:8b --keep-alive -1
```

## Permissions

### "Permission denied: ... protected path"

Correct behaviour. Tools may never modify localai's own config, database or audit log,
in any mode. Edit those files directly if you intend to change them.

### Everything asks for confirmation

You are in `manual` mode. Use `/mode auto` or press `Ctrl+G`.

### Nothing works after Ctrl+Q

That was the emergency stop; the kill switch is engaged.

```
/permissions killswitch off
```

### A path inside my workspace is refused

Probably a junction or symlink pointing outside it. Ask the engine:

```powershell
localai permissions explain --action request.json
```

It names the exact link component that caused the escape. If the traversal is
intentional, set `safety.follow_symlinks = true` — `doctor` will then flag it, because
it does weaken containment.

### A rule is not matching

```powershell
localai permissions validate
localai permissions explain --action request.json --json
```

Common causes: **every** path in a request must be covered for a path rule to match;
deny rules beat allow rules; an expired `expires_at` makes a rule inert; and an allow
rule does not cover sensitive paths unless `allow_sensitive = true`.

## Files

### "access denied" reading a file

Another program has it open, or it needs elevation. Close the program; run elevated
only if you genuinely intend to.

### Recursive search misses deep files

Check `safety.max_scan_depth` (default 25) and `safety.max_scan_entries` (50,000).
`DEFAULT_EXCLUDES` in `tools/fs_read.py` also skips `node_modules`, `.git`, `Windows`
and similar.

### A scan is slow on an old mechanical drive

```toml
[safety]
max_concurrent_reads = 1
tool_timeout_s = 300
```

### Unicode filenames look wrong

localai preserves Unicode throughout. If it renders wrongly, the terminal font is the
problem — Windows Terminal with Cascadia Mono handles it.

### A path is too long

Long-path support is applied automatically past `MAX_PATH`. For system-wide support:

```powershell
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1
```

## Database

### "integrity check failed"

```powershell
localai config path                       # find the database
Copy-Item <path> <path>.backup            # keep a copy
Remove-Item <path>                        # recreated on next start
```

Conversations in the damaged file are lost; export what matters first if you can.

### "migration pending"

```powershell
localai migrations apply
```

### Conversation search finds nothing

Your SQLite build may lack FTS5 — `localai doctor` says so. Search falls back to
substring matching, which is slower and less flexible but works.

## UI

### Colours or borders look wrong

Use Windows Terminal. Classic conhost has limited support. `ui.theme = "high-contrast"`
is available.

### Enter inserts a newline instead of sending

Should not happen — if it does, it is a bug worth reporting. `Shift+Enter` is the
newline.

### The interface will not start

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade textual
localai doctor
```

Use the CLI meanwhile: `localai chat --prompt "..."`.

## Diagnostics to include in a report

```powershell
localai doctor --json > doctor.json
localai capabilities --json > capabilities.json
localai version --json
```

Plus `%LOCALAPPDATA%\localai\logs\localai.log`. **Review these before sharing** — the
log can contain file paths from your machine.
