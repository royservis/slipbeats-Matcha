"""Playlist export: M3U8 and rekordbox XML."""
from __future__ import annotations

import datetime as dt
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from xml.sax.saxutils import escape


def absolute_path(root_path: str, export_path: str | None, rel_path: str) -> str:
    """Path as rekordbox on the Mac will see it. export_path overrides root_path when the
    index was built from a different mount of the same folder."""
    base = export_path or root_path
    return str(PurePosixPath(base) / PurePosixPath(*Path(rel_path).parts))


def to_m3u8(name: str, items: list[dict]) -> str:
    """items: [{path, artist, title, duration_ms}]"""
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for it in items:
        secs = int(round((it.get("duration_ms") or 0) / 1000)) or -1
        lines.append(f"#EXTINF:{secs},{it.get('artist','')} - {it.get('title','')}")
        lines.append(it["path"])
    return "\n".join(lines) + "\n"


def to_rekordbox_xml(name: str, items: list[dict]) -> str:
    """rekordbox collection XML (File > Import > rekordbox xml / Preferences > Advanced > Database).
    Includes only the tracks in this playlist; rekordbox matches them to its own collection by
    Location when the file is already imported, otherwise adds them."""
    today = dt.date.today().isoformat()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<DJ_PLAYLISTS Version="1.0.0">',
           '  <PRODUCT Name="slipbeats" Version="0.1" Company="Slipbeats"/>',
           f'  <COLLECTION Entries="{len(items)}">']
    for i, it in enumerate(items, start=1):
        loc = "file://localhost" + quote(it["path"], safe="/()[]!,'=+$@;:-_.~")
        secs = int(round((it.get("duration_ms") or 0) / 1000))
        attrs = {
            "TrackID": str(i), "Name": it.get("title", ""), "Artist": it.get("artist", ""),
            "Album": it.get("album") or "", "Genre": it.get("genre") or "",
            "Kind": kind_for(it["path"]), "Size": str(it.get("size") or 0),
            "TotalTime": str(secs), "Year": str(it.get("year") or 0),
            "AverageBpm": f"{float(it['bpm']):.2f}" if it.get("bpm") else "0.00",
            "DateAdded": today, "BitRate": str(it.get("bitrate") or 0),
            "Tonality": it.get("key") or "", "Comments": it.get("comment") or "",
            "Location": loc,
        }
        a = " ".join(f'{k}="{escape(str(v), {chr(34): "&quot;"})}"' for k, v in attrs.items())
        out.append(f"    <TRACK {a}/>")
    out.append("  </COLLECTION>")
    out.append("  <PLAYLISTS>")
    out.append('    <NODE Type="0" Name="ROOT" Count="1">')
    out.append(f'      <NODE Name="{escape(name, {chr(34): "&quot;"})}" Type="1" KeyType="0" Entries="{len(items)}">')
    for i in range(1, len(items) + 1):
        out.append(f'        <TRACK Key="{i}"/>')
    out.append("      </NODE>")
    out.append("    </NODE>")
    out.append("  </PLAYLISTS>")
    out.append("</DJ_PLAYLISTS>")
    return "\n".join(out) + "\n"


def kind_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".mp3": "MP3 File", ".m4a": "M4A File", ".aif": "AIFF File", ".aiff": "AIFF File",
            ".wav": "WAV File", ".flac": "FLAC File"}.get(ext, "Unknown")
