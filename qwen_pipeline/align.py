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
        if device in (None, "auto"):
            device = "cuda:0" if torch.cuda.is_available() else "cpu"

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
