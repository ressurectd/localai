#!/usr/bin/env python
"""Task runner.

One obvious command interface for every development operation, working identically
in PowerShell, cmd and bash. A Makefile would need make installed; a justfile would
need just. This needs only the Python that already runs the project.

    python tasks.py <task> [args...]
    python tasks.py --list

Every task is also documented as a direct command in docs/development.md, so nothing
here is a black box.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
SRC = ROOT / "src"
TESTS = ROOT / "tests"

#: Modules where a change should trigger extra scrutiny. Reported by `changed-security`
#: and listed in docs/security-model.md. Keep in step with CODEOWNERS.md.
SECURITY_SENSITIVE = (
    "src/localai/permissions/engine.py",
    "src/localai/permissions/audit.py",
    "src/localai/safety/pathsafe.py",
    "src/localai/safety/sensitive.py",
    "src/localai/safety/injection.py",
    "src/localai/safety/recycle.py",
    "src/localai/tools/runner.py",
    "src/localai/tools/shell.py",
    "src/localai/tools/fs_write.py",
    "src/localai/config/paths.py",
)

TASKS: dict[str, Callable[[list[str]], int]] = {}


def task(
    name: str, help_text: str
) -> Callable[[Callable[[list[str]], int]], Callable[[list[str]], int]]:
    def register(fn: Callable[[list[str]], int]) -> Callable[[list[str]], int]:
        fn.__doc__ = help_text
        TASKS[name] = fn
        return fn

    return register


def python() -> str:
    """The interpreter to use: the project venv if present, else the current one.

    Preferring the venv means `python tasks.py test` works from a system shell
    without the user having to remember to activate anything first.
    """
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(candidate) if candidate.exists() else sys.executable


def run(*args: str, check: bool = True, **kwargs: object) -> int:
    """Run a command, echoing it so the underlying tool is never hidden."""
    printable = " ".join(str(a) for a in args)
    print(f"$ {printable}", flush=True)
    result = subprocess.run([str(a) for a in args], cwd=ROOT, **kwargs)  # type: ignore[call-overload]
    if check and result.returncode != 0:
        return result.returncode
    return 0


# --- environment ------------------------------------------------------------


@task("setup", "Create the virtualenv and install everything, including dev tools")
def setup(args: list[str]) -> int:
    venv = ROOT / ".venv"
    if not venv.exists():
        if code := run(sys.executable, "-m", "venv", str(venv)):
            return code
    if code := run(python(), "-m", "pip", "install", "--upgrade", "pip", "--quiet"):
        return code
    return run(python(), "-m", "pip", "install", "-e", ".[dev]", "--quiet")


@task("install", "Install the package in editable mode (no dev tools)")
def install(args: list[str]) -> int:
    return run(python(), "-m", "pip", "install", "-e", ".")


# --- running ----------------------------------------------------------------


@task("run", "Launch the interactive terminal UI")
def run_app(args: list[str]) -> int:
    return run(python(), "-m", "localai", *args)


@task("sandbox", "Launch against synthetic data with destructive actions disabled")
def sandbox(args: list[str]) -> int:
    return run(python(), "-m", "localai", "dev", "sandbox", *args)


@task("doctor", "Check the environment and report problems")
def doctor(args: list[str]) -> int:
    return run(python(), "-m", "localai", "doctor", *args, check=False)


# --- testing ----------------------------------------------------------------


@task("test", "Run the whole test suite")
def test(args: list[str]) -> int:
    return run(python(), "-m", "pytest", *(args or [str(TESTS)]))


@task("test-unit", "Run unit tests only")
def test_unit(args: list[str]) -> int:
    return run(python(), "-m", "pytest", str(TESTS / "unit"), *args)


@task("test-integration", "Run integration tests only")
def test_integration(args: list[str]) -> int:
    return run(python(), "-m", "pytest", str(TESTS / "integration"), *args)


@task("test-security", "Run only the permission and path-safety boundary tests")
def test_security(args: list[str]) -> int:
    return run(python(), "-m", "pytest", "-m", "security", "-v", *args)


@task("test-one", "Run a single test: python tasks.py test-one tests/unit/x.py::test_y")
def test_one(args: list[str]) -> int:
    if not args:
        print("usage: python tasks.py test-one <path>::<test_name>", file=sys.stderr)
        return 2
    return run(python(), "-m", "pytest", *args, "-v")


@task("coverage", "Run tests with a coverage report (installs coverage if needed)")
def coverage(args: list[str]) -> int:
    run(python(), "-m", "pip", "install", "coverage", "--quiet", check=False)
    if code := run(python(), "-m", "coverage", "run", "-m", "pytest", str(TESTS), check=False):
        return code
    return run(python(), "-m", "coverage", "report", "-m")


# --- quality ----------------------------------------------------------------


@task("lint", "Check for lint errors")
def lint(args: list[str]) -> int:
    return run(python(), "-m", "ruff", "check", str(SRC), str(TESTS), *args)


@task("format", "Reformat the code")
def format_code(args: list[str]) -> int:
    if code := run(python(), "-m", "ruff", "check", "--fix", str(SRC), str(TESTS), check=False):
        pass  # fixes applied; remaining findings are reported by `lint`
    return run(python(), "-m", "ruff", "format", str(SRC), str(TESTS))


@task("typecheck", "Run mypy (strict on security-sensitive modules)")
def typecheck(args: list[str]) -> int:
    return run(python(), "-m", "mypy", *args)


@task("check", "Everything a change must pass: format, lint, typecheck, test")
def check(args: list[str]) -> int:
    """The single gate. CI and the pre-commit hook both call this."""
    steps: list[tuple[str, list[str]]] = [
        ("format", [python(), "-m", "ruff", "format", "--check", str(SRC), str(TESTS)]),
        ("lint", [python(), "-m", "ruff", "check", str(SRC), str(TESTS)]),
        ("typecheck", [python(), "-m", "mypy"]),
        ("test", [python(), "-m", "pytest", str(TESTS), "-q"]),
        ("docs", [python(), str(ROOT / "tasks.py"), "validate-docs"]),
    ]
    failures: list[str] = []
    for name, command in steps:
        print(f"\n--- {name} ---", flush=True)
        if subprocess.run(command, cwd=ROOT).returncode != 0:
            failures.append(name)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


# --- database ---------------------------------------------------------------


@task("migrate", "Apply pending database migrations")
def migrate(args: list[str]) -> int:
    return run(python(), "-m", "localai", "migrations", "apply", *args)


@task("seed-test-data", "Populate a scratch database with synthetic conversations and usage")
def seed_test_data(args: list[str]) -> int:
    return run(python(), str(ROOT / "scripts" / "seed_test_data.py"), *args)


# --- artefacts --------------------------------------------------------------


@task("schemas", "Regenerate the JSON Schemas from the pydantic models")
def schemas(args: list[str]) -> int:
    return run(python(), str(ROOT / "scripts" / "generate_schemas.py"))


@task("build", "Build the wheel and sdist")
def build(args: list[str]) -> int:
    run(python(), "-m", "pip", "install", "build", "--quiet", check=False)
    return run(python(), "-m", "build")


@task("package", "Build and report the artefacts")
def package(args: list[str]) -> int:
    if code := build(args):
        return code
    for artefact in sorted((ROOT / "dist").glob("*")):
        print(f"  {artefact.name}  {artefact.stat().st_size / 1024:.0f} KB")
    return 0


@task("exe", "Build the standalone ai.exe (no Python needed to run it)")
def build_exe(args: list[str]) -> int:
    """Freeze the app with PyInstaller.

    One-folder, not one-file: a one-file build unpacks to a temp directory on every
    launch, which costs a second or two of startup and trips some corporate
    antivirus. The installer hides the folder, so one-file would only buy a tidier
    dist/ at the cost of the thing users actually notice.
    """
    run(python(), "-m", "pip", "install", "pyinstaller", "--quiet", check=False)
    if code := run(python(), "-m", "PyInstaller", "--clean", "--noconfirm", "localai.spec"):
        return code

    built = ROOT / "dist" / "ai" / ("ai.exe" if os.name == "nt" else "ai")
    if not built.exists():
        print(f"expected {built} but it was not produced", file=sys.stderr)
        return 1

    # A build that cannot answer --version is not a build. Frozen apps fail at
    # runtime over missing data files, which no amount of successful packaging
    # catches, so smoke-test before declaring victory.
    print(f"\n{built}  ({_folder_size(built.parent) / 1024 / 1024:.0f} MB)")
    print("smoke test: --version")
    return run(str(built), "--version", check=True)


def _folder_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def _inno_compiler() -> Path | None:
    """Locate ISCC.exe, Inno Setup's command-line compiler."""
    candidates: list[Path] = []
    if found := shutil.which("iscc"):
        candidates.append(Path(found))
    # winget installs per-user by default, which is neither Program Files location.
    for base in (
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
    ):
        if base:
            candidates.append(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe")
            candidates.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
    return next((c for c in candidates if c.exists()), None)


@task("installer", "Build the Windows setup .exe (requires Inno Setup)")
def build_installer(args: list[str]) -> int:
    """Wrap dist/ai into the standard Inno Setup wizard."""
    if not (ROOT / "dist" / "ai").exists():
        print("dist/ai is missing; building it first.\n")
        if code := build_exe([]):
            return code

    compiler = _inno_compiler()
    if compiler is None:
        print("Inno Setup is not installed.", file=sys.stderr)
        print("", file=sys.stderr)
        print("  winget install JRSoftware.InnoSetup", file=sys.stderr)
        print("  or download from https://jrsoftware.org/isdl.php", file=sys.stderr)
        print("", file=sys.stderr)
        print("Then re-run: python tasks.py installer", file=sys.stderr)
        return 78  # EX_CONFIG

    if code := run(str(compiler), str(ROOT / "installer" / "ai.iss")):
        return code

    out = ROOT / "dist" / "installer"
    for artefact in sorted(out.glob("*.exe")):
        print(f"\n  {artefact}  ({artefact.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


@task("release", "Full release build: check, exe, installer")
def release(args: list[str]) -> int:
    """Everything a release needs, in the order that fails fastest."""
    for name, fn in (("check", check), ("exe", build_exe), ("installer", build_installer)):
        print(f"\n=== {name} ===")
        if code := fn([]):
            print(f"\nrelease aborted at: {name}", file=sys.stderr)
            return code
    print("\nRelease artefacts are in dist/installer.")
    return 0


@task("clean", "Remove build artefacts and caches (never touches user data)")
def clean(args: list[str]) -> int:
    removed = 0
    for pattern in (
        "build",
        "dist",
        "*.egg-info",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        "htmlcov",
    ):
        for path in ROOT.glob(pattern):
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(
                missing_ok=True
            )
            removed += 1
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
        removed += 1
    print(f"Removed {removed} artefact(s). User data in the localai home directory is untouched.")
    return 0


# --- agent-facing -----------------------------------------------------------


@task("validate-docs", "Check that agent-facing documentation matches the code")
def validate_docs(args: list[str]) -> int:
    """Catch documentation drift that a human reviewer would miss.

    Documentation that lies is worse than documentation that is absent, because an
    agent will act on it. This is why `check` runs it.
    """
    import json

    problems: list[str] = []

    required = [
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "HANDOFF.md",
        "CODEOWNERS.md",
        ".project/status.json",
        "docs/architecture.md",
        "docs/development.md",
        "docs/testing.md",
        "docs/security-model.md",
        "docs/tool-api.md",
        "docs/permissions-engine.md",
        "docs/database-schema.md",
        "docs/release-process.md",
        "docs/installation.md",
        "docs/user-guide.md",
        "docs/troubleshooting.md",
        "docs/privacy.md",
        "docs/training.md",
    ]
    for name in required:
        if not (ROOT / name).exists():
            problems.append(f"missing required document: {name}")

    # Every registered tool must appear in the tool-api table.
    sys.path.insert(0, str(SRC))
    try:
        from localai.tools.builtin import BUILTIN_TOOL_NAMES, register_builtins

        registry = register_builtins()
        if set(registry.names()) != set(BUILTIN_TOOL_NAMES):
            problems.append(
                "BUILTIN_TOOL_NAMES disagrees with the registry: "
                f"{set(registry.names()) ^ set(BUILTIN_TOOL_NAMES)}"
            )
        tool_doc = ROOT / "docs" / "tool-api.md"
        if tool_doc.exists():
            text = tool_doc.read_text(encoding="utf-8")
            for name in registry.names():
                if f"`{name}`" not in text:
                    problems.append(f"docs/tool-api.md does not document the tool `{name}`")

        # Every slash command must appear in the user guide.
        from localai.ui.commands import COMMANDS

        guide = ROOT / "docs" / "user-guide.md"
        if guide.exists():
            text = guide.read_text(encoding="utf-8")
            for command in COMMANDS.all():
                if f"/{command.name}" not in text:
                    problems.append(f"docs/user-guide.md does not document /{command.name}")

        # Every documented environment variable must exist in ENV_OVERRIDES.
        from localai.config.manager import ENV_OVERRIDES

        dev = ROOT / "docs" / "development.md"
        if dev.exists():
            text = dev.read_text(encoding="utf-8")
            for variable in ENV_OVERRIDES:
                if variable not in text:
                    problems.append(f"docs/development.md does not document {variable}")
    except ImportError as exc:
        problems.append(f"could not import localai to cross-check docs: {exc}")

    status = ROOT / ".project" / "status.json"
    if status.exists():
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
            for key in ("phase", "implemented", "partial", "planned", "tests", "schema_versions"):
                if key not in data:
                    problems.append(f".project/status.json is missing '{key}'")
        except json.JSONDecodeError as exc:
            problems.append(f".project/status.json is not valid JSON: {exc}")

    if problems:
        print(f"{len(problems)} documentation problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Agent-facing documentation is consistent with the code.")
    return 0


@task("changed-security", "List changed files that touch a security boundary")
def changed_security(args: list[str]) -> int:
    base = args[0] if args else "HEAD"
    result = subprocess.run(
        ["git", "diff", "--name-only", base], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        print("not a git repository, or git is unavailable", file=sys.stderr)
        return 0

    changed = {
        line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()
    }
    hits = sorted(changed & set(SECURITY_SENSITIVE))
    if not hits:
        print("No security-sensitive files changed.")
        return 0

    print("=" * 68)
    print("WARNING: this change touches security-sensitive modules:")
    for path in hits:
        print(f"  - {path}")
    print("\nBefore committing:")
    print("  python tasks.py test-security     (permission and path-safety boundaries)")
    print("  python tasks.py check")
    print("  Re-read docs/security-model.md and confirm the invariants still hold.")
    print("=" * 68)
    return 0


@task("secret-scan", "Check the working tree for likely secrets or private data")
def secret_scan(args: list[str]) -> int:
    """A pre-commit guard against committing the things .gitignore might miss."""
    import re

    patterns = [
        (
            re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"][^'\"]{12,}"),
            "possible hardcoded credential",
        ),
        (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
        (re.compile(r"(?i)\bsk-[a-z0-9]{20,}"), "API key"),
        (re.compile(r"(?i)\bghp_[a-z0-9]{30,}"), "GitHub token"),
    ]
    forbidden_names = ("localai.db", "audit.jsonl", ".env", "conversations.json")
    problems: list[str] = []

    result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    files = (
        [ROOT / f for f in result.stdout.splitlines() if f.strip()]
        if result.returncode == 0
        else list(ROOT.rglob("*"))
    )

    for path in files:
        if not path.is_file() or ".venv" in path.parts or ".git" in path.parts:
            continue
        if path.name in forbidden_names:
            problems.append(f"{path.relative_to(ROOT)}: private data file must not be committed")
            continue
        if path.suffix.lower() in {".png", ".jpg", ".gguf", ".bin", ".zip", ".pdf"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # tasks.py itself contains these patterns as detection rules.
        if path.name == "tasks.py":
            continue
        for pattern, label in patterns:
            if pattern.search(text):
                problems.append(f"{path.relative_to(ROOT)}: {label}")

    if problems:
        print(f"{len(problems)} potential problem(s) found:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nReview these before committing.", file=sys.stderr)
        return 1
    print("No secrets or private data files detected.")
    return 0


@task("hooks", "Install the pre-commit hook")
def hooks(args: list[str]) -> int:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if not hook.parent.exists():
        print("not a git repository; run 'git init' first", file=sys.stderr)
        return 1
    hook.write_text(
        "#!/bin/sh\n"
        "# Installed by: python tasks.py hooks\n"
        'exec python "$(git rev-parse --show-toplevel)/tasks.py" pre-commit\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        hook.chmod(0o755)
    print(f"Installed {hook}")
    return 0


@task("pre-commit", "The pre-commit gate: secret scan, security warning, then check")
def pre_commit(args: list[str]) -> int:
    if code := secret_scan([]):
        return code
    changed_security([])
    return check([])


# --- dispatch ---------------------------------------------------------------


def usage() -> int:
    print(__doc__)
    print("Tasks:\n")
    width = max(len(name) for name in TASKS)
    for name, fn in TASKS.items():
        print(f"  {name:<{width}}  {fn.__doc__}")
    print("\nEvery task is also documented as a direct command in docs/development.md.")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "--list", "help"}:
        return usage()
    name, *rest = argv
    handler = TASKS.get(name)
    if handler is None:
        print(f"unknown task: {name}\n", file=sys.stderr)
        close = [t for t in TASKS if t.startswith(name[:3])]
        if close:
            print(f"did you mean: {', '.join(close)}?\n", file=sys.stderr)
        return usage() or 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
