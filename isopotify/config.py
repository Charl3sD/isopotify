"""Configuration for Isopotify.

Precedence: built-in defaults < config file < CLI flags.

The config file lives at ``~/.config/isopotify/config.toml`` (TOML) and is
optional. TOML is used when a reader is available (the ``toml`` package, or
``tomllib`` on Python 3.11+); otherwise a ``config.json`` sibling is read as a
dependency-free fallback. Neither file is required to run.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


class OnExists(str, enum.Enum):
    """What to do when the target file already exists."""

    SKIP = "skip"
    OVERWRITE = "overwrite"
    DUPLICATE = "duplicate"


class MediaFormat(str, enum.Enum):
    MP3 = "mp3"
    WAV = "wav"


SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample (s16le)
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH  # 176400

SINK_NAME = "isopotify"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "isopotify"


@dataclasses.dataclass
class Config:
    """Runtime configuration. All fields have sane defaults."""

    output_dir: str = os.path.expanduser("~/Music/Isopotify")
    media_format: MediaFormat = MediaFormat.MP3
    bitrate: int = 320  # kbps, for MP3
    separator: str = " "  # whitespace replacement in file names
    group_by_folder: bool = False  # Artist/Album/ sub-folders
    min_length: int = 30  # seconds; shorter recordings are dropped
    on_exists: OnExists = OnExists.SKIP
    record_ads: bool = False  # keep ad segments instead of skipping them

    # Cancel-on-skip feature: discard a track the listener skipped before it
    # (nearly) finished. "Completed" iff recorded >= length - skip_tolerance.
    keep_skipped: bool = False  # True disables the feature (keep partials)
    skip_tolerance: float = 5.0  # seconds of slack for fade-out / latency

    listen: bool = True  # also route audio to the default sink so you still hear it
    verbose: bool = False

    # ------------------------------------------------------------------ loaders
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Load defaults overlaid with an optional config file."""
        cfg = cls()
        data = _read_config_file(path)
        if data:
            cfg.apply(data)
        return cfg

    def apply(self, data: Dict[str, Any]) -> None:
        """Overlay a dict of raw values onto this config, coercing enums/types.

        Unknown keys are ignored; invalid values raise ValueError so a typo in
        the config file or CLI is reported rather than silently dropped.
        """
        fields = {f.name: f for f in dataclasses.fields(self)}
        for key, value in data.items():
            if value is None or key not in fields:
                continue
            setattr(self, key, _coerce(key, fields[key].type, value))

    def validate(self) -> None:
        if self.bitrate <= 0:
            raise ValueError(f"bitrate must be positive, got {self.bitrate}")
        if self.min_length < 0:
            raise ValueError(f"min_length must be >= 0, got {self.min_length}")
        if self.skip_tolerance < 0:
            raise ValueError(f"skip_tolerance must be >= 0, got {self.skip_tolerance}")


def _coerce(key: str, _type: Any, value: Any) -> Any:
    if key == "media_format":
        return MediaFormat(str(value).lower())
    if key == "on_exists":
        return OnExists(str(value).lower())
    if key in ("bitrate", "min_length"):
        return int(value)
    if key == "skip_tolerance":
        return float(value)
    if key in ("group_by_folder", "record_ads", "keep_skipped", "listen", "verbose"):
        return bool(value)
    if key == "output_dir":
        return os.path.expanduser(str(value))
    return value


def _read_config_file(path: Optional[Path]) -> Dict[str, Any]:
    """Read the TOML config if present, else the JSON fallback, else {}."""
    if path is not None:
        return _read_one(path)
    toml_path = config_dir() / "config.toml"
    if toml_path.exists():
        return _read_one(toml_path)
    json_path = config_dir() / "config.json"
    if json_path.exists():
        return _read_one(json_path)
    return {}


def _read_one(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text) or {}
    # TOML: stdlib on 3.11+, else the 'tomli' backport (a declared dependency),
    # else 'toml' if it happens to be installed.
    for modname in ("tomllib", "tomli"):
        try:
            mod = __import__(modname)
            return mod.loads(text) or {}
        except ModuleNotFoundError:
            continue
    try:
        import toml  # type: ignore
        return toml.loads(text) or {}
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            f"Cannot read {path}: no TOML parser available. Install 'tomli' "
            f"(or 'toml'), or use a config.json instead."
        ) from exc
