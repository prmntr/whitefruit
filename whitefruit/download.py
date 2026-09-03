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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from . import settings as settings_mod
from . import sources
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
    """Return (dirname, [(id, title), ...], [nested playlist url, ...]).

    A library page (youtube.com/feed/playlists) lists playlists rather than
    tracks; those come back as the third element for the caller to work
    through one by one. An ordinary playlist leaves it empty.

    Uses --dump-json (not --get-filename/--print) because on this setup yt-dlp's
    printed output is mangled when piped (not a real console), silently dropping
    or corrupting non-ASCII characters; the JSON payload is unaffected.
    """
    r = run([ytdlp, "--flat-playlist", "--dump-json", "--ignore-errors"]
            + cookie_args(cfg or {}) + [url])
    entries = []
    nested = []
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
        # A tab entry is another playlist, not a track.
        if e.get("ie_key") == "YoutubeTab" and e.get("url"):
            nested.append(e["url"])
            continue
        entries.append((e["id"], title))
    dirname = sanitize_dirname(playlist_title) if playlist_title else "Unknown Playlist"
    return dirname, entries, nested


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
    keeps mirroring it when songs are removed upstream.

    Returns (removed, held back). Deleting rests entirely on the source list
    being complete, and it is not: iTunes fills its cloud library in
    progressively, so a read taken too early looks exactly like a playlist
    that genuinely shrank. Anything past ordinary churn is left alone and
    reported, because guessing wrong here costs audio files. The guard lives
    here rather than in each caller so every path that deletes is covered.
    """
    if not cfg.get("remove_deleted", True) or not target_dir.exists():
        return 0, 0
    ours = scan_existing(target_dir, None)
    doomed = [tid for tid in ours if tid not in current_ids]
    if doomed and len(doomed) > 10 and len(doomed) / max(len(ours), 1) > 0.25:
        print(f"  {C.YELLOW}[{name}] {len(doomed)} of {len(ours)} files are not in "
              f"the source any more — too many to trust to one read, so they "
              f"have been kept.{C.RESET}")
        return 0, len(doomed)
    removed = 0
    for f in list(target_dir.iterdir()):
        m = FILENAME_RE.match(f.name)
        if m and m.group(3) not in current_ids:
            print(f"[{name}] no longer in playlist, removing: {f.name}")
            f.unlink()
            removed += 1
    return removed, 0


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


def read_tag(path: Path, key: str) -> str:
    """One metadata field out of an existing file."""
    return (run(["ffprobe", "-v", "error", "-show_entries", f"format_tags={key}",
                 "-of", "default=nw=1:nk=1", str(path)]).stdout or "").strip()


def tag_fields(tags: dict):
    """ffmpeg -metadata arguments for a tag set.

    Underscore-prefixed keys are whitefruit's own bookkeeping (see
    sources._entries), not metadata, and must never reach the file.
    """
    return [a for k, v in (tags or {}).items() if v and not k.startswith("_")
            for a in ("-metadata", f"{k}={v}")]


def retag(path: Path, tags: dict) -> bool:
    """Set tags on an already-encoded file, without re-encoding.

    Done as its own ffmpeg pass rather than through --postprocessor-args,
    which can't carry a value containing spaces (see encode_args). What the
    source service says wins over what the YouTube upload claims: the upload
    is usually a video titled with the whole "Artist - Song (Official Video)"
    line, naming no album at all.
    """
    # Underscore-prefixed keys are whitefruit's own bookkeeping (see
    # sources._entries), not metadata to write into the file.
    fields = tag_fields(tags)
    if not fields:
        return True
    tmp = path.with_name(path.stem + ".retag" + path.suffix)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-map", "0", "-c", "copy",
         "-map_metadata", "0"] + fields + ["-loglevel", "error", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, path)
    return True


def encode_args(cfg: dict, track_number: int = None):
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
                    target_dir: Path, cfg: dict, tags: dict = None,
                    source: str = None):
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
        # Stop as soon as one result actually downloads. Paired with the
        # ytsearchN below this means "the best result that works": the top hit
        # is regularly age-restricted, which needs a signed-in account, and
        # taking only that one hit reports the song as missing when a perfectly
        # good upload sits at number two.
        "--max-downloads", "1",
        "-f", "bestaudio",
        "--extract-audio", "--audio-format", cfg.get("audio_format", "mp3"),
        "--embed-metadata",
        "--no-overwrites",
        "-o", str(out),
    ]
    if cfg.get("audio_format", "mp3") != "alac":
        args += ["--audio-quality", cfg.get("audio_bitrate", AUDIO_BITRATE)]
    # The playlist position is only a sensible track number when there is no
    # album to speak of -- a YouTube playlist. For a real album it is the thing
    # that made iTunes show 19 tracks numbered 1, 13, 48, 63, 197...; the
    # album's own number comes through `tags` instead, and playlist order is
    # carried by the iTunes playlist rather than by this tag.
    args += cookie_args(cfg) + encode_args(
        cfg, track_number=None if tags else index)
    if cfg.get("embed_art", True):
        args += [
            "--embed-thumbnail", "--convert-thumbnails", "jpg",
            "--postprocessor-args",
            f"ThumbnailsConvertor+ffmpeg_o:-vf {art_filter(cfg)}",
        ]
    def attempt(spec):
        """Run yt-dlp against one search or link; return (file or None, output)."""
        proc = subprocess.run(args + spec, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if cfg.get("verbose_logging", False):
            print(proc.stdout)
        # Trust the filesystem rather than the exit code: --ignore-errors means
        # a failed search still exits cleanly. Look for files carrying *our* id
        # rather than diffing the directory before and after -- several of
        # these run at once, and a diff would hand this track another's file.
        ours = [f for f in target_dir.iterdir()
                if f"[{vid}]" in f.name] if target_dir.exists() else []
        found = [f for f in ours if f.suffix.lower() in settings_mod.ALL_EXTS]
        # A failed extraction can leave the raw download and thumbnail behind.
        for leftover in ours:
            if leftover not in found:
                leftover.unlink(missing_ok=True)
        return (found[0] if found else None), (proc.stdout or "") + (proc.stderr or "")

    n = max(1, int(cfg.get("search_results", 3) or 1))
    if source:
        specs = [[source]]           # a link the user typed in, taken as-is
    else:
        # YouTube Music first. Its results are "art tracks" -- audio-only
        # uploads carrying real square cover art and proper album metadata --
        # whereas plain YouTube's best match is the music *video*, whose
        # thumbnail is a frame of the video and which names no album. That is
        # the whole reason art came out as vevo stills and screenshots.
        # '#songs' keeps albums and playlists out of the results, which would
        # otherwise expand into whole records.
        specs = []
        if cfg.get("search_youtube_music", True):
            specs.append(["--playlist-items", f"1-{n}",
                          "https://music.youtube.com/search?q="
                          + quote(title) + "#songs"])
        # Fall back to plain YouTube: anything unofficial, or simply not on
        # YouTube Music, only exists there.
        specs.append([f"ytsearch{n}:{title}"])

    out = ""
    for spec in specs:
        got, out = attempt(spec)
        if got:
            retag(got, tags or {"title": title})
            return True, ""

    # Say why, when yt-dlp said why. "Sign in to confirm your age" is by far
    # the most common one and is fixable (Settings -> cookies), so it deserves
    # better than being reported as if nothing existed.
    if "confirm your age" in out or "age-restricted" in out:
        return False, "age-restricted, needs sign-in"
    if "Private video" in out or "unavailable" in out:
        return False, "no usable upload"
    return False, ""


def search_many(ytdlp: str, jobs, target_dir: Path, cfg: dict):
    """Search-download several tracks at once; returns the (id, title) pairs
    that landed. `jobs` is [(id, track number, search title, tags or None), ...].

    Each track stays its own yt-dlp process. They are deliberately not folded
    into one call: a single invocation can only number its outputs with
    --autonumber, which counts files that actually downloaded, so one search
    finding nothing would silently shift every later track onto the wrong
    number and id. The time goes on network and encoding anyway, not on
    starting processes, so running a few concurrently is the speedup that was
    actually available.
    """
    if not jobs:
        return [], []
    workers = max(1, int(cfg.get("search_workers", 4) or 1))
    if not cfg.get("concurrent_search", True):
        workers = 1
    if cfg.get("verbose_logging", False):
        workers = 1  # interleaved yt-dlp output is unreadable

    target_dir.mkdir(parents=True, exist_ok=True)
    total = len(jobs)
    # Right-aligned so the titles line up in one column however far in we are,
    # the way the settings screen numbers its entries.
    width = len(str(total))

    if workers > 1:
        print(f"  {C.DIM}{workers} downloads at a time. You may notice songs finishing"
              f"in the wrong order; don't worry about that. The playlist "
              f"itself at the end keeps its order.{C.RESET}")
    print(f"  {C.DIM}searching YouTube for {total} track(s)…{C.RESET}", flush=True)

    done, failed = [], []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(search_download, ytdlp, title, vid, index,
                               target_dir, cfg, tags): (vid, title)
                   for vid, index, title, tags in jobs}
        for n, fut in enumerate(as_completed(pending), 1):
            vid, title = pending[fut]
            counter = f"  {C.DIM}[{n:>{width}}/{total}]{C.RESET} "
            try:
                ok, note = fut.result()
            except Exception as e:  # one bad track shouldn't sink the rest
                ok, note = False, str(e)
            if ok:
                status(f"{counter}{C.GREEN}{title}{C.RESET}", done=True)
                done.append((vid, title))
            else:
                why = f" {C.DIM}({note}){C.RESET}" if note else ""
                status(f"{counter}{C.YELLOW}no result for {title}{C.RESET}{why}",
                       done=True)
                failed.append((vid, title, note))
            # Overwritten by the next finished line; keeps the display from
            # going quiet while the workers are busy on the ones still running.
            if n < total:
                nxt = next((t for f, (_, t) in pending.items()
                            if not f.done()), None)
                if nxt:
                    status(f"  {C.DIM}{' ' * (width * 2 + 4)}processing "
                           f"{nxt}…{C.RESET}")

    # Wall-clock per track, at whatever concurrency was actually used, which is
    # what the estimate needs.
    _rate["secs"] += time.monotonic() - started
    _rate["tracks"] += total
    return done, failed


def renumber(target_dir: Path, id_to_index: dict):
    if not target_dir.exists():
        return
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
    # No tags: a YouTube playlist entry is only a title, and yt-dlp's own
    # --embed-metadata has already had a better look at the upload than we can.
    jobs = [(vid, id_to_index[vid], titles_by_id[vid], None) for vid in want_ids
            if vid not in arrived and vid in id_to_index and titles_by_id.get(vid)]
    if not jobs:
        return []
    # Said once rather than per track: with several running at a time the
    # per-track lines would arrive interleaved and out of order anyway.
    print(f"  {C.DIM}{len(jobs)} track(s) not available from the playlist, "
          f"searching YouTube{C.RESET}", flush=True)
    found, _ = search_many(ytdlp, jobs, target_dir, cfg)
    return found


def link_known(music_dir: Path, target_dir: Path, entries, id_to_index: dict,
               ext: str) -> int:
    """Hard-link songs we already hold in another folder instead of fetching
    them again. Returns how many were linked.

    An id is derived from the text we search for, so the same song downloaded
    for some other playlist is the file we would end up with here anyway.
    This is dedupe.py's trick applied *before* the download rather than after
    it, which matters whenever a library gets reorganised into different
    folders: without it every track is fetched a second time only to produce a
    byte-identical file.
    """
    if not music_dir.exists():
        return 0
    elsewhere = {}
    for folder in music_dir.iterdir():
        if folder.is_dir() and folder != target_dir:
            elsewhere.update(scan_existing(folder, ext))
    if not elsewhere:
        return 0

    on_disk = scan_existing(target_dir, ext)
    linked = 0
    for entry in entries:
        tid, query = entry[0], entry[1]
        if tid in on_disk or tid not in elsewhere:
            continue
        src = elsewhere[tid]
        dst = target_dir / (f"{id_to_index[tid]:03d} - {sanitize_dirname(query)} "
                            f"[{tid}]{src.suffix}")
        if dst.exists():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
            linked += 1
        except OSError:
            pass  # another volume, or it vanished under us: just fetch it
    return linked


def retag_existing(target_dir: Path, have, cfg: dict, name: str):
    """Correct the tags on files downloaded before now.

    Tracks already on disk are otherwise never revisited, so a file written
    under older tagging rules -- or whose album you have since fixed in
    iTunes -- would keep the wrong tags until it was deleted and refetched.

    Only the first file is inspected: a folder is written by one version of
    whitefruit, so if its title is right the rest almost certainly are, and
    probing all of them costs about 0.2s each on every run.
    ponytail: heuristic. Probe every file if folders ever end up mixed.
    """
    # Same reason as in repair_folder: never retag a track adopted from your
    # own library, or its hard link breaks and the file stops being shared.
    have = [e for e in have if not (e[2] or {}).get("_local")]
    if not have:
        return
    on_disk = scan_existing(target_dir, None)
    first_tid, _, first_tags = have[0]
    if (first_tid not in on_disk
            or read_tag(on_disk[first_tid], "title") == first_tags["title"]):
        return

    print(f"[{name}] correcting tags on {len(have)} file(s) already downloaded")
    workers = max(1, int(cfg.get("search_workers", 4) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(retag, on_disk[tid], tags) for tid, _, tags in have
                if tid in on_disk]
        for n, _ in enumerate(as_completed(jobs), 1):
            status(f"  {C.DIM}[{n}/{len(jobs)}] retagging…{C.RESET}",
                   done=(n == len(jobs)))


# Running average of how long one searched track actually takes, so the
# estimate settles on this machine and connection instead of a fixed guess.
_rate = {"secs": 0.0, "tracks": 0}


def eta(tracks: int, cfg: dict) -> str:
    """Rough time for `tracks` more searched downloads."""
    workers = max(1, int(cfg.get("search_workers", 4) or 1))
    if not cfg.get("concurrent_search", True):
        workers = 1
    if _rate["tracks"]:
        per = _rate["secs"] / _rate["tracks"]
    else:
        per = 7.0 / workers  # first guess, replaced as soon as anything lands
    secs = int(tracks * per)
    if secs < 90:
        return f"~{max(secs, 1)}s"
    if secs < 5400:
        return f"~{round(secs / 60)}m"
    return f"~{secs / 3600:.1f}h"


def plan_line(playlists: int, songs: int, cfg: dict, word: str = "") -> str:
    """The "3 playlists | 412 songs | ~9m" banner."""
    tail = f" {word}" if word else ""
    return (f"{C.MAGENTA}{playlists} playlist(s){tail}  |  {songs} song(s){tail}"
            f"  |  est. {eta(songs, cfg)} remaining{C.RESET}")


def swap_to_local(target: dict) -> bool:
    """Replace a YouTube download with the original file you already own.

    The original is only ever read: it is hard-linked into the playlist folder
    (a copy only if they sit on different volumes) and left exactly where
    iTunes expects to find it.
    """
    have, local = target["have"], target["local"]
    dst = have.with_suffix(local.suffix.lower())
    try:
        have.unlink()
        try:
            os.link(local, dst)
        except OSError:
            shutil.copy2(local, dst)
        return True
    except OSError:
        return False


def download_link(ytdlp: str, url: str, target: dict, cfg: dict) -> bool:
    """Fetch one track from a link the user supplied by hand.

    Same output naming and tagging as the search path, so the file drops into
    its playlist at the right position and the next run sees it as present.
    """
    return search_download(ytdlp, target["query"], target["id"], target["index"],
                           target["dir"], cfg, target["tags"], source=url)[0]


def adopt_local(target_dir: Path, todo, id_to_index: dict, ext: str):
    """Bring in files you already own instead of downloading them again.

    Hard-linked rather than copied, so the audio is stored once and your
    original stays exactly where iTunes expects it; a copy is only made when
    the two are on different volumes. Either way the original file is never
    moved, altered or deleted.

    Returns the ids adopted.
    """
    done = []
    for tid, query, tags in todo:
        src = Path((tags or {}).get("_local") or "")
        # Only formats an iPod can actually play; a FLAC or WAV you own is
        # better re-encoded by the repair pass than linked in as-is.
        if not (tags or {}).get("_local") or src.suffix.lower() not in settings_mod.ALL_EXTS:
            continue
        if not src.exists():
            continue
        dst = target_dir / (f"{id_to_index[tid]:03d} - {sanitize_dirname(query)} "
                            f"[{tid}]{src.suffix.lower()}")
        if dst.exists():
            done.append(tid)
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            try:
                shutil.copy2(src, dst)
            except OSError:
                continue  # unreadable or gone: fall through to searching
        done.append(tid)
    return done


def repair_folder(target_dir: Path, entries, id_to_index: dict, cfg: dict,
                  ext: str, name: str) -> dict:
    """Put one folder back in agreement with what its source says.

    Everything here is a correction to files that are already downloaded, so
    it never touches the network. Collected in one place rather than run
    silently on every download: these are repairs, and a healthy library does
    not need them.
    """
    fixed = {"retagged": 0, "renumbered": 0, "removed": 0, "stale": 0,
             "held_back": 0}
    on_disk = scan_existing(target_dir, None)
    if not on_disk:
        return fixed

    # Tags, from the source rather than from whatever upload was found.
    # Files adopted from your own library are left exactly as they are.
    # Retagging one rewrites whitefruit's copy, and because that copy is a hard
    # link to your file the link breaks -- leaving a second full-size copy on
    # disk per track, silently. (Your original is never altered either way: the
    # tag pass replaces the directory entry rather than writing through the
    # link.) Your own tags are also likelier to be right than the source's.
    jobs = [(on_disk[tid], tags) for tid, _, tags in entries
            if tid in on_disk and not (tags or {}).get("_local")]
    workers = max(1, int(cfg.get("search_workers", 4) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda j: retag(j[0], j[1]), jobs))
    fixed["retagged"] = sum(1 for r in results if r)

    before = {f.name for f in target_dir.iterdir()}
    renumber(target_dir, id_to_index)
    fixed["renumbered"] = len(before - {f.name for f in target_dir.iterdir()})

    fixed["removed"], fixed["held_back"] = reconcile(
        target_dir, set(id_to_index), ext, cfg, name)
    fixed["stale"] = remove_stale_formats(target_dir, ext)
    return fixed


def repair_library(ytdlp: str, urls, music_dir: Path, cfg: dict):
    """Walk every source and correct what is already on disk.

    Returns (per-folder totals, folders on disk that no source claims). Those
    orphans are the one thing it will not act on by itself: a playlist you
    deleted upstream and a folder whitefruit never made look identical from
    here, so the caller asks.
    """
    ext = settings_mod.ext_for(cfg)
    totals = {"folders": 0, "retagged": 0, "renumbered": 0, "removed": 0,
              "stale": 0, "held_back": 0}
    claimed = set()

    def walk(url):
        if sources.kind(url) == "youtube":
            name, entries, nested = get_playlist_info(ytdlp, url, cfg)
            entries = [(vid, title, {"title": title}) for vid, title in entries]
        else:
            name, entries, nested = sources.resolve(url, cfg)
        for sub in nested:
            walk(sub)
        if not entries:
            return
        name = sanitize_dirname(name)
        claimed.add(name)
        target_dir = music_dir / name
        if not target_dir.exists():
            return
        id_to_index = {e[0]: i + 1 for i, e in enumerate(entries)}
        got = repair_folder(target_dir, entries, id_to_index, cfg, ext, name)
        totals["folders"] += 1
        for k, v in got.items():
            totals[k] += v
        bits = ", ".join(f"{v} {k}" for k, v in got.items() if v) or "nothing to fix"
        print(f"  {C.DIM}{name}: {bits}{C.RESET}", flush=True)

    for url in urls:
        walk(url)

    orphans = sorted(f for f in music_dir.iterdir()
                     if f.is_dir() and f.name not in claimed) if music_dir.exists() else []
    return totals, orphans


def process_external(ytdlp: str, url: str, music_dir: Path, cfg: dict,
                     position=None, unresolved=None):
    """Fetch a Spotify or Apple Music track list, then search YouTube for each
    track. Returns (skipped, from_search) like process_playlist.

    Neither service lets anything download its audio, so there is no bulk
    yt-dlp run here: every track goes one at a time through search_download(),
    and what arrives is whichever public upload best matched the name. That is
    the same trade-off as the Premium fallback on the YouTube path, except it
    applies to every track rather than the odd one, so it's stated once per
    playlist instead of listed per track.
    """
    ext = settings_mod.ext_for(cfg)
    prefix = f"{C.DIM}[{position[0]}/{position[1]}]{C.RESET} " if position else ""
    service = "Spotify" if sources.kind(url) == "spotify" else "Apple Music"
    status(f"  {prefix}{C.DIM}reading {service}…{C.RESET}")

    name, entries, nested = sources.resolve(url, cfg)
    name = sanitize_dirname(name)

    if nested:
        # Resolved up front, and the results kept, so the totals below are
        # real rather than guessed -- and so nothing gets resolved twice.
        status(f"  {prefix}{C.DIM}reading {len(nested)} playlists…{C.RESET}")
        children = [(sub,) + sources.resolve(sub, cfg) for sub in nested]
        songs = sum(len(e) for _, _, e, _ in children)
        print()
        print(f"  {prefix}{C.BOLD}{C.CYAN}{name}{C.RESET}")
        print(f"  {plan_line(len(children), songs, cfg)}\n")

        skipped, from_search = [], []
        left = songs
        for i, (sub, _, entries, _) in enumerate(children, 1):
            s, f = process_external(ytdlp, sub, music_dir, cfg, (i, len(children)),
                                    unresolved)
            skipped += s
            from_search += f
            left -= len(entries)
            if i < len(children):
                print(f"  {plan_line(len(children) - i, left, cfg, 'left')}\n")
        return skipped, from_search

    if not entries:
        status(f"  {prefix}{C.YELLOW}no tracks{C.RESET} {C.DIM}{url}{C.RESET}", done=True)
        return [], []
    status(f"  {prefix}{C.BOLD}{C.CYAN}{name}{C.RESET} "
           f"{C.DIM}({len(entries)} tracks){C.RESET}", done=True)

    target_dir = music_dir / name
    id_to_index = {tid: i + 1 for i, (tid, *_) in enumerate(entries)}
    linked = link_known(music_dir, target_dir, entries, id_to_index, ext)
    if linked:
        print(f"[{name}] {linked} track(s) already downloaded elsewhere, hard-linked")

    existing = scan_existing(target_dir, ext)
    todo = [e for e in entries if e[0] not in existing]

    retag_existing(target_dir, [e for e in entries if e[0] in existing], cfg, name)

    # Tracks whitefruit fetched from YouTube that you have since turned out to
    # own. Collected rather than acted on: swapping is the user's call, and
    # asking once at the end beats asking per playlist across thirty of them.
    if unresolved is not None:
        for tid, query, tags in entries:
            local = (tags or {}).get("_local") or ""
            if (tid in existing and local and Path(local).exists()
                    and Path(local).suffix.lower() in settings_mod.ALL_EXTS):
                unresolved.append({"kind": "swap", "playlist": name,
                                   "dir": target_dir, "id": tid,
                                   "index": id_to_index[tid], "query": query,
                                   "tags": tags, "note": "",
                                   "have": existing[tid], "local": Path(local)})

    if not todo:
        print(f"[{name}] up to date ({len(entries)} tracks)")
    else:
        print(f"[{name}] {len(todo)} track(s) to fetch. {service} audio can't be "
              f"downloaded, so each one is searched for on YouTube. See README "
              f"for details.")

    # Anything you already own is taken from your own file; only what's left
    # is searched for on YouTube.
    adopted = set(adopt_local(target_dir, todo, id_to_index, ext))
    if adopted:
        print(f"[{name}] {len(adopted)} track(s) you already own, linked from your "
              f"own files instead of downloading")
    todo = [t for t in todo if t[0] not in adopted]

    _, failed = search_many(
        ytdlp, [(tid, id_to_index[tid], q, tags) for tid, q, tags in todo],
        target_dir, cfg)
    tags_by_id = {tid: tags for tid, _, tags in todo}
    skipped = []
    for tid, query, note in failed:
        skipped.append(f"[{name}] nothing found for {query}"
                       + (f" ({note})" if note else ""))
        # Kept so the run can offer to take a link for these by hand at the end.
        if unresolved is not None:
            unresolved.append({"kind": "missing", "playlist": name,
                               "dir": target_dir, "id": tid,
                               "index": id_to_index[tid], "query": query,
                               "tags": tags_by_id.get(tid), "note": note})

    renumber(target_dir, id_to_index)
    reconcile(target_dir, set(id_to_index), ext, cfg, name)
    remove_stale_formats(target_dir, ext)
    return skipped, []


def process_playlist(ytdlp: str, url: str, music_dir: Path, auto_yes: bool,
                     cfg: dict = None, position=None, unresolved=None):
    """Returns (skipped, from_search).

    skipped     "[playlist] reason" strings for tracks that couldn't be
                fetched at all (Premium-only, region-locked, rate-limited).
    from_search "[playlist] title" strings for tracks the playlist wouldn't
                serve that were recovered from a public YouTube search, which
                may not be the same recording.
    """
    cfg = cfg or settings_mod.load()
    ext = settings_mod.ext_for(cfg)

    if sources.kind(url) != "youtube":
        return process_external(ytdlp, url, music_dir, cfg, position, unresolved)

    # Reading a playlist is a network round trip, so say what's happening
    # rather than sitting silent until it returns.
    prefix = f"{C.DIM}[{position[0]}/{position[1]}]{C.RESET} " if position else ""
    status(f"  {prefix}{C.DIM}reading playlist…{C.RESET}")

    name, entries, nested = get_playlist_info(ytdlp, url, cfg)
    if nested:
        # A library page: work through each playlist it holds as if it had been
        # listed in playlists.txt on its own, so each still gets its own folder.
        status(f"  {prefix}{C.BOLD}{name}{C.RESET} {C.DIM}({len(nested)} playlists){C.RESET}",
               done=True)
        skipped, from_search = [], []
        for i, sub in enumerate(nested, 1):
            s, f = process_playlist(ytdlp, sub, music_dir, auto_yes, cfg,
                                    position=(i, len(nested)),
                                    unresolved=unresolved)
            skipped += s
            from_search += f
        return skipped, from_search
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
        _, fresh, _ = get_playlist_info(ytdlp, url, cfg)
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
