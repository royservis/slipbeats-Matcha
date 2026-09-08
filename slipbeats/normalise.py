"""Text normalisation and filename/tag parsing for DJ music files.

Handles the conventions seen in Roy's library:
  "Artist - Title (Version) (Dj Beats) 92_PN.mp3"
  "99 Bpm - Artist Feat X - Title (Intro Clean)_PN.mp3"
  underscores standing in for apostrophes ("Don_t stop")
  "_PN" Platinum Notes suffix, trailing BPM numbers, "Feat./Ft/Vs." variants
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".aif", ".aiff", ".wav", ".flac", ".ogg", ".alac"}

# Words that describe a *version* rather than the song itself.
VERSION_WORDS = {
    "remix", "mix", "edit", "extended", "radio", "club", "dub", "instrumental",
    "acapella", "intro", "outro", "clean", "dirty", "explicit", "original",
    "version", "rework", "bootleg", "mashup", "mash", "revibe", "redrum",
    "re-drum", "quick", "hit", "short", "long", "12", "7", "single", "album",
    "live", "acoustic", "remaster", "remastered", "vip", "flip", "refix",
    "transition", "throwback", "dj", "beats", "pn", "vocal", "chorus",
}

# Bracketed segments that are just source/label noise, not a version.
NOISE_BRACKETS = re.compile(
    r"\((?:dj beats|crate cuts|mastermix|for promotional[^)]*|promo(?:tional)?(?: only)?)\)",
    re.I,
)

FEAT_RE = re.compile(r"\s*[\(\[]?\b(?:feat\.?|ft\.?|featuring|with)\b\s*", re.I)
VS_RE = re.compile(r"\s+(?:vs\.?|versus|x)\s+", re.I)
LEADING_BPM_RE = re.compile(r"^\s*\d{2,3}\s*bpm\s*-\s*", re.I)
LEADING_TRACKNO_RE = re.compile(r"^\s*(?:\d{1,3}\s*[\.\-_]+\s*|0\d\s+)(?=\D)")  # "01 - ", "03. ", "07 Title" - but not "50 Cent"
TRAILING_BPM_RE = re.compile(r"[\s_\-]+(\d{2,3})(?:\s*bpm)?\s*$", re.I)
PN_SUFFIX_RE = re.compile(r"[_\s\-]+PN$", re.I)
BRACKET_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
BPM_IN_BRACKET_RE = re.compile(r"\b(\d{2,3})\s*bpm\b", re.I)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def basic_clean(s: str) -> str:
    """Lower-case, ASCII-fold, unify apostrophes/underscores, collapse whitespace."""
    s = strip_accents(s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    # underscores in this library are almost always apostrophes ("Don_t")
    s = re.sub(r"(\w)_(\w)", r"\1'\2", s)
    s = s.replace("_", " ")
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def display(s: str) -> str:
    """Human-facing text: underscores back to apostrophes, tidy spaces."""
    s = re.sub(r"(\w)_(\w)", r"\1'\2", s or "")
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    """Alphanumeric tokens with apostrophes and punctuation removed."""
    s = basic_clean(s)
    s = s.replace("'", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t]
    return toks


def key(s: str) -> str:
    """Comparison string: normalised tokens joined by single spaces."""
    toks = tokens(s)
    # drop a leading "the" - "The Real Thing" vs "Real Thing"
    if len(toks) > 1 and toks[0] == "the":
        toks = toks[1:]
    return " ".join(toks)


def split_artists(artist: str) -> list[str]:
    """'Kid Ink Feat Usher & Tinashe' -> ['kid ink', 'usher', 'tinashe'] (primary first)."""
    a = basic_clean(artist)
    parts = FEAT_RE.split(a)
    out: list[str] = []
    for p in parts:
        for q in VS_RE.split(p):
            for r in re.split(r"\s*,\s*|\s*/\s*|\s*;\s*", q):
                r = key(r)
                if r:
                    out.append(r)
    return out or [key(artist)]


@dataclass
class ParsedName:
    artist: str = ""
    title: str = ""
    version: str = ""          # human readable e.g. "Extended Mix, Clean"
    version_tokens: set = field(default_factory=set)
    bpm: int | None = None
    raw: str = ""


def _extract_version(title_part: str) -> tuple[str, str, set, int | None]:
    """Return (clean_title, version_text, version_tokens, bpm_from_brackets)."""
    bpm = None
    versions: list[str] = []
    vtoks: set = set()

    def _bracket(m):
        nonlocal bpm
        inner = m.group(1).strip()
        if not inner:
            return " "
        b = BPM_IN_BRACKET_RE.search(inner)
        if b:
            bpm = int(b.group(1))
            inner = BPM_IN_BRACKET_RE.sub("", inner).strip(" -")
        if inner:
            versions.append(inner)
            vtoks.update(tokens(inner))
        return " "

    t = NOISE_BRACKETS.sub(" ", title_part)
    t = BRACKET_RE.sub(_bracket, t)
    # unbracketed trailing markers: "... Clean 122", "... Intro Clean"
    toks = t.split()
    trailing: list[str] = []
    while toks and tokens(toks[-1]) and tokens(toks[-1])[0] in {"clean", "dirty", "intro", "outro", "extended", "explicit"}:
        trailing.insert(0, toks.pop())
    if trailing:
        versions.append(" ".join(trailing))
        vtoks.update(tokens(" ".join(trailing)))
    t = " ".join(toks)
    t = re.sub(r"\s+", " ", t).strip(" -")
    # drop generic noise tokens from the version set
    vtoks -= {"dj", "beats", "pn"}
    return t, ", ".join(versions), vtoks, bpm


def parse_filename(stem: str) -> ParsedName:
    """Parse a filename stem (no extension) into artist/title/version/bpm."""
    raw = stem
    s = stem.strip()
    s = PN_SUFFIX_RE.sub("", s)
    s = LEADING_BPM_RE.sub("", s)
    s = LEADING_TRACKNO_RE.sub("", s)
    bpm = None
    m = TRAILING_BPM_RE.search(s)
    if m and 60 <= int(m.group(1)) <= 200:
        bpm = int(m.group(1))
        s = s[: m.start()]
    parts = re.split(r"\s+-\s+", s, maxsplit=1)
    if len(parts) == 2:
        artist, title_part = parts
    else:
        artist, title_part = "", parts[0]
    title, version, vtoks, bbpm = _extract_version(title_part)
    if bpm is None:
        bpm = bbpm
    return ParsedName(artist=artist.strip(), title=title, version=version,
                      version_tokens=vtoks, bpm=bpm, raw=raw)


def parse_tag_title(title: str) -> tuple[str, str, set, int | None]:
    """Tag titles in this library carry the version and BPM too: 'Cuff It (Rubber Disco Edit) 120'."""
    s = PN_SUFFIX_RE.sub("", title or "")
    bpm = None
    m = TRAILING_BPM_RE.search(s)
    if m and 60 <= int(m.group(1)) <= 200:
        bpm = int(m.group(1))
        s = s[: m.start()]
    t, v, vt, bb = _extract_version(s)
    return t, v, vt, bpm if bpm is not None else bb


def parse_request_line(line: str) -> tuple[str, str, int | None]:
    """Parse one line of a pasted playlist into (artist, title, duration_ms).

    Accepts:  Artist - Title | Artist – Title | Title by Artist | Artist: Title
              tab-separated  | "Title" - Artist
    Returns ("", whole_line, None) when no separator is found.
    """
    s = line.strip().strip("•*-–—· \t")
    s = re.sub(r"^\d{1,3}[\.\)]\s+", "", s)     # numbered lists
    s = s.replace("–", "-").replace("—", "-")
    dur = None
    md = re.search(r"\s\(?(\d{1,2}):(\d{2})\)?\s*$", s)
    if md:
        dur = (int(md.group(1)) * 60 + int(md.group(2))) * 1000
        s = s[: md.start()]
    if "\t" in s:
        a, t = s.split("\t", 1)
        return a.strip(), t.strip(), dur
    m = re.match(r"^(.*?)\s+by\s+(.*)$", s, re.I)
    if m and " - " not in s:
        return m.group(2).strip(), m.group(1).strip(), dur
    for sep in (" - ", ": ", " | "):
        if sep in s:
            a, t = s.split(sep, 1)
            return a.strip(), t.strip(), dur
    return "", s.strip(), dur
