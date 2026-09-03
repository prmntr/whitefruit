# Imports each subfolder of $srcRoot into iTunes as a playlist of the same name,
# tracks in filename order, which is playlist order. Re-running refills an
# existing playlist in place rather than appending duplicates, and leaves it
# untouched entirely when its contents already match the folder.
#
# Dedup-aware: a song's video id (embedded in its filename as "... [id].mp3")
# is added to the library at most once. The first folder that contains a given
# id imports the file normally; every later playlist that also contains that
# id (whether the file is a separate copy or an NTFS hard link from
# dedupe.py) references the *same* library track via AddTrack instead of
# importing it again.
#
# In practice a handful of duplicate library entries still slip through
# during the import phase (an intermittent iTunes COM timing quirk around
# AddFile that wasn't fully pinned down -- InProgress-waiting and delay/order
# tweaks reduced but didn't eliminate it). Rather than chase that further,
# the cleanup pass (which reliably finds and removes duplicates whenever it
# runs) is simply run again after import, so the script converges to zero
# duplicates in one invocation regardless of the exact cause.
param(
    # whitefruit always passes -MusicDir; this default is only for running the
    # script directly, and matches settings.py's default.
    [string]$MusicDir = "$env:USERPROFILE\Music\whitefruit",
    # Remove whitefruit's tracks from the library instead of importing them.
    [switch]$Forget
)
$ErrorActionPreference = "Stop"
$srcRoot = $MusicDir
# Non-capturing group on the extension so $Matches[1] stays the video id.
$idPattern = '\[([\w-]+)\]\.(?:mp3|m4a)$'
$audioExts = @('.mp3', '.m4a')

$itunes = New-Object -ComObject iTunes.Application
$lib = $itunes.LibrarySource
$libraryPlaylist = $lib.Playlists | Where-Object { $_.Kind -eq 1 } | Select-Object -First 1

# Mutated in place by Sync-Library rather than returned -- a PowerShell
# function's "return value" is actually everything written to its output
# stream during the call, so any Write-Output/uncaptured-COM-call inside it
# corrupts a `return $hashtable` value. Since hashtables are reference types,
# having the function just clear/repopulate this shared variable sidesteps
# that entirely.
# Take whitefruit's tracks back out of the library, and its playlists with
# them. The files stay on disk, so re-importing puts everything back.
#
# Wanted before turning Sync Library on: with it on, iTunes matches or uploads
# everything in the library into your Apple Music library, and that would
# include every file whitefruit has downloaded.
#
# Only playlists that are entirely whitefruit's own files are removed. One of
# yours that merely shares a name is left alone -- that mistake has already
# emptied real Apple Music playlists once.
#
# Note this does lose the iPod's "sync this playlist" tick for the ones it
# removes; the automation interface cannot set that back, so it has to be
# re-ticked by hand after the next import.
if ($Forget) {
    $removed = 0

    # Worked out BEFORE any track is deleted: once whitefruit's tracks are
    # gone its playlists are empty, and an empty playlist is indistinguishable
    # from an empty one of yours.
    $ours = New-Object System.Collections.ArrayList
    foreach ($p in $lib.Playlists) {
        if ($p.SpecialKind -ne 0) { continue }
        $tracks = @($p.Tracks)
        if ($tracks.Count -eq 0) { continue }
        $allOurs = $true
        foreach ($t in $tracks) {
            $loc = $t.Location
            if (-not $loc -or -not $loc.StartsWith($srcRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $allOurs = $false
                break
            }
        }
        if ($allOurs) { [void]$ours.Add($p.Name) }
    }
    # Walked backwards over the live collection by index, NOT forwards over an
    # @() snapshot. Deleting a track invalidates the references after it, so a
    # snapshotted loop deletes the first match and then silently matches
    # nothing else -- it reported "Removed 1" against 824 tracks. Taking the
    # last one first leaves every lower index still valid.
    $total = $libraryPlaylist.Tracks.Count
    for ($i = $total; $i -ge 1; $i--) {
        $t = $libraryPlaylist.Tracks.Item($i)
        if (-not $t) { continue }
        $loc = $t.Location
        # Both tests matter: the id pattern alone would also match a file of
        # yours that happens to be named the same way somewhere else.
        if ($loc -and $loc -match $idPattern -and
            $loc.StartsWith($srcRoot, [StringComparison]::OrdinalIgnoreCase)) {
            $t.Delete() | Out-Null
            $removed++
        }
    }
    # Backwards by index for the same reason as the tracks above: deleting a
    # playlist invalidates the references after it, so a forward pass over a
    # snapshot removes one and then quietly matches nothing else.
    $gone = 0
    for ($i = $lib.Playlists.Count; $i -ge 1; $i--) {
        $p = $lib.Playlists.Item($i)
        if (-not $p) { continue }
        if ($p.SpecialKind -eq 0 -and $ours -contains $p.Name) {
            $p.Delete() | Out-Null
            $gone++
        }
    }
    Write-Output "Removed $removed of $total whitefruit track(s) and $gone playlist(s) from iTunes (files kept)"
    exit 0
}

$idToTrack = @{}

function Sync-Library {
    # Deletes tracks whose file no longer exists, and all but one library
    # entry per video id, then rebuilds $idToTrack from what's left.
    $idToTrack.Clear()
    $seenIds = @{}
    foreach ($t in @($libraryPlaylist.Tracks)) {
        $loc = $t.Location
        if (-not $loc) { continue }
        if (-not (Test-Path -LiteralPath $loc)) {
            Write-Output "Removing missing-file track: $($t.Name) [$loc]"
            $t.Delete() | Out-Null
            continue
        }
        if ($loc -match $idPattern) {
            $vid = $Matches[1]
            if ($seenIds.ContainsKey($vid)) {
                Write-Output "Removing duplicate library entry: $($t.Name) [$loc]"
                $t.Delete() | Out-Null
            } else {
                $seenIds[$vid] = $true
            }
        }
    }
    foreach ($t in @($libraryPlaylist.Tracks)) {
        if ($t.Location -and $t.Location -match $idPattern) {
            $idToTrack[$Matches[1]] = $t
        }
    }
}

$folderNames = @(Get-ChildItem $srcRoot -Directory | ForEach-Object { $_.Name })

# Phase 1: reduce each of our playlist names to a single playlist object,
# deleting only the surplus copies left by older runs. The survivor is kept
# and refilled in place later: deleting and recreating a playlist produces a
# *new* object, so anything iTunes attached to the old one is lost -- most
# importantly the iPod's "sync these playlists" tick, which would have to be
# re-selected by hand after every sync.
#
# This happens before any track object is touched, because deleting a
# playlist while holding onto track references obtained earlier in the same
# run was observed to invalidate those references ("The track has been
# deleted" COM errors), even for tracks unrelated to the deleted playlist.
#
# SpecialKind -eq 0 means an ordinary user playlist (not the built-in
# Music/Movies/TV Shows/etc. views, which share Kind but not SpecialKind).
# -ccontains is case-sensitive so e.g. our 'music' folder never touches the
# built-in 'Music' library view.
#
# Deleting from $lib.Playlists while iterating it -- even a @()-snapshotted
# copy of it -- was observed to skip entries (deleting only the first of
# several same-named playlists, leaving the rest). Delete one match, then
# re-query the live collection from scratch and repeat, instead of trying to
# iterate and delete in the same pass.
$foundOne = $true
while ($foundOne) {
    $foundOne = $false
    $seenNames = @{}
    foreach ($p in $lib.Playlists) {
        if ($p.SpecialKind -eq 0 -and ($folderNames -ccontains $p.Name)) {
            if ($seenNames.ContainsKey($p.Name)) {
                Write-Output "Removing surplus playlist: $($p.Name)"
                $p.Delete() | Out-Null
                $foundOne = $true
                break
            }
            $seenNames[$p.Name] = $true
        }
    }
}
Start-Sleep -Milliseconds 500  # let iTunes settle before touching library tracks

# Phase 2: clean the library, then build the id -> track map.
Sync-Library

# Phase 3: fill each playlist. No library-level deletions happen during this
# phase, so cached track references stay valid.
#
# One folder's work, as a function so that a failure can simply be retried.
# iTunes can invalidate a playlist reference underneath us -- most often while
# Sync Library is on and the cloud sync is rewriting playlists -- after which
# the next call against it throws "The playlist has been deleted".
function Sync-Folder($folderName) {
    $folderPath = Join-Path $srcRoot $folderName
    # By name, not modified time: the leading number in a filename *is* the
    # playlist position, whereas mtime is only whenever that file happened to
    # get written. Three things routinely make the two disagree -- searched
    # tracks download several at a time and finish out of order, a re-encode
    # rewrites the file, and a hard-linked duplicate carries the mtime of
    # whichever folder was downloaded first.
    $files = @(Get-ChildItem $folderPath -File |
        Where-Object { $audioExts -contains $_.Extension.ToLower() } |
        Sort-Object Name)
    # `return`, not `continue`: this is a function now, and a `continue` here
    # would escape into the caller's loop and skip its retry bookkeeping.
    if ($files.Count -eq 0) { return }

    # Looked up fresh on every attempt, so a retry is never handed back the
    # same stale reference that just failed.
    $playlist = $null
    foreach ($p in $lib.Playlists) {
        if ($p.SpecialKind -eq 0 -and $p.Name -ceq $folderName) { $playlist = $p; break }
    }

    # A playlist is only ours to manage if everything in it is a file we put
    # there. Filling one means clearing it out first, so doing that to one of
    # *your* playlists that merely shares a folder name destroys it -- and
    # with Sync Library on, uploads the damage to your Apple Music account.
    # Folder names come straight from your playlist names now, so this
    # collides constantly rather than being a corner case.
    if ($playlist) {
        $ours = $true
        foreach ($t in @($playlist.Tracks)) {
            $loc = $t.Location
            if (-not $loc -or -not $loc.StartsWith($srcRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $ours = $false
                break
            }
        }
        if (-not $ours) {
            # Sidestep rather than refuse: the tracks still reach the iPod,
            # under a name that can't be confused with your own playlist.
            $folderName = "$folderName (whitefruit)"
            $playlist = $null
            foreach ($p in $lib.Playlists) {
                if ($p.SpecialKind -eq 0 -and $p.Name -ceq $folderName) { $playlist = $p; break }
            }
            Write-Output "  '$($folderName -replace ' \(whitefruit\)$','')' is one of your own playlists; using '$folderName' instead"
        }
    }
    if (-not $playlist) { $playlist = $itunes.CreatePlaylist($folderName) }

    # Compare what the playlist holds against what the folder says it should
    # hold, in order, keyed by video id. Identical means there is nothing to
    # do -- no need to churn the playlist (or the iPod's next sync) at all.
    $desired = @($files | ForEach-Object {
        if ($_.Name -match $idPattern) { $Matches[1] } else { $_.FullName }
    })
    $current = @($playlist.Tracks | ForEach-Object {
        if ($_.Location -and $_.Location -match $idPattern) { $Matches[1] } else { $_.Location }
    })
    if (($current -join "`n") -ceq ($desired -join "`n")) {
        Write-Output "Unchanged '$folderName': $($files.Count) tracks"
        return
    }

    # iTunes' COM API has no way to move a track to a given position, so the
    # only way to guarantee order is to fill from empty. Clear the tracks out
    # rather than the playlist itself -- removing a track from a user playlist
    # detaches it from that playlist only, leaving the library entry and the
    # file on disk alone. Index 1 repeatedly, since the collection reindexes
    # as it shrinks.
    while ($playlist.Tracks.Count -gt 0) { $playlist.Tracks.Item(1).Delete() }

    foreach ($f in $files) {
        $vid = $null
        if ($f.Name -match $idPattern) { $vid = $Matches[1] }

        if ($vid -and $idToTrack.ContainsKey($vid)) {
            # A cached reference can go stale -- iTunes deletes the track, or a
            # failed attempt at an earlier folder left a dead entry behind.
            # Untreated, one dead entry then throws "The track has been
            # deleted" on every later folder that shares that song, which is
            # how a single failure used to take the rest of the run with it.
            try {
                $playlist.AddTrack($idToTrack[$vid]) | Out-Null
                continue
            } catch [System.Runtime.InteropServices.COMException] {
                $idToTrack.Remove($vid)   # fall through and import it afresh
            }
        }

        $status = $playlist.AddFile($f.FullName)
        while ($status.InProgress) { Start-Sleep -Milliseconds 100 }
        if ($vid -and $status.Tracks.Count -gt 0) {
            $idToTrack[$vid] = $status.Tracks.Item(1)
        }
    }
    Write-Output "Updated '$folderName': $($files.Count) tracks"
}

foreach ($folderName in $folderNames) {
    # Two attempts, then move on. Losing one playlist is annoying; aborting the
    # whole run part-way leaves the library half-synced, which is worse -- and
    # across a library's worth of folders it is near certain that one of them
    # hits this eventually.
    $done = $false
    foreach ($attempt in 1, 2) {
        try {
            Sync-Folder $folderName
            $done = $true
            break
        } catch [System.Runtime.InteropServices.COMException] {
            Write-Output "  iTunes dropped '$folderName' mid-update (attempt $attempt): $($_.Exception.Message)"
            Start-Sleep -Milliseconds 500
            # A half-finished attempt leaves references in the cache that
            # iTunes has already invalidated, so rebuild it before retrying
            # rather than handing the retry the same dead objects.
            Sync-Library
        }
    }
    if (-not $done) { Write-Output "Skipped '$folderName': iTunes kept dropping it" }
}

# Phase 4: mop up any duplicate entries the import phase introduced.
Sync-Library
