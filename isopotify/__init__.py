"""Isopotify — a Linux Spotify recorder.

Isopotify watches Spotify over MPRIS (D-Bus), captures its audio through a
PipeWire/PulseAudio null-sink, and writes one tagged WAV/MP3 file per track —
skipping ads and tracks the listener skipped before they finished.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
