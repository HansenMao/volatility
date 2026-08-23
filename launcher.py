"""Entry point for the packaged application.

Double-clicking the executable should start the tool and open a browser, so
with no arguments this runs ``serve``.  Every command-line option still works
when the exe is called from a terminal.
"""

from __future__ import annotations

import sys


def main() -> int:
    from volkit.cli import main as cli_main

    argv = sys.argv[1:]
    known = {"check", "serve", "validate", "events", "tenors", "daily", "vol", "smile"}
    if not any(a in known for a in argv):
        argv = argv + ["serve"]
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
        if getattr(sys, "frozen", False):
            try:
                input("\nPress Enter to close...")
            except EOFError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
