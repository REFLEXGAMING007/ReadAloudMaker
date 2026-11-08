"""Split long chapters into chunks that Qwen3-ForcedAligner can process.

Qwen3-ForcedAligner handles long audio, but qwen-asr's packaged inference
helpers cap a forced-alignment call at 180 seconds of audio
(``MAX_FORCE_ALIGN_INPUT_SECONDS``), while the model card advertises up to five
minutes. This module stays within the conservative 180-second package limit: it
picks text boundaries at sentence breaks and audio boundaries at detected
silences, then the build step verifies that the aligned text really ends where
the audio chunk ends. It refuses to silently accept a bad split.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass

from .errors import PipelineError

DEFAULT_MAX_SECONDS = 150.0
HARD_LIMIT_SECONDS = 180.0


@dataclass
class Chunk:
    index: int
    start: float
    end: float
    first: int      # first fragment index, inclusive
    last: int       # last fragment index, exclusive
    text_start: int
    text_end: int

    @property
    def duration(self):
        return self.end - self.start


def probe_duration(path):
    """Duration of an audio file in seconds, via ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PipelineError(
            "ffprobe was not found; install FFmpeg and add it to PATH"
        )
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text = result.stdout.decode("utf-8", "replace").strip()
    try:
        return float(text)
    except ValueError:
        raise PipelineError(
            "could not read the duration of %s with ffprobe" % path
        )


def detect_silences(path, noise_db=-35, min_silence=0.30):
    """Return midpoints of silences in an audio file.

    Pressing ffmpeg is best-effort: an empty list simply means chunk boundaries
    are estimated from the speaking rate instead of snapped to a pause.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []

    command = [
        ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
        "-af", "silencedetect=noise=%ddB:d=%s" % (noise_db, min_silence),
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return []

    text = result.stderr.decode("utf-8", "replace")
    starts = []
    midpoints = []

    for line in text.splitlines():
        start_match = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if start_match:
            starts.append(float(start_match.group(1)))
            continue
        end_match = re.search(r"silence_end:\s*(-?[0-9.]+)", line)
        if end_match and starts:
            start = starts.pop(0)
            end = float(end_match.group(1))
            if end > start:
                midpoints.append((start + end) / 2.0)

    midpoints.sort()
    return midpoints


def _chunk_window(est_end, duration, margin):
    """Audio window end: past the estimated last word, clamped to the file."""
    return min(duration, est_end + margin)


def _snap_window_end(silences, est_end, window_end):
    """Use the last silence inside the safety margin, if there is one.

    The window only has to contain the narration, so snapping is optional and
    must never move the end before the estimated last word.
    """
    best = None
    for value in silences:
        if est_end <= value <= window_end:
            best = value
    return window_end if best is None else best


def plan_chunks(duration, fragments, max_seconds=DEFAULT_MAX_SECONDS,
                silences=None):
    """Group fragments into alignment passes.

    ``max_seconds`` applies to the *estimated narration* of each chunk. Every
    chunk also gets a safety margin of audio after that estimate, so the model
    always hears the end of the last sentence. The returned ``end`` is that
    window end, not a hard cut; the build step shrinks it to the real narration
    end after alignment (and re-aligns with a larger window if it was too
    short).

    ``fragments`` need ``start``, ``end`` and ``words`` attributes.
    """
    if not fragments:
        return []
    if duration <= 0:
        raise PipelineError("audio duration must be positive")
    if max_seconds <= 0 or max_seconds > HARD_LIMIT_SECONDS - 10.0:
        raise PipelineError(
            "--max-chunk-seconds must be between 1 and %.0f"
            % (HARD_LIMIT_SECONDS - 10.0)
        )

    silences = sorted(silences or [])
    total_words = sum(fragment.words for fragment in fragments)
    if total_words <= 0:
        raise PipelineError("chapter text has no words")

    rate = duration / float(total_words)
    margin = min(30.0, max(1.0, HARD_LIMIT_SECONDS - max_seconds))

    chunks = []
    first = 0
    cumulative = 0.0

    while first < len(fragments):
        words = 0
        last = first
        while last < len(fragments):
            word_count = fragments[last].words
            if words and (words + word_count) * rate > max_seconds:
                break
            words += word_count
            last += 1
        if last == first:  # one fragment longer than max_seconds
            last = first + 1
            words = fragments[first].words

        est_start = cumulative * rate
        est_end = (cumulative + words) * rate
        window_end = _chunk_window(est_end, duration, margin)
        if silences:
            window_end = _snap_window_end(silences, est_end, window_end)

        chunks.append(
            Chunk(
                index=len(chunks),
                start=est_start,
                end=window_end,
                first=first,
                last=last,
                text_start=fragments[first].start,
                text_end=fragments[last - 1].end,
            )
        )

        if len(chunks) > 10000:  # pathological guard
            raise PipelineError("chunk planning did not converge")

        cumulative += words
        first = last

    return chunks

