"""Local web interface for ReadAloudMaker.

The UI is a small, dependency-free HTTP server that drives either build
pipeline (classic aeneas + syncabook, or Qwen3 forced alignment) and streams
the build log to the browser.
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
