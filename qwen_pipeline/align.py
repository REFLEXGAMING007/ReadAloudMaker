"""Qwen3-ForcedAligner wrapper.

The heavy stack (``torch``, ``qwen_asr``, ``librosa``) is imported lazily so the
rest of the pipeline, and its tests, work without a GPU environment.
"""

from dataclasses import dataclass

from .errors import PipelineError

LANGUAGE_NAMES = {
    "en": "English",
    "eng": "English",
    "de": "German",
    "deu": "German",
    "ger": "German",
    "fr": "French",
    "fra": "French",
    "fre": "French",
    "es": "Spanish",
    "spa": "Spanish",
    "it": "Italian",
    "ita": "Italian",
    "pt": "Portuguese",
    "por": "Portuguese",
    "ru": "Russian",
    "rus": "Russian",
    "zh": "Chinese",
    "zho": "Chinese",
    "chi": "Chinese",
    "ja": "Japanese",
    "jpn": "Japanese",
    "ko": "Korean",
    "kor": "Korean",
    "yue": "Cantonese",
    "cantonese": "Cantonese",
}

SUPPORTED_LANGUAGES = (
    "Chinese", "English", "Cantonese", "French", "German", "Italian",
    "Japanese", "Korean", "Portuguese", "Russian", "Spanish",
)

MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B"

# A device needs at least this much free VRAM to be a useful `auto` candidate;
# below it the model plus a 150 s audio window will not fit.
MIN_FREE_VRAM = int(2.5 * 1024 ** 3)


def gpu_snapshot(torch):
    """Return one dict per CUDA device, or ``[]`` when CUDA is unusable."""
    gpus = []
    try:
        if not torch.cuda.is_available():
            return gpus
        count = torch.cuda.device_count()
    except Exception:
        return gpus

    for index in range(count):
        gpu = {
            "index": index,
            "name": "cuda:%d" % index,
            "capability": 0,
            "total_memory": 0,
            "free_memory": 0,
        }
        try:
            gpu["name"] = str(torch.cuda.get_device_name(index))
        except Exception:
            pass
        try:
            major, minor = torch.cuda.get_device_capability(index)
            gpu["capability"] = int(major) * 10 + int(minor)
        except Exception:
            pass
        try:
            gpu["total_memory"] = int(torch.cuda.get_device_properties(index).total_memory)
        except Exception:
            pass
        try:
            gpu["free_memory"] = int(torch.cuda.mem_get_info(index)[0])
        except Exception:
            gpu["free_memory"] = gpu["total_memory"]
        gpus.append(gpu)
    return gpus


def _gigabytes(value):
    return "%.1f GB" % (float(value or 0) / (1024.0 ** 3))


def select_device(torch, requested="auto"):
    """Resolve ``--device`` to ``(device, reason)``.

    ``auto`` picks the *most capable* CUDA device that still has room, instead
    of blindly using ``cuda:0``. Ranking is compute capability first, then free
    VRAM, then total VRAM: a newer architecture runs the model faster even when
    an older card has a little more memory, while a card that is already full
    is skipped entirely.
    """
    requested = (requested or "auto").strip() or "auto"
    lowered = requested.lower()
    gpus = gpu_snapshot(torch)

    if lowered == "cpu":
        return "cpu", "requested"

    if lowered != "auto":
        if lowered == "cuda":
            index = 0
        elif lowered.startswith("cuda:"):
            try:
                index = int(lowered.split(":", 1)[1])
            except ValueError:
                raise PipelineError(
                    "--device %r is not a valid CUDA index" % requested
                )
            if index < 0:
                raise PipelineError(
                    "--device %r is not a valid CUDA index" % requested
                )
        else:
            raise PipelineError(
                "--device must be auto, cpu, or cuda / cuda:N (got %r)" % requested
            )
        if not gpus:
            raise PipelineError(
                "CUDA is not available in this Python, so --device %s cannot be used."
                % requested,
                hint="Install a CUDA build of PyTorch, or pass --device cpu.",
            )
        if index >= len(gpus):
            raise PipelineError(
                "cuda:%d does not exist; this machine has %d CUDA device(s):\n  %s"
                % (index, len(gpus),
                   "\n  ".join("cuda:%d  %s  %s"
                               % (gpu["index"], gpu["name"],
                                  _gigabytes(gpu["total_memory"]))
                               for gpu in gpus)),
                hint="Pick one of the devices above, or use --device auto.",
            )
        return "cuda:%d" % index, "requested"

    if not gpus:
        return "cpu", "no CUDA device"

    usable = [gpu for gpu in gpus if gpu["free_memory"] >= MIN_FREE_VRAM]
    skipped = len(gpus) - len(usable)
    candidates = usable or gpus
    best = max(candidates, key=lambda gpu: (gpu["capability"],
                                            gpu["free_memory"],
                                            gpu["total_memory"]))
    reason = "%s, %s free of %s" % (best["name"], _gigabytes(best["free_memory"]),
                                    _gigabytes(best["total_memory"]))
    if skipped:
        reason += "; %d device(s) skipped for low free memory" % skipped
    if len(gpus) > 1:
        reason += "; best of %d" % len(gpus)
    return "cuda:%d" % best["index"], reason



@dataclass
class Token:
    text: str
    start_time: float
    end_time: float

    def shifted(self, offset):
        return Token(self.text, self.start_time + offset, self.end_time + offset)


def language_name(value):
    """Turn an EPUB language code into the name Qwen3 expects."""
    if not value:
        raise PipelineError(
            "the EPUB has no dc:language; pass --lang (for example --lang eng)"
        )
    cleaned = value.strip()
    for name in SUPPORTED_LANGUAGES:
        if cleaned.lower() == name.lower():
            return name
    code = cleaned.replace("_", "-").split("-")[0].lower()
    if code in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[code]
    raise PipelineError(
        "Qwen3-ForcedAligner does not support %r.\n"
        "Supported languages: %s" % (value, ", ".join(SUPPORTED_LANGUAGES))
    )


class QwenAligner(object):
    """Lazy, single-model Qwen3 forced aligner."""

    def __init__(self, model_id=MODEL_ID, device="auto", compile_model=False,
                 dtype="auto"):
        self.model_id = model_id
        self.requested_device = device
        self.compile_model = compile_model
        self.requested_dtype = dtype
        self._model = None
        self._torch = None
        self.device = None
        self.dtype = None
        self.device_reason = ""

    # -- loading -----------------------------------------------------------

    def _resolve_dtype(self, torch, device):
        """Pick a dtype the GPU actually supports.

        Turing and older cards (GTX 10xx) have no native bfloat16, so use
        float16 there; Ampere and newer get bfloat16. CPU uses float32.
        """
        requested = (self.requested_dtype or "auto").lower()
        if requested != "auto":
            mapping = {
                "float32": torch.float32,
                "fp32": torch.float32,
                "float16": torch.float16,
                "fp16": torch.float16,
                "bfloat16": torch.bfloat16,
                "bf16": torch.bfloat16,
            }
            if requested not in mapping:
                raise PipelineError(
                    "--dtype must be auto, float16, bfloat16 or float32"
                )
            return mapping[requested]

        if not str(device).startswith("cuda"):
            return torch.float32
        try:
            major, _minor = torch.cuda.get_device_capability(device)
        except Exception:
            major = 0
        return torch.bfloat16 if major >= 8 else torch.float16

    def load(self):
        if self._model is not None:
            return
        try:
            import torch
            from qwen_asr import Qwen3ForcedAligner
        except ImportError as exc:
            raise PipelineError(
                "the Qwen3 pipeline needs Python 3.12 with torch and qwen-asr.\n"
                "Missing module: %s\n"
                "Install with:\n"
                "  py -3.12 -m pip install -r requirements-qwen.txt" % exc.name,
            )

        device = self.requested_device
        device, reason = select_device(torch, device)
        self.device_reason = reason
        if device.startswith("cuda"):
            try:
                torch.cuda.set_device(device)
            except Exception:
                pass

        dtype = self._resolve_dtype(torch, device)

        try:
            model = Qwen3ForcedAligner.from_pretrained(
                self.model_id,
                dtype=dtype,
                device_map=device,
            )
        except Exception as exc:  # pragma: no cover - depends on local hardware
            raise PipelineError(
                "could not load %s on %s (%s): %s"
                % (self.model_id, device, dtype, exc),
                hint=(
                    "Try --device cpu (slow) or a smaller --max-chunk-seconds "
                    "to lower memory use."
                ),
            )

        if self.compile_model:
            try:
                model.model.thinker = torch.compile(model.model.thinker)
            except Exception:
                # Compilation is an optimisation; never fail the build for it.
                pass

        self._torch = torch
        self._model = model
        self.device = device
        self.dtype = dtype

    def describe_device(self):
        """Human-readable ``cuda:1 · float16 (GTX 1060, 5.8 GB free)``."""
        if not self.device:
            return self.requested_device or "auto"
        text = self.device
        if self.dtype is not None:
            text += " · " + str(self.dtype).replace("torch.", "")
        if self.device_reason and self.device_reason != "requested":
            text += " (" + self.device_reason + ")"
        return text

    # -- inference ---------------------------------------------------------

    def align_wave(self, wave, sample_rate, text, language):
        self.load()
        try:
            results = self._model.align(
                audio=(wave, sample_rate),
                text=text,
                language=language,
            )
        except Exception as exc:  # pragma: no cover - model runtime
            raise PipelineError("Qwen3 alignment failed: %s" % exc)

        if not results:
            return []
        return [
            Token(
                text=str(item.text),
                start_time=float(item.start_time),
                end_time=float(item.end_time),
            )
            for item in results[0]
        ]

    def align_file_slice(self, path, start, duration, text, language,
                         sample_rate=16000):
        """Align a slice of an audio file; timestamps are shifted to global."""
        try:
            import librosa
        except ImportError:
            raise PipelineError(
                "librosa is required to read audio slices; it is installed with "
                "qwen-asr."
            )

        wave, _ = librosa.load(
            str(path),
            sr=sample_rate,
            mono=True,
            offset=float(start),
            duration=float(duration),
        )
        tokens = self.align_wave(wave, sample_rate, text, language)
        return [token.shifted(start) for token in tokens]


class ProportionalAligner(object):
    """Deterministic stand-in used by tests and ``--dry-run`` style checks.

    It spreads tokens over the first three quarters of the supplied audio
    slice, leaving a silence tail like a real alignment would. It is never used
    in a real build unless requested explicitly.
    """

    def __init__(self, fill=0.75):
        self.fill = fill
        self.calls = 0

    def align_wave(self, wave, sample_rate, text, language):
        self.calls += 1
        words = [piece for piece in text.split() if piece]
        total = max(len(words), 1)
        spoken = (float(len(wave)) / float(sample_rate)) * self.fill
        step = spoken / float(total)
        return [
            Token(text=word, start_time=index * step, end_time=(index + 1) * step)
            for index, word in enumerate(words)
        ]

    def align_file_slice(self, path, start, duration, text, language,
                         sample_rate=16000):
        self.calls += 1
        words = [piece for piece in text.split() if piece]
        total = max(len(words), 1)
        step = (float(duration) * self.fill) / float(total)
        return [
            Token(
                text=word,
                start_time=start + index * step,
                end_time=start + (index + 1) * step,
            )
            for index, word in enumerate(words)
        ]
