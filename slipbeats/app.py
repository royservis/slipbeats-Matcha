"""Desktop entry point: runs the local server in a thread and shows it in a native macOS window.

Used by the packaged Slipbeats.app (PyInstaller) and by `python3 -m slipbeats app`.
"""
from __future__ import annotations

import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from .server import App, make_handler

DB_PATH = Path.home() / ".slipbeats" / "library.db"


def free_port(preferred: int = 8765) -> int:
    """Prefer 8765 (the Spotify redirect URI is registered against it); fall back if it's taken."""
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return 0


class Api:
    """Methods callable from the page as window.pywebview.api.<name>()."""

    def __init__(self):
        self.window = None

    def pick_folder(self):
        import webview
        dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        res = self.window.create_file_dialog(dialog, allow_multiple=False)
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        return str(path).rstrip("/")

    def reveal(self, path: str):
        import subprocess
        subprocess.Popen(["open", "-R", path])
        return True


def main():
    try:
        import webview
    except ImportError:
        print("pywebview is not installed: pip install pywebview   (or run `python3 -m slipbeats serve` for the browser version)")
        sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    app = App(str(DB_PATH))
    port = free_port()
    app.port = port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    api = Api()
    window = webview.create_window(
        "Slipbeats", f"http://127.0.0.1:{port}/", js_api=api,
        width=1480, height=920, min_size=(1000, 640), background_color="#111214",
    )
    api.window = window

    def on_closed():
        httpd.shutdown()

    window.events.closed += on_closed
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
