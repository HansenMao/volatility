# PyInstaller spec for volkit.  Build on the platform you want to run on:
# PyInstaller bundles the host interpreter and host-compiled extension
# modules, so it cannot cross-compile.  A Windows .exe must be built on
# Windows -- see build_windows.bat, or the GitHub Actions workflow which does
# it on a hosted Windows runner.
#
#   pyinstaller volkit.spec --noconfirm

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    # Bundled, read-only resources. The paths on the right must match what
    # volkit expects at runtime: STATIC_DIR is volkit/web, and the economic
    # calendar is read from volkit/data.
    ("volkit/web/index.html", "volkit/web"),
    ("volkit/data/econ_events.csv", "volkit/data"),
]

# Windows has no IANA time zone database. Cut times, the weekly market close
# and the whole economic calendar are resolved through zoneinfo, so the tzdata
# package has to travel with the build or none of them work.
try:
    datas += collect_data_files("tzdata")
except Exception:
    pass

hiddenimports = [
    "openpyxl", "openpyxl.cell._writer", "tzdata",
    "scipy.special._cdflib", "scipy._lib.array_api_compat.numpy.fft",
]
hiddenimports += collect_submodules("volkit")

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a GUI toolkit or plotting: the interface is a web page
    # served by the standard library. Excluding them keeps the build small.
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "jupyter", "notebook", "pytest", "sphinx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="volkit",
    debug=False,
    strip=False,
    upx=False,
    console=True,          # keep the console: it prints the URL and any warnings
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="volkit",
)
