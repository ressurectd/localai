# PyInstaller build spec for the `ai` executable.
#
# Built as a ONE-FOLDER app, not one-file. A one-file build unpacks itself to a temp
# directory on every launch, which adds a second or two of startup and trips some
# corporate antivirus. The installer hides the folder anyway, so the only thing
# one-file would buy is a prettier `dist/` -- at the cost of the thing users notice.
#
# Console mode: this is a terminal application. `console=False` would detach it from
# the terminal and make it unusable.
#
# Build with:  python tasks.py exe

from PyInstaller.utils.hooks import collect_all, collect_data_files

# Textual ships .tcss stylesheets and Rich ships terminal data alongside their code.
# Neither is importable, so PyInstaller cannot infer them -- collect_all sweeps the
# package's data, binaries and hidden imports together.
textual_datas, textual_binaries, textual_hidden = collect_all("textual")
rich_datas, rich_binaries, rich_hidden = collect_all("rich")

datas = [
    # Our own non-Python resources. Without these the app starts and then fails at
    # first use: no stylesheet means an unstyled UI, no migrations means no database.
    ("src/localai/ui/theme.tcss", "localai/ui"),
    ("src/localai/storage/migrations/*.sql", "localai/storage/migrations"),
    ("schemas/*.json", "schemas"),
    *textual_datas,
    *rich_datas,
    *collect_data_files("pydantic"),
]

hiddenimports = [
    *textual_hidden,
    *rich_hidden,
    # Imported lazily inside functions, so static analysis misses them.
    "localai.providers.discovery",
    "localai.cli.doctor",
    "localai.ui.app",
    "localai.ui.screens",
    "localai.ui.widgets",
    "localai.ui.art",
    "localai.tools.builtin",
    "pydantic.deprecated.decorator",
]

a = Analysis(
    ["src/localai/__main__.py"],
    pathex=["src"],
    binaries=[*textual_binaries, *rich_binaries],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trimming the obvious dead weight. Nothing here is imported by localai; leaving
    # them in adds tens of megabytes to the installer for no benefit.
    excludes=[
        "tkinter", "matplotlib", "numpy", "scipy", "pandas", "PIL",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "mypy", "ruff",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries are a common false-positive for AV
    console=True,       # terminal application: must keep the console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ai",
)
