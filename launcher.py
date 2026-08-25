"""Entry point for the packaged application.

Double-clicking the executable should start the tool and open a browser, so
with no arguments this runs ``serve``.  Every command-line option still works
when the exe is called from a terminal.

Because a double-click supplies no arguments at all, that is also where the
startup settings file comes in: ``volkit.cfg`` beside the executable is turned
into a command line before anything else happens, so a desk can fix the port,
the workbook, and which hidden screens to switch on without a rebuild and
without anyone typing.  What it read is printed; a packaged app taking silent
orders from a file nobody remembers writing is the failure mode this project
exists to remove.
"""

from __future__ import annotations

import sys


def _subcommands() -> frozenset[str]:
    """The subcommand names argparse itself knows about.

    Read off the parser rather than kept as a list here: a hardcoded copy went
    stale when ``analysis`` and ``listed`` were added, and the symptom was
    obscure -- the launcher decided a real command was not a command, appended
    ``serve``, and argparse rejected the pair.
    """
    from volkit.cli import build_parser
    import argparse

    names: set[str] = set()
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            names.update(action.choices)
    return frozenset(names)


def resolve(argv: list[str]) -> tuple[list[str], object]:
    """The arguments to run, and the settings file they came from (or none).

    ``serve`` goes on the **front** when no subcommand is named, not the end.
    Every option in this tool is either global or belongs to a subcommand, and
    both kinds parse after the command name; an option left in front of it --
    which is exactly what a settings file of nothing but options produces --
    would be rejected as unrecognised.
    """
    from volkit import config
    from volkit.cli import KNOWN_COMMANDS

    argv, cfg = config.startup_argv(list(argv), KNOWN_COMMANDS)
    if not any(a in KNOWN_COMMANDS for a in argv):
        argv = ["serve"] + argv
    return argv, cfg


def main() -> int:
    from volkit.cli import main as cli_main
    from volkit.config import ConfigError

    try:
        argv, cfg = resolve(sys.argv[1:])
    except ConfigError as exc:
        print(f"\nvolkit could not read its settings file:\n  {exc}\n", file=sys.stderr)
        _hold()
        return 2
    if getattr(cfg, "path", None) is not None:
        print(f"settings from {cfg.path}")
        for note in cfg.notes:
            print(f"  {note}")

    try:
        return cli_main(argv)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        # A packaged app has no console to scroll back through, so say what
        # happened and hold the window open rather than vanishing.
        import traceback
        print(f"\nvolkit stopped with an error:\n  {type(exc).__name__}: {exc}\n",
              file=sys.stderr)
        traceback.print_exc()
        _hold()
        return 1


def _hold() -> None:
    if getattr(sys, "frozen", False):
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
