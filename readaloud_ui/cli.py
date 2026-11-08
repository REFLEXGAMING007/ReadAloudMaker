"""Command line entry point for ``python -m readaloud_ui``."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from . import __version__
from .server import App, existing_ui, make_server

DEFAULT_PORT = 9459


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run",
        description=(
            "Start the ReadAloudMaker web interface. The browser opens on "
            "http://127.0.0.1:%d and lets you build with either the classic "
            "aeneas + syncabook pipeline or Qwen3 forced alignment." % DEFAULT_PORT
        ),
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port (default: %d)" % DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--version", action="version",
                        version="ReadAloudMaker UI " + __version__)
    return parser


def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main(argv=None):
    args = build_parser().parse_args(argv)
    app = App()

    if args.port and existing_ui(args.host, args.port):
        url = "http://%s:%d/" % (args.host, args.port)
        print("ReadAloudMaker UI is already running: " + url)
        if not args.no_browser and not os.environ.get("READALOUD_NO_BROWSER"):
            threading.Timer(0.2, _open_browser, args=(url,)).start()
        return 0

    try:
        server = make_server(args.host, args.port, app=app)
    except OSError as exc:
        if existing_ui(args.host, args.port):
            url = "http://%s:%d/" % (args.host, args.port)
            print("ReadAloudMaker UI is already running: " + url)
            if not args.no_browser and not os.environ.get("READALOUD_NO_BROWSER"):
                threading.Timer(0.2, _open_browser, args=(url,)).start()
            return 0
        print("Could not start the UI on %s:%d: %s" % (args.host, args.port, exc),
              file=sys.stderr)
        print("Use --port to choose a different port.", file=sys.stderr)
        return 1

    host, port = server.server_address[0], server.server_address[1]
    url = "http://%s:%d/" % (host, port)

    print("")
    print("  ReadAloudMaker UI  " + __version__)
    print("  " + url)
    print("  Press Ctrl+C or close this window to stop the server.")
    print("")

    if not args.no_browser and not os.environ.get("READALOUD_NO_BROWSER"):
        threading.Timer(0.7, _open_browser, args=(url,)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping the UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
