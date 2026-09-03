<p align="center">
  <img width="350px" src="whitefruit/wf-wm.png" />
</p>
<h3 align="center">Sync your Spotify, Apple Music, and Youtube Music playlists to iTunes.</h3>

## Features

- **Download** whole YouTube Music playlists as tagged, iPod-ready files -
  title, artist, album, track number and cover art, and playlist order all configured.
- **iTunes sync** each playlist over COM, ready for iPod plugin.
- **Deduplicate** repeated songs between playlists, while still maintaining playlist continuity.
- **Re-encode** an existing library to a different format without
  redownloading it.
- **No** python dependencies needed!

## Prerequisites

Requires `yt-dlp`, `ffmpeg`, and classic iTunes with
COM automation, all on Windows. 

> [!WARNING]
> iTunes **must** be downloaded from Apple's [website](https://www.apple.com/itunes/download/win64), not from the Microsoft store.

Both `yt-dlp` and `ffmpeg` need to be on
`PATH` (or edit `YTDLP_FALLBACK` in `whitefruit/download.py`).

## Usage

```
python main.py
```
Gives a dead-easy GUI to sync your playlist over. Self explanatory from there.

For scripting, the same actions are available as regular subcommands:

```
python main.py all --yes       # download new tracks, dedupe, sync to iTunes
python main.py download        # just fetch/update playlists (prompts per playlist)
python main.py dedupe --dry-run
python main.py repair          # re-encode existing files to the configured format
python main.py itunes          # just (re)sync iTunes playlists from disk
```

Local playlists (by default) are in 
`%user%\Music\whitefruit`.

File titles are formatted `# - Title [video_id]`.

Do not remove the embedded file id, as it's how whitefruit labels songs.

## Setup

These links should be listed in playlists.txt.

### YouTube Music

| Line                                          | Fetches      |
| --------------------------------------------- | ------------ |
| `https://music.youtube.com/playlist?list=...` | one playlist |
| `https://www.youtube.com/feed/playlists...`   | every playlist in library*    |
| `https://music.youtube.com/playlist?list=LM`  | Liked Songs*  |

> [!IMPORTANT]
> *using these features requires YouTube sign-in: see [Signing in](#signing-in).

### Spotify

| Line                                    | Fetches                          |
| --------------------------------------- | -------------------------------- |
| `https://open.spotify.com/playlist/...` | one playlist                     |
| `https://open.spotify.com/album/...`    | one album                        |
| `spotify:liked`                         | Liked Songs, as one folder       |
| `spotify:playlists`                     | one folder per playlist you own or follow |
| `spotify:albums`                        | one folder per saved album       |

> [!IMPORTANT]
> **Spotify** needs a free app from the
[developer dashboard](https://developer.spotify.com/dashboard) with
`http://127.0.0.1:8888/callback` added as a redirect URI. Put its Client ID in
settings under `spotify_client_id` - no secret to enter. The first
run opens your browser once to authorise, and remembers it afterwards.

### Apple Music

| Line                   | Fetches                                              |
| ---------------------- | ---------------------------------------------------- |
| `itunes:library`       | your entire library     |
| `itunes:playlists`     | every playlist in your library                       |
| `itunes:playlist:Name` | one playlist, by name                         |
| `itunes:strays`        | only library tracks that are in no playlist          |

> [!IMPORTANT]
> Downloading from Apple Music requires login and temporary sync with Apple Music through iTunes. A developer token or paid developer account are not needed. See below for instructions.

To use whitefruit with Apple Music, ensure iTunes (NOT the Windows store version) is downloaded.

Go to the menu bar, sign in with your Apple Music Apple ID. Make sure your iTunes library is empty (except for non-whitefruit tracks) or it will double sync your songs to Apple Music. 

Sync your tracks by going to edit > preferences > iCloud Music library.

Then follow the instructions in the program on when to turn on and off sync.

> [!Note]
> For greater accuracy, whitefruit takes song metadata from the source of the music, not the media downloaded from.

> [!Important]
> Apple Music and Spotify support work by taking the tracks and downloading through YouTube music. This may result in the odd clean version or cover version being mistakenly downloaded. 
>
> [Signing in](#signing-in) with your YouTube account can improve song search accuracy.

### Music Formats

| Format         | Setting           | Notes                                                                              |
| -------------- | ----------------- | ---------------------------------------------------------------------------------- |
| MP3 (LAME)     | `mp3` *(default)* | Plays on every iPod ever made.                                                     |
| AAC            | `m4a`             | Smaller at the same quality, but see warning below.                                |
| Apple Lossless | `alac`            | 4th gen and newer only. Very large files, and older hardware can struggle to play. |

> **Why MP3 is the default.** ffmpeg's native AAC encoder produces `.m4a`
> files that play perfectly on a computer but can cause 
> [scratching](https://www.reddit.com/r/ipod/comments/r2cti3/squeaking_noises_only_through_the_ipod_and_only/) sounds when played. 
> LAME MP3 avoids the problem. Pick `m4a` only if you've
> confirmed your device is good with it.

Keep the bitrate at or below **320 kbps**. Your iPod [may](https://discussions.apple.com/thread/1819918?sortBy=rank) encounter issues with playback above it.

## iPod support

whitefruit doesn't interface with your iPod's file system, so all Windows iTunes-syncable iPods are supported. This includes:

- iPod Classic (2nd-7th)
- iPod nano (1st–7th)
- iPod mini (1st-2nd)
- iPod shuffle (1st–4th)
- iPod video
- iPod touch (1st-7th)
- all iTunes capable iDevices

## Encoding

Changing output format **or sample rate** causes whitefruit to treat the
existing library as out of date, triggering a library refresh.

To convert what you already have instead of redownloading it, use
**Re-encode local music files**. 

Do note that re-encoding means a slight loss in audio quality. Delete
and refetch the playlist to solve this.

## Signing in

Signing in with YouTube allows whitefruit to get a more accurate library, giving access
to age-restricted (explict) tracks and YouTube Premium-locked tracks (with a premium subscription).
To use, select this option, choose a browser, then login to YouTube on said browser. Firefox is recommended.

You can also manually paste a cookie if you want.

> [!Warning]
> Take great care when signing in., especially with a cookie. This gives someone access to your *entire* youtube account.

## Deduping

- Same song twice **within** one playlist (a real duplicate) -> *extra
  copy is deleted*
- Same song **across** playlists -> *every n+1 copy is replaced with an NTFS hard
  link to one canonical file*

## Disclaimer

whitefruit is a *personal* archiving tool. It does not host, provide or distribute any music.

Download only content you own or have the right to use. Always follow YouTube’s Terms of Service and local copyright laws. whitefruit does not encourage infringing use.

YouTube sign-in support exists so tracks *your own subscription already covers* download instead of being skipped. Signing in with a free account does **not** unlock tracks your account isn't entitled to, and it does not circimvent any additional security measures. Cookies handed
to it are as sensitive as your password. Treat with extreme caution.

whitefruit is not affiliated with, endorsed by, or officially connected to Google LLC, Youtube, or Apple Inc. whitefruit is not responsible for any concquences that may arise from use of this program. 

## License

This project is licensed with GPL v2. See [LICENSE](LICENSE) file for details.
