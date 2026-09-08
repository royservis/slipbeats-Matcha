"""Command line: index a folder, match a playlist, export. Also used for testing."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .indexer import Scanner, add_root, connect
from .matcher import Library, parse_playlist_text

DEFAULT_DB = Path.home() / ".slipbeats" / "library.db"


def cmd_index(args):
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    con = connect(args.db)
    for r in args.root:
        add_root(con, r, args.export_root)
    sc = Scanner(con)
    sc.start(full=args.full)
    last = -1
    while True:
        s = sc.status()
        if s["done"] != last:
            print(f"\r{s['done']}/{s['total']}  +{s['added']} ~{s['updated']}  {s['current'][:60]:<60}", end="", flush=True)
            last = s["done"]
        if not s["running"]:
            break
        time.sleep(0.2)
    s = sc.status()
    print(f"\nDone in {s['finished'] - s['started']:.1f}s: {s['added']} added, {s['updated']} updated, "
          f"{s['removed']} missing." + (f"  ERROR: {s['error']}" if s['error'] else ""))
    n = con.execute("SELECT COUNT(*) FROM tracks WHERE missing=0").fetchone()[0]
    print(f"Library now holds {n} tracks.")


def cmd_match(args):
    con = connect(args.db)
    lib = Library(con)
    text = Path(args.playlist).read_text() if args.playlist != "-" else sys.stdin.read()
    reqs = parse_playlist_text(text)
    found = 0
    for r in reqs:
        res = lib.match(r["artist"], r["title"], r.get("duration_ms"), limit=args.limit)
        print(f"\n>> {r['raw']}   [{res['status']}]")
        if res["candidates"]:
            found += 1
        for c in res["candidates"]:
            t = c["track"]
            d = f"{(t['duration_ms'] or 0)//60000}:{((t['duration_ms'] or 0)//1000)%60:02d}"
            flags = " ".join(c["notes"] + (["SWAPPED"] if c["swapped"] else []))
            print(f"   {c['score']:5.1f} {c['tier']:<8} {t['filename'][:80]}  {t['bpm'] or '':>3} {t['key'] or '':<3} {d}  {flags}")
    print(f"\n{found}/{len(reqs)} requests have at least one candidate.")


def cmd_search(args):
    con = connect(args.db)
    lib = Library(con)
    for r in lib.search(" ".join(args.query), limit=args.limit):
        print(f"{r['score']:5.1f}  {r['track']['filename']}")


def cmd_serve(args):
    from .server import serve
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    serve(args.db, port=args.port, open_browser=not args.no_browser, export_dir=args.export_dir)


def main(argv=None):
    p = argparse.ArgumentParser(prog="slipbeats")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("index", help="scan folder(s) into the library index")
    a.add_argument("root", nargs="+")
    a.add_argument("--export-root", help="path rekordbox should see for this root, if different")
    a.add_argument("--full", action="store_true", help="re-read tags for every file")
    a.set_defaults(fn=cmd_index)
    m = sub.add_parser("match", help="match a playlist text file (or - for stdin)")
    m.add_argument("playlist")
    m.add_argument("--limit", type=int, default=6)
    m.set_defaults(fn=cmd_match)
    s = sub.add_parser("search")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)
    w = sub.add_parser("serve", help="start the web app")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true")
    w.add_argument("--export-dir", help="where playlists are written (default ~/Slipbeats Playlists)")
    w.set_defaults(fn=cmd_serve)
    d = sub.add_parser("app", help="open Slipbeats in a native window (needs pywebview)")
    d.set_defaults(fn=lambda a: __import__("slipbeats.app", fromlist=["main"]).main())
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
