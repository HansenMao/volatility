# PyInstaller spec for volkit.  Build on the platform you want to run on:
# PyInstaller bundles the host interpreter and host-compiled extension
# modules, so it cannot cross-compile.  A Windows .exe must be built on
# Windows -- see build_exe.py, which drives this file, or the GitHub Actions
# workflow which does it on a hosted Windows runner.
#
#   python build_exe.py            # preferred: checks, tests, build, staging
#   pyinstaller volkit.spec        # the build step on its own
#
# Layout is chosen by the VOLKIT_ONEFILE environment variable, which
# build_exe.py sets from its --onefile flag:
#   unset/0  one folder, dist/volkit/volkit.exe beside its libraries (default)
#   1        one file,   dist/volkit.exe, self-extracting

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ONEFILE = os.environ.get("VOLKIT_ONEFILE", "") not in ("", "0", "false", "no")

# Bundled, read-only resources.  The paths on the right must match what volkit
# expects at runtime, relative to paths.resource_dir(): STATIC_DIR is
# volkit/web.  Globbed rather than listed so a new page fragment cannot be
# added to the source tree and silently left out of the build.  volkit/data
# holds nothing in the source tree now -- events live on the workbook's own
# EVENTS sheet -- but the screens manifest is written into it below.
# SPECPATH, not the working directory: PyInstaller may be invoked from
# anywhere, and a relative glob would then quietly match nothing.
ROOT = Path(SPECPATH)
datas = [(str(p), "volkit/web") for p in (ROOT / "volkit/web").glob("*")
         if p.is_file() and not p.name.startswith(".")]

# A build may be made without some of the screens (build_exe.py --exclude-tab).
# It writes the manifest and names it here; volkit/screens.py is the only thing
# that reads it, and its absence means "every screen", which is what a plain
# "pyinstaller volkit.spec" produces.  The file is bundled, not staged: the set
# of screens is the build's decision, not something to edit beside the exe.
SCREENS_FILE = os.environ.get("VOLKIT_SCREENS_FILE", "").strip()
if SCREENS_FILE:
    manifest = Path(SCREENS_FILE)
    if not manifest.exists():
        raise SystemExit(f"volkit.spec: VOLKIT_SCREENS_FILE points at {manifest}, "
                         "which is not there")
    if manifest.name != "screens.txt":
        raise SystemExit(f"volkit.spec: the screens manifest must be named "
                         f"screens.txt, not {manifest.name}")
    datas += [(str(manifest), "volkit/data")]
    chosen = [ln for ln in manifest.read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    print("volkit.spec: building with screens -> " + ", ".join(chosen))

if not any(d[0].endswith("index.html") for d in datas):
    raise SystemExit("volkit.spec: volkit/web/index.html is missing -- "
                     "the built exe would serve an empty page")

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
# Every volkit module, including the ones reached only through a late import
# inside a CLI subcommand (listed, history, moments, analytics).
hiddenimports += collect_submodules("volkit")

a = Analysis(
    ["launcher.py"],
    pathex=[SPECPATH],
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

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="volkit",
        debug=False,
        strip=False,
        upx=False,
        console=True,      # keep the console: it prints the URL and any warnings
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="volkit",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False, upx=False,
        name="volkit",
    )
