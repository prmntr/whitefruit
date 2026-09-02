"""Remove duplicate songs on disk while keeping every playlist folder intact.

Two distinct cases, handled differently:

- Same song twice *within one playlist folder* (e.g. added to the source
  YouTube Music playlist twice) is a real duplicate: keep the lower-numbered
  copy, delete the rest.
- Same song appearing *across different playlist folders* is not a bug (it
  legitimately belongs to multiple playlists) but wastes disk space storing
  the audio twice. Every extra copy is replaced with an NTFS hard link to one
  canonical file, so each folder still contains a fully valid file at its own
  path/track number, but the data is only stored once.

The iTunes side (avoiding duplicate library track objects) is handled
separately by itunes_import.ps1, which is dedup-aware and should be re-run
after this.
"""
import os
from collections import defaultdict
from pathlib import Path

from . import settings as settings_mod
from .download import DEFAULT_MUSIC_DIR, FILENAME_RE
from .term import C, status


def _tracks(folder: Path):
    """Our downloaded tracks in a folder, whatever format they're in."""
    return sorted(f for f in folder.iterdir()
                  if f.suffix.lower() in settings_mod.ALL_EXTS)


def _group_by_id(files):
    by_id = defaultdict(list)
    for f in files:
        m = FILENAME_RE.match(f.name)
        if m:
            by_id[m.group(3)].append(f)
    return by_id


def _same_file(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
        return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino
    except OSError:
        return False


def dedupe_within_folders(music_dir: Path, dry_run: bool = False) -> int:
    """Delete redundant copies of the same song within a single playlist folder."""
    removed = 0
    for folder in sorted(p for p in music_dir.iterdir() if p.is_dir()):
        by_id = _group_by_id(_tracks(folder))
        for vid, paths in by_id.items():
            if len(paths) < 2:
                continue
            paths.sort(key=lambda p: int(FILENAME_RE.match(p.name).group(1)))
            keep, *extra = paths
            for p in extra:
                print(f"[{folder.name}] duplicate track, removing: {p.name} (kept {keep.name})")
                if not dry_run:
                    p.unlink()
                removed += 1
    return removed


def dedupe_across_folders(music_dir: Path, dry_run: bool = False) -> int:
    """Hard-link duplicate copies of the same song across different playlists."""
    status(f"  {C.DIM}scanning for duplicates across playlists…{C.RESET}")
    all_files = [f for folder in music_dir.iterdir() if folder.is_dir() for f in _tracks(folder)]
    by_id = _group_by_id(all_files)
    dupes = [(vid, paths) for vid, paths in by_id.items() if len(paths) > 1]
    status(f"  {C.DIM}{len(all_files)} file(s), {len(dupes)} shared between playlists{C.RESET}",
           done=True)

    freed = 0
    linked = 0
    for n, (vid, paths) in enumerate(dupes, 1):
        canonical, *rest = sorted(paths, key=lambda p: p.stat().st_mtime)
        for p in rest:
            if _same_file(canonical, p):
                continue
            size = p.stat().st_size
            status(f"  {C.DIM}[{n}/{len(dupes)}]{C.RESET} linking {p.name}", done=True)
            if not dry_run:
                p.unlink()
                os.link(canonical, p)
            freed += size
            linked += 1
    if linked:
        verb = "would free" if dry_run else "freed"
        print(f"{verb} {freed / 1_048_576:.1f} MiB by hard-linking {linked} duplicate file(s)")
    return linked


def dedupe(music_dir: Path, dry_run: bool = False):
    removed = dedupe_within_folders(music_dir, dry_run)
    linked = dedupe_across_folders(music_dir, dry_run)
    if not removed and not linked:
        print("no duplicates found")
    return removed, linked


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--music-dir", default=DEFAULT_MUSIC_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dedupe(Path(args.music_dir), args.dry_run)
