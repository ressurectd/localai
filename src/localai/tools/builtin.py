"""Explicit registration of every built-in tool.

This function is the complete list of capabilities the model can reach. There is no
directory scanning and no import-time registration, so reviewing what a session can
do means reading one function.

Adding a tool: implement it in the appropriate module, add one line here, and add its
name to the table in docs/tool-api.md. ``tests/unit/test_builtin_tools.py`` asserts
that the registry, the documentation table and the JSON schema stay in step.
"""

from __future__ import annotations

from localai.tools import fs_read, fs_write, shell, system
from localai.tools.registry import ToolRegistry


def register_builtins(
    registry: ToolRegistry | None = None, *, include_mutating: bool = True
) -> ToolRegistry:
    """Populate and return a registry.

    ``include_mutating=False`` yields the read-only tool set used by investigation
    mode and by the default MCP profile. Filtering at registration rather than at
    call time means a read-only session cannot invoke a mutating tool even if the
    permissions engine were somehow misconfigured -- defence in depth.
    """
    registry = registry or ToolRegistry()

    # Read-only filesystem
    registry.register(fs_read.ListDirectory())
    registry.register(fs_read.FindFiles())
    registry.register(fs_read.SearchFileContents())
    registry.register(fs_read.ReadFile())
    registry.register(fs_read.InspectMetadata())
    registry.register(fs_read.FileHash())
    registry.register(fs_read.FindDuplicates())

    # Read-only system
    registry.register(system.SystemInfo())
    registry.register(system.DiskInfo())
    registry.register(system.ListProcesses())
    registry.register(system.GitCommand())

    if include_mutating:
        registry.register(fs_write.WriteFile())
        registry.register(fs_write.EditFile())
        registry.register(fs_write.CreateDirectory())
        registry.register(fs_write.MovePath())
        registry.register(fs_write.CopyPath())
        registry.register(fs_write.DeletePath())
        registry.register(shell.RunPowerShell())
        registry.register(shell.RunPython())

    return registry


#: Names of every built-in tool, in registration order. Used by tests and by
#: ``localai capabilities --json`` to detect drift between code and documentation.
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "list_directory",
    "find_files",
    "search_file_contents",
    "read_file",
    "inspect_metadata",
    "file_hash",
    "find_duplicates",
    "system_info",
    "disk_info",
    "list_processes",
    "git",
    "write_file",
    "edit_file",
    "create_directory",
    "move_path",
    "copy_path",
    "delete_path",
    "run_powershell",
    "run_python",
)

READ_ONLY_TOOL_NAMES: tuple[str, ...] = BUILTIN_TOOL_NAMES[:11]
