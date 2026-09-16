"""Spotify Web API client (stdlib only) with PKCE user login.

Needs a Spotify developer app (free): https://developer.spotify.com/dashboard
  - Redirect URI must be exactly:  http://127.0.0.1:8765/spotify/callback
  - Copy the Client ID into Slipbeats → Spotify…
Tokens are stored in ~/.slipbeats/spotify.json.

Development Mode rules since 9 March 2026: the account that owns the developer app must have
an active Spotify Premium subscription, one Development Mode app per developer, up to 25
accounts on the app's user list, and playlist CONTENTS are only readable for playlists the
logged-in account owns or collaborates on. Spotify.diagnose() probes all of this.
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
            bare = path.split("?")[0]
            if e.code == 403:
                if "scope" in msg.lower():
                    hint = ("\n  → The login is missing playlist permissions. Spotify… → Disconnect → Connect, "
                            "and approve everything on the Spotify page.")
                elif method == "GET" and bare.endswith("/items"):
                    hint = ("\n  → Since February 2026 a Development Mode app can only read the contents of "
                            "playlists the logged-in account owns or collaborates on.")
                else:
                    hint = ("\n  → Development Mode rules since 9 March 2026: the Spotify account that owns the "
                            "developer app must have an active Premium subscription, one Development Mode app per "
                            "developer, and every account using it must be on the app's user list (dashboard → "
                            "your app → Settings → User Management). Spotify… → Check setup runs all of this.")
            elif e.code == 404:
                hint = ("\n  → Either that playlist/album does not exist or is private, or Spotify removed this "
                        "endpoint in the February 2026 API change.")
            raise SpotifyError(f"Spotify API {e.code} on {method} {path}: {msg}{hint}")
        except urllib.error.URLError as e:
            raise SpotifyError(f"Network error talking to Spotify: {e.reason}")

    # ----- self-check -----
    def diagnose(self, playlist_url: str = "") -> dict:
        """Probe the Spotify setup and report, in plain English, what is refused and why."""
        checks: list[dict] = []
        refused = False

        def add(name, ok, detail):
            checks.append(dict(name=name, ok=bool(ok), detail=str(detail)))

        if not self.client_id:
            add("Client ID", False, "No Client ID saved. Copy it from your app's Settings page and paste it above.")
            return dict(checks=checks, verdict="Slipbeats has no Spotify Client ID yet.")
        add("Client ID", True, f"{self.client_id[:6]}… ({len(self.client_id)} characters)")

        if not self.connected:
            add("Logged in", False, "Not connected. Press Connect and approve Slipbeats in the browser tab.")
            return dict(checks=checks, verdict="Not connected to Spotify yet.")

        try:
            self._refresh()
            add("Token refresh", True, "Spotify issued a fresh access token.")
        except SpotifyError as e:
            add("Token refresh", False, e)
            return dict(checks=checks,
                        verdict="Spotify refused to issue a token at all. Disconnect, then Connect again. If that "
                                "still fails, the Client ID no longer matches a live app in your dashboard.")

        probes = (("Your account", lambda: self.api("GET", "/me")),
                  ("Your playlists", lambda: self.api("GET", "/me/playlists", params={"limit": 1})),
                  ("Search", lambda: self.api("GET", "/search", params={"q": "track:Respect artist:Aretha Franklin",
                                                                        "type": "track", "limit": 1})))
        for label, fn in probes:
            try:
                r = fn()
                if label == "Your account":
                    add(label, True, f"{r.get('display_name') or r.get('id')} ({r.get('id')})")
                else:
                    add(label, True, "Allowed.")
            except SpotifyError as e:
                refused = refused or " 403 " in f" {e} "
                add(label, False, e)

        if playlist_url.strip():
            parsed = self.parse_url(playlist_url)
            if not parsed:
                add("Playlist link", False, "That is not a Spotify playlist or album link.")
            elif parsed[0] == "playlist":
                sid = parsed[1]
                try:
                    meta = self.api("GET", f"/playlists/{sid}", params={"fields": "name,owner(display_name,id)"})
                    own = meta.get("owner") or {}
                    add("Playlist details", True,
                        f"“{meta.get('name')}” — owned by {own.get('display_name') or own.get('id') or 'someone else'}")
                    try:
                        self.api("GET", f"/playlists/{sid}/items", params={"limit": 1})
                        add("Playlist contents", True, "Readable — you own or collaborate on this one.")
                    except SpotifyError as e:
                        add("Playlist contents", False, e)
                except SpotifyError as e:
                    add("Playlist details", False, e)

        failed = [c["name"] for c in checks if not c["ok"]]
        if not failed:
            verdict = "Everything Slipbeats needs is working."
        elif refused:
            verdict = ("Spotify is refusing calls that every app needs, which since 9 March 2026 almost always means "
                       "the Development Mode rules. Check, in this order: (1) the Spotify account that owns the "
                       "developer app has an active Premium subscription; (2) you have only ONE app in the dashboard "
                       "— delete the spares, a second one is dead on arrival; (3) the account you log in with is "
                       "listed under the app → Settings → User Management, with the name and email exactly as "
                       "they appear on that Spotify account.")
        elif "Playlist contents" in failed:
            verdict = ("Your login is fine — Spotify just will not hand an app the contents of a playlist you do "
                       "not own. Saving or following it to your library does NOT make you the owner. In Spotify, "
                       "open the playlist, ⌘A to select every track, right-click → Add to playlist → New "
                       "playlist; paste that new link here instead. Or paste the song list as text.")
        else:
            verdict = "Some checks failed — see the detail above."
        return dict(checks=checks, verdict=verdict)

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
            meta = self.api("GET", f"/playlists/{sid}", params={"fields": "name,owner(display_name,id)"})
            name = meta.get("name", "Spotify playlist")
            own = meta.get("owner") or {}
            owner = own.get("display_name") or own.get("id") or "another account"
            # February 2026 API: /playlists/{id}/items (was /tracks), items[].item (was items[].track)
            fields = "next,items(item(name,artists(name),duration_ms,uri))"
            try:
                page = self.api("GET", f"/playlists/{sid}/items", params={"limit": 50, "fields": fields})
            except SpotifyError as e:
                if " 403 " in f" {e} ":
                    raise SpotifyError(
                        f'Spotify will not give Slipbeats the contents of \u201c{name}\u201d — {owner} owns it. '
                        "Since February 2026 an app may only read playlists you OWN or collaborate on. Saving or "
                        "following it to your library does not count — you are still not the owner. Open it in "
                        "Spotify, press ⌘A to select every track, right-click → Add to playlist → New playlist, "
                        "and paste THAT link here. Or paste the song list as text."
                    ) from None
                page = self.api("GET", f"/playlists/{sid}/items", params={"limit": 50})
            while True:
                for it in page.get("items", []):
                    t = it.get("item") or it.get("track") or {}
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
        found, not_found = [], []
        for r in requests:
            uri = r.get("uri")
            hit = dict(uri=uri, title=r.get("title"), artist=r.get("artist")) if uri else self.find_track(r.get("artist", ""), r.get("title", ""))
            (found if hit else not_found).append(hit or r)
        payload = dict(name=name, public=False, description=(description or "Created by Slipbeats")[:300])
        # February 2026 API: POST /me/playlists (POST /users/{id}/playlists was removed)
        pl = self.api("POST", "/me/playlists", body=payload)
        uris = [f["uri"] for f in found]
        for i in range(0, len(uris), 100):
            self.api("POST", f"/playlists/{pl['id']}/items", body=dict(uris=uris[i:i + 100]))
        return dict(url=(pl.get("external_urls") or {}).get("spotify"), id=pl["id"], name=name,
                    added=found, not_found=not_found)
