#!/usr/bin/env python3

"""An FTP server implementing two-phase data handoff for uploads.

Every STOR is received into a temporary file managed by a HandoffStore
and is atomically renamed ("published") only after the transfer
completes and the payload is verified.  See pyftpdlib.handoff for the
full set of guarantees.

Client workflow (use binary mode, "TYPE I"):

- STOR name           start a new upload session (temp file first)
- XSHA <hex-digest>   optional: declare the expected SHA-256 of the
                      next upload; publishing fails on mismatch
- REST n + STOR name  resume an interrupted upload of the same name
                      from byte offset n (the half-received temp file
                      never overwrites the previously published file)
- XSTA name           query the resumable offset for a pending upload

Only when payload verification and the publish rename both succeed
does the file appear in the store's official manifest.  Repeating a
publish is idempotent, and a same-name re-upload gets a fresh upload
identity, so it can never claim a previous session's receipt.
"""

import os

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handoff import ChecksumMismatchError
from pyftpdlib.handoff import HandoffStore
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer


class HandoffFTPHandler(FTPHandler):
    """FTPHandler routing STOR through a HandoffStore."""

    store: HandoffStore = None  # set before starting the server

    proto_cmds = {
        **FTPHandler.proto_cmds,
        "XSHA": dict(
            perm=None, auth=True, arg=True,
            help="Syntax: XSHA <SP> sha256 (expected digest of next upload)",
        ),
        "XSTA": dict(
            perm=None, auth=True, arg=True,
            help="Syntax: XSTA <SP> name (resumable offset of pending upload)",
        ),
    }

    def _handoff_map(self):
        # ftp path of temp file -> (upload_id, expected sha256 or None)
        if not hasattr(self, "_handoff_uploads"):
            self._handoff_uploads = {}
        return self._handoff_uploads

    # -- custom commands ------------------------------------------------

    def ftp_XSHA(self, line):
        """Declare the expected SHA-256 digest for the next upload."""
        digest = line.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef"
                                    for c in digest):
            self.respond("501 Invalid SHA-256 digest.")
            return
        self._handoff_expected_sha256 = digest
        self.respond("250 Digest registered for next upload.")

    def ftp_XSTA(self, path):
        """Report the resumable offset of a pending upload."""
        name = os.path.basename(self.fs.ftp2fs(path))
        upload_id = self.store.find_resumable(name)
        if upload_id is None:
            self.respond("550 No pending upload for this name.")
        else:
            offset = self.store.offset(upload_id)
            self.respond(f"213 {offset}")

    # -- STOR redirection -------------------------------------------------

    def ftp_STOR(self, file, mode="w"):
        name = os.path.basename(file)
        resume = "a" in mode or self._restart_position
        upload_id = self.store.find_resumable(name) if resume else None
        if upload_id is None:
            if self._restart_position:
                # client asked to resume but we have nothing to resume
                self._restart_position = 0
                self.respond("554 No resumable upload for this name.")
                return
            upload_id = self.store.begin_upload(name)
        temp = self.store.temp_path(upload_id)
        expected = getattr(self, "_handoff_expected_sha256", None)
        self._handoff_expected_sha256 = None
        self._handoff_map()[temp] = (upload_id, expected)
        return super().ftp_STOR(temp, mode)

    # -- publish / resume callbacks ----------------------------------------

    def on_file_received(self, file):
        upload_id, expected = self._handoff_map().pop(file, (None, None))
        if upload_id is None:
            return
        try:
            receipt = self.store.publish(upload_id, sha256=expected)
        except ChecksumMismatchError as err:
            self.log(f"handoff rejected: {err}")
        else:
            self.log(
                f"handoff published {receipt.name!r} "
                f"({receipt.size} bytes, sha256={receipt.sha256})"
            )

    def on_incomplete_file_received(self, file):
        upload_id, _ = self._handoff_map().pop(file, (None, None))
        if upload_id is None:
            return
        offset = self.store.offset(upload_id)
        self.log(
            f"handoff interrupted at {offset} bytes; "
            f"resume with: REST {offset} + STOR"
        )


def main():
    authorizer = DummyAuthorizer()
    authorizer.add_user("user", "12345", os.getcwd(), perm="elradfmwMT")
    authorizer.add_anonymous(os.getcwd())

    handler = HandoffFTPHandler
    handler.authorizer = authorizer
    handler.store = HandoffStore(os.path.join(os.getcwd(), "handoff-store"))
    handler.banner = "pyftpdlib two-phase handoff ftpd ready."
    handler.passive_ports = range(60000, 65535)

    server = FTPServer(("", 2121), handler)
    server.max_cons = 256
    server.max_cons_per_ip = 5
    server.serve_forever()


if __name__ == "__main__":
    main()
