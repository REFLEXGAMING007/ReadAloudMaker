"""ReadAloudMaker v2: Qwen3-ForcedAligner + surgical EPUB 3 packager.

This package is an independent second pipeline. The original aeneas + syncabook
pipeline in ``make_readaloud.py`` is left untouched and remains the default
(``run.bat`` / ``run.sh``). This one is reached through ``run2.bat`` /
``run2.sh`` and produces EPUB 3 Media Overlays by modifying the original EPUB
at ZIP level instead of rebuilding it.
"""

__version__ = "2.0.0a1"
