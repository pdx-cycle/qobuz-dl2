import logging
import os
import re

import mutagen.id3 as id3
from mutagen.flac import FLAC, Picture
from mutagen.id3._util import ID3NoHeaderError  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


# unicode symbols: \u00a9 is the copyright sign, \u2117 is the sound-recording (phonogram) sign
COPYRIGHT, PHON_COPYRIGHT = "\u00a9", "\u2117"
# if a metadata block exceeds this, mutagen will raise error
# and the file won't be tagged
FLAC_MAX_BLOCKSIZE = 16777215

# Map logical tag names to ID3 frame names (avoid static attribute lookups for editor/type stubs)
ID3_LEGEND = {
    "album": "TALB",
    "albumartist": "TPE2",
    "artist": "TPE1",
    "comment": "COMM",
    "composer": "TCOM",
    "copyright": "TCOP",
    "date": "TDAT",
    "genre": "TCON",
    "isrc": "TSRC",
    "label": "TPUB",
    "performer": "TOPE",
    "title": "TIT2",
    "year": "TYER",
}


def _get_title(track_dict):
    title = track_dict["title"]
    version = track_dict.get("version")
    if version:
        title = f"{title} ({version})"
    # for classical works
    if track_dict.get("work"):
        title = f"{track_dict['work']}: {title}"

    return title


def _format_copyright(s: str) -> str:
    if s:
        s = s.replace("(P)", PHON_COPYRIGHT)
        s = s.replace("(C)", COPYRIGHT)
    return s


def _format_genres(genres: list) -> str:
    """Fixes the weirdly formatted genre lists returned by the API.
    >>> g = ['Pop/Rock', 'Pop/Rock→Rock', 'Pop/Rock→Rock→Alternatif et Indé']
    >>> _format_genres(g)
    'Pop, Rock, Alternatif et Indé'
    """
    genres = re.findall(r"([^\u2192\/]+)", "/".join(genres or []))
    no_repeats: list[str] = []
    for g in genres:
        if g not in no_repeats:
            no_repeats.append(g)
    return ", ".join(no_repeats)


def _embed_flac_img(cover_path, audio: FLAC):
    if not cover_path:
        raise ValueError("Completed cover required for embedding")
    if os.path.getsize(cover_path) > FLAC_MAX_BLOCKSIZE:
        raise ValueError("Cover too large for FLAC metadata")
    image = Picture()
    image.type = 3
    image.desc = "cover"
    with open(cover_path, "rb") as stream:
        image.data = stream.read()
    image.mime = "image/png" if image.data.startswith(b"\x89PNG") else "image/jpeg"
    other_pictures = [picture for picture in audio.pictures if picture.type != 3]
    audio.clear_pictures()
    for picture in other_pictures:
        audio.add_picture(picture)
    audio.add_picture(image)


def _embed_id3_img(cover_path, audio: id3.ID3):
    if not cover_path:
        raise ValueError("Completed cover required for embedding")
    with open(cover_path, "rb") as stream:
        data = stream.read()
    other_pictures = [picture for picture in audio.getall("APIC") if picture.type != 3]
    audio.delall("APIC")
    for picture in other_pictures:
        audio.add(picture)
    audio.add(
        id3.APIC(
            encoding=3, mime="image/png" if data.startswith(b"\x89PNG") else "image/jpeg", type=3, desc="", data=data
        )
    )


# Use KeyError catching instead of dict.get to avoid empty tags
def tag_flac(filename, root_dir, final_name, d: dict, album, istrack=True, em_image=False, cover_path=None):
    """
    Tag a FLAC file

    :param str filename: FLAC file path
    :param str root_dir: Root dir used to get the cover art
    :param str final_name: Final name of the FLAC file (complete path)
    :param dict d: Track dictionary from Qobuz_client
    :param dict album: Album dictionary from Qobuz_client
    :param bool istrack
    :param bool em_image: Embed cover art into file
    """
    audio = FLAC(filename)

    audio["TITLE"] = _get_title(d)

    audio["TRACKNUMBER"] = str(d["track_number"])  # TRACK NUMBER

    if "Disc " in final_name:
        audio["DISCNUMBER"] = str(d["media_number"])

    composer = (d.get("composer") or {}).get("name")
    if composer:
        audio["COMPOSER"] = composer

    artist_ = (d.get("performer") or {}).get("name")  # TRACK ARTIST
    if istrack:
        audio["ARTIST"] = artist_ or d["album"]["artist"]["name"]  # TRACK ARTIST
    else:
        audio["ARTIST"] = artist_ or album["artist"]["name"]

    audio["LABEL"] = (album.get("label") or {}).get("name", "n/a")

    if istrack:
        audio["GENRE"] = _format_genres(d["album"]["genres_list"])
        audio["ALBUMARTIST"] = d["album"]["artist"]["name"]
        audio["TRACKTOTAL"] = str(d["album"]["tracks_count"])
        audio["ALBUM"] = d["album"]["title"]
        audio["DATE"] = d["album"]["release_date_original"]
        audio["COPYRIGHT"] = _format_copyright(d.get("copyright") or "n/a")
    else:
        audio["GENRE"] = _format_genres(album["genres_list"])
        audio["ALBUMARTIST"] = album["artist"]["name"]
        audio["TRACKTOTAL"] = str(album["tracks_count"])
        audio["ALBUM"] = album["title"]
        audio["DATE"] = album["release_date_original"]
        audio["COPYRIGHT"] = _format_copyright(album.get("copyright") or "n/a")

    if em_image:
        _embed_flac_img(cover_path, audio)

    audio.save()


def tag_mp3(filename, root_dir, final_name, d, album, istrack=True, em_image=False, cover_path=None):
    """
    Tag an mp3 file

    :param str filename: mp3 temporary file path
    :param str root_dir: Root dir used to get the cover art
    :param str final_name: Final name of the mp3 file (complete path)
    :param dict d: Track dictionary from Qobuz_client
    :param bool istrack
    :param bool em_image: Embed cover art into file
    """

    try:
        audio = id3.ID3(filename)
    except ID3NoHeaderError:
        audio = id3.ID3()

    # temporarily holds metadata
    tags = dict()
    tags["title"] = _get_title(d)
    try:
        label = (album.get("label") or {}).get("name")
        if label:
            tags["label"] = label
    except KeyError:
        pass

    artist_ = (d.get("performer") or {}).get("name")  # TRACK ARTIST
    if istrack:
        tags["artist"] = artist_ or d["album"]["artist"]["name"]  # TRACK ARTIST
    else:
        tags["artist"] = artist_ or album["artist"]["name"]

    if istrack:
        tags["genre"] = _format_genres(d["album"]["genres_list"])
        tags["albumartist"] = d["album"]["artist"]["name"]
        tags["album"] = d["album"]["title"]
        tags["date"] = d["album"]["release_date_original"]
        tags["copyright"] = _format_copyright(d.get("copyright") or "n/a")
        tracktotal = str(d["album"]["tracks_count"])
    else:
        tags["genre"] = _format_genres(album["genres_list"])
        tags["albumartist"] = album["artist"]["name"]
        tags["album"] = album["title"]
        tags["date"] = album["release_date_original"]
        tags["copyright"] = _format_copyright(album.get("copyright") or "n/a")
        tracktotal = str(album["tracks_count"])

    tags["year"] = tags["date"][:4]

    TRCK = getattr(id3, "TRCK")
    TPOS = getattr(id3, "TPOS")
    audio["TRCK"] = TRCK(encoding=3, text=f"{d['track_number']}/{tracktotal}")
    audio["TPOS"] = TPOS(encoding=3, text=str(d["media_number"]))

    # write metadata in `tags` to file
    for k, v in tags.items():
        frame_name = ID3_LEGEND[k]
        frame_cls = getattr(id3, frame_name)
        audio[frame_name] = frame_cls(encoding=3, text=v)

    if em_image:
        _embed_id3_img(cover_path, audio)

    audio.save(filename, v2_version=3)
