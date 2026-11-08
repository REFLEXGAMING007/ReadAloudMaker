"""Shared exception type."""


class PipelineError(Exception):
    """A user-facing failure. The CLI prints the message without a traceback."""

    def __init__(self, message, hint=None):
        Exception.__init__(self, message)
        self.hint = hint
