"""Replay multi-session FTP captures and attribute data connections.

The package reads classic pcap and pcapng files, reassembles TCP
streams, parses FTP control channels and links every data connection
back to the control session and transfer that negotiated it.

Nothing is guessed silently: when the evidence does not single out one
session, the link is reported as a candidate and its bytes stay out of
every session total.
"""

__version__ = "1.0.0"

from .correlate import analyze  # noqa: E402
from .correlate import analyze_file  # noqa: E402

__all__ = ["analyze", "analyze_file"]
