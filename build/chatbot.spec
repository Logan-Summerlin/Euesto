from pathlib import Path


project_root = Path(SPECPATH).parent
icon = project_root / "assets" / "app.ico"

a = Analysis(
    [str(project_root / "app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(icon), "assets"),
        (str(project_root / "qml"), "qml"),
        (str(project_root / "docker"), "docker"),
        (str(project_root / "scripts" / "dev-up.ps1"), "scripts"),
        (str(project_root / "scripts" / "dev-down.ps1"), "scripts"),
        (str(project_root / "server"), "server"),
        (str(project_root / "executor"), "executor"),
        (str(project_root / "shared"), "shared"),
        (str(project_root / "requirements-gateway.txt"), "."),
    ],
    hiddenimports=["PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickControls2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalOpenRouterChat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(icon),
    version=str(project_root / "build" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LocalOpenRouterChat",
)
