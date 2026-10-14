"""Write media tags (ID3) onto the finished file.

Tags are written with mutagen. Core tags come straight from Spotify's MPRIS
metadata, so no external API call is needed; the album-art image is fetched
once from ``mpris:artUrl``. Last.fm enrichment is intentionally
left as a future hook — MPRIS already supplies title/artist/album/cover.
"""

from __future__ import annotations

import logging
from typing import Optional

from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK

from .config import MediaFormat

log = logging.getLogger("isopotify.tagger")


def _fetch_cover(art_url: Optional[str], timeout: float = 10.0):
    """Fetch cover-art bytes + MIME from an http(s) artUrl. Returns (data, mime)."""
    if not art_url or not art_url.lower().startswith(("http://", "https://")):
        return None, None
    try:
        import requests

        resp = requests.get(art_url, timeout=timeout)
        resp.raise_for_status()
        mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return resp.content, mime
    except Exception as exc:  # network is best-effort; never fail the recording
        log.warning("Could not fetch cover art (%s): %s", art_url, exc)
        return None, None


def write_tags(path: str, track, media_format: MediaFormat) -> None:
    """Attach ID3 tags (and cover art) to the file at ``path``.

    Works for both MP3 and WAV (mutagen stores an ID3 chunk in WAV). Failure to
    tag is logged but never raised — a correctly recorded, untagged file is
    better than losing the recording.
    """
    try:
        container, tags = _open_tags(path, media_format)
    except Exception as exc:
        log.warning("Could not open %s for tagging: %s", path, exc)
        return

    try:
        if track.title:
            tags.setall("TIT2", [TIT2(encoding=3, text=[track.title])])
        if track.artist:
            tags.setall("TPE1", [TPE1(encoding=3, text=[track.artist])])
        if track.album:
            tags.setall("TALB", [TALB(encoding=3, text=[track.album])])
        if track.album_artist:
            tags.setall("TPE2", [TPE2(encoding=3, text=[track.album_artist])])
        if track.track_number:
            tags.setall("TRCK", [TRCK(encoding=3, text=[str(track.track_number)])])
        if track.disc_number:
            tags.setall("TPOS", [TPOS(encoding=3, text=[str(track.disc_number)])])
        if getattr(track, "year", None):
            tags.setall("TDRC", [TDRC(encoding=3, text=[str(track.year)])])

        data, mime = _fetch_cover(track.art_url)
        if data:
            tags.setall(
                "APIC",
                [APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data)],
            )

        # WAV must be saved through its container so the ID3 chunk is embedded
        # correctly; MP3 is saved via the ID3 object bound to the file path.
        if container is not None:
            container.save()
        else:
            tags.save(path, v2_version=3)
    except Exception as exc:
        log.warning("Could not write tags to %s: %s", path, exc)


def _open_tags(path: str, media_format: MediaFormat):
    """Return (container, tags). container is None for MP3 (save via tags)."""
    if media_format == MediaFormat.WAV:
        from mutagen.wave import WAVE

        audio = WAVE(path)
        if audio.tags is None:
            audio.add_tags()
        return audio, audio.tags
    # MP3 (and anything else): a bare ID3 tag object bound to the file path
    try:
        return None, ID3(path)
    except Exception:
        return None, ID3()
