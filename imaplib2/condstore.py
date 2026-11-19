"""RFC 7162 CONDSTORE conditional flag updates on top of :class:`imaplib2.IMAP4`.

The task modelled here is "label a set of messages after archiving, but only
while each message is still at the version that was read earlier".  A MODSEQ
read from a mailbox is only meaningful inside that mailbox's current
UIDVALIDITY domain, and the compare-and-swap must be performed *by the server*
via the ``UNCHANGEDSINCE`` STORE marker.  A client-side "FETCH then STORE"
sequence is not atomic and is therefore never used as a substitute.

Outcomes for every requested UID are reported separately as:

* ``succeeded``  - the conditional STORE was applied;
* ``conflicts``  - the message changed since the read-time MODSEQ (server
  returned ``MODIFIED``), so it was *not* overwritten;
* ``denied``     - the server definitively refused the update (tagged NO/BAD,
  e.g. insufficient rights or a read-only mailbox);
* ``unknown``    - the connection failed before a definitive tagged response,
  so it cannot be told whether the change landed.
"""

from __future__ import absolute_import

import re
from dataclasses import dataclass, field

from .imaplib2 import UNCHANGEDSINCE


# Outcome categories.  Every UID handed to :meth:`ConditionalFlagStore.apply`
# ends up in exactly one of these buckets.
SUCCESS = "succeeded"
CONFLICT = "conflicts"
DENIED = "denied"
UNKNOWN = "unknown"

_UID_RE = re.compile(rb"UID\s+([0-9]+)")
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")
_MODSEQ_RE = re.compile(rb"MODSEQ\s+\(([0-9]+)\)")


class ConditionalStoreError(Exception):
    """Base class for conditional-update guarantee failures."""


class ConditionalStoreUnsupported(ConditionalStoreError):
    """Raised when the server cannot guarantee atomic conditional updates.

    CONDSTORE/QRESYNC is not advertised.  The caller must not emulate the
    guarantee with a separate FETCH followed by an unconditional STORE, since
    another agent may change the message in between the two commands.
    """


class MailboxDomainError(ConditionalStoreError):
    """Raised when a MODSEQ is used outside its UIDVALIDITY domain.

    A MODSEQ identifies a message version only within one mailbox incarnation.
    Reusing it after UIDVALIDITY changed would compare against unrelated
    version numbers, so the request is refused before any write is sent.
    """


@dataclass
class ConditionalStoreResult:
    """Per-UID categorisation of a conditional labelling run."""

    uidvalidity: int
    label: str
    succeeded: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    denied: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)

    def all_uids(self):
        return sorted(
            set(self.succeeded) | set(self.conflicts)
            | set(self.denied) | set(self.unknown)
        )

    def as_dict(self):
        return {
            "uidvalidity": self.uidvalidity,
            "label": self.label,
            SUCCESS: list(self.succeeded),
            CONFLICT: list(self.conflicts),
            DENIED: list(self.denied),
            UNKNOWN: list(self.unknown),
            "reasons": dict(self.reasons),
        }


@dataclass
class _Outcome:
    status: str
    reason: str = ""
    fetch: object = None


class ConditionalFlagStore:
    """Apply a keyword/flag to messages using server-side conditional STORE.

    :param imap: a connected, authenticated :class:`imaplib2.IMAP4` instance
        with the target mailbox selected (preferably via
        ``select(mailbox, condstore=True)``).
    :param max_retries: number of additional passes made for messages that
        reported a version conflict.  Each retry re-queries the server first.
    """

    def __init__(self, imap, max_retries=2):
        self.imap = imap
        self.max_retries = max_retries

    # -- public API ---------------------------------------------------------

    def apply(self, targets, label, condition=None, read_uidvalidity=None):
        """Conditionally add ``label`` to each target message.

        :param targets: mapping of ``UID -> read-time MODSEQ``.  The MODSEQ is
            the message version observed when the business decision was made.
        :param label: IMAP flag/keyword to add, e.g. ``"Archived"``.
        :param condition: optional callable ``condition(uid, info) -> bool``
            invoked after a conflict with freshly fetched ``info`` (a dict with
            ``uid``, ``flags`` (tuple of str) and ``modseq``).  Only messages
            for which it returns true are retried.  When omitted a message is
            retried while it does not already carry ``label``.
        :param read_uidvalidity: UIDVALIDITY of the mailbox incarnation in
            which the read-time MODSEQs were obtained.
        :return: :class:`ConditionalStoreResult`.
        :raises ConditionalStoreUnsupported: the server offers no conditional
            update guarantee.
        :raises MailboxDomainError: ``read_uidvalidity`` does not match the
            currently selected mailbox incarnation.
        """

        self._ensure_condstore()
        self._check_domain(read_uidvalidity)

        flag_token = self._normalise_label(label)
        result = ConditionalStoreResult(
            uidvalidity=self.imap.uidvalidity, label=flag_token
        )

        # uid -> MODSEQ to attempt on the current pass.
        pending = {int(uid): int(modseq) for uid, modseq in targets.items()}

        for attempt in range(self.max_retries + 1):
            conflicts_this_pass = []

            for uid, modseq in list(pending.items()):
                outcome = self._store_one(uid, modseq, flag_token)
                if outcome.status == CONFLICT:
                    conflicts_this_pass.append(uid)
                    result.reasons[uid] = outcome.reason or "modified since read"
                else:
                    self._bucket(result, outcome.status, uid, outcome.reason)

            if not conflicts_this_pass or attempt == self.max_retries:
                for uid in conflicts_this_pass:
                    self._bucket(result, CONFLICT, uid, result.reasons.get(uid, ""))
                break

            # Re-query before retrying and keep only items that still meet the
            # business condition.  The retry uses +FLAGS with a fresh MODSEQ so
            # flags the user set in the meantime are never overwritten.
            pending = self._requalify(conflicts_this_pass, flag_token, condition, result)

        return result

    # -- guarantee / domain checks -----------------------------------------

    def read_versions(self, uids):
        """Capture the current MODSEQ version of each UID and the mailbox domain.

        Returns a ``(uidvalidity, {uid: modseq})`` pair.  The UIDVALIDITY must
        be handed back to :meth:`apply` as ``read_uidvalidity`` so the cached
        MODSEQs are rejected if the mailbox incarnation changes in between."""

        self._ensure_condstore()
        typ, data = self.imap.uid(
            "FETCH", self._uid_set(uids), "(FLAGS MODSEQ)"
        )
        versions = {}
        for info in self._parse_fetch(data):
            if info.get("uid") is not None and info.get("modseq") is not None:
                versions[info["uid"]] = info["modseq"]
        return self.imap.uidvalidity, versions

    def _ensure_condstore(self):
        # RFC 7162: a CONDSTORE/QRESYNC-capable server always maintains MODSEQ
        # and honours UNCHANGEDSINCE, even if the mailbox was not selected with
        # the CONDSTORE parameter.  The capability - not a client FETCH/STORE
        # pair - is therefore what provides the atomicity guarantee.  We never
        # re-select here, as that would disturb the caller's selected state.
        if not self.imap.condstore_supported():
            raise ConditionalStoreUnsupported(
                "server does not advertise CONDSTORE/QRESYNC, so atomic "
                "conditional flag updates cannot be guaranteed; a client-side "
                "FETCH-then-STORE sequence is not an atomic substitute"
            )

    def _check_domain(self, read_uidvalidity):
        current = self.imap.uidvalidity
        if read_uidvalidity is None or current is None:
            return
        if int(read_uidvalidity) != int(current):
            raise MailboxDomainError(
                "read-time UIDVALIDITY %s does not match current mailbox "
                "UIDVALIDITY %s; cached MODSEQs must not be reused across "
                "mailbox incarnations" % (read_uidvalidity, current)
            )

    # -- one conditional STORE ---------------------------------------------

    def _store_one(self, uid, modseq, flag_token):
        marker = "(%s %d)" % (UNCHANGEDSINCE, int(modseq))
        try:
            typ, data = self.imap.uid(
                "STORE", str(uid), marker, "+FLAGS", self._parenthesise(flag_token)
            )
            if typ == "NO":
                return _Outcome(DENIED, self._first_text(data))
            if uid in self._modified_uids():
                return _Outcome(
                    CONFLICT, "MODSEQ advanced past %d" % modseq, fetch=data
                )
            for info in self._parse_fetch(data):
                if info.get("uid") == uid and flag_token.encode() in info["flags"]:
                    return _Outcome(SUCCESS, fetch=data)
            # OK with no MODIFIED marker: the server applied the update.
            return _Outcome(SUCCESS, fetch=data)
        except self.imap.readonly as exc:
            # Definitive: the mailbox is read-only, so the write is refused.
            return _Outcome(DENIED, "read-only mailbox: %s" % exc)
        except self.imap.abort as exc:
            # Connection-level failure: no definitive tagged response, so the
            # write may or may not have happened.
            return _Outcome(UNKNOWN, "connection failure: %s" % exc)
        except self.imap.error as exc:
            # Tagged BAD (or another definitive protocol rejection).
            return _Outcome(DENIED, str(exc))

    def _modified_uids(self):
        typ, dat = self.imap.response("MODIFIED")
        if not dat or dat == [None]:
            return set()
        uids = set()
        for chunk in dat:
            if not chunk:
                continue
            uids |= self._parse_uid_set(bytes(chunk).decode("ascii", "replace"))
        return uids

    # -- conflict re-qualification -----------------------------------------

    def _requalify(self, uids, flag_token, condition, result):
        # Default business condition: keep retrying only while the label has
        # not been applied yet.  Flags are passed to the predicate as str.
        if condition is None:
            condition = lambda uid, info: flag_token not in info["flags"]

        fresh = {}
        try:
            typ, data = self.imap.uid(
                "FETCH", self._uid_set(uids), "(FLAGS MODSEQ)"
            )
        except self.imap.abort as exc:
            for uid in uids:
                self._bucket(result, UNKNOWN, "re-query failed: %s" % exc)
            return fresh
        fetched = {
            info["uid"]: info
            for info in self._parse_fetch(data)
            if info.get("uid") is not None
        }

        for uid in uids:
            info = fetched.get(uid)
            if info is None or info.get("modseq") is None:
                # Message vanished (expunged) or no MODSEQ: state changed, do
                # not touch it.
                self._bucket(result, CONFLICT, uid, "no longer present at re-query")
                continue
            info["flags"] = tuple(
                token.decode("ascii", "replace") for token in info["flags"]
            )
            try:
                still_ok = bool(condition(uid, info))
            except Exception as exc:  # a faulty predicate must not write blindly
                self._bucket(result, CONFLICT, uid, "condition error: %s" % exc)
                continue
            if not still_ok:
                self._bucket(result, CONFLICT, uid, "business condition no longer met")
                continue
            fresh[uid] = int(info["modseq"])
        return fresh

    # -- parsing helpers ----------------------------------------------------

    def _parse_fetch(self, entries):
        infos = []
        for entry in entries or []:
            if isinstance(entry, tuple):
                entry = entry[0]
            if not isinstance(entry, (bytes, bytearray)):
                continue
            line = bytes(entry)
            uid_mo = _UID_RE.search(line)
            flags_mo = _FLAGS_RE.search(line)
            modseq_mo = _MODSEQ_RE.search(line)
            if uid_mo is None and flags_mo is None and modseq_mo is None:
                continue
            flags = tuple(flags_mo.group(1).split()) if flags_mo else tuple()
            infos.append({
                "uid": int(uid_mo.group(1)) if uid_mo else None,
                "flags": flags,
                "modseq": int(modseq_mo.group(1)) if modseq_mo else None,
            })
        return infos

    @staticmethod
    def _parse_uid_set(text):
        uids = set()
        for part in text.replace(" ", "").split(","):
            if not part:
                continue
            if ":" in part:
                lo, hi = part.split(":", 1)
                uids.update(range(int(lo), int(hi) + 1))
            else:
                uids.add(int(part))
        return uids

    @staticmethod
    def _uid_set(uids):
        return ",".join(str(int(uid)) for uid in sorted(set(uids)))

    @staticmethod
    def _normalise_label(label):
        token = label.strip()
        if token.startswith("(") and token.endswith(")"):
            token = token[1:-1].strip()
        return token

    @staticmethod
    def _parenthesise(flag_token):
        return "(%s)" % flag_token

    @staticmethod
    def _first_text(data):
        if not data:
            return "server refused the update"
        first = data[0]
        if isinstance(first, tuple):
            first = first[0]
        return bytes(first).decode("ascii", "replace") if first else "server refused"

    @staticmethod
    def _bucket(result, status, uid, reason):
        getattr(result, status).append(uid)
        if reason:
            result.reasons[uid] = reason
        else:
            # A later terminal outcome (e.g. success after a transient conflict)
            # must not leave a stale note from an earlier pass behind.
            result.reasons.pop(uid, None)


def conditional_label(imap, targets, label, condition=None,
                      read_uidvalidity=None, max_retries=2):
    """Convenience wrapper around :class:`ConditionalFlagStore`.

    See :meth:`ConditionalFlagStore.apply` for parameter semantics.
    """

    return ConditionalFlagStore(imap, max_retries=max_retries).apply(
        targets,
        label,
        condition=condition,
        read_uidvalidity=read_uidvalidity,
    )
