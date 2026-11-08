"""HTTP server for the ReadAloudMaker UI.

Only the Python standard library is used, so the UI runs in the same tiny
environment as either pipeline. All routes are local-only: the default bind
address is 127.0.0.1 and API calls are checked against the Host/Origin headers.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .environment import REPO_ROOT, detect
from .jobs import JobManager, OptionError, build_command, normalize_options
from .scan import ScanError, scan_folder

JSON_LIMIT = 2 * 1024 * 1024
UPLOAD_LIMIT = 4 * 1024 * 1024 * 1024
UPLOAD_EXTENSIONS = {
    ".epub", ".mp3", ".m4a", ".m4b", ".wav", ".ogg", ".flac", ".aac", ".opus",
}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
ASSET_EXTENSIONS = {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2"}


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class App(object):
    def __init__(self, repo=REPO_ROOT, detector=None, upload_dir=None):
        self.repo = os.path.abspath(repo)
        self.detector = detector or detect
        self.manager = JobManager()
        self.assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        self.upload_dir = upload_dir or tempfile.mkdtemp(prefix="readaloud-ui-")
        os.makedirs(self.upload_dir, exist_ok=True)
        self.started = time.time()

    # -- engine metadata ---------------------------------------------------
    def env_payload(self, force=False):
        data = self.detector(force=force)
        payload = dict(data)
        payload.update({
            "app": "ReadAloudMaker",
            "version": __version__,
            "ui": True,
            "port": getattr(self, "port", None),
            "uploadFolder": self.upload_dir,
            "time": time.time(),
            "defaults": {
                "engine": "qwen",
                "folder": self.repo,
                "out": "",
                "language": "",
                "max_chunk_seconds": 150,
                "min_coverage": 0.98,
                "device": "auto",
                "dtype": "auto",
                "model": "Qwen/Qwen3-ForcedAligner-0.6B",
            },
            "links": {
                "docs": "docs/QWEN3_PIPELINE.md",
                "readme": "README.md",
            },
        })
        return payload

    def get_engine(self, key, refresh=False):
        data = self.detector(force=refresh)
        for engine in data.get("engines", []):
            if engine.get("key") == key:
                return engine
        return None

    # -- jobs --------------------------------------------------------------
    def create_job(self, payload):
        if not isinstance(payload, dict):
            raise OptionError("Invalid request.")
        key = str(payload.get("engine") or "").strip()
        engine = self.get_engine(key)
        if engine is None:
            raise OptionError("Unknown engine: %r" % key)
        if not engine.get("available"):
            raise OptionError(
                "%s is not ready on this machine." % engine.get("name", key),
                "Missing: " + ", ".join(engine.get("missing") or ["unknown dependency"]),
            )
        options = normalize_options(payload, key)
        command, cwd = build_command(engine, options)
        job = self.manager.create(engine, options, command, cwd=cwd)
        return job

    # -- path allow-list ---------------------------------------------------
    def known_roots(self):
        roots = [self.repo, self.upload_dir]
        for summary in self.manager.list():
            job = self.manager.get(summary["id"])
            if job is None:
                continue
            roots.append(job.options.get("folder", ""))
            roots.append(job.options.get("out", ""))
            for output in job.outputs:
                roots.append(os.path.dirname(output.get("path", "")))
        return [root for root in roots if root]

    def is_revealable(self, path):
        path = os.path.abspath(path)
        for root in self.known_roots():
            root = os.path.abspath(root)
            try:
                if os.path.commonpath([path, root]) == root:
                    return True
            except ValueError:
                continue
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ReadAloudMakerUI/" + __version__
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Keep the console clean; errors are reported by _safe_handle.
        return

    def _send(self, status, body, content_type="application/json; charset=utf-8",
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, payload, extra_headers=None):
        self._send(status, _json_bytes(payload), extra_headers=extra_headers)

    def _error(self, status, message, hint=""):
        self._json(status, {"error": message, "hint": hint})

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise OptionError("Invalid Content-Length.")
        if length < 0 or length > JSON_LIMIT:
            raise OptionError("Request is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise OptionError("Request body is not valid JSON.")

    def _local_request(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host:
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if name not in LOCAL_HOSTS:
                return False
        origin = self.headers.get("Origin")
        if origin:
            match = re.match(
                r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
                origin.strip(),
                re.IGNORECASE,
            )
            if not match:
                return False
        return True

    def _guard(self):
        if not self._local_request():
            self._error(403, "This UI only accepts requests from this computer.")
            return False
        return True

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        self._safe_handle(self._get)

    def do_POST(self):
        self._safe_handle(self._post)

    def _safe_handle(self, function):
        try:
            function()
        except OptionError as exc:
            self._error(400, str(exc), getattr(exc, "hint", ""))
        except ScanError as exc:
            self._error(400, str(exc), getattr(exc, "hint", ""))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            self._error(500, "Internal error", str(exc))

    def _get(self):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)

        if path in ("/", "/index.html"):
            return self._serve_asset("index.html")
        if path.startswith("/assets/"):
            return self._serve_asset(path[len("/assets/"):])

        if not self._guard():
            return

        if path == "/api/env":
            force = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            return self._json(200, self.app.env_payload(force=force))
        if path == "/api/jobs":
            return self._json(200, {"jobs": self.app.manager.list()})

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if match:
            job = self.app.manager.get(match.group(1))
            if job is None:
                return self._error(404, "Job not found.")
            since = 0
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._json(200, job.to_dict(since=max(0, since)))

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/download(?:/(\d+))?", path)
        if match:
            job = self.app.manager.get(match.group(1))
            if job is None:
                return self._error(404, "Job not found.")
            index = int(match.group(2) or 0)
            if index < 0 or index >= len(job.outputs):
                return self._error(404, "This job has no downloadable EPUB.")
            return self._serve_download(job.outputs[index])

        return self._error(404, "Not found.")

    def _post(self):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)

        if not self._guard():
            return

        if path == "/api/scan":
            payload = self._read_json()
            folder = str(payload.get("folder") or "")
            engine = str(payload.get("engine") or "qwen")
            if engine not in ("qwen", "classic"):
                raise OptionError("Unknown engine: %r" % engine)
            exclude = str(payload.get("exclude") or "")
            return self._json(200, scan_folder(folder, engine, exclude))

        if path == "/api/jobs":
            job = self.app.create_job(self._read_json())
            return self._json(201, job.summary())

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path)
        if match:
            job = self.app.manager.get(match.group(1))
            if job is None:
                return self._error(404, "Job not found.")
            job.cancel()
            return self._json(200, job.summary())

        if path == "/api/reveal":
            payload = self._read_json()
            target = str(payload.get("path") or "").strip()
            if not target:
                raise OptionError("No path to open.")
            target = os.path.abspath(os.path.expandvars(os.path.expanduser(target)))
            if not os.path.exists(target):
                raise OptionError("That path no longer exists.")
            if not self.app.is_revealable(target):
                raise OptionError("That folder is outside this app's working folders.")
            _reveal(target)
            return self._json(200, {"ok": True, "path": target})

        if path == "/api/upload":
            return self._upload(query)

        if path == "/api/upload/reset":
            return self._reset_uploads()

        return self._error(404, "Not found.")

    # -- static ------------------------------------------------------------
    def _serve_asset(self, name):
        name = name.replace("\\", "/").strip("/")
        if not name or "/" in name or name.startswith("."):
            return self._error(404, "Not found.")
        if os.path.splitext(name)[1].lower() not in ASSET_EXTENSIONS:
            return self._error(404, "Not found.")
        candidate = os.path.join(self.app.assets, name)
        if not os.path.isfile(candidate):
            return self._error(404, "Not found.")
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "image/svg+xml"):
            content_type += "; charset=utf-8"
        with open(candidate, "rb") as handle:
            body = handle.read()
        return self._send(200, body, content_type)

    def _serve_download(self, output):
        path = output.get("path", "")
        if not os.path.isfile(path):
            return self._error(404, "The EPUB file is missing.")
        size = os.path.getsize(path)
        name = output.get("name") or os.path.basename(path)
        quoted = urllib.parse.quote(name)
        headers = {
            "Content-Disposition": "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                                   % (name.encode("ascii", "replace").decode("ascii").replace('"', ""),
                                      quoted),
        }
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/epub+zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- uploads -----------------------------------------------------------
    def _upload(self, query):
        name = (query.get("name") or [""])[0]
        name = os.path.basename(name.replace("\\", "/")).strip()
        if not name or name in (".", ".."):
            raise OptionError("Missing file name.")
        extension = os.path.splitext(name)[1].lower()
        if extension not in UPLOAD_EXTENSIONS:
            raise OptionError("Unsupported file type: %s" % (extension or "none"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise OptionError("Invalid Content-Length.")
        if length <= 0:
            raise OptionError("The uploaded file is empty.")
        if length > UPLOAD_LIMIT:
            raise OptionError("The file is larger than the 4 GB limit.")

        target = os.path.join(self.app.upload_dir, name)
        temporary = target + ".part"
        remaining = length
        try:
            with open(temporary, "wb") as handle:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                raise OptionError("The upload ended early.")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

        return self._json(201, {
            "name": name,
            "path": target,
            "folder": self.app.upload_dir,
            "size": os.path.getsize(target),
        })

    def _reset_uploads(self):
        removed = 0
        for name in os.listdir(self.app.upload_dir):
            path = os.path.join(self.app.upload_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return self._json(200, {"removed": removed, "folder": self.app.upload_dir})


def _reveal(path):
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys_platform_is_mac():
        import subprocess
        subprocess.Popen(["open", path])
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


def sys_platform_is_mac():
    import sys
    return sys.platform == "darwin"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind an *active* port,
    # which would silently start two servers on 9459. Keep it exclusive there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        """A browser cancelling a keep-alive request is not worth a traceback."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError,
                              BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(host="127.0.0.1", port=9459, app=None):
    application = app or App()
    server = Server((host, port), application)
    application.port = server.server_address[1]
    return server


def existing_ui(host="127.0.0.1", port=9459, timeout=1.5):
    """Return True if something that looks like this UI is already listening."""
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", "/api/env")
        response = connection.getresponse()
        body = response.read()
        connection.close()
    except (OSError, http.client.HTTPException):
        return False
    if response.status != 200:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return payload.get("app") == "ReadAloudMaker"
