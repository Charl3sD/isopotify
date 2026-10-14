from isopotify.config import MediaFormat, OnExists
from isopotify import naming


def test_sanitize_strips_illegal_keeps_unicode():
    assert naming.sanitize_component("AC/DC: Live?") == "ACDC Live"
    assert naming.sanitize_component("Café del Mar") == "Café del Mar"  # accents kept
    assert naming.sanitize_component("  trailing.  ") == "trailing"
    assert naming.sanitize_component(None) == ""


def test_track_basename_and_separator():
    assert naming.track_basename("Daft Punk", "Get Lucky") == "Daft Punk - Get Lucky"
    assert naming.track_basename("Daft Punk", "Get Lucky", separator="_") == "Daft_Punk_-_Get_Lucky"
    assert naming.track_basename("", "Instrumental") == "Instrumental"
    assert naming.track_basename("", "") == ""


def test_folder_grouping():
    flat = naming.folder_path("/music", "A", "Alb", " ", group_by_folder=False)
    assert flat == "/music"
    nested = naming.folder_path("/music", "A", "Alb", " ", group_by_folder=True)
    assert nested == "/music/A/Alb"


def test_resolve_output_path_strategies():
    existing = {"/m/Artist - Song.mp3", "/m/Artist - Song (2).mp3"}
    ex = lambda p: p in existing

    # new file → base path
    assert naming.resolve_output_path("/m", "New - Song", MediaFormat.MP3, OnExists.SKIP, exists=ex) \
        == "/m/New - Song.mp3"
    # exists + SKIP → None
    assert naming.resolve_output_path("/m", "Artist - Song", MediaFormat.MP3, OnExists.SKIP, exists=ex) is None
    # exists + OVERWRITE → base path
    assert naming.resolve_output_path("/m", "Artist - Song", MediaFormat.MP3, OnExists.OVERWRITE, exists=ex) \
        == "/m/Artist - Song.mp3"
    # exists + DUPLICATE → first free (N), skipping the taken (2)
    assert naming.resolve_output_path("/m", "Artist - Song", MediaFormat.MP3, OnExists.DUPLICATE, exists=ex) \
        == "/m/Artist - Song (3).mp3"
