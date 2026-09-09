"""Thin wrapper around itunes_import.ps1 (Windows iTunes COM automation lives
more naturally in PowerShell than through a Python COM binding)."""
import subprocess
from pathlib import Path

from .download import DEFAULT_MUSIC_DIR
from .term import spinner

SCRIPT = Path(__file__).with_name("itunes_import.ps1")


def _run(music_dir, *extra, quiet=False):
    """Run the import script. `quiet` captures its output and returns it, for
    the steps that say nothing until they finish and so need a spinner over
    the top instead of a blank terminal."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-MusicDir", str(music_dir), *extra],
        check=True, capture_output=quiet, text=quiet,
        encoding="utf-8" if quiet else None, errors="replace" if quiet else None,
    )


def import_playlists(music_dir=DEFAULT_MUSIC_DIR):
    _run(music_dir)


def forget_tracks(music_dir=DEFAULT_MUSIC_DIR) -> bool:
    """Drop whitefruit's tracks and playlists from iTunes, keeping the files.

    Returns whether it worked. Failing is not fatal to the run that called it
    -- iTunes is often just busy -- but it is not something to swallow either:
    tracks left in the library are exactly what Sync Library uploads.
    """
    try:
        with spinner("clearing whitefruit's tracks and playlists from iTunes"):
            done = _run(music_dir, "-Forget", quiet=True)
        for line in (done.stdout or "").splitlines():
            if line.strip():
                print(line)
        return True
    except subprocess.CalledProcessError:
        return False


if __name__ == "__main__":
    import_playlists()
