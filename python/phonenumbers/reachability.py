"""Contact reachability records with point-in-time conclusions.

A contact's warehouse record must not collapse reachability to a single
boolean.  Three independent bodies of evidence are stored separately:

  * length possibility -- whether the number length is possible according to
    the metadata library (a ValidationResult, never just true/false)
  * metadata validity -- whether the number matches a valid metadata pattern
  * business receipts -- out-of-band acknowledgements supplied by the contact

Business receipts carry their own contact-point scope (a full number versus a
specific extension), an effective window and explicit version information.
The following rules are enforced when a conclusion is drawn:

  * A switchboard-level receipt never proves that a particular extension is
    reachable; scope is matched exactly.
  * Receipts belonging to a previous holder do not survive a number transfer:
    holder epochs are delimited by transfer events, and only receipts from the
    epoch active at the query instant count.
  * When competing receipts cannot be adjudicated by comparable versions, the
    conflict is preserved instead of being settled by arrival (insertion)
    order.
  * Length and metadata-pattern evidence can only rule a contact point out.
    A valid pattern does not prove that anyone is actually reachable (the
    metadata library never verifies that a number is in use); a positive
    reachable conclusion requires a decisive business receipt.
  * A metadata library upgrade only changes how future assessments understand
    number formats.  Historical assessments and receipts are immutable facts
    and are never rewritten; point-in-time queries only use evidence recorded
    at or before the queried instant.
"""

# Copyright (C) 2026 The python-phonenumbers Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime

from .util import ImmutableMixin, mutating_method
from .phonenumberutil import (ValidationResult,
                              is_possible_number_with_reason,
                              is_valid_number, number_type,
                              region_code_for_number)
from .phonenumberutil import PhoneNumberFormat, format_number

__all__ = ['ReachabilityStatus', 'ReceiptOutcome', 'ContactScope',
           'MetadataAssessment', 'BusinessReceipt', 'ReachabilityConclusion',
           'ContactRecord', 'ReachabilityRegistry',
           'contact_point_for_number', 'length_possibility',
           'length_is_possible']


# ValidationResult values that rule the number out on length grounds.
_IMPOSSIBLE_LENGTH_RESULTS = frozenset((
    ValidationResult.INVALID_COUNTRY_CODE,
    ValidationResult.TOO_SHORT,
    ValidationResult.INVALID_LENGTH,
    ValidationResult.TOO_LONG,
))


class ReachabilityStatus(object):
    """Overall reachability conclusion for a contact at a given instant."""
    # Current evidence establishes that the contact is reachable.
    REACHABLE = "REACHABLE"
    # Current evidence establishes that the contact is not reachable.
    NOT_REACHABLE = "NOT_REACHABLE"
    # Evidence exists but is insufficient to settle the question.
    UNKNOWN = "UNKNOWN"
    # Competing receipts disagree and cannot be adjudicated by version.
    CONFLICT = "CONFLICT"
    # No evidence whatsoever is available at the queried instant.
    NO_EVIDENCE = "NO_EVIDENCE"


class ReceiptOutcome(object):
    """What a business receipt asserts about a contact point."""
    REACHABLE = "REACHABLE"
    NOT_REACHABLE = "NOT_REACHABLE"
    UNKNOWN = "UNKNOWN"

    _ALL = frozenset((REACHABLE, NOT_REACHABLE, UNKNOWN))


def contact_point_for_number(numobj, extension=None):
    """Return the canonical (contact point, extension) scope for a number.

    The contact point is the E.164 representation of the number, so receipts
    are attached to a stable dialable identity rather than raw user input.

    Arguments:
    numobj -- The parsed PhoneNumber object.
    extension -- An optional extension string; a receipt for an extension is
                 scoped to that extension and never proves the switchboard or
                 any other extension.
    """
    contact_point = format_number(numobj, PhoneNumberFormat.E164)
    return ContactScope(contact_point, extension)


def length_possibility(numobj):
    """Return the length possibility (a ValidationResult) for a number.

    This is deliberately richer than a boolean: TOO_SHORT, TOO_LONG,
    INVALID_LENGTH and INVALID_COUNTRY_CODE carry different meanings and are
    retained separately from metadata validity.
    """
    return is_possible_number_with_reason(numobj)


def length_is_possible(length_result):
    """Whether a length possibility result counts as a possible number."""
    return length_result not in _IMPOSSIBLE_LENGTH_RESULTS


class ContactScope(ImmutableMixin):
    """The exact scope a piece of evidence applies to.

    A None extension denotes the full number (e.g. a switchboard).  Scopes are
    matched exactly: evidence for the switchboard does not cover an extension
    and evidence for one extension does not cover another extension.
    """

    @mutating_method
    def __init__(self, contact_point, extension=None):
        if not contact_point:
            raise ValueError("contact_point is required")
        self.contact_point = contact_point
        self.extension = extension

    def __eq__(self, other):
        if not isinstance(other, ContactScope):
            return NotImplemented
        return (self.contact_point == other.contact_point and
                self.extension == other.extension)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.contact_point, self.extension))

    def __repr__(self):
        if self.extension is None:
            return "ContactScope(%r)" % self.contact_point
        return "ContactScope(%r, %r)" % (self.contact_point, self.extension)


class MetadataAssessment(ImmutableMixin):
    """A point-in-time assessment of metadata validity.

    The library version is recorded alongside the verdict so that, after a
    metadata library upgrade, historical verdicts remain attributable to the
    library that produced them.
    """

    @mutating_method
    def __init__(self, valid, region, number_type, library_version, assessed_at):
        self.valid = bool(valid)
        self.region = region
        self.number_type = number_type
        self.library_version = library_version
        if not isinstance(assessed_at, datetime.datetime):
            raise TypeError("assessed_at must be a datetime")
        self.assessed_at = assessed_at

    def __repr__(self):
        return ("MetadataAssessment(valid=%r, region=%r, number_type=%r, "
                "library_version=%r, assessed_at=%r)" %
                (self.valid, self.region, self.number_type,
                 self.library_version, self.assessed_at))


class BusinessReceipt(ImmutableMixin):
    """An out-of-band business acknowledgement for a contact point.

    A receipt is an immutable historical fact.  It asserts an outcome for one
    exact ContactScope, is only valid inside its effective window and carries
    an explicit comparable version so that newer receipts can withdraw older
    ones.  Insertion order is never used to adjudicate disagreements.

    Arguments:
    scope -- The ContactScope this receipt applies to.
    outcome -- One of the ReceiptOutcome values.
    version -- A comparable version token (e.g. a monotonically increasing
               integer); receipts without comparable versions cannot override
               each other.
    effective_from -- Start of the window (inclusive) in which the receipt
                      applies.
    effective_to -- Optional end of the window (exclusive); None means open.
    supersedes_version -- Optional version of the receipt withdrawn by this
                          one.  Withdrawal only takes effect while this receipt
                          is itself effective.
    version_namespace -- Optional namespace for the version token; receipts in
                         different namespaces are not comparable.
    holder_epoch -- Holder epoch this receipt belongs to; stamped by the
                    ContactRecord when the receipt is added.
    library_version -- Metadata library version current when the receipt was
                       recorded, kept for provenance only.
    recorded_at -- Optional observation timestamp; defaults to
                   effective_from.
    notes -- Optional free-text explanation.
    """

    @mutating_method
    def __init__(self, scope, outcome, version, effective_from,
                 effective_to=None, supersedes_version=None,
                 version_namespace=None, holder_epoch=0,
                 library_version=None, recorded_at=None, notes=None):
        if not isinstance(scope, ContactScope):
            raise TypeError("scope must be a ContactScope")
        if outcome not in ReceiptOutcome._ALL:
            raise ValueError("unknown receipt outcome: %r" % (outcome,))
        if version is None:
            raise ValueError("receipt version is required for comparability")
        if not isinstance(effective_from, datetime.datetime):
            raise TypeError("effective_from must be a datetime")
        if (effective_to is not None and
                not isinstance(effective_to, datetime.datetime)):
            raise TypeError("effective_to must be a datetime or None")
        if effective_to is not None and effective_to <= effective_from:
            raise ValueError("effective_to must be later than effective_from")
        if supersedes_version is not None and supersedes_version == version:
            raise ValueError("a receipt cannot supersede its own version")
        self.scope = scope
        self.outcome = outcome
        self.version = version
        self.effective_from = effective_from
        self.effective_to = effective_to
        self.supersedes_version = supersedes_version
        self.version_namespace = version_namespace
        self.holder_epoch = holder_epoch
        self.library_version = library_version
        self.recorded_at = (recorded_at if recorded_at is not None
                            else effective_from)
        self.notes = notes

    def effective_at(self, at):
        """Whether the receipt's window covers the given instant."""
        if at < self.effective_from:
            return False
        if self.effective_to is not None and at >= self.effective_to:
            return False
        return True

    def _comparable_with(self, other):
        """Whether two receipts' versions can be ordered against each other."""
        if self.version_namespace != other.version_namespace:
            return False
        try:
            ordered = (self.version < other.version or
                       self.version >= other.version)
        except TypeError:
            return False
        return ordered

    def __repr__(self):
        return ("BusinessReceipt(scope=%r, outcome=%r, version=%r, "
                "effective_from=%r, effective_to=%r, holder_epoch=%r)" %
                (self.scope, self.outcome, self.version,
                 self.effective_from, self.effective_to, self.holder_epoch))


class ReachabilityConclusion(ImmutableMixin):
    """The reachability conclusion for one scope at one instant.

    The three evidence kinds remain individually visible instead of being
    flattened into a single boolean.  When receipts conflict, status is
    ReachabilityStatus.CONFLICT and both sides are retained.
    """

    @mutating_method
    def __init__(self, scope, at, status, length_result=None,
                 metadata_valid=None, receipts=None, withdrawn_receipts=None,
                 conflicting_receipts=None, reasons=None):
        self.scope = scope
        self.at = at
        self.status = status
        self.length_result = length_result
        self.metadata_valid = metadata_valid
        self.receipts = tuple(receipts or ())
        self.withdrawn_receipts = tuple(withdrawn_receipts or ())
        self.conflicting_receipts = tuple(conflicting_receipts or ())
        self.reasons = tuple(reasons or ())

    def __repr__(self):
        return ("ReachabilityConclusion(scope=%r, at=%r, status=%r)" %
                (self.scope, self.at, self.status))


class ContactRecord(object):
    """Warehouse record for one canonical contact point (E.164 number).

    Holds the timestamped history of length assessments, metadata validity
    assessments and business receipts, plus the holder-transfer timeline.
    Nothing here is ever overwritten: an upgrade or a new assessment appends
    history, and point-in-time queries pick the latest entry not later than
    the queried instant.
    """

    def __init__(self, contact_point):
        if not contact_point:
            raise ValueError("contact_point is required")
        self.contact_point = contact_point
        # Tuples of (assessed_at, ValidationResult, library_version)
        self._length_history = []
        # MetadataAssessment instances
        self._metadata_history = []
        # BusinessReceipt instances
        self._receipts = []
        # Timestamps at which the number changed holder
        self._transfers = []

    def record_length(self, assessed_at, numobj, library_version):
        result = length_possibility(numobj)
        self._length_history.append((assessed_at, result, library_version))
        return result

    def record_metadata(self, assessed_at, numobj, library_version):
        valid = is_valid_number(numobj)
        assessment = MetadataAssessment(
            valid=valid,
            region=region_code_for_number(numobj),
            number_type=number_type(numobj),
            library_version=library_version,
            assessed_at=assessed_at)
        self._metadata_history.append(assessment)
        return assessment

    def mark_transfer(self, at):
        """Record that the number changed holder at the given instant.

        Receipts from the previous holder epoch do not apply afterwards even
        if their own effective windows are still open.  Transfer history must
        be recorded before receipts that belong to a later epoch, so a
        transfer earlier than an already recorded receipt is rejected rather
        than silently mis-assigning the receipt's holder epoch.
        """
        if not isinstance(at, datetime.datetime):
            raise TypeError("transfer time must be a datetime")
        if self._transfers and at < self._transfers[-1]:
            raise ValueError("transfers must be recorded in chronological order")
        for receipt in self._receipts:
            if receipt.effective_from >= at:
                raise ValueError(
                    "transfer at %r would move existing receipt (version %r) "
                    "into a different holder epoch; record transfers first" %
                    (at, receipt.version))
        self._transfers.append(at)

    def add_receipt(self, receipt):
        if receipt.scope.contact_point != self.contact_point:
            raise ValueError("receipt contact point %r does not match record %r" %
                             (receipt.scope.contact_point, self.contact_point))
        epoch = self._epoch_at(receipt.effective_from)
        # Receipts are immutable; the holder epoch is stamped via a guarded
        # copy rather than mutating the caller's object.
        stamped = BusinessReceipt(
            scope=receipt.scope,
            outcome=receipt.outcome,
            version=receipt.version,
            effective_from=receipt.effective_from,
            effective_to=receipt.effective_to,
            supersedes_version=receipt.supersedes_version,
            version_namespace=receipt.version_namespace,
            holder_epoch=epoch,
            library_version=receipt.library_version,
            recorded_at=receipt.recorded_at,
            notes=receipt.notes)
        self._receipts.append(stamped)
        return stamped

    @property
    def receipts(self):
        return list(self._receipts)

    @property
    def transfers(self):
        return list(self._transfers)

    def latest_length(self, at):
        """Latest length assessment recorded at or before at."""
        result = None
        for entry in self._length_history:
            assessed_at = entry[0]
            if assessed_at <= at and (result is None or
                                      assessed_at >= result[0]):
                result = entry
        return result

    def latest_metadata(self, at):
        """Latest metadata assessment recorded at or before at."""
        result = None
        for assessment in self._metadata_history:
            if assessment.assessed_at <= at and (
                    result is None or
                    assessment.assessed_at >= result.assessed_at):
                result = assessment
        return result

    def _epoch_at(self, at):
        """Index of the holder epoch active at the given instant."""
        epoch = 0
        for transfer_at in self._transfers:
            if transfer_at <= at:
                epoch += 1
            else:
                break
        return epoch

    def effective_receipts(self, at, extension):
        """Receipts in scope, in the active holder epoch and known at at.

        A receipt must have been recorded (recorded_at) at or before the query
        instant and be inside its effective window at that instant.  Scope is
        matched exactly: a switchboard receipt (extension None) does not count
        for an extension query and vice versa.
        """
        epoch = self._epoch_at(at)
        candidates = []
        for receipt in self._receipts:
            if receipt.recorded_at > at:
                continue
            if receipt.holder_epoch != epoch:
                continue
            if receipt.scope.extension != extension:
                continue
            if not receipt.effective_at(at):
                continue
            candidates.append(receipt)
        return candidates


def _withdrawn_versions(candidates):
    """Versions withdrawn by effective receipts, following supersede chains.

    Withdrawal only links receipts that share a version namespace, so a
    version number reused in another namespace is never touched.
    """
    by_version = dict(((receipt.version_namespace, receipt.version), receipt)
                      for receipt in candidates)
    withdrawn = set()
    for receipt in candidates:
        target_key = (receipt.version_namespace, receipt.supersedes_version)
        # Walk the chain; every superseded version in an effective chain is
        # considered withdrawn at the queried instant.
        while (receipt.supersedes_version is not None and
               target_key in by_version and target_key not in withdrawn):
            withdrawn.add(target_key)
            target = by_version[target_key]
            target_key = (target.version_namespace, target.supersedes_version)
    return withdrawn


def _adjudicate(receipts):
    """Settle competing receipts by comparable version, or retain a conflict.

    Returns (outcome or None, conflicting receipts).  Insertion order is never
    consulted: if versions cannot be compared, or several receipts share the
    maximum version with different outcomes, the conflict is preserved.
    """
    outcomes = set(receipt.outcome for receipt in receipts)
    if len(outcomes) == 1:
        return receipts[0].outcome, ()
    for index, receipt in enumerate(receipts):
        for other in receipts[index + 1:]:
            if not receipt._comparable_with(other):
                return None, tuple(receipts)
    maximum = max(receipts, key=lambda receipt: receipt.version)
    winners = [receipt for receipt in receipts
               if receipt.version == maximum.version]
    winner_outcomes = set(receipt.outcome for receipt in winners)
    if len(winner_outcomes) == 1:
        return maximum.outcome, ()
    return None, tuple(receipts)


class ReachabilityRegistry(object):
    """Holds ContactRecords and answers point-in-time reachability queries.

    The current metadata library version is tracked for provenance.  Upgrading
    the library only changes how subsequently recorded assessments understand
    number formats; it never rewrites historical assessments or receipts.
    """

    def __init__(self, library_version):
        if library_version is None:
            raise ValueError("library_version is required")
        self.library_version = library_version
        self._records = {}

    def upgrade_library(self, new_version):
        """Move to a new metadata library version without touching history."""
        if new_version is None:
            raise ValueError("new_version is required")
        self.library_version = new_version

    def record_for(self, contact_point):
        """Return the ContactRecord for a contact point, creating it if needed."""
        record = self._records.get(contact_point)
        if record is None:
            record = ContactRecord(contact_point)
            self._records[contact_point] = record
        return record

    def record_length(self, scope, assessed_at, numobj, library_version=None):
        record = self.record_for(scope.contact_point)
        return record.record_length(
            assessed_at, numobj,
            library_version if library_version is not None
            else self.library_version)

    def record_metadata(self, scope, assessed_at, numobj, library_version=None):
        record = self.record_for(scope.contact_point)
        return record.record_metadata(
            assessed_at, numobj,
            library_version if library_version is not None
            else self.library_version)

    def add_receipt(self, receipt):
        record = self.record_for(receipt.scope.contact_point)
        if receipt.library_version is None:
            receipt = BusinessReceipt(
                scope=receipt.scope,
                outcome=receipt.outcome,
                version=receipt.version,
                effective_from=receipt.effective_from,
                effective_to=receipt.effective_to,
                supersedes_version=receipt.supersedes_version,
                version_namespace=receipt.version_namespace,
                holder_epoch=receipt.holder_epoch,
                library_version=self.library_version,
                recorded_at=receipt.recorded_at,
                notes=receipt.notes)
        return record.add_receipt(receipt)

    def mark_transfer(self, contact_point, at):
        self.record_for(contact_point).mark_transfer(at)

    def conclusion_for(self, scope, at):
        """Return the reachability conclusion for scope at instant at.

        Only evidence recorded at or before at is considered, so conclusions
        about past instants are unaffected by later library upgrades or later
        receipts.
        """
        if not isinstance(scope, ContactScope):
            raise TypeError("scope must be a ContactScope")
        if not isinstance(at, datetime.datetime):
            raise TypeError("at must be a datetime")
        record = self._records.get(scope.contact_point)
        if record is None:
            return ReachabilityConclusion(
                scope, at, ReachabilityStatus.NO_EVIDENCE,
                reasons=["no record for contact point %r" % scope.contact_point])

        reasons = []
        length_entry = record.latest_length(at)
        length_result = None
        if length_entry is not None:
            length_result = length_entry[1]

        metadata = record.latest_metadata(at)
        metadata_valid = metadata.valid if metadata is not None else None

        candidates = record.effective_receipts(at, scope.extension)
        withdrawn_keys = _withdrawn_versions(candidates)
        withdrawn = [receipt for receipt in candidates
                     if (receipt.version_namespace, receipt.version) in
                     withdrawn_keys]
        live = [receipt for receipt in candidates
                if (receipt.version_namespace, receipt.version) not in
                withdrawn_keys]
        if withdrawn:
            reasons.append("withdrawn receipt versions: %s" %
                           ", ".join(str(receipt.version) for receipt in withdrawn))

        receipt_outcome = None
        conflicts = ()
        if live:
            receipt_outcome, conflicts = _adjudicate(live)
            if conflicts:
                reasons.append(
                    "competing receipts with incomparable or tied versions; "
                    "arrival order is not used to adjudicate")

        status = self._combine(length_result, metadata_valid,
                               live, receipt_outcome, conflicts, reasons)
        return ReachabilityConclusion(
            scope, at, status,
            length_result=length_result,
            metadata_valid=metadata_valid,
            receipts=live,
            withdrawn_receipts=withdrawn,
            conflicting_receipts=conflicts,
            reasons=reasons)

    @staticmethod
    def _combine(length_result, metadata_valid, live,
                 receipt_outcome, conflicts, reasons):
        if conflicts:
            return ReachabilityStatus.CONFLICT
        if receipt_outcome == ReceiptOutcome.REACHABLE:
            if length_result is not None and not length_is_possible(length_result):
                reasons.append("business receipt says reachable but number "
                               "length is not possible according to metadata")
            if metadata_valid is False:
                reasons.append("business receipt says reachable but metadata "
                               "pattern is invalid")
            return ReachabilityStatus.REACHABLE
        if receipt_outcome == ReceiptOutcome.NOT_REACHABLE:
            return ReachabilityStatus.NOT_REACHABLE
        # No decisive business receipt (none recorded, an explicit UNKNOWN, or
        # all withdrawn).  Format-level evidence can rule a contact point out,
        # but a valid pattern cannot prove that anyone is actually reachable:
        # the metadata library does not verify that a number is in use.  In
        # particular this keeps a switchboard receipt (or the base number's
        # valid pattern) from establishing extension reachability, and keeps
        # pre-transfer evidence from establishing the new holder's
        # reachability after a number transfer.
        if length_result is not None and not length_is_possible(length_result):
            reasons.append("no decisive business receipt; number length is "
                           "not possible")
            return ReachabilityStatus.NOT_REACHABLE
        if metadata_valid is False:
            reasons.append("no decisive business receipt; metadata pattern is "
                           "invalid")
            return ReachabilityStatus.NOT_REACHABLE
        if live or length_result is not None or metadata_valid is not None:
            reasons.append("no decisive business receipt; format-level "
                           "evidence alone cannot establish reachability")
            return ReachabilityStatus.UNKNOWN
        return ReachabilityStatus.NO_EVIDENCE


if __name__ == '__main__':  # pragma no cover
    import doctest
    doctest.testmod()
