"""Encode a raw PCM segment into MP3 or WAV — cancellably.

MP3 is produced by shelling out to ``lame`` (preferred) or ``ffmpeg``; WAV
uses the stdlib ``wave`` module.

The key feature here is **cancellable** encoding: an in-flight MP3 encode can be
aborted (subprocess terminated, partial output removed) when the listener skips
a track.

Note on fidelity: Isopotify does *not* re-normalise the
audio — it records exactly what Spotify outputs (Spotify already applies loudness
normalisation). So there is no lossy level change here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from typing import List, Optional

from .config import CHANNELS, MediaFormat, SAMPLE_RATE, SAMPLE_WIDTH


class EncoderUnavailableError(RuntimeError):
    """No usable MP3 encoder (lame/ffmpeg) is installed."""


def _which(*names: str) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def available_mp3_encoder() -> Optional[str]:
    """Return the name of an available MP3 encoder, or None."""
    if _which("lame"):
        return "lame"
    if _which("ffmpeg"):
        return "ffmpeg"
    return None


class EncodeJob:
    """Handle to a (possibly running) encode. Supports wait() and cancel()."""

    def __init__(self, out_path: str, proc: Optional[subprocess.Popen] = None):
        self.out_path = out_path
        self._proc = proc
        self._cancelled = False
        self._done = proc is None  # WAV path finishes synchronously
        self._ok = proc is None

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until encoding finishes. Returns True on success."""
        if self._proc is None:
            return self._ok
        try:
            rc = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self._done = True
        self._ok = (rc == 0) and not self._cancelled
        if not self._ok:
            _unlink(self.out_path)
        return self._ok

    def cancel(self) -> None:
        """Abort the encode and remove any partial output."""
        self._cancelled = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        _unlink(self.out_path)
        self._done = True
        self._ok = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def encode(
    raw_path: str,
    out_path: str,
    media_format: MediaFormat,
    bitrate: int,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
) -> EncodeJob:
    """Start encoding ``raw_path`` (s16le PCM) into ``out_path``.

    MP3 encodes run as a subprocess (cancellable). WAV is written synchronously.
    """
    if media_format == MediaFormat.WAV:
        _write_wav(raw_path, out_path, sample_rate, channels)
        return EncodeJob(out_path, proc=None)

    cmd = _mp3_command(raw_path, out_path, bitrate, sample_rate, channels)
    if cmd is None:
        raise EncoderUnavailableError(
            "Neither 'lame' nor 'ffmpeg' is installed; cannot encode MP3. "
            "Install lame (recommended) or use --format wav."
        )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return EncodeJob(out_path, proc=proc)


def _mp3_command(
    raw_path: str, out_path: str, bitrate: int, sample_rate: int, channels: int
) -> Optional[List[str]]:
    encoder = available_mp3_encoder()
    if encoder == "lame":
        return [
            "lame",
            "--silent",
            "-r",  # raw PCM input
            "-s", f"{sample_rate / 1000:g}",
            "--bitwidth", str(SAMPLE_WIDTH * 8),
            "--signed",
            "--little-endian",
            "-m", "s" if channels == 2 else "m",
            "--cbr",
            "-b", str(bitrate),
            raw_path,
            out_path,
        ]
    if encoder == "ffmpeg":
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels),
            "-i", raw_path,
            "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k",
            out_path,
        ]
    return None


def _write_wav(raw_path: str, out_path: str, sample_rate: int, channels: int) -> None:
    frame_size = channels * SAMPLE_WIDTH
    with open(raw_path, "rb") as src, wave.open(out_path, "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(SAMPLE_WIDTH)
        dst.setframerate(sample_rate)
        remainder = b""
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            data = remainder + chunk
            # keep whole frames; carry any partial frame to the next read
            usable = len(data) - (len(data) % frame_size)
            dst.writeframesraw(data[:usable])
            remainder = data[usable:]
        # close() (via the context manager) patches the header from the frame count
