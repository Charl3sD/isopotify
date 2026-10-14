"""Audio capture via PipeWire/PulseAudio.

Strategy (isolates *only* Spotify's audio from the rest of the desktop):
  1. load a null-sink named ``isopotify``;
  2. move Spotify's playback stream (sink-input) onto it;
  3. capture ``isopotify.monitor`` with ``parec`` as raw s16le PCM;
  4. optionally loop the monitor back to the real default sink so you still hear it.

All PulseAudio control goes through the ``pactl`` CLI (present on both PulseAudio
and PipeWire's pulse shim); capture is a long-lived ``parec`` subprocess read on
a background thread.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from typing import Callable, List, Optional, Sequence

from .config import CHANNELS, SAMPLE_RATE, SINK_NAME

log = logging.getLogger("isopotify.audio")

_SINK_INPUT_RE = re.compile(r"Sink Input #(\d+)")
_PROP_RE = re.compile(r'^\s*([\w.]+)\s*=\s*"?(.*?)"?\s*$')


class AudioError(RuntimeError):
    pass


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        raise AudioError(
            f"'{binary}' not found. Install PulseAudio/PipeWire utilities "
            f"(e.g. 'libpulse' / 'pipewire-pulse')."
        )


def _run(cmd: Sequence[str]) -> str:
    result = subprocess.run(
        list(cmd), capture_output=True, text=True, check=True
    )
    return result.stdout


def sink_exists(name: str) -> bool:
    try:
        out = _run(["pactl", "list", "sinks", "short"])
    except subprocess.CalledProcessError:
        return False
    return any(line.split("\t")[1:2] == [name] for line in out.splitlines() if "\t" in line)


def load_null_sink(name: str) -> Optional[int]:
    """Load a null-sink; return its module id, or None if it already existed."""
    if sink_exists(name):
        log.debug("null-sink %r already present", name)
        return None
    out = _run(
        [
            "pactl", "load-module", "module-null-sink",
            f"sink_name={name}",
            f"sink_properties=device.description=Isopotify",
        ]
    )
    return int(out.strip())


def load_loopback(source: str, sink: Optional[str] = None) -> Optional[int]:
    """Loop ``source`` (e.g. isopotify.monitor) to ``sink`` (default if None)."""
    args = ["pactl", "load-module", "module-loopback", f"source={source}", "latency_msec=50"]
    if sink:
        args.append(f"sink={sink}")
    try:
        out = _run(args)
        return int(out.strip())
    except subprocess.CalledProcessError as exc:
        log.warning("Could not load loopback (%s): %s", source, exc)
        return None


def unload_module(module_id: Optional[int]) -> None:
    if module_id is None:
        return
    try:
        _run(["pactl", "unload-module", str(module_id)])
    except subprocess.CalledProcessError as exc:
        log.warning("Could not unload module %s: %s", module_id, exc)


def list_sink_inputs() -> List[dict]:
    """Return [{'index': int, 'props': {name: value}}] for each playback stream."""
    try:
        out = _run(["pactl", "list", "sink-inputs"])
    except subprocess.CalledProcessError:
        return []
    inputs: List[dict] = []
    current: Optional[dict] = None
    for line in out.splitlines():
        m = _SINK_INPUT_RE.match(line)
        if m:
            current = {"index": int(m.group(1)), "props": {}}
            inputs.append(current)
            continue
        if current is not None:
            pm = _PROP_RE.match(line)
            if pm:
                current["props"][pm.group(1)] = pm.group(2)
    return inputs


def find_spotify_sink_input(match: Sequence[str] = ("spotify",)) -> Optional[int]:
    """Index of Spotify's playback stream, or None if it isn't playing yet."""
    needles = tuple(m.lower() for m in match)
    for si in list_sink_inputs():
        props = si["props"]
        haystack = " ".join(
            props.get(k, "")
            for k in ("application.name", "application.process.binary", "media.name")
        ).lower()
        if any(n in haystack for n in needles):
            return si["index"]
    return None


def move_sink_input(index: int, sink_name: str) -> bool:
    try:
        _run(["pactl", "move-sink-input", str(index), sink_name])
        return True
    except subprocess.CalledProcessError as exc:
        log.warning("Could not move sink-input %s → %s: %s", index, sink_name, exc)
        return False


class AudioCapture:
    """Owns the null-sink + parec capture and streams PCM to a callback."""

    def __init__(
        self,
        sink_name: str = SINK_NAME,
        listen: bool = True,
        spotify_match: Sequence[str] = ("spotify",),
    ):
        self.sink_name = sink_name
        self.monitor = f"{sink_name}.monitor"
        self.listen = listen
        self.spotify_match = spotify_match

        self._on_data: Optional[Callable[[bytes], None]] = None
        self._parec: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._running = False
        self._owned_modules: List[int] = []  # modules we loaded, to unload on stop

    def set_data_callback(self, cb: Callable[[bytes], None]) -> None:
        self._on_data = cb

    def start(self) -> None:
        for binary in ("pactl", "parec"):
            _require(binary)

        module_id = load_null_sink(self.sink_name)
        if module_id is not None:
            self._owned_modules.append(module_id)

        if self.listen:
            lb = load_loopback(self.monitor)
            if lb is not None:
                self._owned_modules.append(lb)

        self._parec = subprocess.Popen(
            [
                "parec",
                "-d", self.monitor,
                "--format=s16le",
                f"--rate={SAMPLE_RATE}",
                f"--channels={CHANNELS}",
                "--latency-msec=30",  # small, steady buffer to tighten track boundaries
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._running = True
        self._reader = threading.Thread(
            target=self._read_loop, name="parec-reader", daemon=True
        )
        self._reader.start()
        log.info("Capturing %s via parec", self.monitor)

    def route_spotify(self) -> bool:
        """Move Spotify's current stream onto our sink. Safe to call repeatedly.

        Returns True if Spotify's stream was found and routed (or already here).
        """
        index = find_spotify_sink_input(self.spotify_match)
        if index is None:
            return False
        return move_sink_input(index, self.sink_name)

    def _read_loop(self) -> None:
        assert self._parec is not None and self._parec.stdout is not None
        stream = self._parec.stdout
        chunk = 1 << 14  # 16 KiB ≈ 93 ms of audio
        while self._running:
            data = stream.read(chunk)
            if not data:
                break
            if self._on_data is not None:
                try:
                    self._on_data(data)
                except Exception as exc:  # never let a consumer kill capture
                    log.error("data callback error: %s", exc)

    def stop(self) -> None:
        self._running = False
        if self._parec is not None and self._parec.poll() is None:
            self._parec.terminate()
            try:
                self._parec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._parec.kill()
        if self._reader is not None:
            self._reader.join(timeout=2)
        # Unload only the modules we created, in reverse order.
        for module_id in reversed(self._owned_modules):
            unload_module(module_id)
        self._owned_modules.clear()
        log.info("Audio capture stopped")
