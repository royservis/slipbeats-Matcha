# Slipbeats

Match a requested set list against your local DJ library, audition every version you own, pick the ones you want, and export the playlist to rekordbox. Runs locally on your Mac; nothing leaves the machine and no music files are moved or copied.

## Build the Mac app (for the DJ Mac mini)

1. Copy this whole folder to the Mac mini.
2. Double-click **`build-app.command`**. It needs Python 3.10 or newer once — if it can't find one, it tells you; install it from python.org (universal2 installer, ~40 MB) and run it again. The script then creates its own build environment, fetches the four packages it needs, makes the icon and builds `dist/Slipbeats.app` — two or three minutes.
3. Drag `Slipbeats.app` to Applications. First launch: right-click → Open (it's signed locally, not through Apple, so Gatekeeper asks once).

`Slipbeats.app` is self-contained: no Python or Terminal needed after the build. It opens in its own window, uses the standard macOS folder chooser, and keeps its index in `~/.slipbeats/` and playlists in `~/Slipbeats Playlists/`. Rebuild the same way after any code change.

## Run it without building (browser version)

Needs Python 3.10+ (macOS ships it; `xcode-select --install` if `python3` is missing).

1. Double-click `run.command` — or in Terminal: `cd` into this folder and `python3 -m slipbeats serve`.
2. Your browser opens at http://127.0.0.1:8765/.
3. First time: **Folders…** → add your music folder → **Add & scan now**. 47k tracks takes a few minutes; progress is shown in the header. Later scans only touch new/changed files.

The index lives in `~/.slipbeats/library.db`. Exports go to `~/Slipbeats Playlists/` (change with `--export-dir`).

## Use it

- **Requested songs** (left): paste one song per line — `Artist - Title`, `Title by Artist`, tab-separated, or a CSV exported from Spotify with [Exportify](https://exportify.net) (Track Name / Artist Name(s) / Duration columns are used). Press **Match against library**.
- Each song is marked **exact / likely / possible / not found**. Click a song to see every version in your library (middle column). Weak matches are folded away at the bottom.
- **Versions** (middle): ▶ to audition, **0:45 / 1:30 / mid** buttons jump into the track so you can identify the version without sitting through the intro. **Add** puts it in the playlist — you can add several versions of the same song. The search box searches the whole library when the matcher misses.
- Keyboard: ↑↓ move between versions, ←→ between songs, **space** play/pause, **enter** add/remove, **j / l** skip back/forward 10 s.
- **Playlist** (right): drag to reorder, ✕ to remove, **Save** keeps it inside Slipbeats. Slipbeats remembers which version you picked for a song and floats it to the top next time ("your usual pick").

## Getting it into rekordbox

**Export M3U8** → in rekordbox: *File → Import → Import Playlist*, choose the `.m3u8`. Simplest route.

**Export for rekordbox** (XML) → in rekordbox: *Preferences → Advanced → Database → rekordbox xml* → point it at the `.xml` (enable the *rekordbox xml* node under *Preferences → View → Layout* if it is hidden). It appears in the tree view; right-click the playlist → *Import Playlist*. Carries order and basic track info.

Both reference your existing files by absolute path. Slipbeats never writes into the rekordbox database itself.

## Spotify (optional, one-off setup)

Spotify requires every integration to be registered, even a private one, and since 2026 a personal "development mode" app needs a Premium account and allows up to five users. That's fine for you.

1. developer.spotify.com/dashboard → log in → **Create app**. Any name. Redirect URI exactly `http://127.0.0.1:8765/spotify/callback`. Tick **Web API**. Save.
2. In the app's Settings copy the **Client ID**.
3. In Slipbeats: **Spotify…** → paste the Client ID → **Connect** → approve in the browser tab.

After that: paste any `open.spotify.com/playlist/…` or album link into the box above the text area and it fills the list (with durations, which sharpens the matching), and **Missing songs → Spotify playlist** creates a private playlist of the songs you don't have — handy for a record-pool shopping list.

Tokens live in `~/.slipbeats/spotify.json`. If the port 8765 is in use when the app starts it picks another port and the Spotify login will fail — quit whatever is using 8765 and relaunch.

## Keeping it up to date across Macs

The source of truth is this folder. The `.app` on the Mac mini is a build of it, so after any change you either rebuild on the Mini (`build-app.command` — quick after the first time) or, better, let GitHub build it:

1. Put this folder in a GitHub repository (private is fine).
2. `.github/workflows/build-mac.yml` is already here: every push to `main` builds `Slipbeats.app` on an Apple-silicon runner and attaches `Slipbeats-mac-arm64.zip` to the workflow run (Actions tab → the run → Artifacts). Pushing a tag like `v0.3` also creates a GitHub Release with the zip attached.
3. On the Mini: download the zip, unzip, drag to Applications, right-click → Open the first time (locally signed, so Gatekeeper asks once). Your index, saved playlists and Spotify login live in `~/.slipbeats/` and survive replacing the app.

Everything user-specific is in `~/.slipbeats/` and `~/Slipbeats Playlists/`, nothing inside the app bundle, so upgrades are just a swap.

## Command line

```
python3 -m slipbeats index "/path/to/music"        # scan (incremental)
python3 -m slipbeats index "/path/to/music" --full # re-read every tag
python3 -m slipbeats match setlist.txt             # print matches
python3 -m slipbeats search "murder dancefloor"
python3 -m slipbeats serve --port 8765
```

## How matching works

Filenames and tags are parsed into artist / title / version / BPM, tolerant of `Feat.`/`Ft`/`feat`, `&` vs `and`, underscores standing in for apostrophes (`Don_t stop`), accents, `The` prefixes, trailing BPM numbers, `_PN` suffixes, and artist/title being the wrong way round in the file. Version words (Extended, Intro Clean, Dirty, Remix name…) are kept separate so they never count against a match, and instrumentals/acapellas are ranked down unless you asked for them. If the request includes a duration (Spotify CSV), tracks of the same length get a small boost.

## Known limits

- Spotify playlist *links* are not fetched directly (Spotify's 2026 developer-access rules make that awkward for a personal tool). Use Exportify → CSV and paste it in.
- Playback uses the browser's decoder: MP3, M4A/AAC, WAV and FLAC play in Chrome; AIFF plays in Safari. Anything the browser can't decode still indexes and exports fine.
- No waveform display yet.
