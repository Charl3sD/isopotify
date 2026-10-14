"""Recording orchestration.

A continuous PCM stream (from :mod:`audio`) is fed in via :meth:`Recorder.feed`
while MPRIS track/playback events (from :mod:`mpris`) drive :meth:`Recorder.on_track`
and :meth:`Recorder.on_playback`. Each track becomes a *segment*: raw PCM is
buffered to a temp file; when the track ends (a new trackid arrives) the segment
is finalised — kept (encoded + tagged + moved into place) or discarded.

Two filters drop unwanted material, both expressed by the pure :func:`decide`:
  * **too short** — below ``min_length``;
  * **skipped before end** — recorded well short of ``mpris:length`` means the
    listener skipped it, so the segment is dropped *before any transcode starts*
    (no CPU wasted). An already-running encode can still be cancelled on abort.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Callable, List, Optional, Tuple

from . import encoder as _encoder
from . import naming
from . import tagger as _tagger
from .config import BYTES_PER_SECOND, Config
from .mpris import Track

log = logging.getLogger("isopotify.recorder")


def decide(
    recorded_seconds: float,
    length_seconds: float,
    min_length: int,
    keep_skipped: bool,
    skip_tolerance: float,
) -> Tuple[str, str]:
    """Pure keep/discard policy. Returns ('keep'|'discard', reason)."""
    if recorded_seconds < min_length:
        return "discard", f"too short ({recorded_seconds:.0f}s < {min_length}s)"
    if not keep_skipped and length_seconds and length_seconds > 0:
        if recorded_seconds < length_seconds - skip_tolerance:
            return (
                "discard",
                f"skipped before end ({recorded_seconds:.0f}s / {length_seconds:.0f}s)",
            )
    return "keep", ""


class _Segment:
    """An in-progress recording of a single track, buffered to a temp raw file."""

    def __init__(self, track: Track, raw_path: str):
        self.track = track
        self.raw_path = raw_path
        self._fh = open(raw_path, "wb", buffering=1 << 16)
        self.bytes_written = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            return
        self._fh.write(data)
        self.bytes_written += len(data)

    def close(self) -> None:
        if not self.closed:
            self._fh.close()
            self.closed = True

    @property
    def seconds(self) -> float:
        return self.bytes_written / BYTES_PER_SECOND


LogFn = Callable[[str, str], None]


class Recorder:
    """Thread-safe recording session. Feed PCM + drive with MPRIS events."""

    def __init__(
        self,
        config: Config,
        workdir: Optional[str] = None,
        log_fn: Optional[LogFn] = None,
        on_record_start: Optional[Callable[[Track], None]] = None,
        encode_fn=_encoder.encode,
        tagger_fn=_tagger.write_tags,
    ):
        self.config = config
        self._workdir = workdir or tempfile.mkdtemp(prefix="isopotify-")
        os.makedirs(self._workdir, exist_ok=True)
        self._log_fn = log_fn or (lambda level, msg: log.info("[%s] %s", level, msg))
        self._on_record_start = on_record_start
        self._encode_fn = encode_fn
        self._tagger_fn = tagger_fn

        self._lock = threading.Lock()
        self._current: Optional[_Segment] = None
        self._playing = False
        self._encodes: List[threading.Thread] = []
        self._active_jobs: List[_encoder.EncodeJob] = []

    # -------------------------------------------------------------- PCM input
    def feed(self, data: bytes) -> None:
        """Append captured PCM to the active segment (called from audio thread)."""
        with self._lock:
            if self._current is not None and self._playing:
                self._current.write(data)

    # ----------------------------------------------------------- MPRIS events
    def on_track(self, track: Track) -> None:
        """Unified MPRIS update handler — called for every track/playback change.

        Derives the recording transition from the current segment vs. the new
        state, so starting a recording works whether a track *change* or a
        *play* (pause→play on the same track) made it recordable.
        """
        recordable = track.is_recordable(self.config.record_ads)
        with self._lock:
            cur = self._current

        # A different track became current → finalise the previous segment.
        if cur is not None and not track.same_track(cur.track):
            self._finalize(self._detach_current())
            cur = None

        if recordable:
            if cur is None:
                self._start(track)
            else:
                with self._lock:  # same track resumed/continuing
                    self._playing = True
                    # refresh metadata in case tags (e.g. artUrl) arrived late
                    cur.track = track
        elif cur is not None:
            if not track.is_playing and track.same_track(cur.track):
                with self._lock:  # paused on the same track: keep the buffer
                    self._playing = False
            else:
                # idle / ad / otherwise unrecordable on the same id → end it
                self._finalize(self._detach_current())

    def _start(self, track: Track) -> None:
        if self._on_record_start is not None:
            self._on_record_start(track)
        seg = _Segment(track, self._new_raw())
        with self._lock:
            self._current = seg
            self._playing = True
        self._log_fn("rec", _label(track))

    def _detach_current(self) -> Optional["_Segment"]:
        with self._lock:
            cur = self._current
            self._current = None
            self._playing = False
        return cur

    # --------------------------------------------------------------- finalise
    def _finalize(self, seg: Optional[_Segment]) -> None:
        if seg is None:
            return
        seg.close()
        action, reason = decide(
            seg.seconds,
            seg.track.length_sec,
            self.config.min_length,
            self.config.keep_skipped,
            self.config.skip_tolerance,
        )
        if action == "discard":
            _unlink(seg.raw_path)
            self._log_fn("drop", f"{_label(seg.track)}: {reason}")
            return

        out_path = self._target_path(seg.track)
        if out_path is None:
            _unlink(seg.raw_path)
            self._log_fn("skip", f"{_label(seg.track)}: already exists")
            return

        thread = threading.Thread(
            target=self._encode_and_tag, args=(seg, out_path), daemon=True
        )
        thread.start()
        with self._lock:
            self._encodes.append(thread)

    def _encode_and_tag(self, seg: _Segment, out_path: str) -> None:
        try:
            job = self._encode_fn(
                seg.raw_path, out_path, self.config.media_format, self.config.bitrate
            )
            with self._lock:
                self._active_jobs.append(job)
            ok = job.wait()
            with self._lock:
                if job in self._active_jobs:
                    self._active_jobs.remove(job)
            if ok:
                self._tagger_fn(out_path, seg.track, self.config.media_format)
                self._log_fn("done", out_path)
            elif not job.cancelled:
                self._log_fn("fail", f"encode failed: {out_path}")
        except _encoder.EncoderUnavailableError as exc:
            self._log_fn("fail", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._log_fn("fail", f"{out_path}: {exc}")
        finally:
            _unlink(seg.raw_path)

    def _target_path(self, track: Track) -> Optional[str]:
        directory = naming.folder_path(
            self.config.output_dir,
            track.album_artist or track.artist,
            track.album,
            self.config.separator,
            self.config.group_by_folder,
        )
        naming.ensure_directory(directory)
        basename = naming.track_basename(track.artist, track.title, self.config.separator)
        if not basename:
            return None
        return naming.resolve_output_path(
            directory, basename, self.config.media_format, self.config.on_exists
        )

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        """Finalise the current segment and wait for pending encodes."""
        self._finalize(self._detach_current())
        with self._lock:
            threads = list(self._encodes)
        for thread in threads:
            thread.join()

    def abort(self) -> None:
        """Cancel everything immediately (in-flight encodes killed, partials removed)."""
        with self._lock:
            prev = self._current
            self._current = None
            jobs = list(self._active_jobs)
        for job in jobs:
            job.cancel()
        if prev is not None:
            prev.close()
            _unlink(prev.raw_path)

    def _new_raw(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".raw", dir=self._workdir)
        os.close(fd)
        return path


def _label(track: Track) -> str:
    return f"{track.artist} - {track.title}".strip(" -") or track.trackid or "unknown"


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
