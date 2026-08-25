#!/usr/bin/env python3
"""Export volkit as a standalone Windows executable.

    python build_exe.py                 one folder:  dist/volkit/volkit.exe
    python build_exe.py --onefile       one file:    dist/volkit.exe
    python build_exe.py --zip           also produce a zip to hand over
    python build_exe.py --host-check    build for THIS machine, to test the spec
    python build_exe.py --exclude-tab mm --exclude-tab listed
                                        build without those screens
    python build_exe.py --only-tabs pricing,marking
                                        the same thing, said the other way
    python build_exe.py --hidden-tab mm build WITH the screen, but off until
                                        volkit.exe --enable-tab mm

"Standalone" means the target machine needs no Python and no pip: the
interpreter, numpy/scipy/pandas/openpyxl and the IANA time zone database all
travel inside the build.  What does *not* travel inside it is the trader's own
data -- the workbook, the feed, the band and holiday overrides are copied to
sit beside the exe, where they can be edited without rebuilding.

PyInstaller cannot cross-compile.  It bundles the *host* interpreter and
host-compiled C extension modules, so a Windows exe can only be produced on
Windows.  Running this on macOS or Linux therefore does not silently hand back
something unusable: it stops and prints the two routes that do work.  The one
thing it will do off-Windows is --host-check, which runs the identical spec
against the host platform.  That catches a missing hidden import, a resource
that never made it into the bundle or a broken launcher long before anyone
walks over to a Windows machine.

A build can also be made without some of the five screens.  ``--exclude-tab``
and ``--only-tabs`` write the chosen set into the bundle, where
``volkit/screens.py`` reads it: the tab does not appear, its routes are
refused by name and its subcommands are not registered.  What this does *not*
do is remove the code -- numpy and scipy are the size of a build, not
``analytics.py``, and an import that vanished would turn a wrong build into a
stack trace instead of a sentence.  It also is not a permission system:
anybody can run a build that has the tab.

``--hidden-tab`` is the third state.  The screen is built and works, but it is
off until the executable is started with ``--enable-tab NAME`` -- or until an
``enable-tab`` line is put in ``volkit.cfg`` beside it, which is what a
double-click reads.  It is for a screen a desk wants available without having
it on every morning.  Being off, it is turned away by the same routes and the
same subcommand check as an excluded one, and says the *other* sentence: how
to switch it on.

Steps, in order, each one able to fail the build:

  1. preflight   the source tree is complete and the interpreter is new enough
  2. deps        requirements.txt, plus tzdata and pyinstaller
  3. tests       the full unittest suite -- a green build of broken code is
                 worse than no build
  4. build       pyinstaller volkit.spec
  5. stage       the user's data files, beside the exe
  6. smoke       run the thing that was just built and make it price something
  7. zip         optional, for handing over
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from volkit import screens  # noqa: E402  (after sys.path, so a copied tree works)

#: Where the screen manifest is written before PyInstaller picks it up.  Inside
#: build/, never in volkit/data/: a build must not leave the source tree in a
#: state where running from source silently loses a screen.
SCREENS_BUILD_DIR = ROOT / "build" / "screens"

# Files that must exist in the source tree before a build means anything.  The
# spec re-checks index.html; this checks the rest, because a build that quietly
# omits the economic calendar produces an exe that looks fine until someone
# asks for an event.
REQUIRED_SOURCES = [
    "volkit.spec",
    "launcher.py",
    "requirements.txt",
    "volkit/cli.py",
    "volkit/screens.py",
    "volkit/web/index.html",
    "volkit/data/econ_events.csv",
]

# The trader's own files.  Copied beside the exe rather than bundled: they are
# meant to be edited, and paths.app_dir() looks for them there.  Missing ones
# are reported, not fatal -- a desk may keep the workbook on a share.
USER_DATA = [
    "files/vol_marks.xlsx",
    "files/market_feed.csv",
    "files/bands.csv",
    "files/holiday_overrides.csv",
    # The startup settings file.  Staged, never bundled: the whole point of it
    # is to be edited beside the exe, and paths.find_data_file() looks there.
    "files/volkit.cfg",
    "USER_MANUAL.md",
]

# Sample data.  Staged into a samples/ subfolder, never beside the exe where
# find_data_file() would pick it up: synthetic numbers appearing on a screen
# nobody asked for is the same failure as a silent zero.
SAMPLE_DATA = [
    "files/history_sample.xlsx",
]


class BuildError(RuntimeError):
    """A step failed.  Carries the real message; nothing here fails silently."""


def run(cmd: list[str], *, step: str, env: dict[str, str] | None = None) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise BuildError(f"{step} failed with exit code {result.returncode}")


def capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def heading(n: int, total: int, text: str) -> None:
    print(f"\n=== [{n}/{total}] {text} " + "=" * max(0, 52 - len(text)), flush=True)


# --------------------------------------------------------------------------
# steps


def choose_screens(only: str | None, exclude: list[str],
                   hidden: list[str] | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What this build contains: ``(shown, hidden)``.

    --only-tabs sets the starting set, --exclude-tab takes away from it, and
    --hidden-tab moves one from shown to hidden.  An unknown name is an error
    rather than a no-op -- a misspelled screen that quietly stayed in the
    build is the same silent failure as one that quietly left it -- and so is
    hiding a screen that was also excluded, which would otherwise produce a
    build whose --enable-tab switch could never work.
    """
    chosen = list(screens.parse_names(only, source="--only-tabs")) \
        if only else list(screens.ALL)
    for name in exclude:
        for gone in screens.parse_names(name, source="--exclude-tab"):
            if gone in chosen:
                chosen.remove(gone)
    shy: list[str] = []
    for name in (hidden or []):
        for shrink in screens.parse_names(name, source="--hidden-tab"):
            if shrink not in chosen:
                raise BuildError(
                    f"--hidden-tab {shrink}: that screen is not in this build, so nothing "
                    f"could switch it on. Hiding and excluding are different things")
            chosen.remove(shrink)
            if shrink not in shy:
                shy.append(shrink)
    if not chosen:
        raise BuildError(
            "every screen was excluded or hidden; a build needs at least one tab that shows "
            "without a command-line switch, out of: " + ", ".join(screens.ALL))
    return tuple(chosen), tuple(shy)


def preflight(target_windows: bool, chosen: tuple[str, ...],
              shy: tuple[str, ...] = ()) -> None:
    if sys.version_info < (3, 10):
        raise BuildError(
            f"volkit needs Python 3.10 or later; this is {platform.python_version()}")

    missing = [p for p in REQUIRED_SOURCES if not (ROOT / p).exists()]
    if missing:
        raise BuildError("missing from the source tree: " + ", ".join(missing))

    if target_windows and os.name != "nt":
        host = {"Darwin": "macOS"}.get(platform.system(), platform.system())
        raise BuildError(
            "PyInstaller cannot cross-compile, so a Windows .exe cannot be built "
            f"on {host}.\n\n"
            "  Two routes that do work:\n"
            "    1. Copy this repository to a Windows machine with Python 3.10+\n"
            "       and run:   python build_exe.py        (or build_windows.bat)\n"
            "    2. Push a v* tag, or run the 'build-windows' GitHub Actions\n"
            "       workflow by hand; it builds on a hosted Windows runner and\n"
            "       uploads dist/volkit as an artifact.  With the gh CLI:\n"
            "           gh workflow run build-windows.yml\n"
            "           gh run download --name volkit-windows\n\n"
            "  To check the spec and the launcher from here without a Windows\n"
            "  machine, build for this platform instead:\n"
            "       python build_exe.py --host-check")

    print(f"python      {platform.python_version()} ({platform.machine()})")
    print(f"host        {platform.system()} {platform.release()}")
    print(f"target      {'Windows .exe' if target_windows else platform.system() + ' (host check)'}")
    print(f"screens     {', '.join(screens.BY_NAME[n].label for n in chosen)}")
    if shy:
        print("hidden      " + ", ".join(screens.BY_NAME[n].label for n in shy)
              + "  (built, but off until --enable-tab)")
    missing = [n for n in screens.ALL if n not in chosen and n not in shy]
    if missing:
        print("excluded    " + ", ".join(screens.BY_NAME[n].label for n in missing)
              + "  (tab gone, routes refused, subcommands not registered)")


def install_deps() -> None:
    pip = [sys.executable, "-m", "pip", "install"]
    run(pip + ["--upgrade", "pip"], step="pip upgrade")
    run(pip + ["-r", "requirements.txt"], step="dependency install")
    # tzdata is not optional on Windows: there is no system IANA database, and
    # the NY cut, the weekly close and every economic event resolve through
    # zoneinfo.  Installed everywhere so the bundle is identical either way.
    run(pip + ["tzdata", "pyinstaller"], step="build tool install")


def run_tests() -> None:
    # Always the whole suite, with every screen present: what is being checked
    # is the code that is about to be bundled, and a VOLKIT_SCREENS left in the
    # shell would otherwise turn a trimmed run into a green build.
    env = dict(os.environ)
    env.pop(screens.ENV_VAR, None)
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        step="test suite", env=env)


def build(onefile: bool, clean: bool, chosen: tuple[str, ...],
          shy: tuple[str, ...] = ()) -> Path:
    env = dict(os.environ)
    env["VOLKIT_ONEFILE"] = "1" if onefile else "0"
    # The manifest travels inside the bundle; the spec is told where to find
    # it.  A full build writes none, so a bundle without the file means every
    # screen -- the same answer as running from source.
    env.pop(screens.ENV_VAR, None)
    if tuple(chosen) == screens.ALL and not shy:
        env.pop("VOLKIT_SCREENS_FILE", None)
    else:
        manifest = screens.write_manifest(SCREENS_BUILD_DIR, chosen, shy)
        env["VOLKIT_SCREENS_FILE"] = str(manifest)
        print(f"  screens  {manifest.relative_to(ROOT)}")
    cmd = [sys.executable, "-m", "PyInstaller", "volkit.spec", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    run(cmd, step="PyInstaller", env=env)

    suffix = ".exe" if os.name == "nt" else ""
    exe = (ROOT / "dist" / f"volkit{suffix}") if onefile else \
          (ROOT / "dist" / "volkit" / f"volkit{suffix}")
    if not exe.exists():
        raise BuildError(f"PyInstaller reported success but {exe} is not there")
    return exe


def stage_data(exe: Path) -> None:
    dest = exe.parent
    for rel in USER_DATA:
        src = ROOT / rel
        if src.exists():
            shutil.copy2(src, dest / src.name)
            print(f"  staged   {src.name}")
        else:
            print(f"  MISSING  {rel} -- copy it beside the exe before use")

    samples = [ROOT / rel for rel in SAMPLE_DATA if (ROOT / rel).exists()]
    if samples:
        (dest / "samples").mkdir(exist_ok=True)
        for src in samples:
            shutil.copy2(src, dest / "samples" / src.name)
            print(f"  staged   samples/{src.name}")


def smoke_test(exe: Path, chosen: tuple[str, ...], shy: tuple[str, ...] = ()) -> None:
    """Run what was just built.  Anything less proves only that a file exists.

    The commands are chosen from the screens this build has: ``tenors`` belongs
    to the marking screen, so a build made without it would fail a smoke test
    that insisted on running it -- which would report a broken build for doing
    exactly what it was asked to do.
    """
    workbook = exe.parent / "vol_marks.xlsx"
    checks: list[tuple[str, list[str]]] = [
        ("help", [str(exe), "--help"]),
    ]
    if workbook.exists():
        # A fixed --asof so the result does not depend on the day the build ran.
        book = ["-w", str(workbook), "--asof", "2024-02-28 12:00"]
        checks.append(("workbook check", [str(exe), "check", "-w", str(workbook)]))
        if "marking" in chosen:
            checks.append(("term structure", [str(exe), "tenors", "USDJPY"] + book))
        if "pricing" in chosen:
            # A fixed expiry as well as a fixed --asof, so this prices the
            # same three months whenever the build is run.
            checks.append(("volatility", [str(exe), "vol", "USDJPY", "2024-05-28",
                                          "--strike", "1.02"] + book))
    else:
        print("  no vol_marks.xlsx staged; checking only that the exe starts")

    # A screen that was excluded must be *gone*, and a hidden one must be both
    # off by default and reachable with the switch.  The subcommand is the one
    # part of either that can be checked from here, and a hidden screen whose
    # switch did not work would be indistinguishable from an excluded one.
    for name in screens.ALL:
        if name in chosen:
            continue
        for cmd in screens.BY_NAME[name].commands:
            result = capture([str(exe), cmd, "--help"])
            if result.returncode == 0:
                raise BuildError(
                    f"'{cmd}' still runs, but the {screens.BY_NAME[name].label} "
                    f"screen is {'hidden in' if name in shy else 'excluded from'} this build")
            if name in shy:
                switched = capture([str(exe), "--enable-tab", name, cmd, "--help"])
                if switched.returncode != 0:
                    raise BuildError(
                        f"'--enable-tab {name}' did not switch the "
                        f"{screens.BY_NAME[name].label} screen on:\n"
                        f"{switched.stdout}\n{switched.stderr}")
        print(f"  ok       {screens.BY_NAME[name].label} is "
              + ("hidden until --enable-tab" if name in shy else "not in this build"))

    for name, cmd in checks:
        result = capture(cmd)
        if result.returncode != 0:
            raise BuildError(
                f"smoke test '{name}' failed (exit {result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}")
        print(f"  ok       {name}")


def make_zip(exe: Path, onefile: bool) -> Path:
    target = ROOT / "dist" / "volkit-windows.zip"
    root = exe.parent
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        if onefile:
            zf.write(exe, exe.name)
            for rel in USER_DATA + SAMPLE_DATA:
                src = ROOT / rel
                if src.exists():
                    zf.write(src, src.name)
        else:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    zf.write(path, str(Path("volkit") / path.relative_to(root)))
    return target


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="build_exe.py",
        description="Export volkit as a standalone Windows executable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="PyInstaller cannot cross-compile: a Windows .exe must be built\n"
               "on Windows, or by the build-windows GitHub Actions workflow.\n"
               "Use --host-check to validate the spec from any platform.")
    p.add_argument("--onefile", action="store_true",
                   help="a single self-extracting volkit.exe instead of a folder. "
                        "Tidier to hand over; slower to start, because it unpacks "
                        "numpy and scipy to a temp directory on every launch")
    p.add_argument("--host-check", action="store_true",
                   help="build for this machine rather than Windows, to validate "
                        "the spec, the bundled resources and the launcher")
    p.add_argument("--exclude-tab", action="append", default=[], metavar="SCREEN",
                   help="build without this screen; repeatable. One of: "
                        + ", ".join(screens.ALL) + ". The tab is not shown, its "
                        "routes are refused by name and its subcommands are not "
                        "registered")
    p.add_argument("--only-tabs", metavar="LIST",
                   help="build with only these screens, comma separated. "
                        "--exclude-tab then takes further ones away")
    p.add_argument("--hidden-tab", action="append", default=[], metavar="SCREEN",
                   help="build this screen but leave it off until the exe is started "
                        "with --enable-tab SCREEN (or an 'enable-tab' line in volkit.cfg); "
                        "repeatable")
    p.add_argument("--skip-deps", action="store_true",
                   help="do not touch pip (offline, or a prepared environment)")
    p.add_argument("--skip-tests", action="store_true",
                   help="skip the unittest suite. Not for anything shipped")
    p.add_argument("--no-clean", action="store_true",
                   help="reuse the previous PyInstaller work directory")
    p.add_argument("--zip", action="store_true",
                   help="also write dist/volkit-windows.zip")
    args = p.parse_args(argv)

    target_windows = not args.host_check
    steps = 6 + int(args.zip)
    n = 0

    try:
        chosen, shy = choose_screens(args.only_tabs, args.exclude_tab, args.hidden_tab)
    except (screens.ScreenError, BuildError) as exc:
        print(f"\nBUILD FAILED\n\n{exc}\n", file=sys.stderr)
        return 1

    try:
        n += 1; heading(n, steps, "preflight")
        preflight(target_windows, chosen, shy)

        n += 1; heading(n, steps, "dependencies")
        if args.skip_deps:
            print("skipped (--skip-deps)")
        else:
            install_deps()

        n += 1; heading(n, steps, "test suite")
        if args.skip_tests:
            print("SKIPPED (--skip-tests) -- do not ship this build unchecked")
        else:
            run_tests()

        n += 1; heading(n, steps, f"build ({'one file' if args.onefile else 'one folder'})")
        exe = build(args.onefile, not args.no_clean, chosen, shy)

        n += 1; heading(n, steps, "stage data files")
        stage_data(exe)

        n += 1; heading(n, steps, "smoke test")
        smoke_test(exe, chosen, shy)

        if args.zip:
            n += 1; heading(n, steps, "zip")
            archive = make_zip(exe, args.onefile)
            print(f"  wrote    {archive.relative_to(ROOT)} "
                  f"({archive.stat().st_size / 1e6:.1f} MB)")

    except BuildError as exc:
        print(f"\nBUILD FAILED\n\n{exc}\n", file=sys.stderr)
        return 1

    size = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file()) \
        if not args.onefile else exe.stat().st_size
    print(f"\nDone.  {exe.relative_to(ROOT)}  ({size / 1e6:.0f} MB)")
    gone = [n for n in screens.ALL if n not in chosen and n not in shy]
    if gone:
        print("Built without: " + ", ".join(screens.BY_NAME[n].label for n in gone))
    if shy:
        print("Hidden until asked for: "
              + ", ".join(f"{screens.BY_NAME[n].label} (--enable-tab {n})" for n in shy))
        print("A volkit.cfg beside the exe can turn them on for a double-click.")
    if target_windows:
        print("Hand over the whole folder." if not args.onefile
              else "Hand over the exe together with the data files beside it.")
        print("Double-click it to serve the web interface, or run:  volkit.exe --help")
    else:
        print("Host check only -- this is not a Windows executable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
