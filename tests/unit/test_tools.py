"""Tool behaviour: reads, writes, bounds, dry-run, backups and error messages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from localai.config.models import Config
from localai.errors import ToolNotFoundError, ToolValidationError
from localai.tools.base import ToolContext
from localai.tools.builtin import BUILTIN_TOOL_NAMES, register_builtins
from localai.tools.registry import ToolRegistry

# --- registry ---------------------------------------------------------------


def test_every_documented_builtin_is_registered(registry: ToolRegistry) -> None:
    assert set(registry.names()) == set(BUILTIN_TOOL_NAMES)


def test_read_only_registry_excludes_mutating_tools() -> None:
    """Investigation mode must not even be able to name a mutating tool."""
    read_only = register_builtins(include_mutating=False)
    assert not any(t.mutating for t in read_only)
    assert "delete_path" not in read_only
    assert "run_powershell" not in read_only


def test_registering_a_duplicate_name_is_refused(registry: ToolRegistry) -> None:
    """A plugin must not silently shadow a built-in with a laxer implementation."""
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.get("read_file"))


def test_unknown_tool_suggests_a_close_match(registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotFoundError) as info:
        registry.get("read_fil")
    # The suggestion lives in `remediation` so the runner can fold it into the
    # message the model sees; see ToolRunner.execute.
    assert "read_file" in info.value.remediation


def test_every_tool_has_a_description_and_schema(registry: ToolRegistry) -> None:
    for tool in registry:
        assert tool.description.strip(), f"{tool.name} has no description"
        assert tool.parameters.get("type") == "object", f"{tool.name} has a malformed schema"
        for name, spec in tool.parameters.get("properties", {}).items():
            assert "type" in spec, f"{tool.name}.{name} has no type"


def test_path_taking_tools_declare_their_paths(registry: ToolRegistry) -> None:
    """A path the engine never sees is a path it cannot contain."""
    for tool in registry:
        properties = tool.parameters.get("properties", {})
        path_args = {k for k in ("path", "root", "source", "destination") if k in properties}
        if not path_args:
            continue
        sample = dict.fromkeys(path_args, "C:/probe/value")
        declared = {p.as_posix().lower() for p in tool.affected_paths(sample)}
        assert declared, f"{tool.name} accepts {path_args} but declares no affected_paths"


def test_describe_call_never_returns_empty(registry: ToolRegistry) -> None:
    """The confirmation prompt depends on this string being meaningful."""
    for tool in registry:
        assert tool.describe_call({"path": "x", "command": "y", "query": "z"}).strip()


# --- argument validation ----------------------------------------------------


def test_missing_required_argument_is_reported_usefully(registry: ToolRegistry) -> None:
    with pytest.raises(ToolValidationError) as info:
        registry.validate_arguments(registry.get("read_file"), {})
    message = str(info.value)
    assert "path" in message and "Expected" in message


def test_numeric_strings_are_coerced(registry: ToolRegistry) -> None:
    """Small models routinely send "5" where 5 was expected."""
    validated = registry.validate_arguments(
        registry.get("read_file"), {"path": "x.txt", "max_lines": "50"}
    )
    assert validated["max_lines"] == 50


def test_booleans_are_not_accepted_as_integers(registry: ToolRegistry) -> None:
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(registry.get("read_file"), {"path": "x.txt", "max_lines": True})


def test_defaults_are_applied(registry: ToolRegistry) -> None:
    validated = registry.validate_arguments(registry.get("read_file"), {"path": "x.txt"})
    assert validated["start_line"] == 1
    assert validated["line_numbers"] is True


def test_unknown_arguments_are_dropped_not_rejected(registry: ToolRegistry) -> None:
    """An invented option should not fail an otherwise valid call."""
    validated = registry.validate_arguments(
        registry.get("read_file"), {"path": "x.txt", "encoding": "utf-8", "invented": 1}
    )
    assert "invented" not in validated
    assert validated["path"] == "x.txt"


def test_enum_violation_is_rejected(registry: ToolRegistry) -> None:
    with pytest.raises(ToolValidationError, match="must be one of"):
        registry.validate_arguments(registry.get("file_hash"), {"path": "x", "algorithm": "rot13"})


def test_range_violation_is_rejected(registry: ToolRegistry) -> None:
    with pytest.raises(ToolValidationError, match="at most"):
        registry.validate_arguments(registry.get("list_directory"), {"path": "x", "limit": 999_999})


# --- read tools -------------------------------------------------------------


async def test_read_file_returns_content_with_line_numbers(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("read_file").run({"path": "readme.txt"}, context)
    assert result.ok
    assert "Project Aurora" in result.content
    assert "48000" in result.content
    assert result.metadata["total_lines"] == 3


async def test_read_file_paginates_large_files(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    (workspace / "big.txt").write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    result = await registry.get("read_file").run(
        {"path": "big.txt", "start_line": 10, "max_lines": 5}, context
    )
    assert "line 9" in result.content  # 1-indexed: line 10 holds "line 9"
    assert result.metadata["returned_lines"] == 5
    assert result.metadata["has_more"]
    assert "start_line=15" in result.content


async def test_read_file_refuses_binary(registry: ToolRegistry, context: ToolContext) -> None:
    result = await registry.get("read_file").run({"path": "image.bin"}, context)
    assert not result.ok
    assert "binary" in result.error.lower()


async def test_read_file_missing_gives_actionable_error(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("read_file").run({"path": "nope.txt"}, context)
    assert not result.ok
    assert "find_files" in result.error  # tells the model what to do next


async def test_read_file_on_a_directory_redirects(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("read_file").run({"path": "docs"}, context)
    assert not result.ok
    assert "list_directory" in result.error


async def test_read_file_preserves_unicode(registry: ToolRegistry, context: ToolContext) -> None:
    result = await registry.get("read_file").run({"path": "docs/rapport-café-naïve.txt"}, context)
    assert result.ok
    assert "ünïcödé" in result.content
    assert "✓" in result.content


async def test_list_directory_separates_files_and_dirs(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("list_directory").run({"path": "."}, context)
    assert result.ok
    assert result.metadata["directories"] >= 3
    assert result.metadata["files"] >= 2


async def test_list_directory_filters_by_pattern(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("list_directory").run({"path": ".", "pattern": "*.txt"}, context)
    assert all(
        line.endswith(".txt") or ":" in line or not line.startswith("  ")
        for line in result.content.splitlines()[1:]
    )


async def test_find_files_searches_recursively(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("find_files").run({"pattern": "*.txt", "root": "."}, context)
    assert result.ok
    assert result.metadata["matches"] >= 3
    assert any("buried.txt" in p for p in result.metadata["paths"])


async def test_find_files_respects_max_results(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("find_files").run(
        {"pattern": "*", "root": ".", "max_results": 2}, context
    )
    assert result.metadata["matches"] <= 2


async def test_find_files_reports_no_matches_clearly(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("find_files").run({"pattern": "*.nothing", "root": "."}, context)
    assert result.ok  # not an error: "nothing found" is a valid answer
    assert result.metadata["matches"] == 0


async def test_search_file_contents_finds_matches(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("search_file_contents").run(
        {"query": "budget", "root": ".", "ignore_case": True}, context
    )
    assert result.ok
    assert result.metadata["matches"] >= 2
    assert "readme.txt" in result.content


async def test_search_file_contents_rejects_a_bad_regex(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("search_file_contents").run(
        {"query": "unclosed(group", "root": "."}, context
    )
    assert not result.ok
    assert "Escape special characters" in result.error


async def test_search_skips_binary_files(registry: ToolRegistry, context: ToolContext) -> None:
    result = await registry.get("search_file_contents").run(
        {"query": ".", "root": ".", "file_pattern": "*.bin"}, context
    )
    assert result.metadata.get("files_skipped", 0) >= 1


async def test_file_hash_is_stable(registry: ToolRegistry, context: ToolContext) -> None:
    first = await registry.get("file_hash").run({"path": "readme.txt"}, context)
    second = await registry.get("file_hash").run({"path": "readme.txt"}, context)
    assert first.metadata["hash"] == second.metadata["hash"]
    assert len(first.metadata["hash"]) == 64  # sha256 hex


async def test_find_duplicates_detects_the_pair(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("find_duplicates").run({"root": "."}, context)
    assert result.ok
    assert result.metadata["groups"] >= 1
    assert "copy-a.txt" in result.content
    assert "nothing has been deleted" in result.content.lower()


async def test_inspect_metadata_reports_type_and_size(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("inspect_metadata").run({"path": "readme.txt"}, context)
    assert result.metadata["type"] == "file"
    assert result.metadata["size_bytes"] > 0


# --- write tools ------------------------------------------------------------


async def test_write_file_creates_and_reports_a_diff(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    result = await registry.get("write_file").run(
        {"path": "created.txt", "content": "hello\nworld\n"}, context
    )
    assert result.ok
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "hello\nworld\n"
    assert result.metadata["created"] is True
    assert result.metadata["added"] == 2


async def test_write_file_backs_up_before_overwriting(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    result = await registry.get("write_file").run(
        {"path": "readme.txt", "content": "replaced\n"}, context
    )
    assert result.ok
    backup = Path(result.metadata["backup"])
    assert backup.exists()
    assert "Project Aurora" in backup.read_text(encoding="utf-8")


async def test_dry_run_changes_nothing(
    registry: ToolRegistry, config: Config, context: ToolContext, workspace: Path
) -> None:
    context.dry_run = True
    before = (workspace / "readme.txt").read_text(encoding="utf-8")
    result = await registry.get("write_file").run(
        {"path": "readme.txt", "content": "destroyed"}, context
    )
    assert result.ok
    assert "dry_run" in result.flags
    assert (workspace / "readme.txt").read_text(encoding="utf-8") == before


async def test_edit_file_replaces_exact_text(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    result = await registry.get("edit_file").run(
        {"path": "readme.txt", "old_text": "48000", "new_text": "52000"}, context
    )
    assert result.ok
    assert "52000" in (workspace / "readme.txt").read_text(encoding="utf-8")


async def test_edit_file_refuses_an_ambiguous_match(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    """Editing the wrong one of several matches is worse than refusing."""
    (workspace / "repeat.txt").write_text("TODO\nkeep\nTODO\n", encoding="utf-8")
    result = await registry.get("edit_file").run(
        {"path": "repeat.txt", "old_text": "TODO", "new_text": "DONE"}, context
    )
    assert not result.ok
    assert "appears 2 times" in result.error
    assert (workspace / "repeat.txt").read_text(encoding="utf-8").count("TODO") == 2


async def test_edit_file_replace_all_works(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    (workspace / "repeat.txt").write_text("TODO\nkeep\nTODO\n", encoding="utf-8")
    result = await registry.get("edit_file").run(
        {"path": "repeat.txt", "old_text": "TODO", "new_text": "DONE", "replace_all": True},
        context,
    )
    assert result.ok
    assert "TODO" not in (workspace / "repeat.txt").read_text(encoding="utf-8")


async def test_edit_file_missing_text_explains_how_to_fix(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("edit_file").run(
        {"path": "readme.txt", "old_text": "not present", "new_text": "x"}, context
    )
    assert not result.ok
    assert "Read the file first" in result.error


async def test_move_refuses_to_overwrite_by_default(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    result = await registry.get("move_path").run(
        {"source": "readme.txt", "destination": "copy-a.txt"}, context
    )
    assert not result.ok
    assert "overwrite" in result.error


async def test_move_refuses_to_move_a_directory_into_itself(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("move_path").run(
        {"source": "docs", "destination": "docs/nested"}, context
    )
    assert not result.ok
    assert "into itself" in result.error


async def test_delete_refuses_a_non_empty_directory_without_recursive(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("delete_path").run({"path": "docs"}, context)
    assert not result.ok
    assert "recursive" in result.error


async def test_delete_refuses_the_home_directory(
    registry: ToolRegistry, context: ToolContext
) -> None:
    result = await registry.get("delete_path").run(
        {"path": str(Path.home()), "recursive": True}, context
    )
    assert not result.ok
    assert "home directory" in result.error or "drive root" in result.error


async def test_create_directory_is_idempotent(registry: ToolRegistry, context: ToolContext) -> None:
    first = await registry.get("create_directory").run({"path": "newdir"}, context)
    second = await registry.get("create_directory").run({"path": "newdir"}, context)
    assert first.metadata["created"] is True
    assert second.metadata["created"] is False


# --- system tools -----------------------------------------------------------


async def test_system_info_reports_platform(registry: ToolRegistry, context: ToolContext) -> None:
    result = await registry.get("system_info").run({}, context)
    assert result.ok
    assert result.metadata["python"]
    assert result.metadata["cpu_count"]


async def test_disk_info_handles_unavailable_drives(
    registry: ToolRegistry, context: ToolContext
) -> None:
    """Scanning A: through Z: must not raise on empty drive letters."""
    result = await registry.get("disk_info").run({}, context)
    assert result.ok
    assert isinstance(result.metadata["drives"], list)


# --- serialisation ----------------------------------------------------------


def test_tool_definitions_serialise_to_json(registry: ToolRegistry) -> None:
    """``localai tools list --json`` must never fail on an unserialisable field."""
    payload = json.dumps(registry.describe_all())
    assert len(json.loads(payload)) == len(BUILTIN_TOOL_NAMES)


def test_ollama_schemas_have_the_expected_shape(registry: ToolRegistry) -> None:
    for schema in registry.ollama_schemas():
        assert schema["type"] == "function"
        assert schema["function"]["name"]
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"
