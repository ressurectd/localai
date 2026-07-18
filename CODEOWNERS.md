# CODEOWNERS

This project may not be hosted on a platform that enforces CODEOWNERS, so this file is
documentation rather than automation. `python tasks.py changed-security` performs the
mechanical part: it prints a warning when a diff touches anything listed here.

## Security-sensitive areas

Changes to these require: extra tests, a run of `python tasks.py test-security`, and an
explicit note in the commit message describing the security impact.

| Area | Files | Why it matters |
|---|---|---|
| Permissions engine | `src/localai/permissions/engine.py` | The single decision point for every action on every interface. A mistake here silently widens what a model may do. |
| Audit logging | `src/localai/permissions/audit.py`, `src/localai/storage/audit_store.py` | The record of what happened. Must remain append-only and unreachable by tools. |
| Path safety | `src/localai/safety/pathsafe.py` | Workspace containment, junction and symlink escapes, reserved device names, long paths. |
| Credential detection | `src/localai/safety/sensitive.py` | Decides what forces a confirmation. Gaps here mean silent access to secrets. |
| Injection detection | `src/localai/safety/injection.py` | Framing and detection for untrusted file content. |
| Deletion | `src/localai/safety/recycle.py` | Win32 `SHFileOperationW`. A buffer mistake here is a native-code bug. |
| Backups | `src/localai/safety/backup.py` | The reversibility guarantee. |
| Tool execution | `src/localai/tools/runner.py` | The single execution path. Enforces policy, timeout, truncation, audit. |
| Shell execution | `src/localai/tools/shell.py` | Runs commands. Command classification drives risk escalation. |
| Filesystem mutation | `src/localai/tools/fs_write.py` | Writes and deletes user data. |
| Protected paths | `src/localai/config/paths.py` | Defines what a tool may never modify. |
| Future: local API auth | `src/localai/api/` (Phase 4) | Not yet built. Must not expose unrestricted execution. |
| Future: MCP server | `src/localai/mcp/` (Phase 4) | Not yet built. Must reuse the same engine — no second permission path. |

## Configuration that is itself security-relevant

| Setting | Effect if changed |
|---|---|
| `permissions.mode = "bypass"` | Removes per-action confirmation. Still logged and displayed. |
| `permissions.kill_switch` | Global disable for mutating and executing tools. |
| `safety.follow_symlinks = true` | Permits junction traversal out of a trusted workspace. |
| `safety.use_recycle_bin = false` | Deletions become permanent. |
| `safety.backup_before_modify = false` | Edits become unrecoverable. |
| `safety.detect_prompt_injection = false` | Disables the heuristic scanner. Fencing remains. |
| `ollama.host` not loopback | Prompts and file contents leave the machine. |
| `[[tool.mypy.overrides]]` strict block | Relaxing it lowers the bar on the modules above. |

`localai doctor` reports every one of these that is set to a non-default, weaker value.
