"""ReadAloudMaker v2: Qwen3-ForcedAligner + surgical EPUB 3 packager.

This package is an independent second pipeline. The original aeneas + syncabook
pipeline in ``make_readaloud.py`` is left untouched and remains available. Both
are reached through the unified launcher: ``run.bat`` / ``run.sh`` opens the web
UI, ``run.bat --v2`` runs this pipeline, and ``run.bat --v1`` runs the classic
one. This package produces EPUB 3 Media Overlays by modifying the original EPUB
at ZIP level instead of rebuilding it.
"""

__version__ = "2.0.0a1"
