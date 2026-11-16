Resumable File Retrieval
========================

The resumable retrieval API reliably downloads a remote file to a local path and
can continue automatically after a connection drop, instead of restarting from
byte zero. It is available through
:meth:`smb.SMBConnection.SMBConnection.retrieveFileResume` (blocking) and
:meth:`smb.SMBProtocol.SMBProtocolFactory.retrieveFileResume` (Twisted).
Both require an SMB2 connection; SMB1 raises
:class:`smb.smb_structs.UnsupportedFeature`.

On-disk layout
--------------

For a destination ``local_path`` the implementation maintains three paths:

* ``local_path.part`` -- the partial fragment containing only bytes that were
  acknowledged by the server and flushed (``fsync``) to disk;
* ``local_path.smbresume`` -- a JSON resume token, rewritten atomically after
  every confirmed read;
* ``local_path.archive/`` -- fragments that can no longer be proven safe to
  reuse, moved here with a ``.reason`` marker. They are never appended to or
  spliced into a new download.

The resume token records:

* **file identity** -- the share name, the normalized remote path, and (when the
  server grants one) the SMB2 durable create GUID and persistent file id;
* **version evidence** -- the remote file size and its last-write/change
  timestamps;
* **the confirmed range** -- ``confirmed_offset``, the number of trustworthy
  bytes already present in the fragment.

Recovery modes
--------------

The two recovery paths are handled separately.

* **Persistent handle recovery.** When the token holds a durable handle and the
  negotiated dialect is SMB 2.1 or newer, the client sends an SMB2 durable
  reconnect (``DHnQ``) to reattach to the server-side open, then verifies the
  current file size before continuing from ``confirmed_offset``.
* **Path recovery.** If no durable handle exists, the dialect is older than 2.1,
  or the reconnect is refused, the file is reopened by path. The fragment is
  reused only when the server still matches the recorded size and timestamps.
  A refused durable reconnect downgrades the plan to path recovery and clears
  the durable ids from the token.

When the version cannot be confirmed -- the file changed, was replaced at the
same path, the fragment is missing or truncated, or the token lacks evidence --
the old fragment is archived and the full file is re-read. Nothing is ever
written into a fragment whose version cannot be established.

Reconnecting after a process restart
-------------------------------------

Because the token and fragment are durable on disk, a download can resume in a
new process or with a new :class:`smb.SMBConnection.SMBConnection` instance.
Simply call ``retrieveFileResume`` again with the same *service_name*, *path*
and *local_path*. If the remote path was replaced while disconnected, the plan
detects the version mismatch, archives the old fragment, and reports the byte
ranges that must be re-read rather than silently mixing the two versions.

Errors
------

A failed read or unverifiable reopen raises :class:`smb.resume.ResumeError`
without deleting the fragment or token, so the next call can retry. Repeated
resumable calls are idempotent as long as the remote file keeps matching its
recorded version evidence.

.. automodule:: smb.resume
    :members:
    :undoc-members:
