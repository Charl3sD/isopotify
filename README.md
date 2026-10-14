# Isopotify 🪲🎧

A **Linux Spotify recorder**.

Isopotify watches the Spotify desktop app over **MPRIS** (D-Bus), captures its
audio through a **PipeWire/PulseAudio null-sink**, and writes one tagged
`WAV`/`MP3` file per track. Ad breaks **and tracks you skip before they
finish** are not saved to disk; Spotify playback itself is never altered.

It records what Spotify plays (no downloading), so each track is the real
release at the quality Spotify streams (Free ≈ 160 kbps, Premium ≈ 320 kbps).

> **Personal use only.** Recording may breach Spotify's Terms of Use and may
> be restricted by copyright law where you live — read the
> [Disclaimer](#disclaimer) before using Isopotify.

---

## Building blocks

Isopotify is a **CLI** built from standard Linux pieces:

| Concern | Implementation |
|---|---|
| Track detection | **MPRIS2** over D-Bus (`org.mpris.MediaPlayer2.spotify`) |
| Audio capture | **PipeWire/PulseAudio** null-sink + `parec` |
| Monitoring | `module-loopback` to your real sink (so you still hear it) |
| MP3 encoding | `lame` (or `ffmpeg`) subprocess |
| Tagging | **mutagen** (ID3v2.3 + cover art) |
| Interface | `argparse` CLI + config file |

Because MPRIS exposes full metadata, title/artist/album/track-number and the
cover-art URL come straight from Spotify — no Last.fm/Spotify-API key needed.

---

## Requirements

- **Linux** with **PipeWire** (pulse shim) or **PulseAudio** — provides
  `pactl` and `parec` (packages: `libpulse`, and `pipewire-pulse` on PipeWire).
- **Spotify desktop app**, running and logged in.
- **`lame`** (recommended) or **`ffmpeg`** for MP3 (WAV needs neither).
- **Python ≥ 3.9**, plus:
  - `PyGObject` (`gi`) — the MPRIS/D-Bus binding. On Arch/Manjaro:
    `sudo pacman -S python-gobject`. On Debian/Ubuntu: `sudo apt install python3-gi`.
  - `mutagen`, `requests` (installed automatically via `pip`).

### Install

```bash
cd isopotify
python -m pip install -e .          # installs the `isopotify` command
# PyGObject usually comes from your distro (see above) rather than pip
```

On Arch/Manjaro the one-liner for system deps:

```bash
sudo pacman -S --needed lame pipewire-pulse libpulse python-gobject
```

---

## Usage

Start Isopotify, then just play music in Spotify:

```bash
isopotify --bitrate 320 --format mp3 --output ~/Music/Isopotify
```

```
• [info] Isopotify 0.1.0 → ~/Music/Isopotify (mp3/320k)
• [info] Watching Spotify — play something. Press Ctrl-C to stop.
🔴 [rec] Radio Radio - Dekshoo
🗑️  [drop] Radio Radio - Dekshoo: skipped before end (8s / 206s)
🔴 [rec] a6el - Monstre
✅ [done] ~/Music/Isopotify/a6el - Monstre.mp3
```

Press **Ctrl-C** to stop; the current track is finalised before exit.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--output DIR` | `~/Music/Isopotify` | where files are written |
| `--format mp3\|wav` | `mp3` | output format |
| `--bitrate N` | `320` | MP3 bitrate (kbps), e.g. `160`, `320` |
| `--separator S` | `" "` | replaces whitespace in file names (e.g. `_`) |
| `--group-by-folder` | off | save into `Artist/Album/` sub-folders |
| `--min-length N` | `30` | drop recordings shorter than N seconds |
| `--on-exists skip\|overwrite\|duplicate` | `skip` | collision policy (duplicate → `name (2).mp3`) |
| `--record-ads` | off | save ad segments instead of discarding them |
| `--keep-skipped` | off | **disable** cancel-on-skip (keep partial tracks) |
| `--skip-tolerance N` | `5` | seconds of slack before a track counts as "skipped" |
| `--no-listen` | off | don't also play audio to your speakers while recording |
| `--list-sinks` | — | list audio sinks/streams and exit |
| `-v, --verbose` | off | verbose logging |

### Config file

Persistent defaults live in `~/.config/isopotify/config.toml` (or `config.json`
if you don't have a TOML parser). CLI flags override it.

```toml
output_dir = "~/Music/Isopotify"
media_format = "mp3"
bitrate = 320
min_length = 30
on_exists = "skip"
skip_tolerance = 5
group_by_folder = false
```

---

## The cancel-on-skip feature

When you **skip a track before it has played to near its full length**,
Isopotify treats that take as unwanted/incomplete and **drops it before any
transcode starts** — so no CPU is wasted and no half-songs clutter your library.
A track counts as *complete* when:

```
recorded_seconds >= track_length - skip_tolerance   (default tolerance: 5 s)
```

If Spotify doesn't report a length (ads, some tracks), it falls back to the
`--min-length` filter. Disable the whole behaviour with `--keep-skipped`. An
already-running MP3 encode is also cancellable (subprocess terminated, partial
file removed) on abort.

---

## How it works

1. Load a null-sink named `isopotify` (`pactl load-module module-null-sink`).
2. Move Spotify's playback stream onto it (`pactl move-sink-input`), re-checked
   every second so it stays captured even if Spotify recreates the stream. With
   `--listen` (default) a `module-loopback` also sends it to your real speakers.
3. Capture `isopotify.monitor` with `parec` as raw s16le/44.1 kHz/stereo PCM.
4. Watch `org.mpris.MediaPlayer2.spotify` over D-Bus. On each track change the
   buffered PCM is finalised: kept (encoded → tagged → saved) or dropped
   (ad / skipped / too short).
5. On exit, the current track is finalised and **all modules Isopotify loaded
   are unloaded** — it leaves your audio graph as it found it.

### Ad detection

Ads are detected heuristically. The real-track id **shape** was verified live
(`/com/spotify/track/<id>`), so the id-based check looks for an `ad` segment in
the trackid; the **actual ad payload has not yet been observed**, so the robust
fallbacks are: an empty or `Advertisement` title, or a missing artist. Keep ads
with `--record-ads`. **The first time an ad plays, run with `-v`, grab the
logged metadata, and tune `Track.is_ad` in `isopotify/mpris.py`** if needed.

---

## Troubleshooting

- **"Could not connect to Spotify over MPRIS"** — start the Spotify desktop app
  first and make sure it's logged in. Check with:
  `gdbus call --session --dest org.mpris.MediaPlayer2.spotify --object-path /org/mpris/MediaPlayer2 --method org.freedesktop.DBus.Properties.Get org.mpris.MediaPlayer2.Player PlaybackStatus`
- **"No MP3 encoder found"** — `sudo pacman -S lame` (or use `--format wav`).
- **Recordings are silent** — Spotify's stream wasn't routed. Confirm Spotify is
  actually playing, then `isopotify --list-sinks` to see the streams; the router
  moves anything whose app name/binary matches `spotify`.
- **`import gi` fails** — install your distro's PyGObject package (see Requirements).
- **A leftover `isopotify` sink after a crash** — Isopotify only unloads modules
  it created; if a previous run was killed hard, it may reuse (and not clean up)
  an existing sink. Remove it with:
  `pactl unload-module $(pactl list short modules | awk '/isopotify/{print $1}')`.

---

## Development

```bash
python -m pytest -q        # 16 tests: naming, encoder (incl. cancel), recorder policy
```

Package layout:
`mpris.py` · `audio.py` · `recorder.py` · `encoder.py` · `tagger.py` ·
`naming.py` · `config.py` · `cli.py`.

### Roadmap

Possible future work: a GTK/Qt GUI, optional Last.fm / Spotify-Web-API tag
enrichment, loudness normalisation, and **silence trimming / a pre-roll
buffer** at track boundaries. Without the latter, the ~30 ms `parec` capture latency can
leave the very start of a track at the tail of the previous file; track-boundary
precision across two fully-played tracks is **not yet verified**.

## Disclaimer

Isopotify is an independent project. It is not affiliated with, endorsed by,
or sponsored by Spotify AB. "Spotify" is a trademark of Spotify AB.

Isopotify does not download, decrypt, or modify Spotify's streams or client,
and contains no code to bypass DRM or other access controls. It records the
audio your computer is already playing through the system sound server, like
any other audio recorder.

Recording streamed content may breach Spotify's Terms of Use and may be
restricted by copyright law in your country. Isopotify is intended for
personal use where permitted by law (e.g. private copying). Do not distribute
recordings. You are solely responsible for how you use this software, which is
provided "as is", without warranty (see LICENSE).

## Licence

MIT — see [LICENSE](LICENSE). Originally based on
[Spytify](https://github.com/jwallet/spy-spotify) by jwallet (MIT); its notice
is retained in the licence file.
