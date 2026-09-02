"""Thin wrapper around itunes_import.ps1 (Windows iTunes COM automation lives
more naturally in PowerShell than through a Python COM binding)."""
import subprocess
from pathlib import Path

from .download import DEFAULT_MUSIC_DIR

SCRIPT = Path(__file__).with_name("itunes_import.ps1")


def import_playlists(music_dir=DEFAULT_MUSIC_DIR):
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-MusicDir", str(music_dir)],
        check=True,
    )


if __name__ == "__main__":
    import_playlists()
