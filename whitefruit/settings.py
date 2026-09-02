"""Persisted user settings, stored as settings.json next to main.py.

Defaults are chosen for old iPod hardware. In particular the output format
defaults to MP3: ffmpeg's native AAC encoder produces .m4a files that aren't 
fully compatible with iPods, while LAME MP3 plays cleanly on the same hardware.
"""
import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

DEFAULTS = {
    # The user's own Music folder, wherever that is on this machine -- not
    # everyone has a D: drive. An existing settings.json overrides this, so
    # changing it doesn't move anyone's library.
    "music_dir": str(Path.home() / "Music" / "whitefruit"),
    "playlists_file": "playlists.txt",
    "audio_format": "mp3",
    "audio_bitrate": "192K",
    "sample_rate": "44100",
    "encoder_quality": "best",
    "embed_art": True,
    "square_art": True,
    "art_size": 600,
    "dedupe_hardlink": True,
    "itunes_sync": True,
    "remove_deleted": True,
    "cookies_from_browser": "none",
    "cookies_file": "",
    "search_fallback": True,
    "verbose_logging": False,
}

# Shown in the settings screen. Keep these short.
DESCRIPTIONS = {
    "music_dir": "Where downloaded playlist folders are stored.",
    "playlists_file": "Text file listing the playlist URLs to fetch.",
    "audio_format": 
    """Output codec.
       mp3  = LAME, safest on old iPods.
       m4a  = AAC, smaller, but may cause audio problems on some iPods.
       alac = lossless, very large. Not recommended on hard drive iPods.""",
    "audio_bitrate": "Target bitrate. Keep at or below 320K to prevent iPod decoding issues.",
    "sample_rate": 
    """Output sample rate. 
       iPod DACs run natively at 44100; feeding them 48000
       makes the device resample on the fly, which can introduce
       audio artifacting. 'source' keeps whatever YouTube sent.""",
    "encoder_quality": 
    """Encoder effort. 
       'best' spends more CPU for cleaner output
       'fast' encodes quicker, but might have impact on audio quality. 
       Only affects mp3 and m4a.""",
    "embed_art": "Embed album art into each file.",
    "square_art": 
    """Crop art to a square instead of leaving it 16:9 letterboxed. 
       Not sure why you'd disable this.""",
    "art_size": "Pixel size of embedded art. Large art is slow to load on an iPod.",
    "dedupe_hardlink": "Hard-link songs that appear in several playlists so the audio is stored once.",
    "itunes_sync": "Automatically push playlists into iTunes after downloading.",
    "remove_deleted": 
    """Delete local files for songs you've removed from the YouTube playlist,
       so the folder keeps mirroring the playlist. 
       Off leaves them behind.""",
    "cookies_from_browser":
    """Sign in using browser cookies, so tracks your account can play
       (YouTube Premium / Music Premium) download instead of being skipped.

       To use, select this option, choose a browser, then login to YouTube
       on said browser. Firefox is recommended.
       
       'none' downloads signed out.""",
    "cookies_file":
    """Alternative to the above: path to a cookies.txt exported from your
       browser. Takes precedence when set. Treat with CAUTION.""",
    "search_fallback":
    """When a track can't be fetched (Premium-only, region-locked), search
       public YouTube for the same song and take the top result instead.
       On by default.""",
    "verbose_logging": "Show yt-dlp's full output. Useful for debugging purposes.",
}

CHOICES = {
    "audio_format": ["mp3", "m4a", "alac"],
    "sample_rate": ["44100", "48000", "source"],
    "encoder_quality": ["best", "fast"],
    "cookies_from_browser": ["none", "firefox", "chrome", "edge", "brave",
                             "chromium", "opera", "vivaldi"],
}

BOOLS = [k for k, v in DEFAULTS.items() if isinstance(v, bool)]

# Extensions whitefruit recognises as its own downloaded tracks.
FORMAT_EXT = {"mp3": ".mp3", "m4a": ".m4a", "alac": ".m4a"}
ALL_EXTS = (".mp3", ".m4a")


def load() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt or unreadable -> fall back to defaults
    return s


def save(settings: dict):
    SETTINGS_FILE.write_text(
        json.dumps(settings, indent=2), encoding="utf-8")


def ext_for(settings: dict) -> str:
    return FORMAT_EXT.get(settings.get("audio_format", "mp3"), ".mp3")
