#!/usr/bin/env python3
"""Batch-download YouTube Music playlists as iPod-ready m4a with metadata.

playlists.txt: one playlist URL per line, '#' comments and blank lines ignored.
For each playlist, compares its current tracks against what's already on disk
(matched by video id embedded in the filename) and asks whether to do a full
redownload, download only the new tracks, or skip.

Normally run via `python main.py download` from the project root.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import settings as settings_mod
from .term import C, status

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MUSIC_DIR = settings_mod.DEFAULTS["music_dir"]
# Matches any format whitefruit writes, so a library downloaded under an
# earlier format setting is still recognised.
FILENAME_RE = re.compile(r"^(\d{3}) - (.+) \[([\w-]+)\]\.(?:mp3|m4a)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# yt-dlp progress markers, used to drive the concise (non-verbose) log.
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")
# ExtractAudio only: [download] also prints a Destination line (the raw
# .webm), which would log every track twice.
DEST_RE = re.compile(r"\[ExtractAudio\] Destination: (.+)$")

# iPod hardware limits: it can't decode above 320 kbps, and oversized cover
# art is slow to load. Keep both comfortably inside spec.
AUDIO_BITRATE = settings_mod.DEFAULTS["audio_bitrate"]
ART_MAX = settings_mod.DEFAULTS["art_size"]

def find_ytdlp() -> str:
    exe = shutil.which("yt-dlp")
    if exe:
        return exe
    # A winget install doesn't appear on PATH in shells that were already open
    # when it happened, so look where winget puts it before giving up.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        for found in packages.glob("yt-dlp.yt-dlp_*/yt-dlp.exe"):
            return str(found)
    sys.exit("yt-dlp not found on PATH. Install it, or open a new terminal if "
             "you just did.")


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


NTFS_UNSAFE = str.maketrans({
    ":": "：", '"': "＂", "*": "＊", "<": "＜", ">": "＞",
    "?": "？", "|": "｜", "/": "⧸", "\\": "⧹",
})


def sanitize_dirname(name: str) -> str:
    return name.translate(NTFS_UNSAFE).strip(" .")


def get_playlist_info(ytdlp: str, url: str, cfg: dict = None):
    """Return (dirname, [(id, title), ...]) for available tracks in the playlist.

    Uses --dump-json (not --get-filename/--print) because on this setup yt-dlp's
    printed output is mangled when piped (not a real console), silently dropping
    or corrupting non-ASCII characters; the JSON payload is unaffected.
    """
    r = run([ytdlp, "--flat-playlist", "--dump-json", "--ignore-errors"]
            + cookie_args(cfg or {}) + [url])
    entries = []
    playlist_title = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if playlist_title is None:
            playlist_title = e.get("playlist_title")
        title = e.get("title") or ""
        if not e.get("id") or title in ("[Private video]", "[Deleted video]"):
            continue
        entries.append((e["id"], title))
    dirname = sanitize_dirname(playlist_title) if playlist_title else "Unknown Playlist"
    return dirname, entries


def scan_existing(target_dir: Path, ext: str = None):
    """Return {video_id: Path} for tracks already downloaded here.

    Only files in the currently configured format count. A library downloaded
    under a previous format setting therefore reads as "not downloaded yet"
    and gets refetched in the new format, rather than being left as-is.
    """
    found = {}
    if target_dir.exists():
        for f in target_dir.iterdir():
            if ext and f.suffix.lower() != ext:
                continue
            m = FILENAME_RE.match(f.name)
            if m:
                found[m.group(3)] = f
    return found


def reconcile(target_dir: Path, current_ids: set, ext: str, cfg: dict, name: str):
    """Drop local tracks that are no longer in the playlist, so the folder
    keeps mirroring it when songs are removed upstream."""
    if not cfg.get("remove_deleted", True) or not target_dir.exists():
        return 0
    removed = 0
    for f in list(target_dir.iterdir()):
        m = FILENAME_RE.match(f.name)
        if m and m.group(3) not in current_ids:
            print(f"[{name}] no longer in playlist, removing: {f.name}")
            f.unlink()
            removed += 1
    return removed


def remove_stale_formats(target_dir: Path, ext: str):
    """Delete our own tracks left behind in a format we no longer use."""
    removed = 0
    if not target_dir.exists():
        return 0
    for f in list(target_dir.iterdir()):
        if f.suffix.lower() in settings_mod.ALL_EXTS and f.suffix.lower() != ext \
                and FILENAME_RE.match(f.name):
            print(f"  removing stale {f.suffix} file: {f.name}")
            f.unlink()
            removed += 1
    return removed


def cookie_args(cfg: dict):
    """yt-dlp flags for signing in, so tracks the account is entitled to
    (Premium-only) download instead of being skipped. Needed for listing a
    playlist as well as fetching from it."""
    cookies_file = (cfg.get("cookies_file") or "").strip()
    if cookies_file:
        if Path(cookies_file).exists():
            return ["--cookies", cookies_file]
        print(f"  [warn] cookies_file not found, continuing signed out: {cookies_file}")
        return []
    browser = (cfg.get("cookies_from_browser") or "none").strip().lower()
    if browser and browser != "none":
        return ["--cookies-from-browser", browser]
    return []


def signin_status(cfg: dict) -> str:
    """One-line description of how (or whether) whitefruit will sign in."""
    args = cookie_args(cfg)
    if not args:
        return "not signed in"
    if args[0] == "--cookies":
        return f"cookies file: {args[1]}"
    return f"{args[1]} cookies"


def test_signin(ytdlp: str, cfg: dict, url: str):
    """Actually try the configured cookies. Returns (ok, message).

    Cookie extraction is the usual failure (recent Chrome encrypts its store),
    and it fails silently mid-run otherwise, so it's worth checking directly.
    """
    if not cookie_args(cfg):
        return False, "no sign-in configured — Premium-only tracks can't be fetched"
    r = run([ytdlp, "--simulate", "--flat-playlist", "--playlist-items", "1"]
            + cookie_args(cfg) + [url])
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    low = out.lower()
    for marker in ("could not copy", "failed to decrypt", "unsupported browser",
                   "no cookies", "cookie database", "permission denied",
                   "could not find"):
        if marker in low:
            return False, next((l for l in out.splitlines() if marker in l.lower()), out[:200])
    if r.returncode != 0:
        return False, (out.splitlines() or ["yt-dlp failed"])[-1][:200]
    return True, f"signed in using {signin_status(cfg)}"


def track_name(titles, position: int):
    """Title of the nth item yt-dlp is processing, from the clean JSON data."""
    if titles and 1 <= position <= len(titles):
        return titles[position - 1]
    return None


def art_filter(cfg: dict) -> str:
    """ffmpeg -vf string for the embedded cover art."""
    size = cfg.get("art_size", ART_MAX)
    if cfg.get("square_art", True):
        # YouTube art is 16:9, so a square of side ih centred horizontally is
        # the cover. Deliberately not min(iw,ih): that needs quotes and an
        # escaped comma, neither of which survives yt-dlp's
        # --postprocessor-args parsing (ffmpeg then fails with
        # "Filter not found" and the whole download errors out).
        return f"crop=ih:ih,scale={size}:{size}"
    return f"scale={size}:{size}:force_original_aspect_ratio=decrease"


def retag(path: Path, title: str) -> bool:
    """Set the title tag on an already-encoded file, without re-encoding.

    Done as its own ffmpeg pass rather than through --postprocessor-args,
    which can't carry a value containing spaces (see encode_args).
    """
    tmp = path.with_name(path.stem + ".retag" + path.suffix)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-map", "0", "-c", "copy",
         "-map_metadata", "0", "-metadata", f"title={title}",
         "-loglevel", "error", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, path)
    return True


def encode_args(cfg: dict, track_number: int = None, title: str = None):
    """yt-dlp flags for the ffmpeg call that produces the audio file."""
    opts = []
    rate = str(cfg.get("sample_rate", "44100"))
    if rate != "source":
        # iPod DACs are natively 44.1 kHz. Handing them 48 kHz makes the device
        # resample in hardware, which can leave faint artifacts that are most
        # audible in quiet passages -- better to resample here, properly.
        opts += ["-ar", rate]
    if cfg.get("audio_format", "mp3") == "mp3":
        # libmp3lame algorithm quality: 0 is the slowest and cleanest, 9 the
        # fastest and roughest.
        opts += ["-compression_level",
                 "0" if cfg.get("encoder_quality", "best") == "best" else "5"]
    if track_number is not None:
        # The search path has no playlist context, so --parse-metadata can't
        # pick the position up; set it directly.
        opts += ["-metadata", f"track={track_number}"]
    # NB: the title tag is deliberately NOT set here. yt-dlp splits the
    # --postprocessor-args value on whitespace, so "-metadata title=Too Good"
    # arrives as "-metadata title=Too" plus a stray "Good" and the encode
    # fails outright. retag() handles it afterwards instead.
    return ["--postprocessor-args", "ExtractAudio+ffmpeg_o:" + " ".join(opts)] if opts else []


def download(ytdlp: str, url: str, music_dir: Path, playlist_items: str = None,
             cfg: dict = None, titles: list = None):
    """Run yt-dlp, streaming its output live as before, but also collect the
    reason for each track it skipped (rate limits, region locks, Premium-only
    videos, etc.) so callers can report them instead of letting them scroll
    past unnoticed."""
    cfg = cfg or settings_mod.load()
    fmt = cfg.get("audio_format", "mp3")

    args = [
        ytdlp, "--ignore-errors",
        "--color", "always",  # piping stdout below makes yt-dlp think it's not a
                               # terminal and disable its own colors; force them on
        "-f", "bestaudio",
        "--extract-audio", "--audio-format", fmt,
        "--parse-metadata", "%(playlist_index)s:%(track_number)s",
        "--embed-metadata",
        "--continue", "--no-overwrites",
        "-o", str(music_dir / "%(playlist_title)s" / "%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"),
    ]
    # An explicit bitrate, never "0". --audio-quality 0 means ffmpeg VBR q=0,
    # which for the native AAC encoder emitted 350-465 kbps -- past the iPod's
    # 320 kbps decode ceiling, which made it artifact and then skip tracks.
    if fmt != "alac":
        args += ["--audio-quality", cfg.get("audio_bitrate", AUDIO_BITRATE)]

    args += cookie_args(cfg) + encode_args(cfg)

    if cfg.get("embed_art", True):
        args += [
            "--embed-thumbnail", "--convert-thumbnails", "jpg",
            "--postprocessor-args",
            f"ThumbnailsConvertor+ffmpeg_o:-vf {art_filter(cfg)}",
        ]
    if playlist_items:
        args += ["--playlist-items", playlist_items]
    args.append(url)

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    verbose = cfg.get("verbose_logging", False)
    skipped = []
    cur = total = 0
    logged = set()  # items already reported, so each song logs once

    # Overwriting a line in place only makes sense on a real terminal; when
    # redirected, each state gets its own line instead.
    live = sys.stdout.isatty()

    def progress(text, color=C.RESET, done=True):
        counter = f"{C.DIM}[{cur}/{total}]{C.RESET} " if total else ""
        line = f"  {counter}{color}{text}{C.RESET}"
        if not live:
            if done:
                print(line, flush=True)
                logged.add(cur)
            return
        # \033[K clears whatever the previous, possibly longer, state left.
        print(f"\r{line}\033[K", end="\n" if done else "", flush=True)
        if done:
            logged.add(cur)

    for line in proc.stdout:
        if verbose:
            print(line, end="")
        plain = ANSI_RE.sub("", line).strip()

        # yt-dlp announces each playlist entry before working on it, and
        # reports the playlist's own totals, so the counter comes free.
        m = ITEM_RE.search(plain)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if not verbose:
                # Show the track as in progress right away, rather than
                # leaving the line silent until it finishes.
                name = track_name(titles, cur)
                progress(f"{name} {C.DIM}…{C.RESET}" if name else "working …",
                         C.RESET, done=False)
            continue

        if plain.startswith("ERROR:"):
            reason = plain[len("ERROR:"):].strip()
            skipped.append(reason)
            if not verbose:
                progress(f"skipped — {reason}", C.YELLOW)
            continue

        if verbose or cur in logged:
            continue
        dest = DEST_RE.search(plain)
        if dest:
            # Prefer the title from --dump-json: yt-dlp writes its log in the
            # Windows ANSI codepage, which mangles anything outside Latin-1
            # (an ellipsis arrives as a lone 0x85, CJK titles don't survive at
            # all), so the path in this line is not trustworthy.
            progress(track_name(titles, cur) or Path(dest.group(1)).stem, C.GREEN)
        elif "has already been downloaded" in plain:
            progress("already downloaded", C.DIM)

    proc.wait()
    return skipped


def search_download(ytdlp: str, title: str, vid: str, index: int,
                    target_dir: Path, cfg: dict) -> bool:
    """Fetch a track from public YouTube by searching for its title.

    Used for tracks the playlist itself won't serve (Premium-only,
    region-locked). The file keeps the *playlist's* video id in its name so
    the rest of the tool still tracks it as that playlist entry, even though
    the audio came from a different upload.
    """
    # Name it from the playlist's title, not the search result's -- the found
    # upload is often titled "Artist - Song (Official Video)" and would stick
    # out against the rest of the library. '%' is escaped so it isn't read as
    # an output-template field.
    safe = sanitize_dirname(title).replace("%", "%%")
    out = target_dir / f"{index:03d} - {safe} [{vid}].%(ext)s"
    args = [
        ytdlp, "--ignore-errors", "--color", "always",
        "--no-playlist",
        "-f", "bestaudio",
        "--extract-audio", "--audio-format", cfg.get("audio_format", "mp3"),
        "--embed-metadata",
        "--no-overwrites",
        "-o", str(out),
    ]
    if cfg.get("audio_format", "mp3") != "alac":
        args += ["--audio-quality", cfg.get("audio_bitrate", AUDIO_BITRATE)]
    args += cookie_args(cfg) + encode_args(cfg, track_number=index, title=title)
    if cfg.get("embed_art", True):
        args += [
            "--embed-thumbnail", "--convert-thumbnails", "jpg",
            "--postprocessor-args",
            f"ThumbnailsConvertor+ffmpeg_o:-vf {art_filter(cfg)}",
        ]
    args.append(f"ytsearch1:{title}")

    before = set(target_dir.iterdir()) if target_dir.exists() else set()
    proc = subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    after = set(target_dir.iterdir()) if target_dir.exists() else set()
    if cfg.get("verbose_logging", False):
        print(proc.stdout)

    # Trust the filesystem rather than the exit code: --ignore-errors means a
    # failed search still exits cleanly.
    new_files = after - before
    got = [f for f in new_files if f.suffix.lower() in settings_mod.ALL_EXTS]
    # A failed extraction can leave the raw download and thumbnail behind.
    for leftover in new_files:
        if leftover not in got and leftover.suffix.lower() in (".webm", ".jpg", ".webp", ".m4a.part"):
            leftover.unlink(missing_ok=True)
    if not got:
        return False
    retag(got[0], title)
    return True


def renumber(target_dir: Path, id_to_index: dict):
    for f in list(target_dir.iterdir()):
        m = FILENAME_RE.match(f.name)
        if not m:
            continue
        title, vid = m.group(2), m.group(3)
        correct = id_to_index.get(vid)
        if correct is None:
            continue  # no longer in the playlist; leave file alone
        new_name = f"{correct:03d} - {title} [{vid}]{f.suffix}"
        if new_name == f.name:
            continue
        target = f.with_name(new_name)
        if target.exists():
            print(f"  [warn] skipping renumber, target already exists: {new_name}")
            continue
        f.rename(target)


def prompt_action(name: str, total: int, have: int, new: int, stale: int) -> str:
    msg = f"[{name}] {total} tracks, {have} already downloaded, {new} new"
    if stale:
        msg += f", {stale} no longer in playlist"
    print(msg)
    while True:
        choice = input("  (F)ull redownload / (N)ew only / (S)kip? [N]: ").strip().lower() or "n"
        if choice in ("f", "n", "s"):
            return {"f": "full", "n": "new", "s": "skip"}[choice]
        print("  Please enter F, N, or S.")


def recover_missing(ytdlp: str, target_dir: Path, ext: str, entries: list,
                    id_to_index: dict, want_ids, cfg: dict, name: str):
    """Search public YouTube for tracks the playlist itself wouldn't serve.

    Returns [(video_id, title), ...] for the ones found. The audio comes from
    a different upload, so callers surface these to the user rather than
    treating them as ordinary downloads.
    """
    if not cfg.get("search_fallback", True):
        return []
    arrived = set(scan_existing(target_dir, ext))
    titles_by_id = dict(entries)
    recovered = []
    for vid in want_ids:
        if vid in arrived or vid not in id_to_index:
            continue
        title = titles_by_id.get(vid)
        if not title:
            continue
        print(f"  {C.DIM}not available from the playlist, searching YouTube:{C.RESET} {title}",
              flush=True)
        if search_download(ytdlp, title, vid, id_to_index[vid], target_dir, cfg):
            recovered.append((vid, title))
            print(f"  {C.GREEN}found a public upload:{C.RESET} {title}")
        else:
            print(f"  {C.YELLOW}no usable result for:{C.RESET} {title}")
    return recovered


def process_playlist(ytdlp: str, url: str, music_dir: Path, auto_yes: bool,
                     cfg: dict = None, position=None):
    """Returns (skipped, from_search).

    skipped     "[playlist] reason" strings for tracks that couldn't be
                fetched at all (Premium-only, region-locked, rate-limited).
    from_search "[playlist] title" strings for tracks the playlist wouldn't
                serve that were recovered from a public YouTube search, which
                may not be the same recording.
    """
    cfg = cfg or settings_mod.load()
    ext = settings_mod.ext_for(cfg)

    # Reading a playlist is a network round trip, so say what's happening
    # rather than sitting silent until it returns.
    prefix = f"{C.DIM}[{position[0]}/{position[1]}]{C.RESET} " if position else ""
    status(f"  {prefix}{C.DIM}reading playlist…{C.RESET}")

    name, entries = get_playlist_info(ytdlp, url, cfg)
    if not entries:
        status(f"  {prefix}{C.YELLOW}no available tracks{C.RESET} {C.DIM}{url}{C.RESET}",
               done=True)
        return [], []
    status(f"  {prefix}{C.BOLD}{name}{C.RESET} {C.DIM}({len(entries)} tracks){C.RESET}",
           done=True)

    target_dir = music_dir / name
    id_to_index = {vid: i + 1 for i, (vid, _) in enumerate(entries)}
    current_ids = set(id_to_index)

    existing = scan_existing(target_dir, ext)
    known_ids = set(existing)
    new_ids = [vid for vid, _ in entries if vid not in known_ids]
    overlap = known_ids & current_ids
    stale = known_ids - current_ids

    all_titles = [t for _, t in entries]

    def finish(skipped, want_ids):
        """Search for whatever didn't arrive, then report both lists."""
        recovered = recover_missing(ytdlp, target_dir, ext, entries,
                                    id_to_index, want_ids, cfg, name)
        got = {vid for vid, _ in recovered}
        # A track recovered by search isn't "skipped" any more.
        skipped = [s for s in skipped if not any(vid in s for vid in got)]
        return ([f"[{name}] {reason}" for reason in skipped],
                [f"[{name}] {t}" for _, t in recovered])

    if not target_dir.exists() or not known_ids:
        print(f"[{name}] new playlist, {len(entries)} tracks -> downloading all")
        skipped = download(ytdlp, url, music_dir, cfg=cfg, titles=all_titles)
        remove_stale_formats(target_dir, ext)
        return finish(skipped, list(id_to_index))

    if not new_ids and not stale:
        print(f"[{name}] up to date ({len(entries)} tracks)")
        return [], []

    action = "new" if auto_yes else prompt_action(name, len(entries), len(overlap), len(new_ids), len(stale))

    if action == "skip":
        print(f"[{name}] skipped")
        return [], []
    elif action == "full":
        shutil.rmtree(target_dir)
        skipped = download(ytdlp, url, music_dir, cfg=cfg, titles=all_titles)
        return finish(skipped, list(id_to_index))
    elif action == "new":
        skipped = []
        if new_ids:
            indices = sorted(id_to_index[vid] for vid in new_ids)
            # yt-dlp works through --playlist-items in playlist order, so the
            # titles line up with its "item N of M" counter.
            skipped = download(ytdlp, url, music_dir,
                               playlist_items=",".join(map(str, indices)), cfg=cfg,
                               titles=[all_titles[i - 1] for i in indices])
        remove_stale_formats(target_dir, ext)

        # The playlist may have been edited while we were downloading, which
        # would make the positions we captured earlier wrong. Re-read it and
        # number from that, so a playlist edited mid-run still ends up correct.
        _, fresh = get_playlist_info(ytdlp, url, cfg)
        if fresh and fresh != entries:
            print(f"[{name}] playlist changed while downloading "
                  f"({len(entries)} -> {len(fresh)} tracks); using the new order")
            entries = fresh
            id_to_index = {vid: i + 1 for i, (vid, _) in enumerate(entries)}

        renumber(target_dir, id_to_index)
        reconcile(target_dir, set(id_to_index), ext, cfg, name)

        result = finish(skipped, new_ids)

        # Report what actually landed on disk, not what we set out to fetch --
        # tracks yt-dlp refused (Premium-only, region-locked) never arrived.
        arrived = set(scan_existing(target_dir, ext))
        added = [vid for vid in new_ids if vid in arrived]
        missed = len(new_ids) - len(added)
        if added and missed:
            print(f"[{name}] added {len(added)} new track(s), {missed} could not be downloaded")
        elif added:
            print(f"[{name}] added {len(added)} new track(s)")
        elif missed:
            print(f"[{name}] no new tracks added ({missed} could not be downloaded)")
        return result
    return [], []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("playlists_file", nargs="?", default="playlists.txt")
    ap.add_argument("--music-dir", default=DEFAULT_MUSIC_DIR)
    ap.add_argument("--yes", action="store_true", help="don't prompt; download new tracks only")
    args = ap.parse_args()

    playlists_file = Path(args.playlists_file)
    if not playlists_file.exists():
        sys.exit(f"playlists file not found: {playlists_file}")

    urls = [
        line.strip() for line in playlists_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        sys.exit("no playlist URLs found in " + str(playlists_file))

    ytdlp = find_ytdlp()
    music_dir = Path(args.music_dir)
    music_dir.mkdir(parents=True, exist_ok=True)

    for url in urls:
        process_playlist(ytdlp, url, music_dir, args.yes)


if __name__ == "__main__":
    main()
