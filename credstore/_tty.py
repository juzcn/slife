"""Terminal I/O helpers — platform-agnostic masked input.

Extracted from the CLI module so the command implementations in
``__main__.py`` don't carry the bulk of raw terminal handling.
"""

from __future__ import annotations

import sys


def masked_input(prompt: str = "") -> str:
    """Read a line from stdin, echoing ``*`` for each character.

    Supports paste and backspace.  The actual characters are never
    displayed — only ``*`` placeholders.  Works on Windows (msvcrt)
    and Unix (termios).

    Ctrl+C raises KeyboardInterrupt as usual.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if sys.platform == "win32":
        return _masked_input_windows()
    else:
        return _masked_input_unix()


def _masked_input_windows() -> str:
    import msvcrt

    chars: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if ch == "\x03":  # Ctrl+C
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt()
        if ch in ("\x08", "\x7f"):  # Backspace / DEL
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
        elif ch == "\x1b":  # Escape sequence (arrow keys, etc.)
            # Read the rest of the escape sequence and ignore it
            while msvcrt.kbhit():
                msvcrt.getwch()
        elif ord(ch) >= 32:  # Printable characters only
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    return "".join(chars)


def _masked_input_unix() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            if ch == "\x03":
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt()
            if ch in ("\x08", "\x7f"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ord(ch) >= 32:
                chars.append(ch)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return "".join(chars)
