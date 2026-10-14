import math
import struct
import subprocess
import wave

import pytest

from isopotify import encoder
from isopotify.config import BYTES_PER_SECOND, CHANNELS, MediaFormat, SAMPLE_RATE


def _write_sine_raw(path, seconds=2.0, freq=440.0):
    n = int(seconds * SAMPLE_RATE)
    with open(path, "wb") as fh:
        for i in range(n):
            val = int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
            fh.write(struct.pack("<hh", val, val))  # stereo
    return path


def test_wav_roundtrip_has_correct_duration(tmp_path):
    raw = _write_sine_raw(tmp_path / "in.raw", seconds=1.5)
    out = str(tmp_path / "out.wav")
    job = encoder.encode(str(raw), out, MediaFormat.WAV, bitrate=0)
    assert job.wait() is True
    with wave.open(out, "rb") as w:
        assert w.getnchannels() == CHANNELS
        assert w.getframerate() == SAMPLE_RATE
        assert abs(w.getnframes() / SAMPLE_RATE - 1.5) < 0.01


@pytest.mark.skipif(encoder.available_mp3_encoder() is None, reason="no lame/ffmpeg")
def test_mp3_encode_produces_playable_file(tmp_path):
    raw = _write_sine_raw(tmp_path / "in.raw", seconds=2.0)
    out = str(tmp_path / "out.mp3")
    job = encoder.encode(str(raw), out, MediaFormat.MP3, bitrate=160)
    assert job.wait() is True
    from mutagen.mp3 import MP3

    audio = MP3(out)
    assert audio.info.length == pytest.approx(2.0, abs=0.3)
    assert audio.info.channels == 2


def test_cancel_terminates_process_and_removes_output(tmp_path):
    out = tmp_path / "partial.mp3"
    out.write_bytes(b"partial data")  # pretend an encode started writing
    proc = subprocess.Popen(["sleep", "30"])
    job = encoder.EncodeJob(str(out), proc=proc)

    job.cancel()

    assert job.cancelled is True
    assert proc.poll() is not None  # process was terminated
    assert not out.exists()  # partial output removed
    assert job.wait() is False
