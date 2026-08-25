"""The startup configuration file: a command line for a build nobody types at.

The packaged tool is normally double-clicked.  That gives it no arguments at
all, so every choice a desk might want -- which workbook, which port, whether
to open a browser, which hidden screens to turn on -- had only one home, which
was the source of a rebuild.  This file is that home instead:

    # volkit.cfg, beside volkit.exe
    command      = serve
    workbook     = vol_marks.xlsx
    port         = 8900
    no-browser   = false
    enable-tab   = analysis

It is read **when the executable is started with no command line of its own**
-- the double-click case.  Anything typed means the file stays shut, because a
configuration file that partly overrode what somebody just typed would be the
most confusing possible arrangement.  The two exceptions are explicit:
``--config PATH`` reads that file whatever else was typed and puts what was
typed after it, and ``--no-config`` reads nothing.  Both are handled before
argparse sees anything, since the file has to be turned into arguments before
there are any.

The format is deliberately not INI, YAML or JSON:

* ``key = value`` becomes ``--key value``.  The value is the rest of the line,
  untouched, so a Windows path with spaces in it needs no quoting.
* ``command = serve`` (or ``run =``) is the subcommand and any positional
  arguments after it; that one line *is* split, on shell rules.
* a boolean value -- ``true``/``yes``/``on``/``1`` or their opposites --
  becomes a bare flag, or nothing at all.
* a key may repeat, and repeats as an option: two ``enable-tab`` lines turn on
  two screens.
* ``#`` starts a comment, blank lines are ignored.

Nothing here validates option *names*.  A misspelled key becomes an option
argparse has never heard of, and argparse says so by name and stops -- which
is a better error than any this file could invent, and one that cannot drift
out of step with the actual options.  What is validated is the shape of a
line, because ``port 8900`` with no ``=`` would otherwise vanish silently.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: Names looked for beside the executable, in order.
DEFAULT_NAMES = ("volkit.cfg", "volkit.conf")

#: Keys that name the subcommand rather than an option.
COMMAND_KEYS = ("command", "run")

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0", ""}


class ConfigError(ValueError):
    """The configuration file cannot be read as a command line."""


@dataclass
class StartupConfig:
    """A configuration file turned into an argument list."""

    path: Path | None = None
    argv: list[str] = field(default_factory=list)
    #: One line per setting, for the console.  A packaged app that silently
    #: took its orders from a file nobody remembers writing is exactly the
    #: kind of invisible behaviour this project removes.
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.path is None:
            return "no startup configuration file"
        return f"{self.path}: " + " ".join(shlex.quote(a) for a in self.argv)


def find_config(explicit: str | Path | None = None) -> Path | None:
    """The configuration file to use, or None.

    An explicit path that is not there is an error, not a shrug: somebody
    named a file and expects it to be read.  The default names are allowed to
    be absent, because most installations will not have one.
    """
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ConfigError(f"--config {path} does not exist")
        return path
    return paths.find_data_file(*DEFAULT_NAMES)


def parse(text: str, *, source: str = "volkit.cfg") -> StartupConfig:
    """Read the file into an argument list.

    The command comes first and its options after it: every subcommand in this
    tool accepts the global options too, so one ordering works for all of them
    and there is no need to know which option belongs where.
    """
    command: list[str] = []
    options: list[str] = []
    notes: list[str] = []
    bad: list[str] = []

    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            bad.append(f"line {n}: {raw.strip()!r} is not 'key = value'")
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("-").lower()
        value = value.strip()
        if not key:
            bad.append(f"line {n}: no setting name before the '='")
            continue

        if key in COMMAND_KEYS:
            if command:
                bad.append(f"line {n}: the command is already set to {command[0]!r}")
                continue
            try:
                command = shlex.split(value)
            except ValueError as exc:
                bad.append(f"line {n}: {exc}")
                continue
            if not command:
                bad.append(f"line {n}: '{key}' needs a subcommand, e.g. 'command = serve'")
                continue
            notes.append(f"command: {' '.join(command)}")
            continue

        low = value.lower()
        if low in _TRUE:
            options.append(f"--{key}")
            notes.append(f"--{key}")
        elif low in _FALSE:
            notes.append(f"--{key} off")
        else:
            options.extend([f"--{key}", value])
            notes.append(f"--{key} {value}")

    if bad:
        raise ConfigError(f"{source} could not be read:\n  " + "\n  ".join(bad))
    return StartupConfig(argv=command + options, notes=notes)


def load(path: str | Path) -> StartupConfig:
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"{path} could not be opened: {exc}") from None
    cfg = parse(text, source=str(path))
    cfg.path = path
    return cfg


def startup_argv(argv: list[str], subcommands=frozenset()) -> tuple[list[str], StartupConfig]:
    """The arguments to run with, and where they came from.

    ``argv`` is what the user typed.  The rules, in order:

    * ``--no-config`` -- no file is read.
    * ``--config PATH`` -- that file is read whatever else was typed, and what
      was typed is appended after it, so an option given on both wins on the
      command line.  Naming a subcommand in both places is refused rather than
      silently resolved.
    * nothing typed at all -- the double-click case -- reads the first of
      :data:`DEFAULT_NAMES` found beside the executable.
    * anything else typed -- the file stays shut.
    """
    explicit: str | None = None
    skip = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--no-config":
            skip = True
        elif tok.startswith("--config="):
            explicit = tok.split("=", 1)[1]
        elif tok == "--config":
            if i + 1 >= len(argv):
                raise ConfigError("--config needs the path to a configuration file")
            explicit = argv[i + 1]
            i += 1
        else:
            rest.append(tok)
        i += 1

    if skip and explicit:
        raise ConfigError("--config and --no-config were both given; pick one")
    if skip or (rest and not explicit):
        # Something real was typed, or the file was refused.  It stays shut.
        return rest, StartupConfig()
    found = find_config(explicit)
    if found is None:
        return rest, StartupConfig()
    cfg = load(found)
    typed_command = [a for a in rest if a in subcommands]
    file_command = [a for a in cfg.argv if a in subcommands]
    if typed_command and file_command:
        if typed_command[0] != file_command[0]:
            raise ConfigError(
                f"{found} runs '{file_command[0]}' and the command line asks for "
                f"'{typed_command[0]}'; run one or the other, or use --no-config")
        # The same command in both places is not a conflict, it is somebody
        # typing what the file already says.  Its own arguments still count.
        rest = [a for a in rest if a != typed_command[0]]
    return list(cfg.argv) + rest, cfg
