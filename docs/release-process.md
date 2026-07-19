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

## Building the release artefacts

```powershell
python tasks.py release      # check, then exe, then installer
```

Or the steps individually:

```powershell
python tasks.py exe          # dist\ai\ai.exe        standalone, ~39 MB
python tasks.py installer    # dist\installer\...    setup wizard, ~19.5 MB
```

### `ai.exe` — the application

PyInstaller, **one-folder** rather than one-file. A one-file build unpacks itself to
a temp directory on every launch, costing a second or two of startup and tripping
some corporate antivirus. The installer hides the folder anyway, so one-file would
only buy a tidier `dist/` at the cost of the thing users notice.

Built with `console=True`: this is a terminal application, and a windowed build would
detach it from the console and be unusable.

The spec collects data files PyInstaller cannot infer — Textual's stylesheets, Rich's
terminal data, our own `theme.tcss` and `migrations/*.sql`. Miss any of them and the
exe builds cleanly, then fails at first use. `tasks.py exe` therefore smoke-tests
`--version` before reporting success.

### `ai-setup-<version>.exe` — the installer

Inno Setup, deliberately the stock wizard: no custom pages, no bitmaps, no animation.
It is what most Windows installers look like, users recognise it instantly, and every
unusual thing an installer does is another thing that can fail on someone else's
machine.

Requires Inno Setup 6:

```powershell
winget install JRSoftware.InnoSetup
```

Behaviour worth knowing:

- **Per-user install** (`PrivilegesRequired=lowest`) — no UAC prompt, no admin
  rights. The state directory is per-user anyway, so machine-wide would be
  inconsistent as well as intrusive.
- **PATH is opt-in**, user scope only, and removed cleanly on uninstall. Both the add
  and remove paths are idempotent and match on exact entries, so a directory whose
  name merely contains ours is never corrupted.
- **`AppId` must never change.** It is how Windows recognises an upgrade rather than
  a second parallel installation.
- **Uninstall asks before deleting your data**, defaults to keeping it, and names the
  directory so "No" is an informed choice. A *silent* uninstall never prompts and
  never deletes — there is nobody to answer, and destroying someone's conversation
  history because a deployment script ran with `/VERYSILENT` would be indefensible.

### Verifying a build

Do not trust a successful compile. Install it and run it:

```powershell
$dir = "$env:TEMP\ai-test"
Start-Process dist\installer\ai-setup-0.1.0.exe -Wait -ArgumentList `
  '/VERYSILENT','/SUPPRESSMSGBOXES',"/DIR=$dir",'/MERGETASKS=!addtopath,!desktopicon'
& "$dir\ai.exe" doctor
& "$dir\unins000.exe" /VERYSILENT
```

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
