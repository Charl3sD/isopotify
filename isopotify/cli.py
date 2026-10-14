"""Command-line entry point and session orchestration.

Wires MPRIS events → Recorder and PCM capture → Recorder, then blocks until the
user stops it (Ctrl-C) or Spotify disappears.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import audio as audio_mod
from . import __version__
from .config import Config, MediaFormat, OnExists, SINK_NAME
from .encoder import available_mp3_encoder
from .recorder import Recorder


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isopotify",
        description="Record Spotify on Linux into per-track MP3/WAV files "
        "(ads and tracks you skip are not saved). Start it, then play music in Spotify.",
    )
    p.add_argument("--output", metavar="DIR", help="output directory")
    p.add_argument("--format", dest="media_format", choices=["mp3", "wav"], help="output format")
    p.add_argument("--bitrate", type=int, help="MP3 bitrate in kbps (e.g. 160, 320)")
    p.add_argument("--separator", help="whitespace replacement in file names")
    p.add_argument("--group-by-folder", action="store_true", default=None,
                   help="save into Artist/Album/ sub-folders")
    p.add_argument("--min-length", type=int, help="drop recordings shorter than N seconds")
    p.add_argument("--on-exists", choices=["skip", "overwrite", "duplicate"],
                   help="what to do if the target file already exists")
    p.add_argument("--record-ads", action="store_true", default=None,
                   help="save ad segments instead of discarding them")
    p.add_argument("--keep-skipped", action="store_true", default=None,
                   help="keep tracks you skip before they finish (disables cancel-on-skip)")
    p.add_argument("--skip-tolerance", type=float,
                   help="seconds of slack before a track counts as 'skipped' (default 5)")
    p.add_argument("--no-listen", dest="listen", action="store_false", default=None,
                   help="do not route audio to your speakers while recording")
    p.add_argument("--config", metavar="FILE", help="path to a config file (TOML or JSON)")
    p.add_argument("--list-sinks", action="store_true", help="list audio sinks/streams and exit")
    p.add_argument("-v", "--verbose", action="store_true", default=None, help="verbose logging")
    p.add_argument("--version", action="version", version=f"isopotify {__version__}")
    return p


def _config_from_args(args: argparse.Namespace) -> Config:
    cfg = Config.load(Path(args.config) if args.config else None)
    overrides = {}
    if args.output is not None:
        overrides["output_dir"] = args.output
    if args.media_format is not None:
        overrides["media_format"] = args.media_format
    if args.bitrate is not None:
        overrides["bitrate"] = args.bitrate
    if args.separator is not None:
        overrides["separator"] = args.separator
    if args.group_by_folder is not None:
        overrides["group_by_folder"] = args.group_by_folder
    if args.min_length is not None:
        overrides["min_length"] = args.min_length
    if args.on_exists is not None:
        overrides["on_exists"] = args.on_exists
    if args.record_ads is not None:
        overrides["record_ads"] = args.record_ads
    if args.keep_skipped is not None:
        overrides["keep_skipped"] = args.keep_skipped
    if args.skip_tolerance is not None:
        overrides["skip_tolerance"] = args.skip_tolerance
    if args.listen is not None:
        overrides["listen"] = args.listen
    if args.verbose is not None:
        overrides["verbose"] = args.verbose
    cfg.apply(overrides)
    cfg.validate()
    return cfg


def _print_sinks() -> int:
    print("# Sinks:")
    for si in _safe(lambda: __import__("subprocess").run(
        ["pactl", "list", "sinks", "short"], capture_output=True, text=True).stdout):
        print("  " + si)
    print("# Sink inputs (playback streams):")
    for si in audio_mod.list_sink_inputs():
        name = si["props"].get("application.name") or si["props"].get("media.name", "?")
        print(f"  #{si['index']}  {name}")
    return 0


def _safe(fn):
    try:
        out = fn()
        return out.strip().splitlines() if out else []
    except Exception:
        return []


_STAMP = {"rec": "🔴", "done": "✅", "drop": "🗑️ ", "skip": "⏭️ ", "fail": "⚠️ ",
          "info": "•", "ad": "📢"}


def _make_logger(verbose: bool):
    def log_fn(level: str, msg: str) -> None:
        print(f"{_STAMP.get(level, '•')} [{level}] {msg}", flush=True)
    return log_fn


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s: %(message)s",
    )

    if args.list_sinks:
        return _print_sinks()

    try:
        cfg = _config_from_args(args)
    except (ValueError, RuntimeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if cfg.media_format == MediaFormat.MP3 and available_mp3_encoder() is None:
        print("No MP3 encoder found (install 'lame' or 'ffmpeg'), or use --format wav.",
              file=sys.stderr)
        return 3

    log_fn = _make_logger(cfg.verbose)

    audio = audio_mod.AudioCapture(sink_name=SINK_NAME, listen=cfg.listen)
    recorder = Recorder(
        cfg, log_fn=log_fn, on_record_start=lambda _t: audio.route_spotify()
    )
    audio.set_data_callback(recorder.feed)

    # Import the MPRIS watcher lazily so --list-sinks etc. work without gi/Spotify.
    from .mpris import MprisWatcher

    stop_event = threading.Event()

    def _on_spotify_gone() -> None:
        log_fn("info", "Spotify closed — stopping.")
        stop_event.set()

    watcher = MprisWatcher(on_change=recorder.on_track, on_gone=_on_spotify_gone)

    def _handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log_fn("info", f"Isopotify {__version__} → {cfg.output_dir} "
                   f"({cfg.media_format.value}"
                   f"{'/' + str(cfg.bitrate) + 'k' if cfg.media_format == MediaFormat.MP3 else ''})")
    try:
        audio.start()
    except audio_mod.AudioError as exc:
        print(f"Audio error: {exc}", file=sys.stderr)
        return 4

    try:
        watcher.start()
    except Exception as exc:
        print(f"Could not connect to Spotify over MPRIS: {exc}\n"
              f"Make sure the Spotify desktop app is running.", file=sys.stderr)
        audio.stop()
        return 5

    log_fn("info", "Watching Spotify — play something. Press Ctrl-C to stop.")

    # Keep Spotify's stream routed onto our sink even if it is (re)created.
    router = _Router(audio, stop_event)
    router.start()

    try:
        while not stop_event.wait(0.5):
            pass
    finally:
        log_fn("info", "Stopping…")
        # Each step is isolated so one failure can't skip finalising/cleanup.
        for step in (
            lambda: router.join(timeout=2),
            watcher.stop,
            recorder.close,  # finalise the current segment + flush encodes
            audio.stop,      # terminate parec and unload our modules
        ):
            try:
                step()
            except Exception as exc:  # pragma: no cover - defensive shutdown
                logging.getLogger("isopotify").error("shutdown step failed: %s", exc)
    return 0


class _Router(threading.Thread):
    """Periodically (re)route Spotify's playback stream onto our sink."""

    def __init__(self, audio: "audio_mod.AudioCapture", stop_event: threading.Event):
        super().__init__(name="spotify-router", daemon=True)
        self._audio = audio
        self._stop_event = stop_event  # NB: do not name this `_stop` (Thread uses it)

    def run(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                self._audio.route_spotify()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
