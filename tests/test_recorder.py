import os

import pytest

from isopotify.config import BYTES_PER_SECOND, Config, MediaFormat, OnExists
from isopotify.mpris import Track, parse_metadata
from isopotify.recorder import Recorder, decide


# ------------------------------------------------------------------ decide()
def test_decide_keep_when_completed():
    assert decide(300, 302, min_length=30, keep_skipped=False, skip_tolerance=5)[0] == "keep"


def test_decide_discard_when_skipped_before_end():
    action, reason = decide(40, 300, min_length=30, keep_skipped=False, skip_tolerance=5)
    assert action == "discard" and "skipped" in reason


def test_decide_discard_when_too_short():
    action, reason = decide(5, 300, min_length=30, keep_skipped=False, skip_tolerance=5)
    assert action == "discard" and "short" in reason


def test_decide_keep_skipped_disables_feature():
    assert decide(40, 300, min_length=30, keep_skipped=True, skip_tolerance=5)[0] == "keep"


def test_decide_unknown_length_falls_back_to_min_length():
    assert decide(45, 0, min_length=30, keep_skipped=False, skip_tolerance=5)[0] == "keep"
    assert decide(10, 0, min_length=30, keep_skipped=False, skip_tolerance=5)[0] == "discard"


# ----------------------------------------------------------- Track / metadata
def test_parse_metadata_from_mpris_shape():
    meta = {
        "mpris:trackid": "spotify:track:abc",
        "mpris:length": 215_000_000,  # µs
        "xesam:title": "Get Lucky",
        "xesam:artist": ["Daft Punk", "Pharrell"],
        "xesam:album": "Random Access Memories",
        "xesam:albumArtist": ["Daft Punk"],
        "xesam:trackNumber": 8,
    }
    t = parse_metadata(meta, "Playing")
    assert t.title == "Get Lucky"
    assert t.artist == "Daft Punk, Pharrell"
    assert t.length_sec == pytest.approx(215.0)
    assert t.is_playing and not t.is_ad and t.is_recordable()


def test_ad_detection():
    assert Track(trackid="spotify:ad:123", playback_status="Playing").is_ad
    assert Track(title="Advertisement", playback_status="Playing").is_ad
    assert Track(title="Song", artist="", playback_status="Playing").is_ad
    assert not Track(trackid="spotify:track:x", title="Song", artist="A",
                     playback_status="Playing").is_ad


# -------------------------------------------------------- end-to-end recorder
def _pcm(seconds):
    return b"\x00" * int(seconds * BYTES_PER_SECOND)


def _track(tid, title, artist, length, status="Playing"):
    return Track(trackid=tid, title=title, artist=artist, length_sec=length,
                 playback_status=status)


def test_recorder_keeps_full_tracks_and_drops_skipped_ads_and_short(tmp_path):
    cfg = Config(
        output_dir=str(tmp_path),
        media_format=MediaFormat.WAV,  # synchronous, no external encoder needed
        min_length=1,
        skip_tolerance=2,
        on_exists=OnExists.SKIP,
    )
    rec = Recorder(cfg, workdir=str(tmp_path / "work"))

    # A: played in full → kept
    rec.on_track(_track("spotify:track:a", "Song A", "Artist A", length=5))
    rec.feed(_pcm(5))
    # B: skipped after 2s of a 60s track → dropped
    rec.on_track(_track("spotify:track:b", "Song B", "Artist B", length=60))
    rec.feed(_pcm(2))
    # Ad: not recordable (also finalises B as a skip)
    rec.on_track(_track("spotify:ad:1", "Advertisement", "", length=30))
    rec.feed(_pcm(3))  # ignored: nothing is being recorded during the ad
    # C: played in full → kept
    rec.on_track(_track("spotify:track:c", "Song C", "Artist C", length=4))
    rec.feed(_pcm(4))
    # D: unknown length, only 0.5s → dropped (too short) on close()
    rec.on_track(_track("spotify:track:d", "Song D", "Artist D", length=0))
    rec.feed(_pcm(0.5))

    rec.close()

    files = sorted(os.listdir(tmp_path))
    files = [f for f in files if f.endswith(".wav")]
    assert files == ["Artist A - Song A.wav", "Artist C - Song C.wav"]


def test_recorder_duplicate_strategy(tmp_path):
    cfg = Config(
        output_dir=str(tmp_path),
        media_format=MediaFormat.WAV,
        min_length=1,
        skip_tolerance=2,
        on_exists=OnExists.DUPLICATE,
    )
    rec = Recorder(cfg, workdir=str(tmp_path / "work"))
    # Two distinct tracks that sanitise to the same file name (e.g. single vs
    # album version) → the duplicate strategy appends a " (N)" suffix.
    for tid in ("spotify:track:a1", "spotify:track:a2"):
        rec.on_track(_track(tid, "Song", "Artist", length=3))
        rec.feed(_pcm(3))
    rec.close()

    files = sorted(f for f in os.listdir(tmp_path) if f.endswith(".wav"))
    assert files == ["Artist - Song (2).wav", "Artist - Song.wav"]
