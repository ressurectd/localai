# Permissions engine

The permissions engine is the product. Everything else — the UI, the tools, the agent
loop — exists to give a local model useful access to your computer; this decides what
that access is.

**One entry point:** `PermissionEngine.evaluate(request) -> Decision`.

Every interface calls it. TUI, CLI, and (in Phase 4) the local API and MCP server
produce identical verdicts for identical requests. There is no second path, and
`tests/unit/test_permission_boundaries.py::test_every_interface_gets_the_same_verdict`
asserts it.

## Evaluation order

The order is the security contract. Stages 1–3 are **unconditional**: no rule, mode,
grant or configuration value can override them, and `Decision.overridable` is `False`
so a UI must not offer a confirmation prompt.

| # | Stage | Effect | Overridable |
|---|---|---|---|
| 1 | Protected application paths | DENY | **No** |
| 2 | Global kill switch | DENY | **No** |
| 3 | Read-only mode | DENY | **No** |
| 4 | Network policy | DENY | yes |
| 5 | Explicit deny rules | DENY | yes |
| 6 | High-risk rate limit | DENY | yes |
| 7 | Path containment | DENY / CONFIRM | yes |
| 8 | Sensitive-path classification | CONFIRM | via `allow_sensitive` |
| 9 | Allow rules and session grants | ALLOW | — |
| 10 | Permission-mode default | ALLOW / CONFIRM | — |
| 11 | Fallback | CONFIRM | — |

**Stage 11 exists so the engine fails closed.** Anything unrecognised requires
confirmation rather than proceeding.

### Stage 1 — protected application paths

`config.toml`, `data/localai.db`, `logs/audit.jsonl`, `logs/localai.log` and
`profiles/` are in `AppPaths.protected_paths()`. Mutation of anything at or beneath
them is denied before any policy is consulted.

This is the concrete form of two requirements: the model cannot silently alter the
permission database, and it cannot disable logging. *Reading* them is allowed —
inspecting your own audit log is legitimate.

### Stage 2 — kill switch

`permissions.kill_switch` disables every mutating and executing tool across every
interface. Reads still work, so the tool stays useful while you investigate. Bound to
**Ctrl+Q** in the TUI, which also cancels in-flight work.

### Stage 3 — read-only mode

`safety.read_only` denies anything above `RiskLevel.READ`. Distinct from a permission
*mode*: it is a safety flag that composes with all four modes. In read-only mode the
mutating tools are additionally never registered — defence in depth.

### Stage 7 — path containment

Decided on the **fully resolved real path**, never the string the model supplied.
`D:\Trusted\..\..\Windows` and a junction at `D:\Trusted\out` pointing at `C:\` both
look harmless as text.

- `..` is resolved before comparison.
- Junctions and symlinks are reparse points. `resolve_path` reports *which* ancestor
  was a link, so a denial is explainable rather than opaque.
- Traversing a reparse point to a location outside every workspace is denied unless
  `safety.follow_symlinks` is set.
- Reserved DOS device names (`CON`, `NUL`, `COM1`…) are denied outright.

### Stage 8 — sensitive paths

Browser profiles, SSH keys, cloud credentials, password databases, crypto wallets,
registry hives and `.env` files force a confirmation **even in Auto mode**.

This never blocks. Reading your own browser profile is a legitimate thing to want to
do; the job is to make sure you knew that is what you asked for. To pre-authorise it,
a rule must set `allow_sensitive = true` — an ordinary allow rule does not cover
sensitive paths.

## Permission modes

| Mode | Reads | Writes | Execute | Notes |
|---|---|---|---|---|
| `manual` | confirm | confirm | confirm | Every action, including reads. |
| `auto` | **allow** | confirm | confirm | The default. |
| `workspace` | allow inside | allow inside if `allow_write` | confirm unless `allow_execute` | Outside a workspace: confirm. |
| `bypass` | allow | allow | allow | No per-action prompt. Everything still shown and audited. |

**Bypass is not reachable by keystroke.** `Ctrl+G` cycles manual → auto → workspace
only. Entering bypass requires typing the exact phrase in
`permissions.bypass_confirmation_phrase` into a modal that spells out the consequences.

Even in bypass: every action is displayed, every action is audited, protected paths
stay unwritable, Ctrl+Q stops everything, and deletes still go to the Recycle Bin.

## Rules

```toml
[[permissions.rules]]
id = "git-readonly"
effect = "allow"                       # allow | deny | confirm
note = "routine git inspection"
tools = ["run_powershell"]
command_patterns = ["git status*", "git log*", "git diff*"]
max_risk = "execute"
interfaces = ["tui", "cli"]            # empty = every interface
allow_sensitive = false
# expires_at = 1789000000              # unix time; omit for permanent
```

Matching rules:

- Patterns are `fnmatch` globs, case-insensitive, with separators normalised, so a
  rule can be written with forward slashes and still match Windows paths.
- **Every path in a request must be covered** for a path rule to match. A rule allowing
  `D:/Work/**` will not bless a `move_path` whose destination is elsewhere.
- Deny rules are evaluated at stage 5, allow rules at stage 9 — **deny always wins**.
- An allow rule with no `tools`, `paths` or `command_patterns` is rejected at load
  time, because it would grant everything.
- Expired rules are ignored.

Schema: `schemas/permission-rule.schema.json`. Validate with
`localai permissions validate`.

## Session grants

Approvals given in the confirmation dialog. Never persisted automatically.

| Scope | Meaning |
|---|---|
| `once` | This call only. Consumed on use. |
| `session` | This tool until the session ends. |
| `tool` | This tool, no further prompts this session. |
| `command` | This exact command pattern. |

Clear them with `/permissions clear`.

## External agents

An external agent connecting over MCP or the local API is **powerful but not trusted**.

- A caller supplies a `client_id`. It is recorded in every audit entry and may select a
  *more restrictive* profile. It never grants permission.
- `test_external_agent_identity_confers_no_authority` asserts that naming yourself
  `claude-code` or `codex` changes nothing.
- High-risk tools must be enabled explicitly in configuration before they are reachable
  over MCP or the API.
- Developer access to the repository is not runtime tool permission. Editing the source
  does not grant the running application anything.

## Explaining a decision

```bash
localai permissions explain --action request.json --json
```

Returns the requested action, matched rule, decision, whether confirmation is required,
risk classification, workspace boundary, resolved paths and the reason. Exit code 77
means denied.

The same request-construction code serves this command and real enforcement
(`ToolRunner.preview` and `ToolRunner.execute` share it), so the explanation cannot
drift from behaviour. `test_preview_matches_enforcement` asserts it.

## Rate limiting

`permissions.max_high_risk_per_minute` (default 20) caps destructive and higher actions
in a rolling minute — a backstop against a runaway loop, active even in bypass mode.

## Changing this engine

1. Read this document fully.
2. Never reorder stages 1–3.
3. Write the boundary test **before** the change.
4. `python tasks.py test-security`
5. `python tasks.py changed-security`
6. Note the security impact in the commit message.
