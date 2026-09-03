# Reads playlist contents out of the local iTunes library, as
# tab-separated lines for sources.py:
#   artist, title, album, albumArtist, location, track, disc, year, genre
#
# The location is empty when the track has no file, and set when you already
# own one. Both are emitted: a track you own must not be re-downloaded from
# YouTube, but it does still belong in the playlist, so the caller needs to
# know about it either way.
#
# Everything in a playlist is emitted except whitefruit's own downloads, which
# are its previous output rather than part of the source. So a track arrives
# either with an empty location (a stream, an iCloud track not downloaded, or
# one whose file has gone missing - fetch it) or with a path (you already own
# it - reference that file, never fetch it again).
#
# Streams are NOT identified by KindAsString saying "Apple Music": iTunes
# labels plenty of them plainly as "AAC audio file" - 13 of the 22 in one
# playlist tested here - so matching the label silently dropped over half of
# a playlist. Having no file is the reliable test.
#
# Apple Music content only appears once Sync Library is on in iTunes
# (Edit > Preferences > General). Signing into the account alone does not put
# the cloud library into iTunes, and this will find nothing.
#
# Nothing here downloads anything: Apple Music audio is DRM-protected, so this
# only ever produces names to search YouTube for.
#
# Needs the iTunes COM interface, which on this machine is served by the Apple
# Music app from the Microsoft Store -- there is no iTunes.exe involved.
param(
    [switch]$List,          # print playlist names instead of tracks
    [switch]$Strays,        # only library tracks that are in no playlist
    [switch]$Count,         # how many fetchable tracks the playlists hold
    [string]$Playlist = "", # restrict to one playlist by name
    # Used only to tell whitefruit's own downloads apart from your files.
    [string]$MusicDir = "$env:USERPROFILE\Music\whitefruit"
)
$ErrorActionPreference = "Stop"
# Titles are routinely non-ASCII; without this they arrive mangled in Python.
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$itunes = New-Object -ComObject iTunes.Application
$lib = $itunes.LibrarySource
# SpecialKind -eq 0 means an ordinary user playlist, not a built-in view.
$userPlaylists = @($lib.Playlists | Where-Object { $_.SpecialKind -eq 0 })

# How much of the cloud library has arrived so far. Sampled repeatedly by
# sources.py: iTunes pulls Sync Library down progressively, and a count that
# is still climbing is the only outward sign that it hasn't finished.
#
# Counted across the playlists, NOT the library. A playlist you merely follow
# holds tracks that were never added to your library, so the library total
# settles long before the playlists have finished arriving -- which is exactly
# the window a read must not happen in.
if ($Count) {
    # Each playlist's Count property, NOT a walk over its tracks. Reading one
    # property per track is a COM round trip per track, which on a library
    # this size took over two minutes -- for a number sampled repeatedly. The
    # totals move as Apple Music arrives either way, so this is the same
    # signal for about half a second.
    $n = 0
    foreach ($p in $userPlaylists) { $n += $p.Tracks.Count }
    Write-Output $n
    exit 0
}

if ($List) {
    foreach ($p in $userPlaylists) {
        # Skip playlists with nothing to fetch, or they would be listed,
        # re-read and found empty on every single run.
        foreach ($t in @($p.Tracks)) {
            # Anything that isn't already one of whitefruit's own downloads:
            # a stream to fetch, or a file of yours to reference in place. A
            # playlist made entirely of whitefruit files has nothing to do and
            # must not be listed, or it would be re-read on every run.
            $l = $t.Location
            if (-not $l -or -not $l.ToLower().StartsWith($MusicDir.ToLower())) {
                Write-Output $p.Name; break
            }
        }
    }
    exit 0
}

# For -Strays: anything a playlist already covers gets fetched with that
# playlist, so only what no playlist reaches belongs here.
#
# Note this is not the same as "every Apple Music track". A playlist you
# merely follow holds tracks that were never added to your library, which is
# why the playlists between them can reference far more tracks than the
# library itself contains.
#
# ponytail: matched on TrackDatabaseID. If an id ever fails to match across
# the library and a playlist, that track is fetched twice -- once into its
# playlist folder, once into this one -- and dedupe then hard-links the two,
# so the cost is one extra entry and no extra disk.
$skip = @{}
if ($Strays) {
    foreach ($p in $userPlaylists) {
        foreach ($t in @($p.Tracks)) { $skip[$t.TrackDatabaseID] = $true }
    }
}

if ($Playlist) {
    $source = $null
    foreach ($p in $userPlaylists) {
        if ($p.Name -ceq $Playlist) { $source = $p; break }
    }
    if (-not $source) {
        # Trimmed fallback. A folder name can't keep trailing whitespace on
        # NTFS, so a playlist genuinely called "Noye " and the folder "Noye"
        # have to be able to find each other again.
        foreach ($p in $userPlaylists) {
            if ($p.Name.Trim() -ceq $Playlist.Trim()) { $source = $p; break }
        }
    }
    if (-not $source) {
        # Compatibility normalisation, so a name typed on a normal keyboard
        # reaches a playlist named with the characters iTunes actually stores.
        # "zzz..." finds "zzz<ellipsis>", and "Fur real ?!" finds the one with
        # a full-width question mark. Both are otherwise a silent zero-track
        # read that looks exactly like an empty playlist.
        $k = [Text.NormalizationForm]::FormKD
        $want = $Playlist.Trim().Normalize($k)
        foreach ($p in $userPlaylists) {
            if ($p.Name.Trim().Normalize($k) -ceq $want) { $source = $p; break }
        }
    }
    if (-not $source) {
        # Not fatal. A playlist can be renamed, deleted, or simply invisible
        # because Sync Library is off, and none of those is a reason to take
        # down a run that is working through thirty other playlists. Nothing
        # on stdout means "no tracks", which the caller already handles.
        [Console]::Error.WriteLine("playlist not found: $Playlist")
        exit 0
    }
} else {
    # Kind -eq 1 is the library playlist itself: every track, in or out of a
    # playlist, which is how stray songs and whole albums get picked up.
    $source = $lib.Playlists | Where-Object { $_.Kind -eq 1 } | Select-Object -First 1
}

# Anything skipped is reported at the end rather than silently dropped. A
# playlist that reads short is otherwise indistinguishable from one that is
# genuinely short, and knowing what the skipped tracks *are* is the difference
# between "still syncing", "you already own these" and "unavailable here".
$skipped = @{}
foreach ($t in @($source.Tracks)) {
    $loc = $t.Location
    # whitefruit's own downloads are the one thing to leave out entirely: they
    # are this playlist's previous output, not part of its source.
    if ($loc -and $loc.ToLower().StartsWith($MusicDir.ToLower())) {
        $skipped['already downloaded by whitefruit'] = 1 + $skipped['already downloaded by whitefruit']
        continue
    }
    if ($skip.ContainsKey($t.TrackDatabaseID)) { continue }
    $artist = $t.Artist
    if (-not $artist) { $artist = $t.AlbumArtist }
    # Album artist is emitted separately, not just as a fallback for a missing
    # artist. iTunes groups an album by album *and* album artist, so leaving it
    # out splits one record into a pile of near-identical entries, one per
    # guest feature ("Daft Punk", "Daft Punk & Pharrell Williams", ...).
    $albumArtist = $t.AlbumArtist
    if (-not $albumArtist) { $albumArtist = $artist }
    # Track and disc numbers are the album's own, not a playlist position:
    # iTunes shows them in album view, where a playlist index reads as
    # nonsense (a 19-track album numbered 1, 13, 48, 63, 197...).
    Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}`t{5}`t{6}`t{7}`t{8}" -f `
        $artist, $t.Name, $t.Album, $albumArtist, $loc, `
        $t.TrackNumber, $t.DiscNumber, $t.Year, $t.Genre)
}

if ($skipped.Count -gt 0) {
    $total = @($source.Tracks).Count
    $detail = (($skipped.GetEnumerator() | Sort-Object Value -Descending |
        ForEach-Object { "$($_.Value)x $($_.Key)" }) -join ', ')
    [Console]::Error.WriteLine("of $total track(s) here, $($skipped.Values | Measure-Object -Sum | ForEach-Object Sum) already have a local file: $detail")
}
