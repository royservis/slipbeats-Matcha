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


def to_rekordbox_xml(name: str, items: list[dict], groups: list[dict] | None = None) -> str:
    """rekordbox collection XML (Preferences > Advanced > Database > rekordbox xml).
    Includes only the tracks in this playlist; rekordbox matches them to its own collection by
    Location when the file is already imported, otherwise adds them.

    groups: optional [{"name": "Must play", "paths": [...]}] — when given, the playlist becomes a
    folder containing one sub-playlist per group plus an "All" playlist in set order."""
    today = dt.date.today().isoformat()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<DJ_PLAYLISTS Version="1.0.0">',
           '  <PRODUCT Name="slipbeats" Version="0.1" Company="Slipbeats"/>',
           f'  <COLLECTION Entries="{len(items)}">']
    id_by_path: dict[str, int] = {}
    for i, it in enumerate(items, start=1):
        id_by_path[it["path"]] = i
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
    q = lambda v: escape(str(v), {chr(34): "&quot;"})

    def playlist_node(pname: str, ids: list[int], indent: str) -> list[str]:
        lines = [f'{indent}<NODE Name="{q(pname)}" Type="1" KeyType="0" Entries="{len(ids)}">']
        lines += [f'{indent}  <TRACK Key="{i}"/>' for i in ids]
        lines.append(f"{indent}</NODE>")
        return lines

    out.append("  <PLAYLISTS>")
    out.append('    <NODE Type="0" Name="ROOT" Count="1">')
    all_ids = list(range(1, len(items) + 1))
    if groups:
        out.append(f'      <NODE Type="0" Name="{q(name)}" Count="{len(groups) + 1}">')
        out += playlist_node("All", all_ids, "        ")
        for g in groups:
            ids = [id_by_path[p] for p in g.get("paths", []) if p in id_by_path]
            out += playlist_node(g["name"], ids, "        ")
        out.append("      </NODE>")
    else:
        out += playlist_node(name, all_ids, "      ")
    out.append("    </NODE>")
    out.append("  </PLAYLISTS>")
    out.append("</DJ_PLAYLISTS>")
    return "\n".join(out) + "\n"


def merge_into_rekordbox_xml(existing_path: str, name: str, items: list[dict], groups: list[dict] | None) -> str:
    """Add (or replace) a Slipbeats folder/playlist inside the user's own rekordbox.xml.
    Existing tracks are reused by Location; new ones get fresh TrackIDs. A .bak copy is written first.
    Returns the path written."""
    import shutil
    import xml.etree.ElementTree as ET
    from urllib.parse import unquote

    src = Path(existing_path).expanduser()
    if not src.exists() or src.stat().st_size < 50:
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(to_rekordbox_xml(name, items, groups), encoding="utf-8")
        return str(src)
    shutil.copy2(src, src.with_suffix(src.suffix + ".bak"))
    tree = ET.parse(src)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    pls = root.find("PLAYLISTS")
    if coll is None or pls is None:
        raise ValueError("That file doesn't look like a rekordbox xml (no COLLECTION/PLAYLISTS)")
    # index existing tracks by decoded location path
    by_loc: dict[str, str] = {}
    max_id = 0
    for t in coll.findall("TRACK"):
        loc = unquote(t.get("Location", "").replace("file://localhost", ""))
        by_loc[loc] = t.get("TrackID", "0")
        try:
            max_id = max(max_id, int(t.get("TrackID", "0")))
        except ValueError:
            pass
    today = dt.date.today().isoformat()
    id_by_path: dict[str, str] = {}
    for it in items:
        if it["path"] in by_loc:
            id_by_path[it["path"]] = by_loc[it["path"]]
            continue
        max_id += 1
        secs = int(round((it.get("duration_ms") or 0) / 1000))
        ET.SubElement(coll, "TRACK", {
            "TrackID": str(max_id), "Name": it.get("title", ""), "Artist": it.get("artist", ""),
            "Album": it.get("album") or "", "Genre": it.get("genre") or "", "Kind": kind_for(it["path"]),
            "Size": str(it.get("size") or 0), "TotalTime": str(secs), "Year": str(it.get("year") or 0),
            "AverageBpm": f"{float(it['bpm']):.2f}" if it.get("bpm") else "0.00", "DateAdded": today,
            "BitRate": str(it.get("bitrate") or 0), "Tonality": it.get("key") or "",
            "Comments": it.get("comment") or "",
            "Location": "file://localhost" + quote(it["path"], safe="/()[]!,'=+$@;:-_.~"),
        })
        id_by_path[it["path"]] = str(max_id)
        by_loc[it["path"]] = str(max_id)
    coll.set("Entries", str(len(coll.findall("TRACK"))))
    rootnode = pls.find("NODE")
    if rootnode is None:
        rootnode = ET.SubElement(pls, "NODE", {"Type": "0", "Name": "ROOT", "Count": "0"})
    # replace any previous node with this name
    for old in [n for n in rootnode.findall("NODE") if n.get("Name") == name]:
        rootnode.remove(old)

    def add_playlist(parent, pname, paths):
        node = ET.SubElement(parent, "NODE", {"Name": pname, "Type": "1", "KeyType": "0", "Entries": str(len(paths))})
        for p in paths:
            ET.SubElement(node, "TRACK", {"Key": id_by_path[p]})
    all_paths = [it["path"] for it in items]
    if groups:
        folder = ET.SubElement(rootnode, "NODE", {"Type": "0", "Name": name, "Count": str(len(groups) + 1)})
        add_playlist(folder, "All", all_paths)
        for g in groups:
            add_playlist(folder, g["name"], [p for p in g.get("paths", []) if p in id_by_path])
    else:
        add_playlist(rootnode, name, all_paths)
    rootnode.set("Count", str(len(rootnode.findall("NODE"))))
    ET.indent(tree, space="  ")
    tree.write(src, encoding="UTF-8", xml_declaration=True)
    return str(src)


def kind_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".mp3": "MP3 File", ".m4a": "M4A File", ".aif": "AIFF File", ".aiff": "AIFF File",
            ".wav": "WAV File", ".flac": "FLAC File"}.get(ext, "Unknown")
