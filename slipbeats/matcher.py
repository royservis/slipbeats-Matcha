"""Match requested songs against the indexed library."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .normalise import _extract_version, key, split_artists, tokens

try:
    from rapidfuzz import fuzz, process
    HAVE_RF = True
except Exception:  # pragma: no cover
    HAVE_RF = False
    import difflib

    class _F:
        @staticmethod
        def token_set_ratio(a, b, **kw):
            sa, sb = set(a.split()), set(b.split())
            if not sa or not sb:
                return 0.0
            inter = " ".join(sorted(sa & sb))
            da = " ".join(sorted(sa - sb)); db = " ".join(sorted(sb - sa))
            s1 = f"{inter} {da}".strip(); s2 = f"{inter} {db}".strip()
            return 100 * max(difflib.SequenceMatcher(None, inter, s1).ratio(),
                             difflib.SequenceMatcher(None, inter, s2).ratio(),
                             difflib.SequenceMatcher(None, s1, s2).ratio())

        @staticmethod
        def ratio(a, b, **kw):
            return 100 * difflib.SequenceMatcher(None, a, b).ratio()
    fuzz = _F()

TIERS = [(93, "exact"), (80, "likely"), (60, "possible")]
# versions you rarely want unless you asked for them
UNWANTED_UNLESS_ASKED = {"instrumental", "acapella", "dub"}


@dataclass
class Track:
    id: int
    artist: str
    title: str
    artist_key: str
    title_key: str
    primary_artist_key: str
    featured_keys: list
    version: str
    version_tokens: set
    bpm: int | None
    musical_key: str | None
    energy: int | None
    genre: str | None
    year: str | None
    duration_ms: int | None
    bitrate: int | None
    filename: str
    rel_path: str
    root_id: int
    all_artist_keys: list = field(default_factory=list)


class Library:
    """In-memory view of the tracks table for fast fuzzy matching."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.tracks: list[Track] = []
        self.by_id: dict[int, Track] = {}
        self.title_keys: list[str] = []
        self.artist_keys: list[str] = []
        self.combo_keys: list[str] = []
        self.reload()

    def reload(self):
        rows = self.con.execute("SELECT * FROM tracks WHERE missing=0").fetchall()
        self.tracks = []
        for r in rows:
            feats = json.loads(r["featured_keys"] or "[]")
            t = Track(
                id=r["id"], artist=r["artist"] or "", title=r["title"] or "",
                artist_key=r["artist_key"] or "", title_key=r["title_key"] or "",
                primary_artist_key=r["primary_artist_key"] or "", featured_keys=feats,
                version=r["version"] or "", version_tokens=set(json.loads(r["version_tokens"] or "[]")),
                bpm=r["bpm"], musical_key=r["musical_key"], energy=r["energy"], genre=r["genre"],
                year=r["year"], duration_ms=r["duration_ms"], bitrate=r["bitrate"],
                filename=r["filename"], rel_path=r["rel_path"], root_id=r["root_id"],
            )
            t.all_artist_keys = [t.primary_artist_key] + feats if t.primary_artist_key else feats
            self.tracks.append(t)
        self.by_id = {t.id: t for t in self.tracks}
        self.title_keys = [t.title_key for t in self.tracks]
        self.artist_keys = [t.artist_key for t in self.tracks]
        self.combo_keys = [f"{t.artist_key} {t.title_key}".strip() for t in self.tracks]

    # ------------------------------------------------------------------
    def _candidates(self, q: str, keys: list[str], limit: int, cutoff: int) -> list[int]:
        if not q:
            return []
        if HAVE_RF:
            res = process.extract(q, keys, scorer=fuzz.token_set_ratio, limit=limit, score_cutoff=cutoff)
            return [idx for _, _, idx in res]
        scored = [(fuzz.token_set_ratio(q, k), i) for i, k in enumerate(keys) if k]
        scored.sort(reverse=True)
        return [i for s, i in scored[:limit] if s >= cutoff]

    @staticmethod
    def _title_score(tq: str, tk: str) -> float:
        """token_set_ratio treats a subset as a perfect match ("one more time" vs
        "do that to me one more time"), so penalise unexplained extra words on either side."""
        if not tq or not tk:
            return 0.0
        s = float(fuzz.token_set_ratio(tq, tk))
        a, b = set(tq.split()), set(tk.split())
        extra_track = len(b - a)      # words in the file's title the request never mentioned
        extra_req = len(a - b)        # words in the request the file lacks (subtitles etc.)
        s -= min(30, 7 * extra_track) + min(20, 4 * extra_req)
        # a partial (character-level) match still deserves some credit
        s = max(s, 0.6 * fuzz.ratio(tq, tk))
        return max(0.0, s)

    def _artist_score(self, req_artists: list[str], t: Track) -> float:
        if not req_artists:
            return 0.0
        best = 0.0
        for ra in req_artists:
            for ta in t.all_artist_keys or [t.artist_key]:
                if not ta:
                    continue
                s = fuzz.token_set_ratio(ra, ta)
                # exact primary-artist agreement is worth a little extra
                if ra == t.primary_artist_key:
                    s = max(s, 100)
                best = max(best, s)
        # if the only overlap is on a featured artist, dampen slightly
        if best >= 90 and req_artists[0] != t.primary_artist_key and \
                fuzz.token_set_ratio(req_artists[0], t.primary_artist_key) < 70:
            best -= 8
        return best

    def match(self, artist: str, title: str, duration_ms: int | None = None,
              limit: int = 12) -> dict:
        title_clean, req_version, req_vtoks, _ = _extract_version(title or "")
        tq = key(title_clean) or key(title)
        req_artists = split_artists(artist) if artist else []
        aq = req_artists[0] if req_artists else ""

        idxs: set[int] = set()
        idxs.update(self._candidates(tq, self.title_keys, 60, 55))
        if aq:
            # artist/title swapped in the file, or request pasted the wrong way round
            idxs.update(self._candidates(aq, self.title_keys, 15, 85))
            idxs.update(self._candidates(f"{aq} {tq}", self.combo_keys, 40, 70))
        else:
            idxs.update(self._candidates(tq, self.combo_keys, 40, 70))

        results = []
        for i in idxs:
            t = self.tracks[i]
            ts = self._title_score(tq, t.title_key)
            if aq:
                as_ = self._artist_score(req_artists, t)
                # swapped case
                ts_sw = self._title_score(tq, t.artist_key)
                as_sw = self._title_score(aq, t.title_key)
                straight = 0.62 * ts + 0.38 * as_
                swapped = 0.62 * ts_sw + 0.38 * as_sw - 3
                score = max(straight, swapped)
                swapped_hit = swapped > straight
            else:
                score = ts * 0.97  # no artist: cap a touch below "exact"
                as_ = 0
                swapped_hit = False

            notes = []
            # version handling
            if req_vtoks:
                overlap = len(req_vtoks & t.version_tokens)
                if overlap:
                    score += min(6, 3 * overlap)
                    notes.append("version match")
                elif t.version_tokens - {"clean", "dirty", "intro", "outro"}:
                    score -= 4
            unwanted = UNWANTED_UNLESS_ASKED & t.version_tokens
            if unwanted and not (req_vtoks & unwanted):
                score -= 12
                notes.append(f"{'/'.join(sorted(unwanted))} version")

            dur_diff = None
            if duration_ms and t.duration_ms:
                dur_diff = (t.duration_ms - duration_ms) / 1000
                if abs(dur_diff) <= 3:
                    score += 4
                    notes.append("same length")
                elif abs(dur_diff) > 90:
                    score -= 2
            score = max(0, min(100, score))
            if score < 45:
                continue
            results.append(dict(
                track=track_to_dict(t), score=round(score, 1), tier=tier_for(score),
                title_score=round(ts), artist_score=round(as_), swapped=swapped_hit,
                duration_diff_s=None if dur_diff is None else round(dur_diff),
                notes=notes,
            ))
        results.sort(key=lambda r: (-r["score"], r["track"]["filename"]))
        results = results[:limit]
        return dict(
            request=dict(artist=artist, title=title, duration_ms=duration_ms,
                         title_key=tq, artist_key=aq, version=req_version),
            candidates=results,
            status=(results[0]["tier"] if results and results[0]["tier"] != "weak" else "not_found"),
        )

    def search(self, q: str, limit: int = 40) -> list[dict]:
        qk = key(q)
        if not qk:
            return []
        idxs = self._candidates(qk, self.combo_keys, limit * 2, 40)
        scored = []
        for i in idxs:
            t = self.tracks[i]
            s = fuzz.token_set_ratio(qk, self.combo_keys[i])
            if qk in self.combo_keys[i]:
                s = max(s, 95)
            scored.append((s, t))
        scored.sort(key=lambda x: (-x[0], x[1].filename))
        return [dict(track=track_to_dict(t), score=s) for s, t in scored[:limit]]


def tier_for(score: float) -> str:
    for th, name in TIERS:
        if score >= th:
            return name
    return "weak"


def track_to_dict(t: Track) -> dict:
    return dict(
        id=t.id, artist=t.artist, title=t.title, version=t.version, bpm=t.bpm,
        key=t.musical_key, energy=t.energy, genre=t.genre, year=t.year,
        duration_ms=t.duration_ms, bitrate=t.bitrate, filename=t.filename,
        rel_path=t.rel_path, root_id=t.root_id,
    )


def parse_playlist_text(text: str) -> list[dict]:
    """Turn pasted text or CSV (incl. Exportify / Spotify exports) into request dicts."""
    import csv
    import io
    from .normalise import parse_request_line

    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    head = lines[0].lower()
    if ("," in head or "\t" in head) and any(h in head for h in ("track name", "title", "artist")):
        dialect = "excel-tab" if "\t" in head else "excel"
        reader = csv.DictReader(io.StringIO("\n".join(lines)), dialect=dialect)
        out = []
        for row in reader:
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            title = low.get("track name") or low.get("title") or low.get("song") or low.get("name") or ""
            artist = (low.get("artist name(s)") or low.get("artist name") or low.get("artist")
                      or low.get("artists") or "")
            dur = low.get("duration (ms)") or low.get("duration_ms") or low.get("duration") or ""
            dur_ms = None
            if dur:
                try:
                    dur_ms = int(float(dur)) if dur.isdigit() or "." in dur else None
                except ValueError:
                    dur_ms = None
                if dur_ms is None and ":" in dur:
                    m, s = dur.split(":")[:2]
                    dur_ms = (int(m) * 60 + int(s)) * 1000
            if title:
                out.append(dict(artist=artist, title=title, duration_ms=dur_ms, raw=f"{artist} - {title}"))
        if out:
            return out
    out = []
    for l in lines:
        a, t, d = parse_request_line(l)
        if t:
            out.append(dict(artist=a, title=t, duration_ms=d, raw=l.strip()))
    return out
