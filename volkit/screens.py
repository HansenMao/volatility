"""Which screens a build contains.

Every screen is a self-contained function of the book: a tab in the page, the
routes behind it, and a command-line equivalent.  A desk does not always want
all five -- a build handed to somebody who only marks the surface has no
business showing a market-maker tab, and a screen that is present but unwanted
is a screen that gets clicked by accident.

So the set of screens is chosen at build time.  ``build_exe.py --exclude-tab``
writes the chosen names into ``volkit/data/screens.txt`` inside the bundle, and
this module is the one place that reads it.  Everything downstream asks here:
``webapp`` refuses the routes of an excluded screen, ``cli`` does not register
its subcommands, and the page hides its tab.

Two things this deliberately does *not* do:

* It does not remove code from the build.  The modules still travel inside the
  exe -- numpy and scipy are the size of a build, not ``analytics.py`` -- and
  an import that vanished would turn a wrong build into a stack trace instead
  of a sentence.  Exclusion is functional: the screen cannot be reached, and
  everything that turns it away says so by name.
* It is not a permission system.  Anyone who can run the exe can run a build
  that has the tab.  It keeps a screen off a desk that did not ask for it; it
  does not keep anybody out.

A screen has three possible states in a build, not two:

* **in** -- the tab is there and works.
* **hidden** -- the code and the tab are built, but off until somebody asks
  for them: ``volkit.exe --enable-tab analysis``, or an ``enable-tab`` line in
  the startup configuration file.  This is for a screen a desk wants
  available without it being on the screen every morning; it is *not* a lock,
  for the same reason exclusion is not one.
* **out** -- excluded at build time, and nothing turns it back on.

Outside a frozen build there is no manifest, so every screen is present.  The
``VOLKIT_SCREENS`` environment variable then selects a subset, which is how the
excluded case is exercised without building an exe; a bundled manifest wins
over it, because the manifest is the build's own decision and an environment
variable must not quietly re-enable a screen somebody chose to leave out.  A
name suffixed with ``hidden`` in either place is built hidden.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import paths

#: Where the manifest lives inside the bundle, relative to ``resource_dir()``.
MANIFEST = "volkit/data/screens.txt"

#: The environment variable consulted when there is no manifest.
ENV_VAR = "VOLKIT_SCREENS"

#: The word that marks a screen as built-but-hidden, in the manifest and in
#: ``$VOLKIT_SCREENS``: ``analysis hidden``.
HIDDEN_WORD = "hidden"

#: Hidden screens turned on for this process, by :func:`activate`.  Runtime
#: state, deliberately: what a build contains is a property of the build, and
#: what is *showing* is a property of how it was started.
_ACTIVE: set[str] = set()


class ScreenError(ValueError):
    """A screen name that is not one of ours, or a selection that is empty."""


@dataclass(frozen=True)
class Screen:
    """One tab, and everything that belongs only to it.

    ``routes`` and ``commands`` list what is *owned* by this screen.  Anything
    shared -- ``/api/state``, ``/api/reload``, ``check``, ``serve`` -- belongs
    to no screen and is always present, because the shell has to work whichever
    screens a build was given.
    """

    name: str
    label: str
    panel: str
    routes: tuple[str, ...]
    commands: tuple[str, ...]


SCREENS: tuple[Screen, ...] = (
    Screen(
        name="pricing", label="Pricing", panel="p-pricing",
        routes=("/api/price",),
        commands=("vol", "smile"),
    ),
    Screen(
        name="marking", label="Vol marking", panel="p-marking",
        routes=("/api/marks", "/api/overwrite", "/api/smile", "/api/term",
                "/api/daily", "/api/curve", "/api/events", "/api/events/suggest",
                "/api/rrfly", "/api/export/daily", "/api/band"),
        commands=("tenors", "daily", "events", "validate", "band"),
    ),
    Screen(
        name="listed", label="Exchange traded", panel="p-listed",
        routes=("/api/listed/fit",),
        commands=("listed",),
    ),
    Screen(
        name="analysis", label="Analysis", panel="p-analysis",
        routes=("/api/analysis", "/api/history", "/api/analysis/curves"),
        commands=("analysis",),
    ),
    Screen(
        name="mm", label="Market maker", panel="p-mm",
        routes=("/api/mm/fit", "/api/mm/learn", "/api/mm/bank"),
        commands=("mm",),
    ),
)

ALL: tuple[str, ...] = tuple(s.name for s in SCREENS)
BY_NAME: dict[str, Screen] = {s.name: s for s in SCREENS}

# Route -> owning screen.  Built once; a route claimed twice is a typo, and a
# silently shared route would make one screen's exclusion break another's.
_OWNER: dict[str, str] = {}
for _s in SCREENS:
    for _r in _s.routes:
        if _r in _OWNER:
            raise AssertionError(f"{_r} is claimed by {_OWNER[_r]} and {_s.name}")
        _OWNER[_r] = _s.name
_COMMAND_OWNER: dict[str, str] = {c: s.name for s in SCREENS for c in s.commands}


def all_commands() -> tuple[str, ...]:
    """Every subcommand owned by a screen, whatever this build contains.

    Static on purpose: the startup configuration file has to recognise a
    command name before it knows whether this build has it, so that a
    configuration written for the full tool fails with "that screen is not in
    this build" rather than with an argument it cannot place.
    """
    return tuple(_COMMAND_OWNER)


def parse_names(text: str | list[str], *, source: str = ENV_VAR) -> tuple[str, ...]:
    """Names from a comma-, space- or newline-separated selection.

    Raises rather than dropping what it does not recognise: a misspelled screen
    that silently left the build is exactly the failure this project removes.
    Order follows :data:`SCREENS`, so a manifest cannot change the tab order.
    """
    parts = text if isinstance(text, list) else [text]
    got: list[str] = []
    for part in parts:
        for line in str(part).splitlines():
            line = line.split("#", 1)[0]
            for word in line.replace(",", " ").split():
                got.append(word.strip().lower())
    unknown = [w for w in got if w not in BY_NAME]
    if unknown:
        raise ScreenError(
            f"{source}: unknown screen{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(sorted(set(unknown)))}; known screens are {', '.join(ALL)}")
    if not got:
        raise ScreenError(f"{source}: no screens selected; a build needs at least one")
    return tuple(n for n in ALL if n in set(got))


def parse_selection(text: str | list[str], *, source: str = ENV_VAR) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A selection into ``(visible, hidden)``.

    Each line names one screen, optionally followed by ``hidden``.  The two
    are parsed together rather than from two separate lists because a screen
    that appeared in both would otherwise be in an undefined state, and the
    thing a build must never be is ambiguous about what it contains.
    """
    parts = text if isinstance(text, list) else [text]
    visible: list[str] = []
    hidden: list[str] = []
    for part in parts:
        for line in str(part).splitlines():
            # Tokens, not columns: a selection is written as "pricing, marking"
            # on one line, one name a line in the manifest, or space separated
            # in the environment variable, and all three have to mean the same
            # thing.  A name opens an entry and ``hidden`` attaches to the one
            # before it.
            last: str | None = None
            for word in line.split("#", 1)[0].replace(",", " ").split():
                word = word.strip().lower()
                if word in BY_NAME:
                    visible.append(word)
                    last = word
                elif word == HIDDEN_WORD:
                    if last is None:
                        raise ScreenError(
                            f"{source}: {HIDDEN_WORD!r} does not follow a screen name")
                    visible.remove(last)
                    hidden.append(last)
                    last = None
                else:
                    raise ScreenError(
                        f"{source}: unknown screen {word}; known screens are "
                        f"{', '.join(ALL)}")
    if not visible and not hidden:
        raise ScreenError(f"{source}: no screens selected; a build needs at least one")
    both = set(visible) & set(hidden)
    if both:
        raise ScreenError(f"{source}: {', '.join(sorted(both))} listed both openly and as "
                          f"{HIDDEN_WORD}; a screen is one or the other")
    if not visible:
        raise ScreenError(f"{source}: every screen is hidden; a build needs at least one tab "
                          f"that shows without a command-line switch")
    return (tuple(n for n in ALL if n in set(visible)),
            tuple(n for n in ALL if n in set(hidden)))


def manifest_text(names: tuple[str, ...] | list[str],
                  hidden: tuple[str, ...] | list[str] = ()) -> str:
    """The manifest a build writes.  Read by :func:`enabled`, and by a human."""
    chosen = parse_names(list(names), source="build")
    shy = parse_names(list(hidden), source="build") if hidden else ()
    both = set(chosen) & set(shy)
    if both:
        raise ScreenError(f"build: {', '.join(sorted(both))} cannot be both shown and hidden")
    missing = [n for n in ALL if n not in chosen and n not in shy]
    lines = [
        "# Screens built into this copy of volkit.  Written by build_exe.py;",
        "# volkit/screens.py is the only thing that reads it.",
    ]
    if shy:
        lines.append("# Hidden until asked for: --enable-tab "
                     + ", --enable-tab ".join(shy))
    if missing:
        lines.append("# Excluded: " + ", ".join(BY_NAME[n].label for n in missing))
    body = [n for n in ALL if n in set(chosen)]
    body += [f"{n} {HIDDEN_WORD}" for n in ALL if n in set(shy)]
    return "\n".join(lines + body) + "\n"


@lru_cache(maxsize=1)
def _selection() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What this copy was *built* with: ``(visible, hidden)``.

    A property of the build, so it is read once.  What is *showing* is
    :func:`enabled`, which adds whatever was turned on at startup.
    """
    manifest = paths.resource_dir() / MANIFEST
    if manifest.exists():
        return parse_selection(manifest.read_text(), source=str(manifest))
    env = os.environ.get(ENV_VAR)
    if env and env.strip():
        return parse_selection(env, source=f"${ENV_VAR}")
    return ALL, ()


@lru_cache(maxsize=1)
def _enabled() -> tuple[str, ...]:
    visible, shy = _selection()
    on = set(visible) | (set(shy) & _ACTIVE)
    return tuple(n for n in ALL if n in on)


def enabled() -> tuple[str, ...]:
    """The screens showing right now, in tab order.

    Cached, because it is asked once per HTTP request and per subcommand
    registration.  ``enabled.cache_clear()`` drops the manifest with it: a
    test that changes the selection means to change all of it, and a stale
    half would be worse than no cache at all.
    """
    return _enabled()


def _clear_caches() -> None:
    _enabled.cache_clear()
    _selection.cache_clear()


enabled.cache_clear = _clear_caches  # type: ignore[attr-defined]


def built() -> tuple[str, ...]:
    """Every screen in this build, showing or hidden."""
    visible, shy = _selection()
    return tuple(n for n in ALL if n in set(visible) | set(shy))


def hidden() -> tuple[str, ...]:
    """Screens built but off until asked for, whether or not they are on now."""
    return _selection()[1]


def activate(names: str | list[str], *, source: str = "--enable-tab") -> tuple[str, ...]:
    """Turn on hidden screens for this process.  Returns the ones it turned on.

    A name that was excluded from the build raises: quietly ignoring it would
    let somebody start the tool with a switch that does nothing and never say
    so.  A name that is already showing is not an error -- a configuration
    file that lists every screen it wants is a reasonable thing to write -- but
    it is not reported as activated either.
    """
    wanted = parse_names(names, source=source)
    visible, shy = _selection()
    gone = [n for n in wanted if n not in set(visible) | set(shy)]
    if gone:
        raise ScreenError(
            f"{source}: {', '.join(BY_NAME[n].label for n in gone)} "
            f"{'were' if len(gone) > 1 else 'was'} excluded from this build and cannot be "
            f"switched on; this build has {', '.join(BY_NAME[n].label for n in built())}")
    turned = tuple(n for n in wanted if n in shy and n not in _ACTIVE)
    _ACTIVE.update(n for n in wanted if n in shy)
    _clear_caches()
    return turned


def deactivate_all() -> None:
    """Forget every runtime activation.  For tests and for a fresh process."""
    _ACTIVE.clear()
    _clear_caches()


def is_enabled(name: str) -> bool:
    return name in enabled()


def excluded() -> tuple[str, ...]:
    on = enabled()
    return tuple(n for n in ALL if n not in on)


def excluded_message(name: str) -> str:
    """Why a request was turned away, in a sentence that names the build.

    A hidden screen and an excluded one are turned away by the same code and
    must not be turned away by the same sentence: one of them can be had by
    starting the tool differently, and saying so is the difference between a
    switch somebody can find and one they cannot.
    """
    if name in hidden():
        return (f"the {BY_NAME[name].label} screen is hidden in this build. Start volkit with "
                f"--enable-tab {name} to turn it on, or add 'enable-tab = {name}' to "
                f"volkit.cfg beside the executable")
    on = enabled()
    have = ", ".join(BY_NAME[n].label for n in on) if on else "none"
    return (f"the {BY_NAME[name].label} screen was excluded from this build "
            f"(this build has: {have})")


def route_refusal(path: str) -> str | None:
    """The message for a route whose screen is not in this build, else None."""
    owner = _OWNER.get(path)
    if owner is None or owner in enabled():
        return None
    return excluded_message(owner)


def command_screen(name: str) -> str | None:
    """The screen a CLI subcommand belongs to, or None if it belongs to all."""
    return _COMMAND_OWNER.get(name)


def command_enabled(name: str) -> bool:
    owner = _COMMAND_OWNER.get(name)
    return owner is None or owner in enabled()


def summary() -> str:
    """One line for a console banner.  Empty when the build shows everything."""
    off = [n for n in excluded() if n not in hidden()]
    shy = [n for n in hidden() if n not in enabled()]
    bits = []
    if off:
        bits.append("excluded from this build: " + ", ".join(BY_NAME[n].label for n in off))
    if shy:
        bits.append("hidden until asked for (--enable-tab): "
                    + ", ".join(BY_NAME[n].label for n in shy))
    return "; ".join(bits)


def write_manifest(directory: str | Path, names: tuple[str, ...] | list[str],
                   hidden_names: tuple[str, ...] | list[str] = ()) -> Path:
    """Write the manifest for a build into *directory*; returns the file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(MANIFEST).name
    target.write_text(manifest_text(names, hidden_names))
    return target
