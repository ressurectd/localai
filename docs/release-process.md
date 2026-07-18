# Release process

## Versioning

Each public interface is versioned independently in `src/localai/version.py`, so a
change to one does not force a bump of the others. Consumers should branch on the
specific schema they use, not on `APP_VERSION`.

| Constant | Covers |
|---|---|
| `APP_VERSION` | The application. Semantic versioning. |
| `CLI_SCHEMA_VERSION` | Shape of `--json` output. |
| `CONFIG_SCHEMA_VERSION` | `config.toml` format. |
| `TOOL_API_VERSION` | Tool definition and result contract. |
| `PERMISSION_SCHEMA_VERSION` | Permission rule format. |
| `PLUGIN_MANIFEST_VERSION` | Plugin manifest format. |
| `LOCAL_API_VERSION` | Local HTTP API (Phase 4). |

`localai version --json` reports all of them.

## What counts as breaking

**Breaking** — needs a major bump, a migration note and a compatibility shim where
reasonable:

- Removing or retyping a field in `--json` output
- Renaming or removing a tool, or narrowing its accepted arguments
- Changing the meaning of a permission mode or the evaluation order
- Removing a configuration key
- A migration that discards data

**Not breaking:**

- Adding a field to JSON output
- Adding a tool or an optional argument
- Adding a configuration key with a default
- Changing prose, formatting or colour

Golden tests in `tests/integration/test_cli_contracts.py` make accidental breakage fail
CI rather than reaching a user.

## Steps

1. `python tasks.py check`
2. `python tasks.py test-security`
3. `python tasks.py validate-docs`
4. Update `CHANGELOG.md` — real entries, not "various fixes".
5. Bump the relevant constants in `version.py`.
6. `python tasks.py schemas` if any model changed; commit the result.
7. Update `.project/status.json` and `HANDOFF.md`.
8. `python tasks.py secret-scan`
9. `python tasks.py package`
10. Install the built wheel into a clean venv and run `localai doctor`.
11. Tag: `git tag -a v0.1.0 -m "..."`

## Deprecation

Never break silently. When something must change:

1. Add the replacement.
2. Keep the old form working, emitting a warning to **stderr** (never stdout — it would
   corrupt JSON output).
3. Document it in `CHANGELOG.md` under `Deprecated`, with the replacement and the
   earliest version that may remove it.
4. Update examples and schemas.
5. Remove no earlier than the next major version.

## Migration compatibility

Migrations are forward-only. A newer database opened by an older localai is not
supported and reports a version mismatch rather than corrupting anything.

A release containing a migration must say so in the changelog, and the migration must
have a test.
