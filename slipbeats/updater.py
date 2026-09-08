"""Self-update from GitHub Releases.

Settings live in ~/.slipbeats/update.json: {"repo": "owner/slipbeats-matcha", "token": "github_pat_..."}
The token is only needed for a private repo: a fine-grained personal access token with
Repository access = that repo, Permissions → Contents: Read-only.

Flow: check() compares the latest release tag with VERSION; install() downloads the
Slipbeats-mac-arm64.zip asset, unpacks it with `ditto`, swaps the running .app bundle for
the new one, and relaunches.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import VERSION
from .spotify import SSL_CTX

ASSET_NAME = "Slipbeats-mac-arm64.zip"


class UpdateError(Exception):
    pass


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def app_bundle() -> Path | None:
    """Path to the running Slipbeats.app, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


class Updater:
    def __init__(self, store: Path):
        self.store = store
        self.data: dict = {}
        self.state = {"phase": "idle", "message": "", "progress": 0}
        self.latest: dict | None = None
        try:
            self.data = json.loads(store.read_text())
        except Exception:
            self.data = {}

    # ----- settings -----
    @property
    def repo(self) -> str:
        return (self.data.get("repo") or "").strip().strip("/")

    def configure(self, repo: str, token: str | None):
        self.data["repo"] = repo.strip().strip("/")
        if token is not None:
            self.data["token"] = token.strip()
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(self.data, indent=2))

    def status(self) -> dict:
        return dict(version=VERSION, repo=self.repo, has_token=bool(self.data.get("token")),
                    bundle=str(app_bundle() or ""), can_install=app_bundle() is not None,
                    latest=self.latest, last_check=self.data.get("last_check"), **self.state)

    # ----- GitHub -----
    def _headers(self, accept="application/vnd.github+json") -> dict:
        h = {"Accept": accept, "User-Agent": f"Slipbeats/{VERSION}", "X-GitHub-Api-Version": "2022-11-28"}
        if self.data.get("token"):
            h["Authorization"] = f"Bearer {self.data['token']}"
        return h

    def check(self) -> dict:
        if not self.repo:
            raise UpdateError("Set the GitHub repository first (owner/name)")
        req = urllib.request.Request(f"https://api.github.com/repos/{self.repo}/releases/latest", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                rel = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdateError("No release found. Either the repo name is wrong, the token can't see it (private repo needs a "
                                  "fine-grained token with Contents: read), or no release has been published yet.")
            if e.code == 401:
                raise UpdateError("GitHub rejected the token (401). Create a new fine-grained token with Contents: Read-only.")
            raise UpdateError(f"GitHub API {e.code}: {e.read().decode()[:200]}")
        except urllib.error.URLError as e:
            raise UpdateError(f"Could not reach GitHub: {e.reason}")
        asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET_NAME), None)
        latest_v = rel.get("tag_name", "").lstrip("v")
        self.latest = dict(version=latest_v, tag=rel.get("tag_name"), name=rel.get("name"), notes=rel.get("body") or "",
                           published=rel.get("published_at"), url=rel.get("html_url"),
                           asset_url=asset["url"] if asset else None, asset_size=asset["size"] if asset else 0,
                           newer=_vtuple(latest_v) > _vtuple(VERSION))
        self.data["last_check"] = time.time()
        try:
            self.store.write_text(json.dumps(self.data, indent=2))
        except Exception:
            pass
        return self.latest

    def _download(self, asset_url: str, dest: Path):
        """GitHub asset download: the API answers with a redirect to a signed S3 URL that must be
        fetched WITHOUT the Authorization header, so don't let urllib forward it."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=SSL_CTX))
        req = urllib.request.Request(asset_url, headers=self._headers("application/octet-stream"))
        try:
            resp = opener.open(req, timeout=30)
            final = resp  # 200 straight away (public repo may stream directly)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                final = urllib.request.urlopen(urllib.request.Request(e.headers["Location"], headers={"User-Agent": "Slipbeats"}),
                                               timeout=60, context=SSL_CTX)
            else:
                raise UpdateError(f"Download failed ({e.code})")
        total = int(final.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = final.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    self.state["progress"] = int(100 * done / total)
        if done < 1_000_000:
            raise UpdateError("Downloaded file is suspiciously small — is the release asset the right zip?")

    # ----- install -----
    def install(self):
        bundle = app_bundle()
        if not bundle:
            raise UpdateError("Running from source — use git pull instead of self-update.")
        if not self.latest or not self.latest.get("asset_url"):
            self.check()
        if not self.latest.get("asset_url"):
            raise UpdateError(f"The latest release has no {ASSET_NAME} attached yet — the build may still be running.")
        tmp = Path(tempfile.mkdtemp(prefix="slipbeats-update-"))
        zip_path = tmp / ASSET_NAME
        self.state.update(phase="downloading", message="Downloading…", progress=0)
        self._download(self.latest["asset_url"], zip_path)
        self.state.update(phase="installing", message="Unpacking…", progress=100)
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(tmp)], check=True)
        new_app = next(iter(tmp.glob("*.app")), None) or next(iter(tmp.glob("*/*.app")), None)
        if not new_app:
            raise UpdateError("The zip didn't contain an .app")
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new_app)], check=False)
        old = bundle.with_name(bundle.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        try:
            os.rename(bundle, old)
            shutil.move(str(new_app), str(bundle))
        except PermissionError:
            if old.exists() and not bundle.exists():
                os.rename(old, bundle)
            raise UpdateError(f"macOS wouldn't let me replace {bundle}. Move Slipbeats.app to your home folder or "
                              f"Applications (not a system folder) and try again, or replace it by hand.")
        self.state.update(phase="relaunching", message="Relaunching…")
        # Relaunch after we exit; tidy the old copy afterwards.
        script = f'sleep 1; open -n "{bundle}"; sleep 5; rm -rf "{old}"; rm -rf "{tmp}"'
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dict(ok=True, bundle=str(bundle))
