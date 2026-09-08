"""Scan music folders into a local SQLite index. Incremental: unchanged files are skipped."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .normalise import AUDIO_EXTS, display, key, parse_filename, parse_tag_title, split_artists

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, export_path TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  root_id INTEGER NOT NULL REFERENCES roots(id),
  rel_path TEXT NOT NULL,
  filename TEXT NOT NULL,
  ext TEXT, size INTEGER, mtime REAL,
  artist TEXT, title TEXT,          -- best-guess display values
  artist_key TEXT, title_key TEXT,  -- normalised for matching
  primary_artist_key TEXT,
  featured_keys TEXT,               -- JSON list
  version TEXT, version_tokens TEXT,  -- JSON list
  bpm INTEGER, musical_key TEXT, energy INTEGER,
  genre TEXT, year TEXT, album TEXT, comment TEXT,
  duration_ms INTEGER, bitrate INTEGER,
  tag_artist TEXT, tag_title TEXT,
  missing INTEGER DEFAULT 0,
  UNIQUE(root_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_tracks_title_key ON tracks(title_key);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_key ON tracks(artist_key);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, created REAL, updated REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS preferences (
  request_key TEXT PRIMARY KEY, track_id INTEGER, count INTEGER DEFAULT 1
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def read_tags(path: Path) -> dict:
    """Return tag dict via mutagen; tolerant of missing/odd tags."""
    out: dict = {}
    try:
        from mutagen import File
        f = File(str(path))
        if f is None:
            return out
        info = f.info
        out["duration_ms"] = int(round(getattr(info, "length", 0) * 1000))
        out["bitrate"] = int(getattr(info, "bitrate", 0) or 0) // 1000
        tags = f.tags or {}

        def first(*names):
            for n in names:
                v = tags.get(n)
                if v is None:
                    continue
                if hasattr(v, "text"):
                    v = v.text
                if isinstance(v, (list, tuple)):
                    v = v[0] if v else None
                if v is not None and str(v).strip():
                    return str(v).strip()
            return None

        # ID3 frame ids and MP4/Vorbis names
        out["artist"] = first("TPE1", "\xa9ART", "artist", "ARTIST")
        out["title"] = first("TIT2", "\xa9nam", "title", "TITLE")
        out["album"] = first("TALB", "\xa9alb", "album", "ALBUM")
        out["genre"] = first("TCON", "\xa9gen", "genre", "GENRE")
        out["year"] = first("TDRC", "TYER", "\xa9day", "date", "DATE", "year")
        out["bpm"] = first("TBPM", "tmpo", "bpm", "BPM")
        out["musical_key"] = first("TKEY", "initialkey", "INITIALKEY", "----:com.apple.iTunes:initialkey")
        out["energy"] = first("TXXX:EnergyLevel", "EnergyLevel", "ENERGYLEVEL", "----:com.apple.iTunes:EnergyLevel")
        comm = None
        for k in list(tags.keys()):
            if str(k).startswith("COMM"):
                v = tags[k]
                comm = str(v.text[0]) if getattr(v, "text", None) else str(v)
                break
        out["comment"] = comm or first("comment", "COMMENT", "\xa9cmt")
        if out["year"]:
            out["year"] = str(out["year"])[:4]
        if out["bpm"]:
            try:
                out["bpm"] = int(round(float(str(out["bpm"]).split()[0])))
            except ValueError:
                out["bpm"] = None
        if out["energy"]:
            try:
                out["energy"] = int(str(out["energy"]).strip()[:2])
            except ValueError:
                out["energy"] = None
    except Exception:
        pass
    return out


def build_row(root_id: int, root: Path, path: Path, st: os.stat_result) -> dict:
    stem = path.stem
    fn = parse_filename(stem)
    tags = read_tags(path)

    artist = fn.artist or tags.get("artist") or ""
    title = fn.title or ""
    version = fn.version
    vtoks = set(fn.version_tokens)
    bpm = fn.bpm

    t_artist = tags.get("artist") or ""
    t_title_raw = tags.get("title") or ""
    if t_title_raw:
        t_title, t_ver, t_vt, t_bpm = parse_tag_title(t_title_raw)
        if not title:
            title = t_title
        if not version:
            version = t_ver
        vtoks |= t_vt
        if bpm is None:
            bpm = t_bpm
    if not artist:
        artist = t_artist
    if tags.get("bpm"):
        bpm = tags["bpm"]  # a real tag beats a filename guess

    artists = split_artists(artist) if artist else []
    return dict(
        root_id=root_id,
        rel_path=str(path.relative_to(root)),
        filename=path.name,
        ext=path.suffix.lower(),
        size=st.st_size,
        mtime=st.st_mtime,
        artist=display(artist),
        title=display(title),
        artist_key=key(artist),
        title_key=key(title),
        primary_artist_key=artists[0] if artists else "",
        featured_keys=json.dumps(artists[1:]),
        version=display(version),
        version_tokens=json.dumps(sorted(vtoks)),
        bpm=bpm,
        musical_key=tags.get("musical_key"),
        energy=tags.get("energy"),
        genre=tags.get("genre"),
        year=tags.get("year"),
        album=tags.get("album"),
        comment=tags.get("comment"),
        duration_ms=tags.get("duration_ms"),
        bitrate=tags.get("bitrate"),
        tag_artist=t_artist,
        tag_title=t_title_raw,
        missing=0,
    )


class Scanner:
    """Runs a scan in a background thread and exposes progress."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.lock = threading.Lock()
        self.state = {"running": False, "total": 0, "done": 0, "added": 0, "updated": 0,
                      "removed": 0, "current": "", "error": None, "started": None, "finished": None}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, full: bool = False) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, total=0, done=0, added=0, updated=0, removed=0,
                              current="", error=None, started=time.time(), finished=None)
        threading.Thread(target=self._run, args=(full,), daemon=True).start()
        return True

    def _run(self, full: bool):
        try:
            roots = self.con.execute("SELECT id, path FROM roots").fetchall()
            files: list[tuple[int, Path, Path]] = []
            for r in roots:
                root = Path(r["path"])
                if not root.exists():
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_slipbeats"]
                    for name in filenames:
                        if name.startswith("."):
                            continue
                        if Path(name).suffix.lower() in AUDIO_EXTS:
                            files.append((r["id"], root, Path(dirpath) / name))
            with self.lock:
                self.state["total"] = len(files)

            existing = {(row["root_id"], row["rel_path"]): (row["size"], row["mtime"], row["id"])
                        for row in self.con.execute("SELECT id, root_id, rel_path, size, mtime FROM tracks")}
            seen = set()
            batch: list[dict] = []
            cols = None
            for i, (root_id, root, path) in enumerate(files):
                try:
                    st = path.stat()
                except OSError:
                    continue
                rel = str(path.relative_to(root))
                seen.add((root_id, rel))
                prev = existing.get((root_id, rel))
                if prev and not full and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1:
                    pass
                else:
                    row = build_row(root_id, root, path, st)
                    batch.append(row)
                    with self.lock:
                        if prev:
                            self.state["updated"] += 1
                        else:
                            self.state["added"] += 1
                if len(batch) >= 200:
                    self._flush(batch)
                    batch = []
                with self.lock:
                    self.state["done"] = i + 1
                    self.state["current"] = path.name
            if batch:
                self._flush(batch)
            # mark files that vanished
            gone = [existing[k][2] for k in existing if k not in seen]
            if gone:
                self.con.executemany("UPDATE tracks SET missing=1 WHERE id=?", [(g,) for g in gone])
                self.con.commit()
            # files that were missing last time but are back now
            revived = [existing[k][2] for k in existing if k in seen]
            if revived:
                self.con.executemany("UPDATE tracks SET missing=0 WHERE id=? AND missing=1",
                                     [(r,) for r in revived])
                self.con.commit()
            with self.lock:
                self.state["removed"] = len(gone)
        except Exception as e:  # pragma: no cover
            with self.lock:
                self.state["error"] = repr(e)
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["finished"] = time.time()

    def _flush(self, batch: list[dict]):
        cols = list(batch[0].keys())
        sql = (f"INSERT INTO tracks ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
               f"ON CONFLICT(root_id, rel_path) DO UPDATE SET " +
               ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("root_id", "rel_path")))
        self.con.executemany(sql, [tuple(r[c] for c in cols) for r in batch])
        self.con.commit()


def add_root(con: sqlite3.Connection, path: str, export_path: str | None = None) -> int:
    p = str(Path(path).expanduser().resolve())
    con.execute("INSERT OR IGNORE INTO roots(path, export_path) VALUES (?, ?)", (p, export_path))
    if export_path is not None:
        con.execute("UPDATE roots SET export_path=? WHERE path=?", (export_path, p))
    con.commit()
    return con.execute("SELECT id FROM roots WHERE path=?", (p,)).fetchone()["id"]
