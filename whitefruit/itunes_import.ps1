# Imports each subfolder of $srcRoot into iTunes as a playlist of the same name,
# tracks ordered oldest-to-newest by file modified time. Re-running refills an
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
    [string]$MusicDir = "$env:USERPROFILE\Music\whitefruit"
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
foreach ($folderName in $folderNames) {
    $folderPath = Join-Path $srcRoot $folderName
    $files = @(Get-ChildItem $folderPath -File |
        Where-Object { $audioExts -contains $_.Extension.ToLower() } |
        Sort-Object LastWriteTime)
    if ($files.Count -eq 0) { continue }

    $playlist = $null
    foreach ($p in $lib.Playlists) {
        if ($p.SpecialKind -eq 0 -and $p.Name -ceq $folderName) { $playlist = $p; break }
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
        continue
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
            $playlist.AddTrack($idToTrack[$vid]) | Out-Null
            continue
        }

        $status = $playlist.AddFile($f.FullName)
        while ($status.InProgress) { Start-Sleep -Milliseconds 100 }
        if ($vid -and $status.Tracks.Count -gt 0) {
            $idToTrack[$vid] = $status.Tracks.Item(1)
        }
    }
    Write-Output "Updated '$folderName': $($files.Count) tracks"
}

# Phase 4: mop up any duplicate entries the import phase introduced.
Sync-Library
