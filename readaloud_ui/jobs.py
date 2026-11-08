"""Build jobs: option validation, command lines, process handling, progress."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .environment import IS_WINDOWS, REPO_ROOT

MAX_LINES = 4000
MAX_JOBS = 30


class OptionError(ValueError):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


NOISE_RE = re.compile(
    r"(libmpg123|mpg123|part2_3_length|libavcodec|layer3\.c|"
    r"\[mp3float|\[libmpg)",
    re.IGNORECASE,
)
ERROR_RE = re.compile(
    r"(^|\W)(error|traceback|exception|failed|fatal|aborted|"
    r"no such file|cannot open)(\W|$)",
    re.IGNORECASE,
)
WARN_RE = re.compile(r"(^|\W)(warning|deprecated)(\W|$)", re.IGNORECASE)
CHAPTER_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")
DONE_RE = re.compile(r"DONE:\s*(.+?)\s*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_STAGES = {
    "prepare": "Preparing",
    "align": "Aligning text to audio",
    "package": "Packaging the EPUB",
    "check": "Validating",
    "done": "Finished",
}


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clean_string(value, name, limit=400):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise OptionError("%s must be text." % name)
    value = value.strip()
    if len(value) > limit:
        raise OptionError("%s is too long." % name)
    return value


def _number(value, name, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OptionError("%s must be a number." % name)
    if not minimum <= number <= maximum:
        raise OptionError("%s must be between %g and %g." % (name, minimum, maximum))
    return number


def normalize_options(payload, engine_key):
    """Validate a JSON payload from the browser and return clean options."""
    if not isinstance(payload, dict):
        raise OptionError("Invalid request.")

    folder = _clean_string(payload.get("folder"), "Folder", 1000)
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not folder:
        raise OptionError("Choose the folder that holds the book and audio.")
    if not os.path.isdir(folder):
        raise OptionError("Folder not found: " + folder,
                          "Check the path, or drag the files into the window.")

    out = _clean_string(payload.get("out"), "Output folder", 1000)
    if out:
        out = os.path.expandvars(os.path.expanduser(out))
        if not os.path.isabs(out):
            out = os.path.join(folder, out)
        out = os.path.abspath(out)
    else:
        out = os.path.join(folder, "output")

    options = {
        "folder": folder,
        "out": out,
        "epub": _clean_string(payload.get("epub"), "EPUB file", 400),
        "language": _clean_string(payload.get("language"), "Language", 80),
        "exclude": _clean_string(payload.get("exclude"), "Exclude pattern", 600),
        "keep_work": _bool(payload.get("keep_work")),
    }

    if options["epub"]:
        candidate = options["epub"]
        if not os.path.isabs(candidate):
            candidate = os.path.join(folder, candidate)
        if not os.path.exists(candidate):
            raise OptionError("EPUB not found: " + candidate)
        options["epub"] = candidate

    if engine_key == "classic":
        options.update({
            "title": _clean_string(payload.get("title"), "Title", 300),
            "author": _clean_string(payload.get("author"), "Author", 300),
            "prepare_only": _bool(payload.get("prepare_only")),
        })
    elif engine_key == "qwen":
        options.update({
            "max_chunk_seconds": _number(
                payload.get("max_chunk_seconds", 150), "Pass length", 30, 170
            ),
            "min_coverage": _number(
                payload.get("min_coverage", 0.98), "Minimum coverage", 0.5, 1.0
            ),
            "device": _clean_string(payload.get("device") or "auto", "Device", 40) or "auto",
            "dtype": _clean_string(payload.get("dtype") or "auto", "Precision", 20) or "auto",
            "model": (_clean_string(payload.get("model"), "Model", 300)
                      or "Qwen/Qwen3-ForcedAligner-0.6B"),
            "dry_run": _bool(payload.get("dry_run")),
            "fake_aligner": _bool(payload.get("fake_aligner")),
            "epubcheck": _bool(payload.get("epubcheck")),
            "compile_model": _bool(payload.get("compile_model")),
        })
        if options["dtype"] not in ("auto", "bfloat16", "float16", "float32"):
            raise OptionError("Precision must be auto, bfloat16, float16 or float32.")
        if options["dry_run"] and options["fake_aligner"]:
            raise OptionError("Dry run and fake aligner cannot be combined.")
    else:
        raise OptionError("Unknown engine: %r" % engine_key)

    return options


def build_command(engine, options):
    """Return ``(argv, cwd)`` for a validated job."""
    key = engine["key"]
    python = list(engine.get("command") or [])
    if not python:
        raise OptionError(
            "%s is not available on this machine." % engine.get("name", key),
            "See the setup notes in the UI."
        )

    cwd = REPO_ROOT if key == "qwen" else options["folder"]

    if key == "classic":
        argv = python + [os.path.join(REPO_ROOT, "make_readaloud.py"),
                         "--folder", options["folder"]]
        if options.get("epub"):
            argv += ["--epub", options["epub"]]
        if options.get("language"):
            argv += ["--lang", options["language"]]
        if options.get("title"):
            argv += ["--title", options["title"]]
        if options.get("author"):
            argv += ["--author", options["author"]]
        if options.get("exclude"):
            argv += ["--exclude", options["exclude"]]
        if options.get("prepare_only"):
            argv.append("--prepare-only")
        if options.get("keep_work"):
            argv.append("--keep-work")
        return argv, cwd

    argv = python + ["-m", "qwen_pipeline",
                     "--folder", options["folder"],
                     "--out", options["out"]]
    if options.get("epub"):
        argv += ["--epub", options["epub"]]
    if options.get("language"):
        argv += ["--lang", options["language"]]
    if options.get("exclude"):
        argv += ["--exclude", options["exclude"]]
    argv += [
        "--max-chunk-seconds", "%g" % options["max_chunk_seconds"],
        "--min-coverage", "%g" % options["min_coverage"],
        "--device", options["device"],
        "--dtype", options["dtype"],
        "--model", options["model"],
    ]
    if options.get("keep_work"):
        argv.append("--keep-work")
    if options.get("dry_run"):
        argv.append("--dry-run")
    if options.get("fake_aligner"):
        argv.append("--fake-aligner")
    if options.get("compile_model"):
        argv.append("--compile")
    if options.get("epubcheck"):
        argv.append("--epubcheck")
    return argv, cwd


def _classify(text):
    if NOISE_RE.search(text):
        return "decoder"
    if ERROR_RE.search(text):
        return "error"
    if WARN_RE.search(text):
        return "warn"
    if DONE_RE.search(text) or "check:   EPUB OK" in text:
        return "success"
    return "info"


def _update_progress(job, text):
    low = text.lower()
    match = CHAPTER_RE.search(text)

    if "done:" in low:
        job.stage = "done"
        job.progress = 1.0
        return
    if "plan written" in low:
        job.stage = "done"
        job.progress = 1.0
        return
    if "packaging" in low:
        job.stage = "package"
        job.progress = max(job.progress, 0.92)
        return
    if "check:" in low or "validating" in low or "epubcheck:" in low:
        job.stage = "check"
        job.progress = max(job.progress, 0.96)
        return
    if match:
        current, total = int(match.group(1)), max(1, int(match.group(2)))
        job.stage = "align"
        job.progress = max(job.progress, 0.10 + 0.80 * current / float(total))
        job.stage_detail = "Chapter %d of %d" % (current, total)
        return
    if "aligning with aeneas" in low or "loading " in low:
        job.stage = "align"
        job.progress = max(job.progress, 0.06)
        return
    if "text:" in low:
        job.progress = max(job.progress, 0.03)
        return
    if "audio:" in low:
        job.progress = max(job.progress, 0.06)
        return


class Job:
    def __init__(self, engine, options, command, cwd=None):
        self.id = uuid.uuid4().hex[:12]
        self.engine_key = engine["key"]
        self.engine_name = engine.get("name", engine["key"])
        self.options = dict(options)
        self.command = list(command)
        self.cwd = cwd or REPO_ROOT

        self.status = "queued"
        self.progress = 0.0
        self.stage = "prepare"
        self.stage_detail = "Waiting to start"
        self.error = ""
        self.outputs = []
        self.report = None
        self.exit_code = None
        self.created = time.time()
        self.started = None
        self.finished = None

        self._lines = []
        self._seq = 0
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._process = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.started = time.time()
        self.status = "running"
        self.stage_detail = "Starting"
        self._thread = threading.Thread(target=self._run, name="job-" + self.id,
                                        daemon=True)
        self._thread.start()
        return self

    def _run(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONUTF8", "1")

        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True

        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **kwargs
            )
        except OSError as exc:
            self._append("Could not start the build: %s" % exc, "error")
            self._finish(1, error="Could not start the build: %s" % exc)
            return

        try:
            for line in iter(self._process.stdout.readline, ""):
                self._append(line.rstrip("\r\n"))
        except Exception as exc:
            self._append("Lost the build output: %s" % exc, "error")
        finally:
            try:
                self._process.stdout.close()
            except Exception:
                pass
            try:
                returncode = self._process.wait()
            except Exception:
                returncode = -1

        self._finish(returncode)

    def _finish(self, returncode, error=""):
        self.exit_code = returncode
        self.finished = time.time()

        if self._cancel_requested:
            self.status = "cancelled"
            self.stage = "done"
            self.stage_detail = "Cancelled"
            self._append("Build cancelled.", "warn")
            return

        if returncode == 0:
            self._copy_classic_output()
            self.outputs = self._collect_outputs()
            self.report = self._collect_report()
            self.status = "succeeded"
            self.stage = "done"
            self.progress = 1.0
            self.stage_detail = "EPUB ready"
        else:
            self.status = "failed"
            self.stage_detail = "Build failed"
            message = error or self._last_error() or "The build failed."
            self.error = message
            if not any(line["level"] == "error" for line in self._lines[-40:]):
                self._append(message, "error")

    def cancel(self):
        self._cancel_requested = True
        if self.status in ("queued", "running", "cancelling"):
            self.status = "cancelling"
            self.stage_detail = "Stopping"
        process = self._process
        if process is not None and process.poll() is None:
            _kill_tree(process)

    # -- logging -----------------------------------------------------------
    def _append(self, text, level=None):
        text = ANSI_RE.sub("", text)
        if len(text) > 4000:
            text = text[:4000] + " …"
        level = level or _classify(text)
        with self._lock:
            self._seq += 1
            self._lines.append({
                "seq": self._seq,
                "level": level,
                "text": text,
                "time": round(time.time() - (self.started or self.created), 3),
            })
            if len(self._lines) > MAX_LINES:
                del self._lines[:len(self._lines) - MAX_LINES]
            _update_progress(self, text)

    def _last_error(self):
        for line in reversed(self._lines):
            if line["level"] == "error":
                return line["text"].strip()
        for line in reversed(self._lines):
            if line["text"].strip():
                return line["text"].strip()
        return ""

    # -- results -----------------------------------------------------------
    def _done_path(self):
        path = ""
        with self._lock:
            for line in self._lines:
                match = DONE_RE.search(line["text"])
                if match:
                    path = match.group(1).strip().strip('"')
        return path

    def _copy_classic_output(self):
        """The classic pipeline always writes to <folder>/output."""
        if self.engine_key != "classic":
            return
        source = self._done_path()
        if not source or not os.path.exists(source):
            return
        default_out = os.path.join(self.options["folder"], "output")
        wanted = self.options["out"]
        if os.path.normcase(os.path.abspath(wanted)) == os.path.normcase(
                os.path.abspath(default_out)):
            return
        try:
            os.makedirs(wanted, exist_ok=True)
            destination = _unique_path(os.path.join(
                wanted, os.path.basename(source)))
            shutil.copy2(source, destination)
            self._append("[readaloud-ui] Copied EPUB to " + destination, "info")
        except OSError as exc:
            self._append("Could not copy the EPUB to %s: %s" % (wanted, exc), "warn")

    def _collect_outputs(self):
        path = self._done_path()
        outputs = []
        candidates = []
        if path and os.path.isfile(path):
            candidates.append(path)

        # The classic pipeline may have been copied to a different out folder.
        if self.engine_key == "classic":
            wanted = self.options["out"]
            if os.path.isdir(wanted):
                found = [os.path.join(wanted, name)
                         for name in os.listdir(wanted)
                         if name.lower().endswith(".epub")]
                found.sort(key=lambda value: os.path.getmtime(value), reverse=True)
                candidates.extend(found[:1])

        seen = set()
        for candidate in candidates:
            key = os.path.normcase(os.path.abspath(candidate))
            if key in seen or not os.path.isfile(candidate):
                continue
            seen.add(key)
            outputs.append({
                "name": os.path.basename(candidate),
                "path": candidate,
                "size": os.path.getsize(candidate),
            })
        return outputs

    def _collect_report(self):
        path = os.path.join(self.options["out"], "report.json")
        try:
            if not os.path.isfile(path):
                return None
            if self.started and os.path.getmtime(path) < self.started - 10:
                return None
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # -- serialization -----------------------------------------------------
    def to_dict(self, since=0):
        with self._lock:
            lines = [line for line in self._lines if line["seq"] > since]
            next_seq = self._seq
            first_seq = self._lines[0]["seq"] if self._lines else next_seq + 1
        now = self.finished or time.time()
        duration = round(now - self.started, 3) if self.started else 0.0
        return {
            "id": self.id,
            "engine": self.engine_key,
            "engineName": self.engine_name,
            "status": self.status,
            "progress": round(self.progress, 4),
            "stage": self.stage,
            "stageLabel": _STAGES.get(self.stage, self.stage),
            "stageDetail": self.stage_detail,
            "error": self.error,
            "outputs": self.outputs,
            "report": self.report,
            "exitCode": self.exit_code,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "duration": duration,
            "folder": self.options.get("folder", ""),
            "out": self.options.get("out", ""),
            "lines": lines,
            "next": next_seq,
            "dropped": bool(since and since < first_seq - 1),
        }

    def summary(self):
        return {
            "id": self.id,
            "engine": self.engine_key,
            "status": self.status,
            "progress": round(self.progress, 4),
            "stageLabel": _STAGES.get(self.stage, self.stage),
            "created": self.created,
            "folder": self.options.get("folder", ""),
            "outputs": self.outputs,
        }


def _unique_path(path):
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    counter = 2
    while os.path.exists("%s (%d)%s" % (stem, counter, extension)):
        counter += 1
    return "%s (%d)%s" % (stem, counter, extension)


def _kill_tree(process):
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            return
        except OSError:
            pass
    try:
        process.terminate()
    except OSError:
        pass


class JobManager:
    def __init__(self):
        self._jobs = {}
        self._order = []
        self._lock = threading.Lock()

    def create(self, engine, options, command, cwd=None):
        job = Job(engine, options, command, cwd=cwd)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune()
        return job.start()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in reversed(self._order)
                    if job_id in self._jobs]
        return [job.summary() for job in jobs]

    def _prune(self):
        while len(self._order) > MAX_JOBS:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is not None and job.status in ("queued", "running", "cancelling"):
                break
            self._order.pop(0)
            self._jobs.pop(oldest, None)
