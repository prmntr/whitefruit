#!/usr/bin/env python3
"""whitefruit: download music from YouTube Music, Spotify and Apple Music,
keep it deduped, and sync it into iTunes as playlists.

Run with no arguments for an interactive menu. List what you want in
playlists.txt first -- playlists.example.txt shows every accepted line.

Non-interactive subcommands are also available for scripting:
    download        fetch/update everything listed in playlists.txt
    dedupe          remove/hard-link duplicate songs on disk
    repair          re-encode tracks that exceed iPod playback limits
    refresh         re-read the Apple Music library from iTunes
    fix             repair tags/order/leftovers on downloaded music
    itunes          (re)import every playlist folder into iTunes
    all             download -> dedupe -> itunes, in order (plug and play)
"""
import argparse
import os
import shutil
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from whitefruit import download, dedupe as dedupe_mod, itunes, repair as repair_mod, settings, sources


from whitefruit.term import C  # noqa: E402  (colour is enabled on import)


def read_urls(playlists_file: Path):
    if not playlists_file.exists():
        sys.exit(f"{C.RED}playlists file not found: {playlists_file}{C.RESET}")
    return [
        line.strip() for line in playlists_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def cmd_download(playlists_file=None, music_dir=None, auto_yes=False, cfg=None):
    cfg = cfg or settings.load()
    playlists_file = Path(playlists_file or cfg["playlists_file"])
    music_dir = Path(music_dir or cfg["music_dir"])
    urls = read_urls(playlists_file)
    if not urls:
        sys.exit(f"{C.RED}no playlist URLs found in {playlists_file}{C.RESET}")

    ytdlp = download.find_ytdlp()
    already = music_dir.exists() and any(music_dir.iterdir())
    music_dir.mkdir(parents=True, exist_ok=True)

    # Pointing music_dir somewhere new doesn't move or find an existing
    # library -- whitefruit only ever looks under the configured folder, so an
    # empty one means every track reads as missing and the whole library gets
    # refetched. Easy to trigger by accident just by changing the setting.
    if not already and sys.stdin.isatty():
        print(f"{C.YELLOW}{C.BOLD}[!] {music_dir} is empty.{C.RESET}")
        print(f"{C.YELLOW}    Every track in {len(urls)} playlist(s) will be downloaded "
              f"fresh into it.{C.RESET}")
        print(f"{C.DIM}    If you already have a library elsewhere, point music_dir at it "
              f"in Settings instead.{C.RESET}")
        if not ask_yes_no("Download everything here?", default=False):
            print(f"{C.DIM}nothing downloaded{C.RESET}")
            return []

    stale = [u for u in urls if sources.kind(u) == "itunes" and not sources.cached(u)]
    if stale and sys.stdin.isatty():
        print(f"{C.YELLOW}{C.BOLD}[!] {len(stale)} Apple Music source(s) have no "
              f"snapshot yet.{C.RESET}")
        print(f"{C.DIM}    Reading them needs Sync Library on in iTunes to allow "
              f"whitefruit to collect your library songs. Sync library MUST be turned"
              f"off before whitefruit starts pushing songs to iTunes."
              f"{C.RESET}")
        if ask_yes_no("Take a snapshot first?", default=True):
            cmd_refresh(cfg=cfg, music_dir=music_dir)

    skipped, from_search, unresolved = [], [], []
    for i, url in enumerate(urls, 1):
        s, f = download.process_playlist(ytdlp, url, music_dir, auto_yes, cfg,
                                         position=(i, len(urls)),
                                         unresolved=unresolved)
        skipped += s
        from_search += f
    offer_local_swaps(unresolved, cfg)
    fill_in_by_hand(ytdlp, unresolved, cfg)
    if from_search:
        print(f"\n{C.YELLOW}{C.BOLD}⚠ {len(from_search)} track(s) came from a YouTube "
              f"search, not the playlist:{C.RESET}")
        for t in from_search:
            print(f"{C.YELLOW}  - {t}{C.RESET}")
        print(f"{C.DIM}  These weren't available from the playlist itself, so the closest "
              f"public upload was used. Worth checking they're the right recording.{C.RESET}")
    if skipped:
        print(f"\n{C.YELLOW}{C.BOLD}⚠ {len(skipped)} track(s) skipped:{C.RESET}")
        for reason in skipped:
            print(f"{C.YELLOW}  - {reason}{C.RESET}")
    return skipped


def cmd_refresh(playlists_file=None, music_dir=None, cfg=None):
    """Re-read the Apple Music library while Sync Library is briefly on.

    iTunes' automation interface has no way to toggle Sync Library (it exposes
    only LibraryPlaylist, LibrarySource and LibraryXMLPath), so the two steps
    whitefruit can't take itself are prompted for instead. Everything after
    this runs with Sync Library off, which is what keeps whitefruit's own
    downloads out of your Apple Music library -- and is required for a classic
    iPod to sync at all.
    """
    cfg = cfg or settings.load()
    playlists_file = Path(playlists_file or cfg["playlists_file"])
    urls = read_urls(playlists_file)
    if not any(sources.kind(u) == "itunes" for u in urls):
        print(f"{C.YELLOW}no itunes: sources in {playlists_file}, "
              f"nothing to refresh{C.RESET}")
        return

    print(f"{C.DIM}Sync Library has to be ON for iTunes to show your Apple Music"
          f" library, and OFF for whitefruit's own files to stay out of it.{C.RESET}")

    if not sync_on_off(cfg, music_dir or cfg["music_dir"]):
        return
    print(f"{C.DIM}Done. Downloading and syncing from here on use this snapshot, "
          f"so nothing goes back up to Apple Music.{C.RESET}")


def offer_local_swaps(unresolved, cfg):
    """Offer to put your own files back where whitefruit downloaded a copy.

    These are tracks whitefruit fetched from YouTube before it knew you owned
    the original -- your playlists on the iPod are currently holding the
    YouTube version of a song sitting on your own disk.
    """
    swaps = [t for t in unresolved if t.get("kind") == "swap"]
    if not swaps:
        return
    print()
    print(f"{C.YELLOW}{C.BOLD}{len(swaps)} track(s) were downloaded from YouTube "
          f"but you own the original.{C.RESET}")
    for t in swaps[:6]:
        print(f"  {C.DIM}{t['playlist']}: {t['query']}{C.RESET}")
    if len(swaps) > 6:
        print(f"  {C.DIM}...and {len(swaps) - 6} more{C.RESET}")
    print(f"{C.DIM}  whitefruit can replace with your own local files, which reduces"
          f"disk space and may be more accurate.{C.RESET}")
    if not ask_yes_no("Replace the YouTube copies with your own files?", default=True):
        return
    done = sum(1 for t in swaps if download.swap_to_local(t))
    print(f"{C.GREEN}replaced {done} of {len(swaps)}{C.RESET}")


def fill_in_by_hand(ytdlp, unresolved, cfg):
    """Offer to take a YouTube link for each track the search couldn't find.

    Left to the end rather than asked mid-run: the searches take a long time
    and are worth leaving unattended, and by now the full list of misses is
    known instead of arriving one at a time.
    """
    unresolved = [t for t in unresolved if t.get("kind") != "swap"]
    if not unresolved or not sys.stdin.isatty():
        return
    print()
    print(f"{C.YELLOW}{C.BOLD}{len(unresolved)} track(s) couldn't be found "
          f"automatically.{C.RESET}")
    if any(t["note"] for t in unresolved):
        print(f"{C.DIM}  Several are age-restricted -- setting "
              f"cookies_from_browser in Settings fixes those in one go.{C.RESET}")
    if not ask_yes_no("Paste YouTube links for them now?", default=False):
        return

    print()
    print(f"{C.DIM}Enter to skip one, or 'q' to stop.{C.RESET}")
    print()
    for i, target in enumerate(unresolved, 1):
        note = f" {C.DIM}({target['note']}){C.RESET}" if target["note"] else ""
        print(f"{C.CYAN}[{i}/{len(unresolved)}]{C.RESET} {C.BOLD}{target['query']}"
              f"{C.RESET}{note}")
        print(f"      {C.DIM}in {target['playlist']}{C.RESET}")
        link = input("      link: ").strip()
        if link.lower() == "q":
            break
        if not link:
            print()
            continue
        if download.download_link(ytdlp, link, target, cfg):
            print(f"      {C.GREEN}saved{C.RESET}")
        else:
            print(f"      {C.YELLOW}that link didn't give a usable track{C.RESET}")
        print()


def cmd_fix(playlists_file=None, music_dir=None, cfg=None):
    """Bring an existing library back into line with its sources.

    Everything here corrects files that are already downloaded -- tags, track
    numbers, songs deleted upstream, leftovers in an old format. Kept as its
    own action rather than run on every download, because a healthy library
    doesn't need any of it.
    """
    cfg = cfg or settings.load()
    music_dir = Path(music_dir or cfg["music_dir"])
    urls = read_urls(Path(playlists_file or cfg["playlists_file"]))

    # Repairing means comparing against what the sources say *now*, so for
    # Apple Music that means a fresh read -- same Sync Library dance as the
    # refresh, for the same reason.
    if any(sources.kind(u) == "itunes" for u in urls):
        print(f"{C.DIM}Repairing compares your files against what each source says "
              f"now, so the Apple Music side has to be re-read first.{C.RESET}")
        if not sync_on_off(cfg, music_dir):
            return

    print()
    print(f"{C.MAGENTA}checking every folder against its source…{C.RESET}")
    print()
    totals, orphans = download.repair_library(
        download.find_ytdlp(), urls, music_dir, cfg)

    print()
    print(f"{C.GREEN}{totals['folders']} folder(s): {totals['retagged']} retagged, "
          f"{totals['renumbered']} renumbered, {totals['removed']} removed as "
          f"deleted upstream, {totals['stale']} old-format leftovers{C.RESET}")
    if totals["held_back"]:
        print(f"{C.YELLOW}{totals['held_back']} file(s) looked deleted upstream but "
              f"were kept -- too many at once to trust one read.{C.RESET}")

    if orphans:
        print()
        print(f"{C.YELLOW}{len(orphans)} folder(s) belong to no source any more:{C.RESET}")
        for f in orphans:
            print(f"  {C.DIM}{f.name}{C.RESET}")
        print(f"{C.DIM}  Either the playlist was deleted upstream, or they were "
              f"never whitefruit's. Deleting removes the audio too.{C.RESET}")
        if ask_yes_no("Delete these folders?", default=False):
            for f in orphans:
                shutil.rmtree(f, ignore_errors=True)
                print(f"  removed {f.name}")

    if cfg.get("itunes_sync", True):
        print()
        print(f"{C.DIM}re-importing so iTunes matches the repaired files{C.RESET}")
        cmd_itunes(music_dir, cfg)


def cmd_dedupe(music_dir=None, dry_run=False, cfg=None):
    cfg = cfg or settings.load()
    dedupe_mod.dedupe(Path(music_dir or cfg["music_dir"]), dry_run)


def cmd_repair(music_dir=None, dry_run=False, cfg=None):
    cfg = cfg or settings.load()
    fixed, _ = repair_mod.repair(Path(music_dir or cfg["music_dir"]), dry_run, cfg)
    # Re-encoding renames files (.m4a -> .mp3), which leaves iTunes pointing at
    # paths that no longer exist. It then refuses to copy them to the iPod
    # ("file type is not supported"). Resync so the library matches disk.
    if fixed and not dry_run:
        if cfg.get("itunes_sync", True):
            print(f"\n{C.DIM}files were renamed; resyncing iTunes so its library "
                  f"doesn't point at the old ones{C.RESET}")
            cmd_itunes(music_dir, cfg)
        else:
            print(f"\n{C.YELLOW}Files were renamed. Run 'Sync to iTunes' before "
                  f"syncing your iPod, or iTunes will still reference the old "
                  f"files and refuse to copy them.{C.RESET}")


def cmd_itunes(music_dir=None, cfg=None):
    cfg = cfg or settings.load()
    itunes.import_playlists(music_dir or cfg["music_dir"])


def describe_snapshot(urls):
    """How stale the Apple Music snapshot is, in words."""
    taken = sources.snapshot_taken(urls)
    if taken is None:
        return "there isn't one yet"
    mins = (datetime.now() - taken).total_seconds() / 60
    if mins < 90:
        return f"the last one is {int(mins)} min old"
    if mins < 2880:
        return f"the last one is {round(mins / 60)} hours old"
    return f"the last one is {round(mins / 1440)} days old"


def refresh_snapshot_first(cfg, music_dir, urls):
    """Re-read Apple Music before a run that is about to compare against it.

    New songs are found by comparing the snapshot against what's on disk, so a
    run against the snapshot taken during the *last* run can only ever conclude
    that nothing has changed. Re-reading is the only part that needs Sync
    Library on, hence the two prompts.
    """
    if not any(sources.kind(u) == "itunes" for u in urls):
        return
    print(f"{C.DIM}Apple Music is compared against a snapshot, so a stale one "
          f"finds no new songs. {describe_snapshot(urls)}.{C.RESET}")
    if ask_yes_no("Re-read Apple Music first?", default=True):
        sync_on_off(cfg, music_dir)


def cmd_all(playlists_file=None, music_dir=None, auto_yes=True, dry_run=False, cfg=None):
    cfg = cfg or settings.load()
    refresh_snapshot_first(cfg, Path(music_dir or cfg["music_dir"]),
                           read_urls(Path(playlists_file or cfg["playlists_file"])))
    cmd_download(playlists_file, music_dir, auto_yes, cfg)
    if cfg.get("dedupe_hardlink", True):
        cmd_dedupe(music_dir, dry_run, cfg)
    if cfg.get("itunes_sync", True):
        cmd_itunes(music_dir, cfg)


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    # A scripted run has no one to answer, and input() would raise EOFError
    # part-way through the work rather than simply taking the default.
    if not sys.stdin.isatty():
        return default
    choice = input(
        f"{C.CYAN}{prompt} {C.DIM}[{hint}]{C.RESET}: ").strip().lower()
    if not choice:
        return default
    return choice.startswith("y")


def press_key(lines):
    """A block the run stops at until a key is actually pressed.

    Used for the Sync Library steps: they have to happen at a particular
    moment, and a line of text among the scroll is far too easy to miss.
    """
    width = max(len(l) for l in lines) + 4
    print()
    print(f"{C.YELLOW}{'=' * width}{C.RESET}")
    for l in lines:
        print(f"{C.YELLOW}  {C.BOLD}{l}{C.RESET}")
    print(f"{C.YELLOW}{'=' * width}{C.RESET}")
    if not sys.stdin.isatty():
        return
    print(f"{C.DIM}  press any key once you have done that…{C.RESET}",
          end="", flush=True)
    try:
        import msvcrt
        msvcrt.getch()
    except ImportError:
        input()
    print()
    print()


def sync_on_off(cfg, music_dir, forget=True):
    """The Sync Library dance, shared by refresh and repair.

    iTunes' automation interface has no way to toggle Sync Library (it offers
    only LibraryPlaylist, LibrarySource and LibraryXMLPath), so these two steps
    are the user's to take and whitefruit just stops until they have.
    """
    if forget:
        print(f"{C.DIM}Clearing removes whitefruit's tracks and its playlists from "
              f"iTunes so Sync Library can't push them into your Apple Music "
              f"library. The files stay on disk and importing puts them back, but "
              f"the iPod's 'sync this playlist' tick is lost and has to be "
              f"re-ticked. Playlists of your own are never touched.{C.RESET}")
    if forget and ask_yes_no("Clear whitefruit's tracks and playlists from iTunes?",
                             default=True):
        itunes.forget_tracks(music_dir)
        print()

    # Scope. Re-reading only what's listed is the fast, predictable option;
    # scanning everything is how a playlist you added in iTunes gets noticed,
    # at the cost of walking every playlist over COM.
    urls = read_urls(Path(cfg["playlists_file"]))
    listed = [u for u in urls if sources.kind(u) == "itunes"]
    print(f"{C.DIM}{len(listed)} Apple Music source(s) are listed in "
          f"{cfg['playlists_file']}.{C.RESET}")
    print(f"{C.DIM}  Scanning the whole library instead also finds playlists you "
          f"haven't listed, but reads every playlist in iTunes -- far slower, and "
          f"it downloads nothing new unless you then add them.{C.RESET}")
    whole = ask_yes_no("Scan the whole library instead of just what's listed?",
                       default=False)
    if whole:
        urls = urls + ["itunes:playlists"]

    while True:
        press_key(["Turn Sync Library ON in iTunes",
                   "iTunes > Edit > Preferences > General > Sync Library"])
        # Read only once iTunes has stopped pulling the cloud library down;
        # otherwise the snapshot is a scattered subset of every playlist.
        count, settled = sources.wait_until_settled()
        if count and not settled:
            print(f"{C.YELLOW}iTunes is still syncing ({count} tracks and "
                  f"climbing).{C.RESET}")
            if not ask_yes_no("Read anyway? (counts will be short)", default=False):
                continue

        read, tracks, shrunk = sources.refresh_cache(urls, cfg)
        if tracks:
            print()
            print(f"{C.GREEN}read {tracks} track(s) from {read} source(s){C.RESET}")
            if shrunk:
                print()
                print(f"{C.YELLOW}{C.BOLD}[!] {len(shrunk)} playlist(s) came back "
                      f"smaller than last time:{C.RESET}")
                for name, was, now in shrunk:
                    print(f"{C.YELLOW}      {name}: {was} -> {now}{C.RESET}")
                print(f"{C.DIM}    iTunes fills the cloud library in gradually, so "
                      f"this usually means it hadn't finished. Check the counts "
                      f"match Apple Music, wait, and read again.{C.RESET}")
                if ask_yes_no("Read again?", default=True):
                    continue
            break
        print()
        print(f"{C.YELLOW}iTunes isn't showing any Apple Music tracks.{C.RESET}")
        print(f"{C.DIM}  It needs an Apple Music subscription, and iTunes can take "
              f"a minute to fill the library in after you tick it.{C.RESET}")
        if not ask_yes_no("Look again?", default=True):
            return False

    press_key(["Turn Sync Library OFF again",
               "Leaving it on would push whitefruit's own files",
               "up into your Apple Music library."])
    return True


def ask_path(prompt: str, default: str) -> str:
    val = input(f"{C.CYAN}{prompt} {C.DIM}[{default}]{C.RESET}: ").strip()
    return val if val else default


def value_style(key: str, val, cfg: dict, checked=None):
    """Colour and trailing hint for a setting's value in the settings screen.

    Used to show at a glance whether sign-in is actually set up, since that's
    the difference between Premium tracks downloading and being searched for.
    `checked` is the (ok, message) result of the last 't' test, if one has run.
    """
    def signed_in():
        if checked is None:
            return C.GREEN, f"{C.DIM} — press 't' to check functionality{C.RESET}"
        ok, msg = checked
        if ok:
            return C.GREEN, f" {C.GREEN}✓{C.RESET}"
        return C.RED, f" {C.RED}✗{C.RESET}{C.DIM} {msg}{C.RESET}"

    if key == "cookies_from_browser":
        if val and val != "none":
            return signed_in()
        if (cfg.get("cookies_file") or "").strip():
            return C.DIM, f"{C.DIM} — using the cookies file below instead{C.RESET}"
        return C.YELLOW, f"{C.YELLOW} — not signed in, using default FREE config{C.RESET}"
    if key == "cookies_file":
        path = (val or "").strip()
        if not path:
            return C.DIM, ""
        if not Path(path).exists():
            return C.RED, f"{C.DIM} — file not found, will run signed out{C.RESET}"
        return signed_in()
    return "", ""


def describe(key: str):
    """Setting description as display lines. The descriptions are written as
    indented triple-quoted strings, so strip that source indentation off the
    continuation lines and let the caller apply its own."""
    text = settings.DESCRIPTIONS.get(key, "")
    if not text:
        return []
    first, *rest = text.splitlines()
    return [first.strip()] + textwrap.dedent("\n".join(rest)).splitlines()


def clear_screen():
    # Skip when output isn't a terminal, so piped runs stay readable.
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def pause():
    if not sys.stdin.isatty():
        return
    print(f"\n{C.DIM}Press any key to return to the menu...{C.RESET}", end="", flush=True)
    try:
        import msvcrt
        msvcrt.getch()
    except ImportError:
        input()
    print()


def section(title: str):
    print(f"\n{C.MAGENTA}{C.BOLD}▶ {title}{C.RESET}", flush=True)


def run_step(title: str, func):
    """Run one menu action with a section header and a clear pass/fail
    completion message. Returns (ok: bool, func()'s return value)."""
    section(title)
    try:
        result = func()
    except SystemExit as e:
        print(f"{C.RED}✗ {title} failed: {e.code}{C.RESET}")
        return False, None
    except Exception as e:
        print(f"{C.RED}✗ {title} failed: {e}{C.RESET}")
        return False, None
    print(f"{C.GREEN}✓ {title} complete, no errors{C.RESET}")
    return True, result


BANNER = """    
                 .:-=+++=-:.                 
            .:=*##***+++***##*=:.            
          .=*#*=:..       ..:=*#*+.          
        .::++.                 .++::.        
      .:*#=.     .-+*****+-.     .=#*:.      
      :*#-    .-*###########*-.    -#*-      
     :#*:   .-*###############*-.   :*#:     
    .*#=.   -######++*##########-   .=#*.    
    :**:   .######*. ..-*########:   :**:    
    -#*.   =######*.     .-*#####=   .*#-    
    -#*.   =######*.     .-*#####=   .*#-    
    -**:   .######*. ..-*########:   :**-    
    .*#=.   -######++*##########-   .=#*.    
     :#*:   .-*###############*-.   :*#:     
      :*#-    .-*###########*-.    -##-      
      .:*#=.     .-+*****+-.     .=#*:.      
        .::++.                 .++::.        
          .=*#*=:..       ..:=*#*+.          
            .:=*##***+++***##*=:.            
                 .:-=+++=-:.                 
    """


def splash():
    """Banner, with the title and tagline centred under the artwork."""
    width = max(len(line) for line in BANNER.splitlines())
    title = "whitefruit"
    tagline = "YouTube Music, Spotify, Apple Music -> iPod."
    print(f"{C.GREEN}{BANNER}{C.RESET}")
    # Centre the text itself -- a newline inside .center() counts as a
    # character and throws the padding off, so keep the blank line separate.
    print(f"{C.BOLD}{C.GREEN}{title.center(width-1)}{C.RESET}")
    print()
    print(f"{C.DIM}{tagline.center(width)}{C.RESET}")


def settings_screen(cfg: dict):
    """Edit and persist settings. Each key shows what it actually does."""
    keys = list(settings.DEFAULTS)
    keyw = max(len(k) for k in keys)  # so values line up whatever the key lengths
    notice = ""  # survives the repaint, so messages aren't wiped before they're read
    checked = None  # (ok, message) from the last 't' sign-in test
    while True:
        clear_screen()
        print(f"\n{C.MAGENTA}{C.BOLD}▶ Settings{C.RESET} {C.DIM}({settings.SETTINGS_FILE.name}){C.RESET}")
        if notice:
            print(notice)
            notice = ""
        for i, k in enumerate(keys, 1):
            val = cfg.get(k)
            shown = {True: "on", False: "off"}.get(val, val)
            colour, hint = value_style(k, val, cfg, checked)
            # Right-align the number so single- and double-digit entries put
            # their key names in the same column.
            print(f"  {C.CYAN}{i:>2}){C.RESET} {k:<{keyw}} {colour}{C.BOLD}{shown}{C.RESET}{hint}")
            for line in describe(k):
                print(f"       {C.DIM}{line}{C.RESET}")
        print(f"\n  {C.CYAN}s){C.RESET} save and go back    {C.CYAN}r){C.RESET} reset to defaults"
              f"    {C.CYAN}t){C.RESET} test sign-in    {C.CYAN}b){C.RESET} back without saving\n")

        choice = input(f"{C.BOLD}Setting to change: {C.RESET}").strip().lower()
        if choice == "b":
            return settings.load()
        if choice == "t":
            urls = [l.strip() for l in Path(cfg["playlists_file"]).read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#")] \
                if Path(cfg["playlists_file"]).exists() else []
            # Only YouTube sources use cookies, so a spotify:/itunes: line
            # would make this test fail for reasons that aren't sign-in.
            urls = [u for u in urls if sources.kind(u) == "youtube"]
            if not urls:
                notice = f"{C.YELLOW}need at least one playlist URL in {cfg['playlists_file']} to test against{C.RESET}"
                continue
            print(f"{C.DIM}checking...{C.RESET}", flush=True)
            # Result shows as a tick/cross on the setting's own row, rather
            # than as a banner above the list.
            checked = download.test_signin(download.find_ytdlp(), cfg, urls[0])
            continue
        if choice == "s":
            settings.save(cfg)
            print(f"{C.GREEN}✓ saved to {settings.SETTINGS_FILE}{C.RESET}")
            return cfg
        if choice == "r":
            cfg = dict(settings.DEFAULTS)
            notice = f"{C.YELLOW}reset to defaults (not saved yet){C.RESET}"
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(keys):
            notice = f"{C.YELLOW}Enter a number from 1 to {len(keys)}, or s/r/b.{C.RESET}"
            continue

        key = keys[int(choice) - 1]
        if key in ("cookies_from_browser", "cookies_file"):
            checked = None  # a previous result doesn't apply to a new setting
        print()
        for line in describe(key):
            print(f"  {C.DIM}{line}{C.RESET}")
        if key in settings.BOOLS:
            cfg[key] = not cfg[key]
            notice = f"{C.GREEN}{key} -> {'on' if cfg[key] else 'off'}{C.RESET}"
        elif key in settings.CHOICES:
            opts = settings.CHOICES[key]
            print(f"  options: {', '.join(opts)}")
            val = input(f"  {key} [{cfg[key]}]: ").strip()
            if val and val in opts:
                cfg[key] = val
                notice = f"{C.GREEN}{key} -> {val}{C.RESET}"
            elif val:
                notice = f"{C.YELLOW}not one of: {', '.join(opts)}{C.RESET}"
        else:
            val = input(f"  {key} [{cfg[key]}]: ").strip()
            if val:
                cfg[key] = int(val) if isinstance(cfg[key], int) else val
                notice = f"{C.GREEN}{key} -> {cfg[key]}{C.RESET}"


def run_menu():
    cfg = settings.load()
    while True:
        clear_screen()
        splash()
        print(
            f"\n{C.CYAN}1){C.RESET} Auto mode\n"
            f"     {C.DIM}Download new tracks from every source, dedupe, then sync to iTunes.{C.RESET}\n\n"
            f"{C.CYAN}2){C.RESET} Download / update local music\n"
            f"     {C.DIM}Fetch anything listed in playlists.txt that isn't on disk yet.{C.RESET}\n\n"
            f"{C.CYAN}3){C.RESET} Remove duplicate songs\n"
            f"     {C.DIM}Drop song duplicates while keeping full playlist integrity.{C.RESET}\n\n"
            f"{C.CYAN}4){C.RESET} Sync downloaded songs to iTunes\n"
            f"     {C.DIM}Rebuild each playlist in iTunes from local files.{C.RESET}\n\n"
            f"{C.CYAN}5){C.RESET} Re-encode local music files\n"
            f"     {C.DIM}Convert existing local tracks to the configured format/bitrate without redownloading.{C.RESET}\n\n"
            f"{C.CYAN}6){C.RESET} Refresh Apple Music library\n"
            f"     {C.DIM}Re-read iTunes with Sync Library briefly on, so downloading can run with it off.{C.RESET}\n\n"
            f"{C.CYAN}7){C.RESET} Repair library\n"
            f"     {C.DIM}Fix tags, track order and leftovers on music already downloaded.{C.RESET}\n\n"
            f"{C.CYAN}8){C.RESET} Settings\n"
            f"     {C.DIM}format {cfg['audio_format']} · {cfg['audio_bitrate']} · sources in {cfg['playlists_file']}{C.RESET}\n\n"
            f"{C.CYAN}9){C.RESET} Quit\n"
        )
        choice = input(f"{C.BOLD}Choice [1-9]: {C.RESET}").strip()
        if choice == "9":
            return
        if choice not in ("1", "2", "3", "4", "5", "6", "7", "8"):
            print(f"{C.YELLOW}Please enter a number from 1 to 9.{C.RESET}")
            pause()
            continue

        clear_screen()
        if choice == "1":
            print(f"{C.YELLOW}{C.BOLD}[!] Auto mode runs start to finish with minimal user input.{C.RESET}")
            print(f"{C.YELLOW}    format {C.BOLD}{cfg['audio_format']}{C.RESET}{C.YELLOW} @ {cfg['audio_bitrate']}"
                  f" · dedupe {'on' if cfg['dedupe_hardlink'] else 'off'}"
                  f" · iTunes sync {'on' if cfg['itunes_sync'] else 'off'}{C.RESET}")
            print(f"{C.YELLOW}    into {cfg['music_dir']}{C.RESET}")
            print(f"{C.YELLOW}    Check option 8 first if that isn't what you want.{C.RESET}\n")
            if not ask_yes_no("Start?", default=True):
                continue

            urls = read_urls(Path(cfg["playlists_file"]))
            if any(sources.kind(u) == "itunes" for u in urls):
                run_step("Re-reading Apple Music",
                         lambda: refresh_snapshot_first(
                             cfg, Path(cfg["music_dir"]), urls))

            ok1, skipped = run_step("Downloading / updating playlists",
                                    lambda: cmd_download(cfg=cfg, auto_yes=True))
            ok = ok1
            if cfg.get("dedupe_hardlink", True):
                ok2, _ = run_step("Removing duplicate songs", lambda: cmd_dedupe(cfg=cfg))
                ok = ok and ok2
            if cfg.get("itunes_sync", True):
                ok3, _ = run_step("Syncing to iTunes", lambda: cmd_itunes(cfg=cfg))
                ok = ok and ok3

            if ok and not skipped:
                print(f"\n{C.GREEN}{C.BOLD}✓ All steps complete, no errors. Enjoy your new music :){C.RESET}")
            elif ok and skipped:
                print(f"\n{C.YELLOW}{C.BOLD}✓ All steps complete, but {len(skipped)} "
                      f"track(s) were skipped during download:{C.RESET}")
                for reason in skipped:
                    print(f"{C.YELLOW}  - {reason}{C.RESET}")
            else:
                print(f"\n{C.RED}{C.BOLD}✗ Finished with errors, see above{C.RESET}")
        elif choice == "2":
            # Off: each changed playlist asks Full / New only / Skip.
            # On: every changed playlist silently takes "New only".
            auto_yes = ask_yes_no(
                "Automatically default to fetching new tracks?",
                default=True)
            run_step("Downloading / updating playlists",
                     lambda: cmd_download(cfg=cfg, auto_yes=auto_yes))
        elif choice == "3":
            dry_run = ask_yes_no(
                "Dry run first (show what would change, don't touch anything)?")
            run_step("Removing duplicate songs", lambda: cmd_dedupe(cfg=cfg, dry_run=dry_run))
        elif choice == "4":
            run_step("Syncing to iTunes", lambda: cmd_itunes(cfg=cfg))
        elif choice == "5":
            dry_run = ask_yes_no(
                "Dry run first (list what would be re-encoded, change nothing)?")
            run_step(f"Re-encoding to {cfg['audio_format']}",
                     lambda: cmd_repair(cfg=cfg, dry_run=dry_run))
        elif choice == "6":
            run_step("Refreshing the Apple Music library", lambda: cmd_refresh(cfg=cfg))
        elif choice == "7":
            run_step("Repairing the library", lambda: cmd_fix(cfg=cfg))
        elif choice == "8":
            cfg = settings_screen(dict(cfg))
            continue  # settings is its own screen; no summary to read

        pause()


def main():
    if len(sys.argv) == 1:
        run_menu()
        return

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    # Default None, not the module default: argparse always supplies a value,
    # so a real default here would silently override settings.json and point
    # every subcommand at the wrong library.
    common.add_argument("--music-dir", default=None,
                        help="override the music directory from settings.json")

    p_dl = sub.add_parser("download", parents=[
                          common], help="fetch/update playlists")
    p_dl.add_argument("playlists_file", nargs="?", default=None)
    p_dl.add_argument("--yes", action="store_true",
                      help="don't prompt; download new tracks only")
    p_dl.set_defaults(func=lambda a: cmd_download(
        a.playlists_file, a.music_dir, a.yes))

    p_dd = sub.add_parser(
        "dedupe", parents=[common], help="remove/hard-link duplicate songs")
    p_dd.add_argument("--dry-run", action="store_true")
    p_dd.set_defaults(func=lambda a: cmd_dedupe(a.music_dir, a.dry_run))

    p_rp = sub.add_parser(
        "repair", parents=[common],
        help="re-encode tracks that exceed iPod playback limits")
    p_rp.add_argument("--dry-run", action="store_true")
    p_rp.set_defaults(func=lambda a: cmd_repair(a.music_dir, a.dry_run))

    p_fx = sub.add_parser(
        "fix", parents=[common],
        help="repair tags, order and leftovers on music already downloaded")
    p_fx.add_argument("playlists_file", nargs="?", default=None)
    p_fx.set_defaults(func=lambda a: cmd_fix(a.playlists_file, a.music_dir))

    p_rf = sub.add_parser(
        "refresh", parents=[common],
        help="re-read the Apple Music library (Sync Library briefly on)")
    p_rf.add_argument("playlists_file", nargs="?", default=None)
    p_rf.set_defaults(func=lambda a: cmd_refresh(a.playlists_file, a.music_dir))

    p_it = sub.add_parser(
        "itunes", parents=[common], help="(re)import playlist folders into iTunes")
    p_it.set_defaults(func=lambda a: cmd_itunes(a.music_dir))

    p_all = sub.add_parser(
        "all", parents=[common], help="download -> dedupe -> itunes")
    p_all.add_argument("playlists_file", nargs="?", default=None)
    p_all.add_argument("--yes", action="store_true",
                       help="don't prompt; download new tracks only")
    p_all.add_argument("--dry-run", action="store_true",
                       help="dry-run the dedupe step")
    p_all.set_defaults(func=lambda a: cmd_all(
        a.playlists_file, a.music_dir, a.yes, a.dry_run))

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
