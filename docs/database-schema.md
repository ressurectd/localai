# Database schema

One SQLite database: `%LOCALAPPDATA%\localai\data\localai.db`. Only `storage/db.py`
opens it.

`localai migrations status --json` for the live state.

## Conventions

- **Text ids** generated in Python (`conv_`, `msg_`, `use_`, `aud_` prefixes), so
  records can be created and cross-referenced before any INSERT.
- **Timestamps** are `REAL` Unix epoch seconds, UTC. Local-time grouping happens in
  Python, where the user's timezone is known.
- **Token counts carry provenance.** Every usage row records whether the figure was
  reported by Ollama or estimated by us.
- WAL journal mode, foreign keys ON, `synchronous = NORMAL`.

## Tables

### `conversations`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `conv_<hex>` |
| `title`, `created_at`, `updated_at` | | |
| `workspace`, `model`, `system_prompt` | | |
| `parent_id` | TEXT FK | Set on a fork. |
| `forked_from_seq` | INTEGER | Message it branched from. |
| `archived`, `metadata_json` | | |

Forking records provenance rather than hiding it: the fork keeps a pointer to its
parent and the branch point.

### `messages`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `conversation_id` | TEXT FK | `ON DELETE CASCADE` |
| `seq` | INTEGER | `UNIQUE (conversation_id, seq)` |
| `role` | TEXT | CHECK: system/user/assistant/tool |
| `content`, `thinking` | TEXT | |
| `tool_calls_json` | TEXT | |
| `tool_call_id`, `tool_name` | TEXT | Links a result to its request. |
| `token_estimate` | INTEGER | Always an estimate; exact counts live in `usage_records`. |

Written incrementally during a turn, so a crash loses at most the partial assistant
message.

### `usage_records`

The honesty-critical table.

| Column | Notes |
|---|---|
| `prompt_tokens`, `completion_tokens` | Exact when `token_source = 'reported'`. |
| `thinking_tokens` | **Always an estimate.** Ollama counts reasoning inside `eval_count` and never reports it separately. |
| `token_source` | CHECK: `reported` / `estimated` / `unknown` |
| `thinking_token_source` | Same domain. |
| `total_duration_ns`, `load_duration_ns`, `prompt_eval_duration_ns`, `eval_duration_ns` | From Ollama. |
| `tokens_per_second` | Derived. NULL when no duration was reported. |
| `energy_wh_estimate` | Assumed watts x wall time. Labelled an estimate at every call site. |

Aggregates degrade to the weakest source present: one unreported generation makes the
whole total inexact, and it is displayed with `~` or `?` rather than as a clean number.

### `audit_log`

Every permission decision and tool execution. Mirrored to `logs/audit.jsonl`.

| Column | Notes |
|---|---|
| `interface`, `client_id` | Which entry point, and which caller. |
| `tool`, `effect`, `stage`, `risk`, `reason` | The full decision. |
| `outcome` | pending / executed / denied / cancelled / failed / dry_run |
| `matched_rule`, `command`, `paths_json`, `arguments_json` | Arguments are redacted for secret-looking keys and truncated at 2000 chars. |
| `sensitive_json`, `injection_json` | Classifications that applied. |
| `confirmed_by_user` | NULL when no confirmation was required. |

### `permission_rules`

Rules created interactively ("always allow this command pattern"), so they survive a
restart without the UI rewriting your `config.toml`. The file remains authoritative for
rules you wrote yourself.

### `memories`

The table exists; the feature ships in Phase 3. Nothing is ever written without an
explicit user action — there is no implicit memory capture anywhere in the codebase.

### `messages_fts`

FTS5 virtual table with triggers keeping it in step with `messages`. The triggers are
SQL rather than Python so the index cannot drift when a write happens through any other
code path.

If the SQLite build lacks FTS5, migration 002 is skipped with a recorded warning and
search falls back to `LIKE`. `localai doctor` reports which engine is in use — it is
never a silent downgrade.

## Migrations

`src/localai/storage/migrations/NNN_description.sql`, applied in order, recorded in
`schema_migrations`.

| Version | Name | Contents |
|---|---|---|
| 001 | `initial` | conversations, messages, usage_records, audit_log, permission_rules, memories |
| 002 | `message_search` | FTS5 index and triggers |

Rules:

- Never edit a released migration. Ship a correction as a new file.
- Numbering must be contiguous from 001; the loader rejects gaps.
- **Transaction control goes inside the script.** `sqlite3.executescript` commits any
  open transaction before running, so an outer `BEGIN`/`COMMIT` has nothing to close.
  The runner wraps each migration in `BEGIN; ... COMMIT;` for this reason — which also
  handles the semicolons inside `CREATE TRIGGER` bodies that naive statement-splitting
  would break.
- A failed migration rolls back completely.
  `test_failed_migration_leaves_the_database_unchanged` uses a script whose first
  statement is valid and second is not, and asserts nothing is left behind.

## Query construction

Some queries interpolate a `WHERE` fragment. In every case the fragment is assembled
from a hard-coded table of literals in the same function, and **every value is bound as
a parameter**. No caller-supplied text ever reaches SQL. These sites carry a documented
`# noqa: S608`.
