"""Track lists from services whitefruit can't download from.

Spotify and Apple Music are DRM-protected: yt-dlp refuses Spotify outright
("known to use DRM protection") and doesn't recognise Apple Music URLs at all.
So nothing in here fetches audio. Each resolver only produces a track list,
and download.py then searches YouTube for every track -- the same path
Premium-only YouTube tracks already take, with the same caveat that the
recording found may not be the one you meant.

Accepted in playlists.txt, alongside ordinary YouTube URLs:

    https://open.spotify.com/playlist/ID   one playlist (also spotify:playlist:ID)
    https://open.spotify.com/album/ID      one album
    spotify:liked                          Liked Songs
    spotify:playlists                      every playlist you own or follow
    spotify:albums                         every saved album
    itunes:library                         every playlist, plus the strays
    itunes:playlists                       every iTunes playlist
    itunes:playlist:Name                   one iTunes playlist
    itunes:strays                          library tracks in no playlist
"""
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .term import C, status

TOKEN_FILE = Path(__file__).resolve().parent.parent / "spotify_token.json"
CACHE_FILE = Path(__file__).resolve().parent.parent / "library_cache.json"
PS_SCRIPT = Path(__file__).with_name("itunes_export.ps1")

# Spotify only permits a loopback redirect on 127.0.0.1 (not "localhost"), and
# this exact URI has to be registered on the app in their dashboard.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
API = "https://api.spotify.com/v1/"


def kind(url: str) -> str:
    """Which resolver owns this line of playlists.txt."""
    u = url.strip().lower()
    if u.startswith(("spotify:", "https://open.spotify.com/",
                     "http://open.spotify.com/")):
        return "spotify"
    if u.startswith("itunes:"):
        return "itunes"
    return "youtube"


def track_id(query: str) -> str:
    """Stable id for a track we can only identify by name.

    A hash of the search string rather than the service's own id, because the
    search string is what actually decides which file we end up with. It
    survives a Spotify relink or an iTunes library rebuild, and two services
    naming the same song produce the same id, so dedupe.py hard-links across
    them. Two entries that would search for exactly the same thing are, for
    our purposes, the same track.
    """
    # Normalised here rather than relying on the caller, so the id depends on
    # what the query means and not on how it was spaced or capitalised.
    query = " ".join(query.lower().split())
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:11]


def _entries(tracks):
    """(id, search query, tags) per track, first occurrence of a song winning.

    `tracks` is an iterable of dicts: artist, title, album, album_artist and
    optionally local, track, disc, year, genre. The artist is folded into the
    search query because that is what finds the right song on YouTube, but it
    is kept out of the title tag -- otherwise every song in iTunes reads
    "Artist - Title". The tags are what the source service says, not what the
    YouTube upload claims, which is usually better and often the only place an
    album name or track number exists at all.

    A library can hold the same song twice; letting both through would give
    two files one id, and renumbering would then fight over the position.
    """
    seen, out = set(), []
    for t in tracks:
        clean = {k: " ".join(str(t.get(k) or "").split())
                 for k in ("artist", "title", "album", "album_artist",
                           "genre", "local")}
        if not clean["title"]:
            continue
        query = (f"{clean['artist']} - {clean['title']}"
                 if clean["artist"] else clean["title"])
        # Keyed by id, not by the string: ids ignore case, so two spellings of
        # the same song would otherwise both get through and then collide.
        tid = track_id(query)
        if tid in seen:
            continue
        seen.add(tid)
        tags = {"title": clean["title"], "artist": clean["artist"],
                "album": clean["album"],
                "album_artist": clean["album_artist"] or clean["artist"],
                "genre": clean["genre"],
                # "_local" is not a tag. It rides along in the same dict so
                # the path to a file you already own reaches the downloader
                # without every entry tuple growing another element; retag()
                # skips underscore-prefixed keys for exactly this reason.
                "_local": clean["local"]}
        # The album's own numbering, so iTunes' album view makes sense. A
        # zero or missing value is left out rather than written as "0".
        for key, field in (("track", "track"), ("disc", "disc"), ("date", "year")):
            try:
                n = int(t.get(field) or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                tags[key] = str(n)
        out.append((tid, query, tags))
    return _one_artist_per_album(out)


def _one_artist_per_album(entries):
    """Give every track on an album the same album artist.

    iTunes groups an album by album *and* album artist, so one differing value
    splits the record into a row of near-identical entries -- "Hurry Up
    Tomorrow" showing up six times, once per guest feature. That happens
    whenever the source has no album artist of its own and the track artist
    stands in for it, which for a collaboration reads "The Weeknd & Anitta".

    The winner is whichever value the album's tracks use most, rather than
    anything parsed out of the name: a real album nearly always has some solo
    tracks, and splitting names on "&" or "," would maul acts like "Tyler, The
    Creator". Even with no clear winner the album still stops splitting,
    because every track ends up agreeing.
    """
    from collections import Counter
    votes = {}
    for _, _, tags in entries:
        if tags["album"]:
            votes.setdefault(tags["album"], Counter())[tags["album_artist"]] += 1
    for _, _, tags in entries:
        if tags["album"]:
            tags["album_artist"] = votes[tags["album"]].most_common(1)[0][0]
    return entries


# --------------------------------------------------------------------------
# Spotify
# --------------------------------------------------------------------------

def _pkce_login(client_id: str) -> dict:
    """Authorization Code + PKCE against a loopback redirect.

    PKCE rather than client credentials so there's no client *secret* to keep
    on disk, and because the same token then reaches private things (Liked
    Songs, your own playlists) that client credentials can't.
    """
    verifier = secrets.token_urlsafe(72)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge,
    })

    class Handler(BaseHTTPRequestHandler):
        code = error = None

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            Handler.code = q.get("code", [None])[0]
            Handler.error = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>whitefruit</h2><p>Signed in. You can close "
                             b"this tab and go back to the terminal.</p>")

        def log_message(self, *a):
            pass  # the default handler logs every hit to stderr

    print(f"  {C.DIM}opening your browser to authorise whitefruit with Spotify...{C.RESET}")
    print(f"  {C.DIM}if it doesn't open, visit:{C.RESET}\n  {auth_url}")
    with HTTPServer(("127.0.0.1", 8888), Handler) as srv:
        webbrowser.open(auth_url)
        srv.handle_request()  # exactly one request: the redirect back
    if Handler.error or not Handler.code:
        sys.exit(f"Spotify sign-in failed: {Handler.error or 'no code returned'}")

    return _token_request({
        "grant_type": "authorization_code", "code": Handler.code,
        "redirect_uri": REDIRECT_URI, "client_id": client_id,
        "code_verifier": verifier,
    })


def _token_request(form: dict) -> dict:
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit("Spotify token request failed: "
                 + e.read().decode("utf-8", "replace")[:300])


def _token(cfg: dict) -> str:
    """A usable access token, signing in or refreshing as needed.

    Cached to disk because signing in is a browser round trip -- doing it every
    run would make an unattended sync impossible.
    """
    client_id = (cfg.get("spotify_client_id") or "").strip()
    if not client_id:
        sys.exit("no spotify_client_id set. Create an app at "
                 "https://developer.spotify.com/dashboard, add the redirect URI "
                 f"{REDIRECT_URI} to it, then put its Client ID in Settings.")

    saved = {}
    if TOKEN_FILE.exists():
        try:
            saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}

    tok = None
    # A refresh token issued to a different app is useless, so treat a changed
    # client id as "not signed in" rather than failing on every request.
    if saved.get("refresh_token") and saved.get("client_id") == client_id:
        tok = _token_request({"grant_type": "refresh_token",
                              "refresh_token": saved["refresh_token"],
                              "client_id": client_id})
        if "access_token" not in tok:
            tok = None  # revoked or expired; sign in again
    if tok is None:
        tok = _pkce_login(client_id)

    # Spotify only returns a new refresh token some of the time; keep the old
    # one otherwise, or the next run has to open a browser again.
    TOKEN_FILE.write_text(json.dumps({
        "client_id": client_id,
        "refresh_token": tok.get("refresh_token") or saved.get("refresh_token"),
    }, indent=2), encoding="utf-8")
    return tok["access_token"]


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Spotify API error {e.code} on {url}: "
                 + e.read().decode("utf-8", "replace")[:300])


def _paged(url: str, token: str):
    while url:
        page = _get(url, token)
        yield from page.get("items") or []
        url = page.get("next")


def _track(t: dict, album_name: str = "", album_artist: str = ""):
    """(artist, title, album, album artist) out of a Spotify track object.

    A track inside an album response carries neither its album nor, sometimes,
    an artist, so the album's own values are passed in as the fallback.
    """
    artists = ", ".join(a["name"] for a in t.get("artists") or [] if a.get("name"))
    album_obj = t.get("album") or {}
    album = album_obj.get("name") or album_name
    # The album's own credited artist, not this track's: a featured guest on
    # one track must not split the album into two in iTunes.
    credited = ", ".join(a["name"] for a in album_obj.get("artists") or []
                         if a.get("name")) or album_artist
    # Spotify never has a local file to point at; only iTunes does.
    return {"artist": artists or credited, "title": t.get("name") or "",
            "album": album, "album_artist": credited or artists,
            "track": t.get("track_number"), "disc": t.get("disc_number"),
            "year": (album_obj.get("release_date") or "")[:4]}


def _playable(item: dict):
    """The track out of a playlist item, or None if it isn't one.

    Playlists also hold podcast episodes, files the user added from their own
    machine (no id, nothing to search for), and tombstones for tracks Spotify
    has since removed.
    """
    t = item.get("track") if "track" in item else item
    if not t or t.get("type") == "episode" or t.get("is_local") or not t.get("name"):
        return None
    return t


def _spotify_id(url: str):
    """(type, id) out of either a share link or a spotify: URI."""
    if url.lower().startswith("spotify:"):
        parts = url.split(":")
        return (parts[1], parts[2]) if len(parts) > 2 else ("", "")
    path = urllib.parse.urlparse(url).path.strip("/").split("/")
    # Share links are localised as /intl-de/playlist/ID.
    path = [p for p in path if not p.startswith("intl-")]
    return (path[0], path[1].split("?")[0]) if len(path) > 1 else ("", "")


def _spotify(url: str, cfg: dict):
    """(name, entries, nested) for any of the Spotify forms."""
    url = url.strip()
    low = url.lower()

    # spotify:liked / :playlists / :albums are whitefruit's own shorthand; the
    # three-part spotify:playlist:ID form is Spotify's real URI scheme.
    if low in ("spotify:liked", "spotify:playlists", "spotify:albums"):
        token = _token(cfg)
        what = low.split(":", 1)[1]
        if what == "liked":
            items = _paged(API + "me/tracks?limit=50", token)
            return "Liked Songs", _entries(
                _track(t) for t in map(_playable, items) if t), []
        if what == "playlists":
            return "Spotify playlists", [], [
                p["external_urls"]["spotify"]
                for p in _paged(API + "me/playlists?limit=50", token)
                if p and p.get("external_urls", {}).get("spotify")]
        return "Spotify albums", [], [
            i["album"]["external_urls"]["spotify"]
            for i in _paged(API + "me/albums?limit=50", token)
            if i.get("album", {}).get("external_urls", {}).get("spotify")]

    item_type, item_id = _spotify_id(url)
    if item_type not in ("playlist", "album", "track") or not item_id:
        sys.exit(f"don't know what to do with this Spotify link: {url}")

    token = _token(cfg)
    if item_type == "track":
        return "Spotify singles", _entries(
            [_track(_get(f"{API}tracks/{item_id}", token))]), []

    meta = _get(f"{API}{item_type}s/{item_id}", token)
    name = meta.get("name") or item_id
    if item_type == "album":
        # An album's own track objects sometimes omit the artist, so fall back
        # to the album's.
        album_artist = ", ".join(a["name"] for a in meta.get("artists") or [])
        items = _paged(f"{API}albums/{item_id}/tracks?limit=50", token)
        return name, _entries(_track(t, name, album_artist)
                              for t in map(_playable, items) if t), []
    items = _paged(f"{API}playlists/{item_id}/tracks?limit=100", token)
    return name, _entries(_track(t) for t in map(_playable, items) if t), []


# --------------------------------------------------------------------------
# Apple Music, read out of the local iTunes library
# --------------------------------------------------------------------------

def _ps(args, cfg: dict = None) -> list:
    """Run itunes_export.ps1 and return its output lines.

    The music dir goes with every call: it is how the script tells whitefruit's
    own downloads apart from files you already owned.
    """
    from . import settings as settings_mod
    music_dir = str((cfg or settings_mod.load()).get("music_dir", ""))
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(PS_SCRIPT), "-MusicDir", music_dir] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        # A warning, not an exit. A library read walks thirty-odd playlists,
        # and one of them being unreadable is not a reason to abandon the
        # other twenty-nine mid-run. An empty result reads as "no tracks
        # here", which every caller already copes with -- and repair's own
        # guard stops an empty read being mistaken for a mass deletion.
        print(f"  {C.YELLOW}couldn't read from iTunes: "
              f"{(r.stderr or r.stdout or '').strip()[:200]}{C.RESET}", flush=True)
        return []
    if r.stderr.strip():
        print(f"  {C.DIM}{r.stderr.strip()[:200]}{C.RESET}", flush=True)
    return [l.rstrip("\r") for l in r.stdout.splitlines() if l.strip()]


def _itunes(url: str):
    parts = url.split(":", 2)
    what = parts[1].lower() if len(parts) > 1 else "library"

    if what in ("library", "playlists"):
        nested = [f"itunes:playlist:{n}" for n in _ps(["-List"])]
        # "the library" means all of it: every playlist, plus whatever is in
        # the library that no playlist covers. Asking for the playlists alone
        # leaves the strays out.
        if what == "library":
            nested.append("itunes:strays")
        return "Apple Music", [], nested
    if what == "playlist":
        if len(parts) < 3 or not parts[2].strip():
            sys.exit("itunes:playlist: needs a playlist name after it")
        name, args = parts[2], ["-Playlist", parts[2]]
    elif what == "strays":
        name, args = "Apple Music (no playlist)", ["-Strays"]
    else:
        sys.exit(f"unknown iTunes source: {url}")

    tracks = []
    for row in _ps(args):
        # artist / title / album / album artist / location; a short row just
        # means the trailing fields were empty.
        f = (row.split("\t") + [""] * 9)[:9]
        tracks.append(dict(zip(("artist", "title", "album", "album_artist",
                                "local", "track", "disc", "year", "genre"), f)))
    return name, _entries(tracks), []


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(url: str, result):
    name, entries, nested = result
    cache = _load_cache()
    cache[url] = {"name": name, "entries": entries, "nested": nested,
                  "read": datetime.now().isoformat(timespec="seconds")}
    CACHE_FILE.write_text(json.dumps(cache, indent=1, ensure_ascii=False),
                          encoding="utf-8")


def snapshot_taken(urls):
    """When the iTunes snapshot behind these sources was last read, or None.

    What "new songs" is measured against is the snapshot, not iTunes itself,
    so its age is the useful thing to show before a run.
    """
    cache = _load_cache()
    stamps = [cache[u]["read"] for u in urls
              if kind(u) == "itunes" and cache.get(u, {}).get("read")]
    if not stamps:
        return None
    try:
        return datetime.fromisoformat(max(stamps))
    except ValueError:
        return None


def cached(url: str) -> bool:
    """Whether this source has a stored snapshot to download from."""
    return url in _load_cache()


def resolve(url: str, cfg: dict, refresh: bool = False):
    """(name, [(id, search query, tags), ...], [nested url, ...]).

    Same shape as download.get_playlist_info, so process_playlist can treat
    every source the same way.

    An iTunes read is served from the cache unless `refresh` says otherwise,
    because it is the one thing that needs Sync Library switched on in iTunes
    -- see refresh_cache. Spotify has no such constraint and is always read
    live, so a playlist edited a minute ago is picked up.
    """
    if kind(url) == "spotify":
        return _spotify(url, cfg)
    if not refresh:
        hit = _load_cache().get(url)
        if hit:
            return hit["name"], hit["entries"], hit["nested"]
    result = _itunes(url)
    _save_cache(url, result)
    return result


def apple_music_count() -> int:
    """How many Apple Music tracks iTunes currently holds."""
    rows = _ps(["-Count"])
    try:
        return int(rows[0])
    except (IndexError, ValueError):
        return 0


def wait_until_settled(tries: int = 25, gap: int = 3):
    """Block until iTunes stops adding tracks to its playlists.

    Sync Library arrives progressively, and reading during that gives a
    scattered subset of each playlist that is indistinguishable from a
    playlist which genuinely is that short -- a 22-song playlist read as 9,
    with the folder and the iPod then inheriting the 9, and reconcile lining
    up the other 13 for deletion. Nothing in the automation interface reports
    sync progress, so the track count is sampled instead: two readings the
    same means it has stopped moving.

    Returns (count, settled).
    """
    last, start = -1, time.monotonic()
    for attempt in range(tries):
        n = apple_music_count()
        # Nothing at all on the first look means Sync Library is off, not that
        # it is mid-download -- there is nothing coming to wait for.
        if not n and attempt == 0:
            return 0, False
        if n and n == last:
            status(f"  {C.DIM}iTunes has settled at {n} track(s).{C.RESET}",
                   done=True)
            return n, True
        status(f"  {C.DIM}waiting for iTunes to finish syncing — {n} track(s) "
               f"after {int(time.monotonic() - start)}s…{C.RESET}")
        last = n
        time.sleep(gap)
    return last, False


def refresh_cache(urls, cfg):
    """Re-read every iTunes source now and store what it said.

    Kept separate from downloading because reading is the only part that needs
    Sync Library on. Reading takes seconds; fetching the tracks takes hours,
    and leaving Sync Library on for those hours is exactly what pushes
    whitefruit's own files back up into your Apple Music library.

    Returns (sources read, tracks found, [(name, was, now) that shrank]).
    """
    before = {u: len(v["entries"]) for u, v in _load_cache().items()}
    seen, queue, tracks, shrunk = set(), [u for u in urls if kind(u) == "itunes"], 0, []
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        # Reading one playlist over COM is seconds of silence, and a library
        # read is dozens of them back to back. Say what is being read before
        # it starts, not only after it finishes.
        label = url.split(":", 2)[-1] if url.count(":") > 1 else url
        status(f"  {C.DIM}reading {label}…{C.RESET}")
        was = before.get(url)
        name, entries, nested = resolve(url, cfg, refresh=True)
        queue += nested
        tracks += len(entries)
        # An empty read is the *worst* case, not one to pass over quietly:
        # it is what a playlist looks like when Sync Library never came on.
        if not entries and not nested and not was:
            continue
        # iTunes fills the cloud library in progressively once Sync Library is
        # switched on, so reading too early snapshots a half-arrived playlist
        # -- and nothing downstream can tell that from a playlist that really
        # is that short. A count that dropped is the one visible symptom.
        if was and len(entries) < was:
            shrunk.append((name, was, len(entries)))
            print(f"  {C.YELLOW}{name}: {len(entries)} track(s) "
                  f"— was {was}{C.RESET}", flush=True)
        elif entries:
            print(f"  {C.DIM}{name}: {len(entries)} track(s){C.RESET}", flush=True)
    return len(seen), tracks, shrunk


def _selfcheck():
    """python -m whitefruit.sources -- the parsing that can silently go wrong."""
    assert kind("https://music.youtube.com/playlist?list=LM") == "youtube"
    assert kind("spotify:liked") == kind("https://open.spotify.com/album/x") == "spotify"
    assert kind("itunes:playlist:Chill") == "itunes"

    # An id follows the song, not how it happened to be written.
    assert track_id("Sam Fender - Rein Me In") == track_id("sam fender  -  rein me in")
    assert track_id("A - B") != track_id("A - C")
    # ...and has to survive the filename pattern the rest of whitefruit matches.
    from .download import FILENAME_RE
    assert FILENAME_RE.match(f"003 - A - B [{track_id('A - B')}].mp3")

    # One entry per song, first spelling kept, untitled dropped.
    T = lambda a, t, al="", aa="", **k: dict(artist=a, title=t, album=al,
                                             album_artist=aa, **k)
    got = _entries([T("A", "B", "Alb", "AA"), T("a", "b"), T("X", ""), T("C", "D")])
    assert [(tid, q) for tid, q, _ in got] == [
        (track_id("A - B"), "A - B"), (track_id("C - D"), "C - D")]
    # The artist rides in the search query but stays out of the title tag, and
    # album artist is kept apart from it so iTunes doesn't split the album.
    assert got[0][2]["title"] == "B" and got[0][2]["album_artist"] == "AA"
    assert got[1][2]["album_artist"] == "C"  # falls back to the artist

    # The album's own numbering reaches the tags; a zero is left off entirely
    # rather than written as "0".
    numbered = _entries([T("A", "B", "Alb", "AA", track=7, disc=0, year=2018)])[0][2]
    assert numbered["track"] == "7" and numbered["date"] == "2018"
    assert "disc" not in numbered

    # A track you already own carries the path to your own file...
    owned = _entries([T("A", "B", "Alb", "AA", local="D:/Music/mine.mp3")])
    assert owned[0][2]["_local"] == "D:/Music/mine.mp3"
    # ...and that path must never be written into the file as a tag.
    from .download import tag_fields
    assert "_local" not in tag_fields(owned[0][2])

    # Every track on one album gets one album artist, so iTunes shows one
    # album rather than one per guest feature.
    split = _entries([T("The Weeknd", "a", "Hurry Up Tomorrow"),
                      T("The Weeknd & Anitta", "b", "Hurry Up Tomorrow"),
                      T("The Weeknd & Future", "c", "Hurry Up Tomorrow"),
                      T("The Weeknd", "d", "Hurry Up Tomorrow")])
    assert {t["album_artist"] for _, _, t in split} == {"The Weeknd"}
    # ...and the per-track artist is untouched, so credits still read right.
    assert [t["artist"] for _, _, t in split][1] == "The Weeknd & Anitta"

    assert _spotify_id("https://open.spotify.com/playlist/abc") == ("playlist", "abc")
    assert _spotify_id("https://open.spotify.com/intl-de/album/1A2b?si=x") == ("album", "1A2b")
    assert _spotify_id("spotify:playlist:37i9") == ("playlist", "37i9")

    # A guest feature on one track must not become a second album artist.
    guest = _track({"name": "T", "artists": [{"name": "A"}, {"name": "Guest"}],
                    "album": {"name": "Alb", "artists": [{"name": "A"}],
                              "release_date": "2018-05-01"}})
    assert (guest["artist"], guest["album"], guest["album_artist"]) \
        == ("A, Guest", "Alb", "A")
    assert guest["year"] == "2018"
    # An album's own tracks fall back to the album's name and artist.
    fell = _track({"name": "T", "artists": []}, "Alb", "AA")
    assert (fell["artist"], fell["album"], fell["album_artist"]) == ("AA", "Alb", "AA")

    # Playlists hold things that aren't downloadable songs.
    assert _playable({"track": {"name": "T", "type": "track"}})
    assert _playable({"track": None}) is None
    assert _playable({"track": {"name": "T", "type": "episode"}}) is None
    assert _playable({"track": {"name": "T", "is_local": True}}) is None

    print("sources selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
