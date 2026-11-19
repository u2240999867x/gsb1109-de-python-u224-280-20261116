from __future__ import absolute_import

from . import imaplib2 as _imaplib2_module
from .imaplib2 import *
from .imaplib2 import __version__, __doc__

_RECORDING_EXPORTS = (
    "IMAPRecordingParser",
    "Literal",
    "ParseError",
    "ParseResult",
    "Pending",
    "Response",
    "parse_recording",
)

__all__ = tuple(_imaplib2_module.__all__) + _RECORDING_EXPORTS


def __getattr__(name):
    if name in _RECORDING_EXPORTS:
        from . import recording

        return getattr(recording, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
