"""Discover which build engines this machine can run.

Detection is deliberately cheap: it asks each candidate interpreter whether a
module *exists* (``importlib.util.find_spec``) instead of importing torch or
qwen-asr, which would take seconds and load CUDA. Results are cached until the
UI asks for a refresh.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WINDOWS = os.name == "nt"

PROBE_SCRIPT = r"""
import importlib.util as util
import sys


def has(name):
    try:
        return util.find_spec(name) is not None
    except Exception:
        return False


print(sys.version.split()[0])
print(" ".join(name for name in ("aeneas", "lxml", "torch", "qwen_asr") if has(name)))
"""


@dataclass
class Candidate:
    label: str
    command: list
    executable: str = ""
    version: str = ""
    modules: list = field(default_factory=list)
    error: str = ""

    @property
    def found(self):
        return bool(self.version)


def _no_window_flags():
    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _run_probe(command, timeout=25):
    try:
        completed = subprocess.run(
            list(command) + ["-c", PROBE_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", [], str(exc)

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        return "", [], detail[-1] if detail else "exit code %d" % completed.returncode

    output = completed.stdout.decode("utf-8", "replace").splitlines()
    version = output[0].strip() if output else ""
    modules = output[1].split() if len(output) > 1 else []
    return version, modules, ""


def _candidate_commands():
    """Return interpreters to probe, most useful first, without duplicates."""

    commands = []

    def add(label, command, executable=None):
        command = [str(part) for part in command]
        key = tuple(os.path.normcase(part) for part in command)
        for item in commands:
            existing = tuple(os.path.normcase(part) for part in item.command)
            if existing == key:
                return
        commands.append(Candidate(label=label, command=command,
                                  executable=executable or command[0]))

    def which(name):
        return shutil.which(name)

    add("current interpreter", [sys.executable], sys.executable)

    venv = os.path.join(REPO_ROOT, ".venv-qwen")
    venv_python = (os.path.join(venv, "Scripts", "python.exe") if IS_WINDOWS
                   else os.path.join(venv, "bin", "python"))
    if os.path.exists(venv_python):
        add("Python 3.12 (.venv-qwen)", [venv_python], venv_python)

    if IS_WINDOWS:
        for version in ("3.12", "3.11", "3.10", "3.9"):
            launcher = which("py")
            if launcher:
                add("Python %s (py launcher)" % version,
                    [launcher, "-" + version], launcher)
        for version in ("312", "311", "310", "39"):
            path = r"C:\Program Files\Python%s\python.exe" % version
            if os.path.exists(path):
                add("Python %s.%s" % (version[0], version[1:]), [path], path)
    else:
        for name in ("python3.12", "python3.11", "python3.10", "python3.9",
                     "python3", "python"):
            path = which(name)
            if path:
                add(name, [path], path)

    for name in ("python3", "python"):
        path = which(name)
        if path:
            add(name, [path], path)

    return commands


def probe_candidates():
    commands = _candidate_commands()
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(commands)))) as pool:
        results = list(pool.map(lambda item: _run_probe(item.command), commands))
    for candidate, (version, modules, error) in zip(commands, results):
        candidate.version = version
        candidate.modules = modules
        candidate.error = error
    return [candidate for candidate in commands if candidate.found]


def _syncabook_for(candidate):
    found = shutil.which("syncabook")
    if found:
        return found
    if candidate.executable:
        base = os.path.dirname(candidate.executable)
        for name in ("syncabook.exe", "syncabook"):
            for folder in ("Scripts", "bin", ""):
                path = os.path.join(base, folder, name) if folder else os.path.join(base, name)
                if os.path.exists(path):
                    return path
    return None


def _tool_paths():
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "espeak": shutil.which("espeak-ng") or shutil.which("espeak"),
    }


def gpu_name():
    """Best-effort GPU label via nvidia-smi (does not import torch)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        completed = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    lines = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    return lines[0].strip() if lines else ""


def _python_label(candidate):
    label = candidate.label
    if candidate.version and candidate.version not in label:
        label = "%s · Python %s" % (label, candidate.version)
    return label


def _classic_engine(candidates, tools):
    chosen = None
    for candidate in candidates:
        if "aeneas" in candidate.modules:
            chosen = candidate
            break

    syncabook = _syncabook_for(chosen) if chosen else shutil.which("syncabook")
    missing = []
    if chosen is None:
        missing.append("aeneas (Python 3.9 with requirements.txt)")
    if not syncabook:
        missing.append("syncabook")
    if not tools["ffmpeg"] or not tools["ffprobe"]:
        missing.append("FFmpeg + ffprobe")
    if not tools["espeak"]:
        missing.append("eSpeak NG")

    return {
        "key": "classic",
        "name": "Classic",
        "subtitle": "aeneas + syncabook",
        "description": (
            "The original pipeline. Uses eSpeak synthesis and dynamic time "
            "warping, then packages with syncabook."
        ),
        "available": not missing,
        "command": list(chosen.command) if chosen else [],
        "pythonLabel": _python_label(chosen) if chosen else "not found",
        "syncabook": syncabook or "",
        "missing": missing,
        "notes": ["battle-tested", "needs Python 3.9"],
        "defaults": {
            "language": "",
            "title": "",
            "author": "",
            "exclude": "",
            "prepare_only": False,
            "keep_work": False,
        },
    }


def _qwen_engine(candidates, tools, gpu):
    chosen = None
    for candidate in candidates:
        modules = candidate.modules
        if "qwen_asr" in modules and "torch" in modules and "lxml" in modules:
            chosen = candidate
            break

    missing = []
    if chosen is None:
        missing.append("Python 3.12 with torch, qwen-asr and lxml")
    if not tools["ffmpeg"] or not tools["ffprobe"]:
        missing.append("FFmpeg + ffprobe")

    notes = ["~32–43 ms average error", "GPU recommended" if not gpu else gpu]

    return {
        "key": "qwen",
        "name": "Qwen3 Forced Aligner",
        "subtitle": "Word-level timing · best accuracy",
        "description": (
            "Qwen3-ForcedAligner-0.6B predicts a timestamp for every word and "
            "a built-in packager preserves the original EPUB exactly."
        ),
        "available": not missing,
        "command": list(chosen.command) if chosen else [],
        "pythonLabel": _python_label(chosen) if chosen else "not found",
        "missing": missing,
        "notes": notes,
        "defaults": {
            "language": "",
            "max_chunk_seconds": 150.0,
            "min_coverage": 0.98,
            "device": "auto",
            "dtype": "auto",
            "model": "Qwen/Qwen3-ForcedAligner-0.6B",
            "exclude": "",
            "dry_run": False,
            "fake_aligner": False,
            "keep_work": False,
            "epubcheck": False,
        },
    }


_cache = None
_cache_lock = threading.Lock()


def detect(force=False):
    """Return a JSON-friendly description of the available build engines."""
    global _cache
    with _cache_lock:
        if _cache is not None and not force:
            return _cache

    candidates = probe_candidates()
    tools = _tool_paths()
    gpu = gpu_name()

    engines = [
        _qwen_engine(candidates, tools, gpu),
        _classic_engine(candidates, tools),
    ]

    payload = {
        "repo": REPO_ROOT,
        "platform": "%s %s" % (sys.platform, os.name),
        "runningPython": sys.version.split()[0],
        "ffmpeg": {
            "ok": bool(tools["ffmpeg"] and tools["ffprobe"]),
            "ffmpeg": tools["ffmpeg"] or "",
            "ffprobe": tools["ffprobe"] or "",
            "espeak": tools["espeak"] or "",
        },
        "gpu": gpu,
        "engines": engines,
        "candidates": [
            {
                "label": candidate.label,
                "command": candidate.command,
                "version": candidate.version,
                "modules": candidate.modules,
            }
            for candidate in candidates
        ],
    }

    with _cache_lock:
        _cache = payload
    return payload


def clear_cache():
    global _cache
    with _cache_lock:
        _cache = None
