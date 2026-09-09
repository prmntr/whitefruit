"""Terminal colour, shared by the CLI and the worker modules.

Colours are enabled once, on import, and become empty strings when output
isn't a terminal (piped or redirected) so logs stay clean.
"""
import contextlib
import itertools
import sys
import threading
import time


class C:
    """ANSI colour/style codes. Empty strings if the terminal doesn't support them."""
    RESET = BOLD = DIM = GREEN = CYAN = YELLOW = RED = MAGENTA = ""


def enable_color():
    if not sys.stdout.isatty():
        return
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return
    C.RESET = "\033[0m"
    C.BOLD = "\033[1m"
    C.DIM = "\033[2m"
    C.GREEN = "\033[32m"
    C.CYAN = "\033[36m"
    C.YELLOW = "\033[33m"
    C.RED = "\033[31m"
    C.MAGENTA = "\033[35m"


enable_color()


def status(text: str, done: bool = False):
    """A line that updates in place while work is happening.

    On a terminal the transient line is overwritten by whatever comes next;
    when redirected, only finished lines are printed so logs stay readable.
    """
    if not sys.stdout.isatty():
        if done:
            print(text, flush=True)
        return
    # \033[K clears whatever the previous, possibly longer, line left behind.
    print(f"\r{text}\033[K", end="\n" if done else "", flush=True)


CR = chr(13)          # carriage return, to rewrite the line
CLEAR = chr(27) + "[K"  # erase to end of line
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DIM_SPIN = ""  # set alongside the other colours in enable_color()


@contextlib.contextmanager
def spinner(text: str):
    """An animated line for work that prints nothing while it runs.

    Several things whitefruit does are one long call into iTunes or ffprobe
    that says nothing at all until it returns -- clearing the library,
    reading a playlist, indexing your own files. Without this the terminal
    just sits there, which looks exactly like a hang.

    Silent when output is not a terminal, so logs stay clean.
    """
    if not sys.stdout.isatty():
        yield
        return
    stop = threading.Event()

    def spin():
        started = time.monotonic()
        for frame in itertools.cycle(FRAMES):
            if stop.wait(0.08):
                return
            secs = int(time.monotonic() - started)
            clock = f" {secs}s" if secs >= 2 else ""
            line = f"  {DIM_SPIN}{frame} {text}{clock}{C.RESET}"
            print(CR + line + CLEAR, end="", flush=True)

    worker = threading.Thread(target=spin, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)
        print(CR + CLEAR, end="", flush=True)
