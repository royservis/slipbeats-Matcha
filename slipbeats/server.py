"""Local web server: serves the UI, JSON API, and audio with Range support."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .export import absolute_path, to_m3u8, to_rekordbox_xml
from .indexer import Scanner, add_root, connect
from .matcher import Library, parse_playlist_text, track_to_dict
from .normalise import key
from .spotify import Spotify, SpotifyError
from .updater import UpdateError, Updater
from . import VERSION

def _static_dir() -> Path:
    # PyInstaller one-folder/one-file bundles unpack data next to sys._MEIPASS
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "static"
    return Path(__file__).resolve().parent.parent / "static"


STATIC = _static_dir()
mimetypes.add_type("audio/aiff", ".aif")
mimetypes.add_type("audio/aiff", ".aiff")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/mp4", ".m4a")


class App:
    def __init__(self, db_path: str, export_dir: str | None = None):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = connect(db_path)
        self.db_lock = threading.Lock()
        self.scanner = Scanner(self.con)
        self.lib = Library(self.con)
        self.lib_loaded_at = time.time()
        self.export_dir = Path(export_dir).expanduser() if export_dir else Path.home() / "Slipbeats Playlists"
        self.spotify = Spotify(Path(db_path).parent / "spotify.json")
        self.updater = Updater(Path(db_path).parent / "update.json")
        self.port = 8765

    # --- helpers -----------------------------------------------------------
    def roots(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM roots")]

    def root_map(self) -> dict[int, sqlite3.Row]:
        return {r["id"]: r for r in self.con.execute("SELECT * FROM roots")}

    def ensure_lib_fresh(self):
        st = self.scanner.status()
        if st["finished"] and st["finished"] > self.lib_loaded_at:
            with self.db_lock:
                self.lib.reload()
                self.lib_loaded_at = time.time()

    def track_path(self, track_id: int) -> Path | None:
        row = self.con.execute("SELECT t.rel_path, r.path FROM tracks t JOIN roots r ON r.id=t.root_id WHERE t.id=?",
                               (track_id,)).fetchone()
        if not row:
            return None
        return Path(row["path"]) / row["rel_path"]

    def export_items(self, ids: list[int]) -> list[dict]:
        rm = self.root_map()
        items = []
        for tid in ids:
            r = self.con.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
            if not r:
                continue
            root = rm[r["root_id"]]
            items.append(dict(
                id=r["id"],
                path=absolute_path(root["path"], root["export_path"], r["rel_path"]),
                artist=r["artist"], title=(r["title"] + (f" ({r['version']})" if r["version"] else "")),
                album=r["album"], genre=r["genre"], year=r["year"], bpm=r["bpm"], key=r["musical_key"],
                comment=r["comment"], duration_ms=r["duration_ms"], bitrate=r["bitrate"], size=r["size"],
            ))
        return items

    def remember_choice(self, request_artist: str, request_title: str, track_id: int):
        rk = f"{key(request_artist)}|{key(request_title)}"
        self.con.execute("INSERT INTO preferences(request_key, track_id, count) VALUES (?,?,1) "
                         "ON CONFLICT(request_key) DO UPDATE SET track_id=excluded.track_id, count=count+1",
                         (rk, track_id))
        self.con.commit()

    def preferred(self, request_artist: str, request_title: str) -> int | None:
        rk = f"{key(request_artist)}|{key(request_title)}"
        r = self.con.execute("SELECT track_id FROM preferences WHERE request_key=?", (rk,)).fetchone()
        return r["track_id"] if r else None


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter
            if "/api/audio/" not in (args[0] if args else ""):
                super().log_message(fmt, *args)

        # --- plumbing ---
        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode())

        def _file(self, path: Path, ctype: str | None = None):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        # --- GET ---
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p == "/" or p == "/index.html":
                    return self._file(STATIC / "index.html", "text/html; charset=utf-8")
                if p == "/api/status":
                    app.ensure_lib_fresh()
                    n = app.con.execute("SELECT COUNT(*) FROM tracks WHERE missing=0").fetchone()[0]
                    return self._json(dict(scan=app.scanner.status(), tracks=n, roots=app.roots(),
                                           export_dir=str(app.export_dir), version=VERSION))
                if p == "/api/update/status":
                    return self._json(app.updater.status())
                if p == "/api/search":
                    app.ensure_lib_fresh()
                    return self._json(dict(results=app.lib.search(q.get("q", ""), int(q.get("limit", 40)))))
                if p == "/api/playlists":
                    rows = app.con.execute("SELECT id, name, created, updated, data FROM playlists ORDER BY updated DESC").fetchall()
                    out = []
                    for r in rows:
                        d = json.loads(r["data"] or "{}")
                        out.append(dict(id=r["id"], name=r["name"], created=r["created"], updated=r["updated"],
                                        tracks=len(d.get("tracks") or []), requests=len(d.get("requests") or [])))
                    return self._json(dict(playlists=out))
                if p == "/api/playlist":
                    r = app.con.execute("SELECT id, name, created, updated, data FROM playlists WHERE id=?", (int(q["id"]),)).fetchone()
                    if not r:
                        return self._json(dict(error="not found"), 404)
                    return self._json(dict(id=r["id"], name=r["name"], updated=r["updated"], data=json.loads(r["data"] or "{}")))
                if p == "/api/spotify/status":
                    sp = app.spotify
                    return self._json(dict(configured=bool(sp.client_id), connected=sp.connected,
                                           user=sp.data.get("user"), redirect_uri=sp.redirect_uri(app.port),
                                           client_id=sp.client_id))
                if p == "/spotify/callback":
                    try:
                        if q.get("error"):
                            raise SpotifyError(q["error"])
                        app.spotify.handle_callback(q.get("code", ""), q.get("state", ""), app.port)
                        html = "<h2 style='font-family:sans-serif'>Spotify connected. You can close this tab and go back to Slipbeats.</h2>"
                    except Exception as e:
                        html = f"<h2 style='font-family:sans-serif;color:#b00'>Spotify login failed</h2><pre>{str(e)}</pre>"
                    body = html.encode()
                    self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                    return
                m = re.match(r"^/api/audio/(\d+)$", p)
                if m:
                    return self._audio(int(m.group(1)))
                m = re.match(r"^/api/track/(\d+)$", p)
                if m:
                    t = app.lib.by_id.get(int(m.group(1)))
                    return self._json(track_to_dict(t) if t else {}, 200 if t else 404)
                if p.startswith("/static/"):
                    f = (STATIC / p[len("/static/"):]).resolve()
                    if STATIC in f.parents and f.is_file():
                        return self._file(f)
                self._json(dict(error="not found"), 404)
            except Exception as e:
                self._json(dict(error=repr(e)), 500)

        def _audio(self, track_id: int):
            path = app.track_path(track_id)
            if not path or not path.is_file():
                return self._json(dict(error="file not found"), 404)
            size = path.stat().st_size
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                    if not m.group(1) and m.group(2):
                        start = max(0, size - int(m.group(2)))
                        end = size - 1
                    status = 206
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

        # --- POST ---
        def do_POST(self):
            p = urlparse(self.path).path
            try:
                body = self._body()
                if p == "/api/roots":
                    path = body.get("path", "").strip()
                    if not path or not Path(path).expanduser().is_dir():
                        return self._json(dict(error="folder not found"), 400)
                    add_root(app.con, path, body.get("export_path") or None)
                    return self._json(dict(roots=app.roots()))
                if p == "/api/roots/delete":
                    rid = int(body["id"])
                    app.con.execute("DELETE FROM tracks WHERE root_id=?", (rid,))
                    app.con.execute("DELETE FROM roots WHERE id=?", (rid,))
                    app.con.commit()
                    app.lib.reload()
                    return self._json(dict(roots=app.roots()))
                if p == "/api/scan":
                    started = app.scanner.start(full=bool(body.get("full")))
                    return self._json(dict(started=started, scan=app.scanner.status()))
                if p == "/api/match":
                    app.ensure_lib_fresh()
                    reqs = parse_playlist_text(body.get("text", ""))
                    if body.get("swap"):   # list was written "Title - Artist"
                        for r in reqs:
                            if r["artist"]:
                                r["artist"], r["title"] = r["title"], r["artist"]
                    out = []
                    for r in reqs:
                        res = app.lib.match(r["artist"], r["title"], r.get("duration_ms"), limit=int(body.get("limit", 10)))
                        res["raw"] = r["raw"]
                        res["request"]["type"] = r.get("type", "")
                        res["request"]["comment"] = r.get("comment", "")
                        res["preferred_track_id"] = app.preferred(r["artist"], r["title"])
                        out.append(res)
                    return self._json(dict(results=out))
                if p == "/api/match_one":
                    app.ensure_lib_fresh()
                    res = app.lib.match(body.get("artist", ""), body.get("title", ""), body.get("duration_ms"),
                                        limit=int(body.get("limit", 10)))
                    return self._json(res)
                if p == "/api/choose":
                    app.remember_choice(body.get("artist", ""), body.get("title", ""), int(body["track_id"]))
                    return self._json(dict(ok=True))
                if p == "/api/playlists":
                    now = time.time()
                    data = json.dumps(body.get("data", {}))
                    if body.get("id"):
                        app.con.execute("UPDATE playlists SET name=?, updated=?, data=? WHERE id=?",
                                        (body["name"], now, data, int(body["id"])))
                        pid = int(body["id"])
                    else:
                        cur = app.con.execute("INSERT INTO playlists(name, created, updated, data) VALUES (?,?,?,?)",
                                              (body["name"], now, now, data))
                        pid = cur.lastrowid
                    app.con.commit()
                    return self._json(dict(id=pid))
                if p == "/api/playlists/delete":
                    app.con.execute("DELETE FROM playlists WHERE id=?", (int(body["id"]),))
                    app.con.commit()
                    return self._json(dict(ok=True))
                if p == "/api/export":
                    name = re.sub(r"[^\w\s\-\.&()']+", "", body.get("name") or "Slipbeats playlist").strip() or "Slipbeats playlist"
                    ids = [int(i) for i in body.get("track_ids", [])]
                    items = app.export_items(ids)
                    path_by_id = {it["id"]: it["path"] for it in items}
                    groups_in = body.get("groups") or []
                    groups = [dict(name=g["name"], paths=[path_by_id[int(t)] for t in g.get("track_ids", []) if int(t) in path_by_id])
                              for g in groups_in if g.get("track_ids")]
                    fmt = body.get("format", "both")
                    app.export_dir.mkdir(parents=True, exist_ok=True)
                    written = []
                    safe = lambda x: re.sub(r"[^\w\s\-\.&()']+", "", x).strip()
                    if fmt in ("m3u8", "both"):
                        f = app.export_dir / f"{name}.m3u8"
                        f.write_text(to_m3u8(name, items), encoding="utf-8")
                        written.append(str(f))
                        by_path = {it["path"]: it for it in items}
                        for g in groups:
                            gf = app.export_dir / f"{name} - {safe(g['name'])}.m3u8"
                            gf.write_text(to_m3u8(f"{name} - {g['name']}", [by_path[p] for p in g["paths"] if p in by_path]), encoding="utf-8")
                            written.append(str(gf))
                    if fmt in ("xml", "both"):
                        f = app.export_dir / f"{name}.xml"
                        f.write_text(to_rekordbox_xml(name, items, groups or None), encoding="utf-8")
                        written.append(str(f))
                    return self._json(dict(written=written, count=len(items), groups=[g["name"] for g in groups]))
                if p == "/api/pick_folder":
                    # native macOS chooser via AppleScript; returns {"path": ...} or {"path": null} if cancelled
                    import subprocess
                    if os.uname().sysname != "Darwin":
                        return self._json(dict(path=None, unsupported=True))
                    script = ('POSIX path of (choose folder with prompt "Choose a music folder for Slipbeats to index")')
                    try:
                        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
                        path = r.stdout.strip().rstrip("/") if r.returncode == 0 else None
                    except Exception:
                        path = None
                    # bring the browser back to the front afterwards
                    return self._json(dict(path=path))
                if p == "/api/listdir":
                    # fallback folder browser: subfolders of a given path
                    base = Path(body.get("path") or Path.home()).expanduser()
                    if not base.is_dir():
                        base = Path.home()
                    try:
                        subs = sorted([d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")],
                                      key=str.lower)
                    except PermissionError:
                        subs = []
                    volumes = [str(v) for v in Path("/Volumes").iterdir()] if Path("/Volumes").is_dir() else []
                    n_audio = sum(1 for f in base.iterdir() if f.is_file() and f.suffix.lower() in
                                  {".mp3", ".m4a", ".aif", ".aiff", ".wav", ".flac"}) if base.is_dir() else 0
                    return self._json(dict(path=str(base), parent=str(base.parent) if base.parent != base else None,
                                           dirs=subs, volumes=volumes, audio_files_here=n_audio))
                if p == "/api/update/config":
                    app.updater.configure(body.get("repo", ""), body.get("token"))
                    return self._json(app.updater.status())
                if p == "/api/update/check":
                    latest = app.updater.check()
                    return self._json(dict(latest=latest, version=VERSION))
                if p == "/api/update/install":
                    up = app.updater
                    if up.state["phase"] not in ("idle", "error"):
                        return self._json(dict(started=False, **up.status()))
                    def run():
                        try:
                            up.install()
                            time.sleep(1.0)
                            os._exit(0)   # the relaunch script takes over
                        except Exception as e:
                            up.state.update(phase="error", message=str(e))
                    threading.Thread(target=run, daemon=True).start()
                    return self._json(dict(started=True))
                if p == "/api/spotify/config":
                    app.spotify.set_client_id(body.get("client_id", ""))
                    return self._json(dict(ok=True))
                if p == "/api/spotify/login":
                    url = app.spotify.login_url(app.port)
                    webbrowser.open(url)
                    return self._json(dict(url=url))
                if p == "/api/spotify/logout":
                    app.spotify.logout()
                    return self._json(dict(ok=True))
                if p == "/api/spotify/playlist":
                    res = app.spotify.fetch_tracks(body.get("url", ""))
                    lines = []
                    for t in res["tracks"]:
                        d = t.get("duration_ms")
                        dd = f" ({d // 60000}:{(d // 1000) % 60:02d})" if d else ""
                        lines.append(f"{t['artist']} - {t['title']}{dd}")
                    res["text"] = "\n".join(lines)
                    return self._json(res)
                if p == "/api/spotify/create":
                    res = app.spotify.create_playlist(body.get("name") or "Slipbeats missing tracks",
                                                      body.get("tracks", []), body.get("description", ""))
                    return self._json(res)
                if p == "/api/save_text":
                    name = re.sub(r"[^\w\s\-\.&()']+", "", body.get("name") or "list").strip() or "list"
                    app.export_dir.mkdir(parents=True, exist_ok=True)
                    f = app.export_dir / f"{name}.txt"
                    f.write_text(body.get("text", ""), encoding="utf-8")
                    return self._json(dict(written=str(f)))
                if p == "/api/reveal":
                    # macOS: show the file in Finder
                    path = app.track_path(int(body["track_id"]))
                    if path and path.exists() and os.uname().sysname == "Darwin":
                        os.system(f"open -R {json.dumps(str(path))}")
                    return self._json(dict(ok=True))
                self._json(dict(error="not found"), 404)
            except (SpotifyError, UpdateError) as e:
                self._json(dict(error=str(e)), 400)
            except Exception as e:
                self._json(dict(error=repr(e)), 500)

    return H


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          export_dir: str | None = None):
    app = App(db_path, export_dir)
    app.port = port
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"Slipbeats running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
