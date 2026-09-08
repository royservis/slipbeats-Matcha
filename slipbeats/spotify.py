"""Spotify Web API client (stdlib only) with PKCE user login.

Needs a Spotify developer app (free): https://developer.spotify.com/dashboard
  - Redirect URI must be exactly:  http://127.0.0.1:8765/spotify/callback
  - Copy the Client ID into Slipbeats → Spotify…
Tokens are stored in ~/.slipbeats/spotify.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
REDIRECT_PORT = 8765
REDIRECT_PATH = "/spotify/callback"


class SpotifyError(Exception):
    pass


def _ssl_context() -> ssl.SSLContext:
    """python.org builds of Python on macOS ship without root certificates wired into urllib,
    so HTTPS fails with CERTIFICATE_VERIFY_FAILED. Prefer certifi's bundle when available."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def _open(req, timeout=30):
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


class Spotify:
    def __init__(self, store: Path):
        self.store = store
        self.data: dict = {}
        self._verifier: str | None = None
        self._state: str | None = None
        self.load()

    # ----- persistence -----
    def load(self):
        try:
            self.data = json.loads(self.store.read_text())
        except Exception:
            self.data = {}

    def save(self):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(self.data, indent=2))

    @property
    def client_id(self) -> str:
        return self.data.get("client_id", "")

    def set_client_id(self, cid: str):
        cid = cid.strip()
        if cid != self.client_id:
            self.data = {"client_id": cid}   # new app → old tokens are useless
            self.save()

    @property
    def connected(self) -> bool:
        return bool(self.data.get("refresh_token"))

    def redirect_uri(self, port: int = REDIRECT_PORT) -> str:
        return f"http://127.0.0.1:{port}{REDIRECT_PATH}"

    # ----- PKCE login -----
    def login_url(self, port: int = REDIRECT_PORT) -> str:
        if not self.client_id:
            raise SpotifyError("Spotify Client ID is not set")
        self._verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).decode().rstrip("=")
        self._state = secrets.token_urlsafe(16)
        q = dict(client_id=self.client_id, response_type="code", redirect_uri=self.redirect_uri(port),
                 scope=SCOPES, code_challenge_method="S256", code_challenge=challenge, state=self._state)
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def handle_callback(self, code: str, state: str, port: int = REDIRECT_PORT):
        if not self._verifier or state != self._state:
            raise SpotifyError("Login session mismatch. Usually another copy of Slipbeats (e.g. the browser version from "
                               "run.command, or an old window) is running and received the login instead. Quit every "
                               "Slipbeats, open just one, and press Connect once.")
        body = dict(grant_type="authorization_code", code=code, redirect_uri=self.redirect_uri(port),
                    client_id=self.client_id, code_verifier=self._verifier)
        tok = self._post_form(TOKEN_URL, body)
        self.data.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                         expires_at=time.time() + int(tok.get("expires_in", 3600)) - 60)
        self._verifier = None
        me = self.api("GET", "/me")
        self.data["user"] = dict(id=me["id"], name=me.get("display_name") or me["id"])
        self.save()

    def logout(self):
        self.data = {"client_id": self.client_id}
        self.save()

    def _refresh(self):
        if not self.data.get("refresh_token"):
            raise SpotifyError("Not connected to Spotify")
        tok = self._post_form(TOKEN_URL, dict(grant_type="refresh_token", refresh_token=self.data["refresh_token"],
                                              client_id=self.client_id))
        self.data["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            self.data["refresh_token"] = tok["refresh_token"]
        self.data["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        self.save()

    def _post_form(self, url: str, form: dict) -> dict:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with _open(req) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise SpotifyError(f"Spotify auth failed ({e.code}): {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            raise SpotifyError(f"Could not reach Spotify: {e.reason}")

    # ----- API -----
    def api(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        if not self.data.get("access_token") or time.time() > self.data.get("expires_at", 0):
            self._refresh()
        url = (path if path.startswith("http") else API + path)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.data['access_token']}",
                                              "Content-Type": "application/json"})
        try:
            with _open(req) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._refresh()
                return self.api(method, path, params, body)
            raw = e.read().decode()[:400]
            try:
                msg = json.loads(raw).get("error", {}).get("message") or raw
            except Exception:
                msg = raw
            hint = ""
            if e.code == 403:
                low = msg.lower()
                if "scope" in low:
                    hint = "  → The login didn't include playlist permissions. Open Spotify… → Disconnect → Connect again and approve everything."
                elif "not registered" in low or "user" in low:
                    hint = "  → In development mode Spotify only allows accounts listed under the app's User Management (developer dashboard → your app → Settings → User Management). Add this account's email there."
                else:
                    hint = "  → Development-mode apps may need the account added under User Management in the developer dashboard."
            raise SpotifyError(f"Spotify API {e.code} on {method} {path}: {msg}{hint}")
        except urllib.error.URLError as e:
            raise SpotifyError(f"Network error talking to Spotify: {e.reason}")

    # ----- playlists -----
    @staticmethod
    def parse_url(url: str) -> tuple[str, str] | None:
        """Return (kind, id) for playlist/album URLs or URIs."""
        url = url.strip()
        m = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(playlist|album)/([A-Za-z0-9]+)", url)
        if m:
            return m.group(1), m.group(2)
        m = re.match(r"spotify:(playlist|album):([A-Za-z0-9]+)$", url)
        if m:
            return m.group(1), m.group(2)
        return None

    def fetch_tracks(self, url: str) -> dict:
        p = self.parse_url(url)
        if not p:
            raise SpotifyError("That doesn't look like a Spotify playlist or album link")
        kind, sid = p
        tracks = []
        if kind == "playlist":
            meta = self.api("GET", f"/playlists/{sid}", params={"fields": "name,owner(display_name),tracks.total"})
            name = meta.get("name", "Spotify playlist")
            page = self.api("GET", f"/playlists/{sid}/tracks",
                            params={"limit": 100, "fields": "next,items(track(name,artists(name),duration_ms,uri))"})
            while True:
                for it in page.get("items", []):
                    t = it.get("track") or {}
                    if t.get("name"):
                        tracks.append(dict(title=t["name"], artist=", ".join(a["name"] for a in t.get("artists", [])),
                                           duration_ms=t.get("duration_ms"), uri=t.get("uri")))
                if not page.get("next"):
                    break
                page = self.api("GET", page["next"])
        else:
            meta = self.api("GET", f"/albums/{sid}")
            name = meta.get("name", "Spotify album")
            page = self.api("GET", f"/albums/{sid}/tracks", params={"limit": 50})
            while True:
                for t in page.get("items", []):
                    tracks.append(dict(title=t["name"], artist=", ".join(a["name"] for a in t.get("artists", [])),
                                       duration_ms=t.get("duration_ms"), uri=t.get("uri")))
                if not page.get("next"):
                    break
                page = self.api("GET", page["next"])
        return dict(name=name, tracks=tracks)

    def find_track(self, artist: str, title: str) -> dict | None:
        q = f"track:{title}" + (f" artist:{artist.split(',')[0].strip()}" if artist else "")
        res = self.api("GET", "/search", params={"q": q, "type": "track", "limit": 3})
        items = (res.get("tracks") or {}).get("items") or []
        if not items and artist:   # loosen
            res = self.api("GET", "/search", params={"q": f"{artist} {title}", "type": "track", "limit": 3})
            items = (res.get("tracks") or {}).get("items") or []
        if not items:
            return None
        t = items[0]
        return dict(uri=t["uri"], title=t["name"], artist=", ".join(a["name"] for a in t.get("artists", [])))

    def create_playlist(self, name: str, requests: list[dict], description: str = "") -> dict:
        user = self.data.get("user") or {}
        uid = user.get("id") or self.api("GET", "/me")["id"]
        found, not_found = [], []
        for r in requests:
            uri = r.get("uri")
            hit = dict(uri=uri, title=r.get("title"), artist=r.get("artist")) if uri else self.find_track(r.get("artist", ""), r.get("title", ""))
            (found if hit else not_found).append(hit or r)
        payload = dict(name=name, public=False, description=(description or "Created by Slipbeats")[:300])
        try:
            pl = self.api("POST", f"/users/{uid}/playlists", body=payload)
        except SpotifyError as first:
            # some accounts get 403 on the /users/{id} form; the /me form is equivalent
            try:
                pl = self.api("POST", "/me/playlists", body=payload)
            except SpotifyError:
                raise first
        uris = [f["uri"] for f in found]
        for i in range(0, len(uris), 100):
            self.api("POST", f"/playlists/{pl['id']}/tracks", body=dict(uris=uris[i:i + 100]))
        return dict(url=(pl.get("external_urls") or {}).get("spotify"), id=pl["id"], name=name,
                    added=found, not_found=not_found)
