"""Re-encode already-downloaded tracks that the iPod can't play cleanly.

Tracks fetched before the encoder settings were fixed came out at 350-465 kbps
AAC (ffmpeg's native-AAC VBR q=0) with 1280x720 cover art. The iPod's hardware
AAC decoder is spec'd for 8-320 kbps: above that it produces audible artifacts
at fixed points in the track and then skips to the next one. This re-encodes
those files in place to `AUDIO_BITRATE` with art capped at `ART_MAX`, leaving
already-compliant files untouched.

Files that dedupe.py hard-linked together are encoded once and re-linked, so
the dedupe state survives the repair.
"""
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from . import settings as settings_mod
from . import download as download_mod
from .download import DEFAULT_MUSIC_DIR, AUDIO_BITRATE, ART_MAX, run

# iPod AAC ceiling. Files above this are what cause the artifact/skip symptoms.
MAX_SAFE_BITRATE = 320_000


def probe(path: Path):
    """Return (audio_bitrate, largest_art_dimension, sample_rate)."""
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,bit_rate,width,height,sample_rate",
             "-of", "json", str(path)])
    try:
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return 0, 0, 0
    audio_bitrate = art_dimension = sample_rate = 0
    for s in streams:
        if s.get("codec_type") == "audio":
            audio_bitrate = int(s.get("bit_rate") or 0)
            sample_rate = int(s.get("sample_rate") or 0)
        elif s.get("codec_type") == "video":
            art_dimension = max(art_dimension,
                                int(s.get("width") or 0), int(s.get("height") or 0))
    return audio_bitrate, art_dimension, sample_rate


CODEC = {"mp3": "libmp3lame", "m4a": "aac", "alac": "alac"}


def needs_repair(path: Path, cfg: dict):
    """Return a reason string if this file isn't iPod-safe, else None."""
    want_ext = settings_mod.ext_for(cfg)
    bitrate, art, rate = probe(path)
    reasons = []
    if path.suffix.lower() != want_ext:
        reasons.append(f"{path.suffix} -> {want_ext}")
    if cfg.get("audio_format") != "alac" and bitrate > MAX_SAFE_BITRATE:
        reasons.append(f"{bitrate // 1000} kbps audio")
    if art > int(cfg.get("art_size", ART_MAX)):
        reasons.append(f"{art}px art")
    want_rate = str(cfg.get("sample_rate", "44100"))
    if want_rate != "source" and rate and rate != int(want_rate):
        reasons.append(f"{rate} Hz -> {want_rate} Hz")
    return ", ".join(reasons) if reasons else None


def reencode(path: Path, has_art: bool, cfg: dict):
    """Re-encode to the configured format. Returns the new path, or None on
    failure. The extension changes if the configured format differs, in which
    case the original file is removed."""
    fmt = cfg.get("audio_format", "mp3")
    want_ext = settings_mod.ext_for(cfg)
    tmp = path.with_name(path.stem + ".repair" + want_ext)

    args = ["ffmpeg", "-y", "-i", str(path), "-map", "0:a"]
    if has_art and cfg.get("embed_art", True):
        args += [
            "-map", "0:v?", "-c:v", "mjpeg",
            "-vf", download_mod.art_filter(cfg),
            "-disposition:v:0", "attached_pic",
        ]
    args += ["-c:a", CODEC.get(fmt, "libmp3lame")]
    if fmt != "alac":
        args += ["-b:a", str(cfg.get("audio_bitrate", AUDIO_BITRATE)).lower()]
    if want_ext == ".m4a":
        args += ["-movflags", "+faststart"]
    args += ["-map_metadata", "0", "-loglevel", "error", str(tmp)]

    if subprocess.run(args).returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None

    final = path.with_suffix(want_ext)
    if final != path:
        path.unlink()
    os.replace(tmp, final)
    return final


def repair(music_dir: Path, dry_run: bool = False, cfg: dict = None):
    cfg = cfg or settings_mod.load()
    files = [f for folder in music_dir.iterdir() if folder.is_dir()
             for f in sorted(folder.iterdir())
             if f.suffix.lower() in settings_mod.ALL_EXTS]

    # Group hard-linked copies so each distinct file is encoded only once.
    by_inode = defaultdict(list)
    for f in files:
        st = f.stat()
        by_inode[(st.st_dev, st.st_ino)].append(f)

    # Work out the full list up front so the counter can show a real total
    # rather than counting up against an unknown.
    todo = []
    for paths in by_inode.values():
        reason = needs_repair(paths[0], cfg)
        if reason:
            todo.append((paths, reason))
    already_fine = len(by_inode) - len(todo)
    total = len(todo)
    if total:
        print(f"  {total} file(s) to re-encode, {already_fine} already fine")

    skipped = already_fine
    fixed = failed = 0
    for n, (paths, reason) in enumerate(todo, 1):
        canonical = paths[0]
        label = canonical.name + (f" (+{len(paths) - 1} linked)" if len(paths) > 1 else "")
        print(f"  [{n}/{total}] {'would fix' if dry_run else 'fixing'}: {label} — {reason}", flush=True)
        if dry_run:
            fixed += 1
            continue

        _, art, _ = probe(canonical)
        new_path = reencode(canonical, art > 0, cfg)
        if not new_path:
            print(f"  [warn] re-encode failed, left as-is: {canonical.name}")
            failed += 1
            continue
        # Re-establish the hard links the re-encode broke.
        for other in paths[1:]:
            other.unlink()
            os.link(new_path, other.with_suffix(new_path.suffix))
        fixed += 1

    verb = "would re-encode" if dry_run else "re-encoded"
    print(f"{verb} {fixed} file(s); {skipped} already fine"
          + (f"; {failed} failed" if failed else "")
          + ("; ready to be synced to iTunes" if not failed and not dry_run else ""))
    return fixed, failed


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--music-dir", default=DEFAULT_MUSIC_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    repair(Path(args.music_dir), args.dry_run)
