# Contributing

## Setup

```powershell
python tasks.py setup
python tasks.py check      # confirm a green baseline before changing anything
```

## The loop

1. `python tasks.py test` — establish that things pass *before* you start.
2. Make a small change.
3. `python tasks.py test-one <path>::<test>` — fast feedback.
4. `python tasks.py check` — the gate.
5. Update docs, `CHANGELOG.md`, `.project/status.json` if the change is significant.

## Before every commit

```powershell
python tasks.py secret-scan       # credentials, private data files
python tasks.py check             # format, lint, typecheck, test, docs
```

Install the hook once and both run automatically:

```powershell
python tasks.py hooks
```

## If you touch a security boundary

```powershell
python tasks.py changed-security  # tells you if you did
python tasks.py test-security
```

The boundaries are listed in [CODEOWNERS.md](CODEOWNERS.md). Changes there need extra
tests and an explicit note in the commit message about the security impact.

**If a test in `test_permission_boundaries.py` or `test_path_safety.py` fails, the test
is right.** Those tests encode the security contract. Do not adjust one to make a
change pass — change the code, or reconsider the change.

## Standards

Ruff and mypy are authoritative for style. What they cannot check:

- **Comments explain why, not what.** If a comment restates the code, delete it. If
  removing it makes the code unclear, rename something instead.
- **Names make the type annotation redundant.** A variable called `data` has failed.
- **The error case is part of the design.** Empty input, missing file, permission error,
  disconnected drive — those are the cases, not edge cases.
- **Error messages say what to do next.** Especially tool errors: the model reads them
  and acts on them. `"{path} does not exist. Use find_files to locate it."` beats
  `"not found"`.
- **A function that does two things is two functions.**

## Adding a tool

See [docs/tool-api.md](docs/tool-api.md). Briefly: subclass `Tool`, declare risk
honestly, implement `affected_paths` (a path the engine never sees is a path it cannot
contain), register it in `builtin.py`, document it in the table, add tests.

## Adding a dependency

Raise it first if it has security, privacy or maintenance implications. The runtime list
is deliberately short. Prefer stdlib, or forty lines of reviewed code, over a dependency
for something contained.

## Documentation

`python tasks.py validate-docs` fails if a tool, slash command or environment variable
is undocumented, or if a required document is missing. It runs as part of `check`,
because documentation that lies is worse than documentation that is absent — an agent
will act on it.

## Commits

Small and focused. Explain *why* in the body when it is not obvious. Mention
security-sensitive modules explicitly when you touch them.

```
Fix junction escape detection on nested workspaces

resolve_path picked the first matching workspace rather than the most
specific, so a file in an inner workspace was evaluated against the outer
one's write permission.

Security-sensitive: safety/pathsafe.py
```

## Do not

- Weaken a permission check to make a demo work.
- Claim a feature works without running it. `localai capabilities --json` must stay
  truthful.
- Edit a released migration — ship a new one.
- Hand-edit `schemas/*.json` — run `python tasks.py schemas`.
- Commit conversation databases, audit logs, drive indexes or anything from your own
  machine. `secret-scan` catches most of it; do not rely on that alone.
