"""File and folder naming.

- sanitize artist/title/album into safe path components (keep Unicode/accents,
  drop characters illegal on disk);
- build ``Artist - Title`` base names, with optional whitespace→separator
  replacement;
- optional ``Artist/Album/`` folder grouping;
- resolve collisions per the on-exists strategy, with a ``" (N)"`` duplicate
  suffix.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Optional

from .config import MediaFormat, OnExists

# Characters that are illegal or troublesome in a single path component.
# '/' and NUL are illegal on Linux; the rest are stripped so files stay portable.
_ILLEGAL = set('/\x00<>:"\\|?*')


def sanitize_component(value: Optional[str]) -> str:
    """Make ``value`` safe to use as one path component. Unicode is preserved."""
    if not value:
        return ""
    text = unicodedata.normalize("NFC", value).strip()
    cleaned = "".join(
        ch for ch in text if ch not in _ILLEGAL and (ch == "\t" or ch >= " ")
    )
    # collapse any control/whitespace oddities and trailing dots/spaces that
    # some filesystems dislike
    cleaned = cleaned.replace("\t", " ").strip().rstrip(".")
    return cleaned


def _apply_separator(name: str, separator: str) -> str:
    """Replace runs of whitespace with the configured separator."""
    if separator == " ":
        return name
    return separator.join(name.split())


def track_basename(artist: str, title: str, separator: str = " ") -> str:
    """Build the ``Artist - Title`` base file name (no extension)."""
    artist_c = sanitize_component(artist)
    title_c = sanitize_component(title)
    if not artist_c and not title_c:
        return ""
    if artist_c and title_c:
        base = f"{artist_c} - {title_c}"
    else:
        base = artist_c or title_c
    return _apply_separator(base, separator)


def folder_path(
    output_dir: str,
    artist: str,
    album: str,
    separator: str,
    group_by_folder: bool,
) -> str:
    """Return the directory a track should live in."""
    if not group_by_folder:
        return output_dir
    parts = [output_dir]
    artist_dir = _apply_separator(sanitize_component(artist), separator)
    album_dir = _apply_separator(sanitize_component(album), separator)
    if artist_dir:
        parts.append(artist_dir)
    if album_dir:
        parts.append(album_dir)
    return os.path.join(*parts)


def resolve_output_path(
    directory: str,
    basename: str,
    media_format: MediaFormat,
    on_exists: OnExists,
    exists=os.path.exists,
) -> Optional[str]:
    """Resolve the final path, honouring the on-exists strategy.

    Returns ``None`` when the file exists and the strategy is SKIP (caller
    should not record). The ``exists`` hook is injectable for testing.
    """
    ext = media_format.value
    base = os.path.join(directory, f"{basename}.{ext}")

    if not exists(base):
        return base

    if on_exists == OnExists.OVERWRITE:
        return base
    if on_exists == OnExists.SKIP:
        return None

    # DUPLICATE: find the first free "basename (N).ext" (N starts at 2).
    n = 2
    while True:
        candidate = os.path.join(directory, f"{basename} ({n}).{ext}")
        if not exists(candidate):
            return candidate
        n += 1


def ensure_directory(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
