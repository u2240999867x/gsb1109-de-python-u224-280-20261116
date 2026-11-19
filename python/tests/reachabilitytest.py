#!/usr/bin/env python
"""Unit tests for reachability.py

These tests pin down the rules for storing contact reachability as three
separate bodies of evidence (length possibility, metadata validity and
business receipts) rather than a single boolean, and for drawing
point-in-time conclusions from that evidence.
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
import unittest

import phonenumbers
from phonenumbers import ValidationResult
from phonenumbers.reachability import BusinessReceipt, ContactRecord
from phonenumbers.reachability import ContactScope, ReachabilityRegistry
from phonenumbers.reachability import ReachabilityStatus, ReceiptOutcome
from phonenumbers.reachability import contact_point_for_number
from phonenumbers.reachability import length_is_possible, length_possibility


def dt(year, month=1, day=1, hour=0):
    return datetime.datetime(year, month, day, hour)


class ReachabilityTest(unittest.TestCase):
    def setUp(self):
        self.registry = ReachabilityRegistry("lib-1.0")
        self.number = phonenumbers.parse("+442083661177", None)
        self.short_number = phonenumbers.parse("+1650253", None)
        self.scope = contact_point_for_number(self.number)
        self.ext_scope = contact_point_for_number(self.number, "1234")

    # -- separate storage of the three evidence kinds --------------------

    def test_three_evidence_kinds_are_stored_separately(self):
        # Nothing recorded yet: no single boolean has been invented.
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 1))
        self.assertEqual(ReachabilityStatus.NO_EVIDENCE, conclusion.status)
        self.assertIsNone(conclusion.length_result)
        self.assertIsNone(conclusion.metadata_valid)
        self.assertEqual((), conclusion.receipts)

        length_result = self.registry.record_length(
            self.scope, dt(2026, 1, 2), self.number, "lib-1.0")
        self.assertEqual(ValidationResult.IS_POSSIBLE, length_result)
        assessment = self.registry.record_metadata(
            self.scope, dt(2026, 1, 2), self.number, "lib-1.0")
        self.assertTrue(assessment.valid)

        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 3))
        self.assertEqual(ValidationResult.IS_POSSIBLE,
                         conclusion.length_result)
        self.assertTrue(conclusion.metadata_valid)

    def test_length_possibility_keeps_reason_not_boolean(self):
        result = length_possibility(self.short_number)
        self.assertEqual(ValidationResult.TOO_SHORT, result)
        self.assertFalse(length_is_possible(result))
        self.assertTrue(length_is_possible(
            length_possibility(self.number)))

    def test_impossible_length_and_invalid_metadata_rule_number_out(self):
        self.registry.record_length(
            self.scope, dt(2026, 1, 2), self.short_number, "lib-1.0")
        self.registry.record_metadata(
            self.scope, dt(2026, 1, 2), self.short_number, "lib-1.0")
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 3))
        self.assertEqual(ReachabilityStatus.NOT_REACHABLE, conclusion.status)
        self.assertEqual(ValidationResult.TOO_SHORT,
                         conclusion.length_result)
        self.assertFalse(conclusion.metadata_valid)

    # -- point-in-time semantics ------------------------------------------

    def test_conclusion_only_uses_evidence_known_at_query_instant(self):
        receipt = BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 2, 1),
            effective_to=dt(2026, 3, 1))
        self.registry.add_receipt(receipt)

        before = self.registry.conclusion_for(self.scope, dt(2026, 1, 15))
        self.assertEqual(ReachabilityStatus.NO_EVIDENCE, before.status)

        during = self.registry.conclusion_for(self.scope, dt(2026, 2, 15))
        self.assertEqual(ReachabilityStatus.REACHABLE, during.status)

        after = self.registry.conclusion_for(self.scope, dt(2026, 3, 1))
        self.assertNotEqual(ReachabilityStatus.REACHABLE, after.status)

    def test_future_assessments_do_not_affect_past_queries(self):
        self.registry.record_metadata(
            self.scope, dt(2026, 5, 1), self.number, "lib-2.0")
        past = self.registry.conclusion_for(self.scope, dt(2026, 1, 1))
        self.assertEqual(ReachabilityStatus.NO_EVIDENCE, past.status)

    def test_late_recorded_receipt_is_invisible_before_recording_instant(self):
        # The window opens in February, but the fact is only known (recorded)
        # in April; March queries must not see it.
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 2, 1),
            recorded_at=dt(2026, 4, 1)))
        self.assertEqual(
            ReachabilityStatus.NO_EVIDENCE,
            self.registry.conclusion_for(self.scope, dt(2026, 3, 1)).status)
        self.assertEqual(
            ReachabilityStatus.REACHABLE,
            self.registry.conclusion_for(self.scope, dt(2026, 4, 2)).status)

    def test_latest_assessment_picked_by_timestamp_not_insertion_order(self):
        record = self.registry.record_for(self.scope.contact_point)
        # Record the later assessment first, then an earlier one.
        record.record_metadata(dt(2026, 5, 1), self.number, "lib-2.0")
        record.record_metadata(dt(2026, 1, 1), self.short_number, "lib-1.0")
        self.assertEqual(
            "lib-1.0",
            record.latest_metadata(dt(2026, 2, 1)).library_version)
        self.assertEqual(
            "lib-2.0",
            record.latest_metadata(dt(2026, 6, 1)).library_version)

    # -- scope: switchboard does not prove extension ----------------------

    def test_valid_metadata_alone_does_not_establish_reachability(self):
        # The metadata library verifies patterns, not whether a number is in
        # use; a format-valid number with no business receipt stays UNKNOWN.
        self.registry.record_length(
            self.scope, dt(2026, 1, 2), self.number, "lib-1.0")
        self.registry.record_metadata(
            self.scope, dt(2026, 1, 2), self.number, "lib-1.0")
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 3))
        self.assertEqual(ReachabilityStatus.UNKNOWN, conclusion.status)
        self.assertTrue(conclusion.metadata_valid)
        self.assertEqual((), conclusion.receipts)

    def test_switchboard_receipt_does_not_prove_extension(self):
        self.registry.record_metadata(
            self.scope, dt(2026, 1, 1), self.number, "lib-1.0")
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1)))
        switchboard = self.registry.conclusion_for(self.scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.REACHABLE, switchboard.status)

        # The base number's valid pattern and the switchboard receipt neither
        # establish that extension 1234 is reachable.
        extension = self.registry.conclusion_for(self.ext_scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.UNKNOWN, extension.status)
        self.assertTrue(extension.metadata_valid)
        self.assertEqual((), extension.receipts)

    def test_extension_receipt_does_not_prove_other_scope(self):
        self.registry.add_receipt(BusinessReceipt(
            self.ext_scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1)))
        extension = self.registry.conclusion_for(self.ext_scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.REACHABLE, extension.status)
        switchboard = self.registry.conclusion_for(self.scope, dt(2026, 1, 2))
        self.assertNotEqual(ReachabilityStatus.REACHABLE, switchboard.status)

        other_ext = contact_point_for_number(self.number, "9999")
        self.assertEqual(
            ReachabilityStatus.NO_EVIDENCE,
            self.registry.conclusion_for(other_ext, dt(2026, 1, 2)).status)

    # -- holder transfers --------------------------------------------------

    def test_old_holder_receipt_does_not_survive_transfer(self):
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1),
            effective_to=dt(2027, 1, 1)))
        self.registry.mark_transfer(self.scope.contact_point, dt(2026, 6, 1))

        before = self.registry.conclusion_for(self.scope, dt(2026, 3, 1))
        self.assertEqual(ReachabilityStatus.REACHABLE, before.status)
        after = self.registry.conclusion_for(self.scope, dt(2026, 6, 2))
        self.assertNotEqual(ReachabilityStatus.REACHABLE, after.status)
        self.assertEqual((), after.receipts)

        # A receipt from the new holder applies from the transfer onwards.
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 6, 2)))
        self.assertEqual(
            ReachabilityStatus.REACHABLE,
            self.registry.conclusion_for(self.scope, dt(2026, 6, 3)).status)

    def test_transfer_splits_holder_epochs_on_the_record(self):
        record = self.registry.record_for(self.scope.contact_point)
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1)))
        self.registry.mark_transfer(self.scope.contact_point, dt(2026, 6, 1))
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.NOT_REACHABLE, version=1,
            effective_from=dt(2026, 7, 1)))
        epochs = [receipt.holder_epoch for receipt in record.receipts]
        self.assertEqual([0, 1], epochs)

    # -- versioned withdrawal and conflict --------------------------------

    def test_new_version_withdraws_old_version_while_effective(self):
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1),
            effective_to=dt(2027, 1, 1)))
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.NOT_REACHABLE, version=2,
            effective_from=dt(2026, 4, 1),
            effective_to=dt(2026, 5, 1),
            supersedes_version=1))

        before = self.registry.conclusion_for(self.scope, dt(2026, 3, 1))
        self.assertEqual(ReachabilityStatus.REACHABLE, before.status)

        withdrawn = self.registry.conclusion_for(self.scope, dt(2026, 4, 15))
        self.assertEqual(ReachabilityStatus.NOT_REACHABLE, withdrawn.status)
        self.assertEqual([1],
                         [r.version for r in withdrawn.withdrawn_receipts])

        # Withdrawal ends when the withdrawing receipt's window closes.
        reinstated = self.registry.conclusion_for(self.scope, dt(2026, 5, 2))
        self.assertEqual(ReachabilityStatus.REACHABLE, reinstated.status)
        self.assertEqual((), reinstated.withdrawn_receipts)

    def test_higher_comparable_version_wins_regardless_of_insertion_order(self):
        first = BusinessReceipt(
            self.scope, ReceiptOutcome.NOT_REACHABLE, version=1,
            effective_from=dt(2026, 1, 1))
        second = BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=2,
            effective_from=dt(2026, 1, 1))
        self.registry.add_receipt(first)
        self.registry.add_receipt(second)
        self.assertEqual(
            ReachabilityStatus.REACHABLE,
            self.registry.conclusion_for(self.scope, dt(2026, 1, 2)).status)

        reversed_registry = ReachabilityRegistry("lib-1.0")
        reversed_registry.add_receipt(second)
        reversed_registry.add_receipt(first)
        reversed_conclusion = reversed_registry.conclusion_for(
            self.scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.REACHABLE,
                         reversed_conclusion.status)

    def test_incomparable_versions_keep_conflict_in_both_orders(self):
        old_holder = BusinessReceipt(
            self.scope, ReceiptOutcome.NOT_REACHABLE, version="a",
            effective_from=dt(2026, 1, 1), version_namespace="carrier-X")
        new_holder = BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version="b",
            effective_from=dt(2026, 1, 1), version_namespace="carrier-Y")
        for receipts in ((old_holder, new_holder), (new_holder, old_holder)):
            registry = ReachabilityRegistry("lib-1.0")
            for receipt in receipts:
                registry.add_receipt(receipt)
            conclusion = registry.conclusion_for(self.scope, dt(2026, 1, 2))
            self.assertEqual(ReachabilityStatus.CONFLICT, conclusion.status)
            self.assertEqual(2, len(conclusion.conflicting_receipts))

    def test_tied_versions_with_different_outcomes_conflict(self):
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=3,
            effective_from=dt(2026, 1, 1)))
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.NOT_REACHABLE, version=3,
            effective_from=dt(2026, 1, 1)))
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.CONFLICT, conclusion.status)

    def test_withdrawal_does_not_cross_version_namespaces(self):
        # Two receipts reuse version number 1 but live in different
        # namespaces; the supersede link must not cross namespaces.
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1),
            version_namespace="carrier-X"))
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.NOT_REACHABLE, version=2,
            effective_from=dt(2026, 1, 1),
            version_namespace="carrier-Y",
            supersedes_version=1))
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.CONFLICT, conclusion.status)
        self.assertEqual((), conclusion.withdrawn_receipts)

    def test_unknown_receipt_and_valid_pattern_do_not_claim_reachability(self):
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.UNKNOWN, version=1,
            effective_from=dt(2026, 1, 1)))
        self.registry.record_metadata(
            self.scope, dt(2026, 1, 1), self.number, "lib-1.0")
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 2))
        self.assertEqual(ReachabilityStatus.UNKNOWN, conclusion.status)

    # -- library upgrades never rewrite history ---------------------------

    def test_library_upgrade_does_not_rewrite_historical_assessments(self):
        self.registry.record_metadata(
            self.scope, dt(2026, 1, 1), self.short_number, "lib-1.0")
        self.registry.record_length(
            self.scope, dt(2026, 1, 1), self.short_number, "lib-1.0")
        historical = self.registry.conclusion_for(self.scope, dt(2026, 2, 1))
        self.assertEqual(ValidationResult.TOO_SHORT,
                         historical.length_result)
        self.assertFalse(historical.metadata_valid)

        self.registry.upgrade_library("lib-2.0")
        self.assertEqual("lib-2.0", self.registry.library_version)

        # The past conclusion and its provenance are unchanged.
        again = self.registry.conclusion_for(self.scope, dt(2026, 2, 1))
        self.assertEqual(historical.status, again.status)
        self.assertEqual(ValidationResult.TOO_SHORT, again.length_result)
        record = self.registry.record_for(self.scope.contact_point)
        self.assertEqual(
            "lib-1.0", record.latest_metadata(dt(2026, 2, 1)).library_version)
        self.assertEqual(
            "lib-1.0", record.latest_length(dt(2026, 2, 1))[2])

        # New assessments are recorded against the upgraded library only.
        self.registry.record_metadata(
            self.scope, dt(2026, 3, 1), self.number)
        self.assertEqual(
            "lib-2.0", record.latest_metadata(dt(2026, 4, 1)).library_version)

    def test_historical_receipts_remain_facts_after_upgrade(self):
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1),
            effective_to=dt(2026, 2, 1)))
        self.registry.upgrade_library("lib-2.0")
        conclusion = self.registry.conclusion_for(self.scope, dt(2026, 1, 15))
        self.assertEqual(ReachabilityStatus.REACHABLE, conclusion.status)
        self.assertEqual("lib-1.0",
                         conclusion.receipts[0].library_version)

    # -- immutability and validation --------------------------------------

    def test_receipts_and_scopes_are_immutable(self):
        scope = ContactScope("+442083661177")
        with self.assertRaises(TypeError):
            scope.contact_point = "+16502530000"
        receipt = BusinessReceipt(
            scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 1, 1))
        with self.assertRaises(TypeError):
            receipt.outcome = ReceiptOutcome.NOT_REACHABLE

        conclusion = self.registry.conclusion_for(scope, dt(2026, 1, 2))
        with self.assertRaises(TypeError):
            conclusion.status = ReachabilityStatus.REACHABLE
        with self.assertRaises(AttributeError):
            conclusion.receipts.append(receipt)

    def test_receipt_validation(self):
        with self.assertRaises(ValueError):
            BusinessReceipt(self.scope, "MAYBE", version=1,
                            effective_from=dt(2026, 1, 1))
        with self.assertRaises(ValueError):
            BusinessReceipt(self.scope, ReceiptOutcome.REACHABLE,
                            version=None, effective_from=dt(2026, 1, 1))
        with self.assertRaises(ValueError):
            BusinessReceipt(self.scope, ReceiptOutcome.REACHABLE, version=1,
                            effective_from=dt(2026, 2, 1),
                            effective_to=dt(2026, 1, 1))
        with self.assertRaises(ValueError):
            BusinessReceipt(self.scope, ReceiptOutcome.REACHABLE, version=1,
                            effective_from=dt(2026, 1, 1),
                            supersedes_version=1)

    def test_receipt_must_match_record_contact_point(self):
        record = ContactRecord("+442083661177")
        foreign = BusinessReceipt(
            ContactScope("+16502530000"), ReceiptOutcome.REACHABLE,
            version=1, effective_from=dt(2026, 1, 1))
        with self.assertRaises(ValueError):
            record.add_receipt(foreign)

    def test_transfers_must_be_chronological(self):
        self.registry.mark_transfer(self.scope.contact_point, dt(2026, 6, 1))
        with self.assertRaises(ValueError):
            self.registry.mark_transfer(self.scope.contact_point, dt(2026, 5, 1))

    def test_transfer_cannot_be_backdated_over_recorded_receipts(self):
        self.registry.add_receipt(BusinessReceipt(
            self.scope, ReceiptOutcome.REACHABLE, version=1,
            effective_from=dt(2026, 7, 1)))
        with self.assertRaises(ValueError):
            self.registry.mark_transfer(self.scope.contact_point, dt(2026, 6, 1))


if __name__ == '__main__':
    unittest.main()
