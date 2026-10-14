"""Watch Spotify over MPRIS2 (D-Bus).

``Track`` is a pure dataclass (no D-Bus import) so the recording logic and its
tests do not depend on PyGObject. ``MprisWatcher`` lazily imports ``gi`` and
runs a GLib main loop, emitting callbacks on track / playback-status changes.

Interface:  org.mpris.MediaPlayer2.spotify  @ /org/mpris/MediaPlayer2
            → org.mpris.MediaPlayer2.Player  { Metadata, PlaybackStatus }
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Callable, Dict, Optional

log = logging.getLogger("isopotify.mpris")

BUS_NAME = "org.mpris.MediaPlayer2.spotify"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# Window titles Spotify shows when idle.
_IDLE_TITLES = {"spotify", "spotify free", "spotify premium"}


@dataclasses.dataclass
class Track:
    """A snapshot of Spotify's current state, parsed from MPRIS metadata."""

    trackid: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    length_sec: float = 0.0
    art_url: str = ""
    track_number: int = 0
    disc_number: int = 0
    url: str = ""
    playback_status: str = "Stopped"  # Playing | Paused | Stopped

    # ------------------------------------------------------------------ state
    @property
    def is_playing(self) -> bool:
        return self.playback_status == "Playing"

    @property
    def is_idle(self) -> bool:
        return (self.title or "").strip().lower() in _IDLE_TITLES

    @property
    def is_ad(self) -> bool:
        """Heuristic ad detection (configurable fallbacks).

        Spotify's Linux client marks ads with an ad-style ``mpris:trackid`` and
        usually an empty or ``Advertisement`` title with no artist. This is the
        one runtime-verified assumption — see README. ``record_ads`` lets a
        caller keep ads regardless.
        """
        tid = (self.trackid or "").lower()
        # Real Spotify-Linux trackids look like "/com/spotify/track/<id>" (and
        # "spotify:track:<id>" via some front-ends); ads use an "ad" segment.
        if (
            tid.startswith("spotify:ad")
            or ":ad:" in tid
            or "/ad/" in tid
            or "/advertisement/" in tid
        ):
            return True
        title = (self.title or "").strip().lower()
        if title in ("", "advertisement"):
            return True
        if not (self.artist or "").strip():
            return True
        return False

    def is_recordable(self, record_ads: bool = False) -> bool:
        if not self.is_playing or self.is_idle:
            return False
        if self.is_ad and not record_ads:
            return False
        return bool(self.artist or self.title)

    def same_track(self, other: Optional["Track"]) -> bool:
        """Identity by trackid (falling back to artist+title)."""
        if other is None:
            return False
        if self.trackid and other.trackid:
            return self.trackid == other.trackid
        return (self.artist, self.title) == (other.artist, other.title)


def parse_metadata(meta: Dict, playback_status: str = "Stopped") -> Track:
    """Build a Track from an MPRIS ``Metadata`` dict + ``PlaybackStatus``."""
    def _first(value) -> str:
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else ""
        return str(value) if value is not None else ""

    def _join(value) -> str:
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value if v)
        return str(value) if value else ""

    length_us = meta.get("mpris:length", 0) or 0
    try:
        length_sec = float(length_us) / 1_000_000.0
    except (TypeError, ValueError):
        length_sec = 0.0

    def _int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return Track(
        trackid=_first(meta.get("mpris:trackid", "")),
        title=_first(meta.get("xesam:title", "")),
        artist=_join(meta.get("xesam:artist", "")),
        album=_first(meta.get("xesam:album", "")),
        album_artist=_join(meta.get("xesam:albumArtist", "")),
        length_sec=length_sec,
        art_url=_first(meta.get("mpris:artUrl", "")),
        track_number=_int(meta.get("xesam:trackNumber", 0)),
        disc_number=_int(meta.get("xesam:discNumber", 0)),
        url=_first(meta.get("xesam:url", "")),
        playback_status=playback_status,
    )


class MprisWatcher:
    """Runs a GLib main loop and reports Spotify track/playback changes.

    Callbacks run on the GLib loop thread:
      - ``on_track(track)``      when the metadata (trackid) changes
      - ``on_playback(track)``   when only PlaybackStatus changes
    """

    def __init__(
        self,
        on_change: Callable[[Track], None],
        on_gone: Optional[Callable[[], None]] = None,
        bus_name: str = BUS_NAME,
    ):
        self._on_change = on_change
        self._on_gone = on_gone  # called if Spotify exits (name owner disappears)
        self._bus_name = bus_name
        self._loop = None
        self._thread: Optional[threading.Thread] = None
        self._proxy = None

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        self._proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            self._bus_name,
            OBJECT_PATH,
            PLAYER_IFACE,
            None,
        )
        self._proxy.connect("g-properties-changed", self._on_props_changed)
        # Detect Spotify exiting (its bus name owner disappears) — matters for
        # the overnight use-case where we'd otherwise wait forever.
        self._proxy.connect("notify::g-name-owner", self._on_owner_changed)

        # Emit an initial snapshot so a track already playing is picked up.
        initial = self.snapshot()
        if initial is not None:
            self._on_change(initial)

        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(
            target=self._loop.run, name="mpris-loop", daemon=True
        )
        self._thread.start()
        log.info("MPRIS watcher started on %s", self._bus_name)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ----------------------------------------------------------------- reads
    def snapshot(self) -> Optional[Track]:
        """Read current Metadata + PlaybackStatus, or None if unavailable."""
        if self._proxy is None:
            return None
        try:
            meta_v = self._proxy.get_cached_property("Metadata")
            status_v = self._proxy.get_cached_property("PlaybackStatus")
            meta = meta_v.unpack() if meta_v is not None else {}
            status = status_v.unpack() if status_v is not None else "Stopped"
            return parse_metadata(meta, status)
        except Exception as exc:  # pragma: no cover - needs live bus
            log.warning("MPRIS snapshot failed: %s", exc)
            return None

    def _on_owner_changed(self, _proxy, _pspec):
        owner = self._proxy.get_name_owner() if self._proxy is not None else None
        if not owner:
            log.info("Spotify exited (bus name released)")
            if self._on_gone is not None:
                self._on_gone()

    def _on_props_changed(self, _proxy, _changed, _invalidated):
        # Re-read a full snapshot on any Metadata/PlaybackStatus change and let
        # the consumer derive the transition (track change vs. pause/resume).
        track = self.snapshot()
        if track is not None:
            self._on_change(track)
