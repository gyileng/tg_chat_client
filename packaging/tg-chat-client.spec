# Build on Windows x64; ship the entire output directory as a zip.
from pathlib import Path

project = Path(SPECPATH).parent
a = Analysis(
    [str(project / 'app.py')],
    pathex=[str(project)],
    binaries=[],
    datas=[],
    hiddenimports=['tzdata', 'python_socks.async_.asyncio'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='TGChatClient',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='TGChatClient')
