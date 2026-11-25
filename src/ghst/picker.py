"""A small arrow-key picker for the terminal (no dependencies).

↑/↓ (or Ctrl+P/Ctrl+N) move, typing filters, Enter selects, Esc cancels.
Falls back to a numbered prompt when stdin or stdout is not a terminal.
"""

from __future__ import annotations

import os
import select
import shutil
import sys
from dataclasses import dataclass

MAX_ROWS = 10

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_ACCENT = "\x1b[36m"
_RESET = "\x1b[0m"


@dataclass
class Option:
    label: str
    hint: str = ""


def filter_options(options: list[Option], query: str) -> list[int]:
    """Indexes of options whose label or hint contains every word of the query."""
    words = query.lower().split()
    return [
        i for i, o in enumerate(options)
        if all(w in f"{o.label} {o.hint}".lower() for w in words)
    ]


def pick(title: str, options: list[Option], default: int = 0) -> int | None:
    """Let the user choose one option. Returns its index, or None if cancelled."""
    if not options:
        return None
    default = min(max(default, 0), len(options) - 1)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _pick_numbered(title, options, default)
    return _pick_interactive(title, options, default)


def _pick_numbered(title: str, options: list[Option], default: int) -> int | None:
    print(f"  {title}")
    for i, option in enumerate(options, 1):
        mark = "  (default)" if i - 1 == default else ""
        hint = f" — {option.hint}" if option.hint else ""
        print(f"    {i}. {option.label}{hint}{mark}")
    try:
        answer = input("  Number [default]: ").strip()
    except EOFError:
        return default
    if not answer:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return int(answer) - 1
    matches = filter_options(options, answer)
    return matches[0] if matches else None


def _read_key(fd: int) -> str:
    key = os.read(fd, 1)
    if key == b"\x1b":
        # An arrow key arrives as one burst; a lone Esc doesn't
        while select.select([fd], [], [], 0.03)[0]:
            key += os.read(fd, 1)
            if len(key) >= 3 and key[-1:].isalpha():
                break
    return key.decode(errors="ignore")


def _pick_interactive(title: str, options: list[Option], default: int) -> int | None:
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    query = ""
    visible = list(range(len(options)))
    selected = default  # position within `visible`
    top = 0
    drawn = 0

    def refilter() -> None:
        # Keep the preselected option highlighted while it's still listed
        nonlocal visible, selected, top
        visible = filter_options(options, query)
        selected = visible.index(default) if default in visible else 0
        top = 0

    def render() -> None:
        nonlocal drawn, top
        width = max(20, shutil.get_terminal_size().columns - 1)
        if selected < top:
            top = selected
        elif selected >= top + MAX_ROWS:
            top = selected - MAX_ROWS + 1
        lines = [f"  {_BOLD}{title}{_RESET}"]
        if query or len(options) > MAX_ROWS:
            lines.append(f"  {_DIM}filter:{_RESET} {query}")
        if not visible:
            lines.append(f"  {_DIM}  no matches{_RESET}")
        for pos in range(top, min(top + MAX_ROWS, len(visible))):
            option = options[visible[pos]]
            text = option.label + (f"  {option.hint}" if option.hint else "")
            text = text[: width - 4]
            if pos == selected:
                label = f"{_ACCENT}❯ {option.label}{_RESET}"
                rest = text[len(option.label):]
                lines.append(f"  {label}{_DIM}{rest}{_RESET}")
            else:
                lines.append(f"    {option.label}{_DIM}{text[len(option.label):]}{_RESET}")
        more = len(visible) - MAX_ROWS
        footer = "↑/↓ move · type to filter · Enter select · Esc cancel"
        if more > 0:
            footer = f"{len(visible)} choices · " + footer
        lines.append(f"  {_DIM}{footer[: width - 2]}{_RESET}")
        if drawn:
            out.write(f"\x1b[{drawn}F")
        out.write("\x1b[J" + "\n".join(lines) + "\n")
        out.flush()
        drawn = len(lines)

    out.write("\x1b[?25l")  # hide the cursor while the menu is up
    try:
        tty.setcbreak(fd)
        while True:
            render()
            key = _read_key(fd)
            if key in ("\r", "\n"):
                if visible:
                    choice = visible[selected]
                    break
            elif key in ("\x1b", "\x03", "\x04"):
                choice = None
                break
            elif key in ("\x1b[A", "\x1bOA", "\x10"):
                selected = (selected - 1) % max(len(visible), 1)
            elif key in ("\x1b[B", "\x1bOB", "\x0e"):
                selected = (selected + 1) % max(len(visible), 1)
            elif key in ("\x7f", "\x08"):
                query = query[:-1]
                refilter()
            elif key.isprintable() and not key.startswith("\x1b"):
                query += key
                refilter()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write(f"\x1b[{drawn}F\x1b[J\x1b[?25h" if drawn else "\x1b[?25h")
        out.flush()

    summary = options[choice].label if choice is not None else f"{_DIM}cancelled{_RESET}"
    print(f"  {title} {_ACCENT}{summary}{_RESET}")
    return choice
