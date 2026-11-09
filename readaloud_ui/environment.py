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


def _parse_gpus(text):
    """Parse ``nvidia-smi --format=csv,noheader,nounits`` rows."""
    gpus = []
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        try:
            total = int(float(parts[2])) * 1024 * 1024
        except ValueError:
            total = 0
        try:
            free = int(float(parts[3])) * 1024 * 1024
        except ValueError:
            free = total
        capability = parts[4] if len(parts) > 4 else ""
        gpus.append({
            "index": index,
            "name": parts[1] or ("cuda:%d" % index),
            "totalMemory": total,
            "freeMemory": free,
            "computeCap": capability,
            "driver": parts[5] if len(parts) > 5 else "",
        })
    return gpus


QUERY_FIELDS = "index,name,memory.total,memory.free,compute_cap,driver_version"


def _visible_gpus(gpus):
    """Reorder/filter to what torch will actually expose.

    With ``CUDA_VISIBLE_DEVICES=1,2`` torch calls the cards ``cuda:0`` and
    ``cuda:1``, so the UI must present them the same way or every index the
    user picks would be off by one.
    """
    visible = (os.environ.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if not visible:
        return gpus
    if visible == "-1":
        return []
    wanted = []
    for part in visible.split(","):
        part = part.strip()
        if part.isdigit():
            wanted.append(int(part))
    if not wanted:
        return gpus  # UUID or MIG identifiers: leave the list untouched

    by_index = {gpu["index"]: gpu for gpu in gpus}
    ordered = []
    for position, index in enumerate(wanted):
        gpu = by_index.get(index)
        if gpu is None:
            continue
        entry = dict(gpu)
        entry["physicalIndex"] = gpu["index"]
        entry["index"] = position
        ordered.append(entry)
    return ordered or gpus


def gpu_devices():
    """Every visible NVIDIA GPU as reported by nvidia-smi (never imports torch)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    environment = os.environ.copy()
    # nvidia-smi already defaults to PCI order, but pin it so the indices match
    # the ones torch reports (jobs.py sets the same variable for builds).
    environment["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    try:
        completed = subprocess.run(
            [exe, "--query-gpu=" + QUERY_FIELDS,
             "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            env=environment,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return _visible_gpus(_parse_gpus(completed.stdout.decode("utf-8", "replace")))


def gpu_name(gpus=None):
    """Label of the first GPU, kept for the older single-GPU payload field."""
    if gpus is None:
        gpus = gpu_devices()
    return gpus[0]["name"] if gpus else ""


def recommended_gpu(gpus):
    """Index of the best *class* of card, used for the UI's recommendation.

    Ranked by compute capability, then total VRAM. Free memory is deliberately
    not part of this: between two identical cards, ``auto`` takes whichever has
    more free VRAM at load time, so the UI promises a class of card (and the
    build log reports the exact index) instead of predicting one.
    """
    if not gpus:
        return None

    def rank(gpu):
        capability = 0
        for part in str(gpu.get("computeCap") or "").split("."):
            if part.isdigit():
                capability = capability * 10 + int(part)
        return (capability, gpu.get("totalMemory") or 0)

    return max(gpus, key=rank)["index"]


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


def _gpu_best_label(gpus, index):
    """Name of the recommended card *class* (no index: ``auto`` decides that)."""
    for gpu in gpus:
        if gpu.get("index") == index:
            return "best GPU · %s" % (gpu.get("name") or "GPU")
    return ""


def _qwen_engine(candidates, tools, gpus=None, recommended=None):
    gpus = gpus or []
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

    label = _gpu_best_label(gpus, recommended)
    notes = ["~32–43 ms average error", label or "GPU recommended"]

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
    gpus = gpu_devices()
    recommended = recommended_gpu(gpus)

    engines = [
        _qwen_engine(candidates, tools, gpus, recommended),
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
        "gpu": gpu_name(gpus),
        "gpus": gpus,
        "recommendedGpu": recommended,
        "cudaVisible": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
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
