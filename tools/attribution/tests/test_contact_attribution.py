# -*- coding: utf-8 -*-
"""Tests for masked-directory contact-point attribution."""

from __future__ import print_function

import csv
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.attribution.contact_attribution import (  # noqa: E402
    AttributionError,
    Extent,
    main,
    parse_contact,
    parse_masked,
    solve,
    structural_candidates,
)

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


class ParsingTest(unittest.TestCase):

    def test_german_trunk_zero_national_and_international_agree(self):
        national = parse_contact("c1", "030 12345678", "DE")
        international = parse_contact("c2", "+493012345678", None)
        self.assertEqual(national.significant, "3012345678")
        self.assertEqual(international.significant, "3012345678")
        self.assertEqual(national.e164, "+493012345678")
        self.assertEqual(national.leading_zeros, 1)
        self.assertEqual(international.leading_zeros, 0)

        masked_national = parse_masked("m1", "030 1234????", "DE")
        masked_international = parse_masked("m2", "+49301234????", None)
        self.assertEqual(masked_national.zero_kind, "prefix")
        self.assertEqual(masked_national.main_glob, "301234????")
        self.assertEqual(masked_international.zero_kind, "none")
        self.assertEqual(masked_international.main_glob, "301234????")

    def test_italian_embedded_zero_is_part_of_e164(self):
        national = parse_contact("c1", "06 1234 5678", "IT")
        international = parse_contact("c2", "+390612345678", "IT")
        self.assertEqual(national.significant, "0612345678")
        self.assertEqual(international.significant, "0612345678")
        self.assertEqual(national.e164, "+390612345678")
        masked = parse_masked("m1", "+39061234????", "IT")
        self.assertEqual(masked.zero_kind, "embedded")
        self.assertEqual(masked.leading_zeros, 1)
        self.assertEqual(masked.main_glob, "061234????")

    def test_ivory_coast_embedded_zero(self):
        contact = parse_contact("c1", "+2250712345678", "CI")
        masked = parse_masked("m1", "+225071234????", "CI")
        self.assertEqual(contact.significant, "0712345678")
        self.assertEqual(masked.zero_kind, "embedded")
        self.assertEqual(
            [c.contact_id for c in structural_candidates(masked, [contact])],
            ["c1"],
        )

    def test_trunk_zero_rejected_in_international_form(self):
        with self.assertRaises(AttributionError):
            parse_masked("m1", "+490301234????", None)
        with self.assertRaises(AttributionError):
            parse_contact("c1", "+4903012345678", None)

    def test_double_trunk_zero_rejected(self):
        with self.assertRaises(AttributionError):
            parse_masked("m1", "00301234????", "DE")

    def test_national_mask_must_keep_trunk_prefix(self):
        with self.assertRaises(AttributionError):
            parse_masked("m1", "301234????", "DE")

    def test_wildcards_forbidden_in_complete_directory(self):
        with self.assertRaises(AttributionError):
            parse_contact("c1", "+49301234????", None)
        with self.assertRaises(AttributionError):
            parse_contact("c1", "+493012345678 ext 4?", "DE")

    def test_region_mismatch_rejected(self):
        with self.assertRaises(AttributionError):
            parse_contact("c1", "030 12345678", "FR")

    def test_italy_and_vatican_are_distinct_regions(self):
        italy = parse_contact("it", "+390612345678", None)
        vatican = parse_contact("va", "+390669812345", None)
        self.assertEqual(italy.region, "IT")
        self.assertEqual(vatican.region, "VA")
        masked = parse_masked("m1", "+39061234????", "IT")
        self.assertEqual(
            [c.contact_id for c in structural_candidates(
                masked, [italy, vatican])],
            ["it"],
        )

    def test_whole_number_mask_refused(self):
        with self.assertRaises(AttributionError):
            parse_masked("m1", "+49??????????", None)

    def test_russia_non_zero_trunk_prefix(self):
        national = parse_contact("c1", "8 (495) 123-45-67", "RU")
        international = parse_contact("c2", "+74951234567", "RU")
        self.assertEqual(national.significant, "4951234567")
        self.assertEqual(international.significant, "4951234567")
        self.assertEqual(national.leading_zeros, 0)
        masked = parse_masked("m1", "849512345??", "RU")
        self.assertEqual(masked.zero_kind, "prefix")
        self.assertEqual(masked.main_glob, "49512345??")
        self.assertEqual(
            [c.contact_id for c in structural_candidates(
                masked, [national, international])],
            ["c1", "c2"],
        )

    def test_extension_markers(self):
        for marker in ("ext. 42", "x42", "#42"):
            contact = parse_contact("c1", "030 12345678 " + marker, "DE")
            self.assertEqual(contact.extension, "42")
            self.assertEqual(contact.significant, "3012345678")


class StructuralMatchingTest(unittest.TestCase):

    def setUp(self):
        self.contacts = [
            parse_contact("C1", "+493012340077", None),
            parse_contact("C2", "+493012349977", None),
            parse_contact("C3", "+498912340077", None),
            parse_contact("C4", "+493012340077 ext 42", None),
        ]

    def test_single_digit_and_glob_wildcards(self):
        masked = parse_masked("M1", "+493012340??7", None)
        self.assertEqual(
            [c.contact_id for c in structural_candidates(masked, self.contacts)],
            ["C1", "C4"],
        )
        broad = parse_masked("M2", "+49*0077", None)
        self.assertEqual(
            sorted(c.contact_id for c in
                   structural_candidates(broad, self.contacts)),
            ["C1", "C3", "C4"],
        )

    def test_mask_position_matters(self):
        masked = parse_masked("M1", "+493012349??7", None)
        self.assertEqual(
            [c.contact_id for c in structural_candidates(masked, self.contacts)],
            ["C2"],
        )

    def test_region_filter(self):
        french = parse_contact("F1", "+33112340077", None)
        masked = parse_masked("M1", "+49*0077", None)
        self.assertEqual(
            structural_candidates(masked, [french] + self.contacts) and
            [c.contact_id for c in
             structural_candidates(masked, [french] + self.contacts)],
            ["C1", "C3", "C4"],
        )

    def test_extension_mask_is_independent_of_main_number(self):
        with_ext = parse_masked("M1", "+493012340077 ext 4?", None)
        self.assertEqual(
            [c.contact_id for c in structural_candidates(with_ext, self.contacts)],
            ["C4"],
        )
        wrong_ext = parse_masked("M2", "+493012340077 ext 99", None)
        self.assertEqual(
            structural_candidates(wrong_ext, self.contacts), []
        )
        no_ext_contact = [c for c in self.contacts if c.extension is None]
        self.assertEqual(
            structural_candidates(with_ext, no_ext_contact), []
        )

    def test_equal_tail_digits_do_not_merge_customers(self):
        masked = parse_masked("M1", "+49*0077", None)
        ids = [c.contact_id for c in structural_candidates(masked, self.contacts)]
        self.assertIn("C1", ids)
        self.assertIn("C3", ids)
        self.assertEqual(len(set(ids)), len(ids))

    def test_only_directory_contacts_are_ever_returned(self):
        # The glob would also fit unknown external numbers, but none are
        # generated; only entries of the complete directory can come back.
        masked = parse_masked("M1", "+49301234????", None)
        self.assertEqual(
            [c.contact_id for c in structural_candidates(masked, self.contacts[:2])],
            ["C1", "C2"],
        )


class FeasibilityTest(unittest.TestCase):

    def setUp(self):
        self.complete = [
            {"contact_id": "C1", "number": "+493012340077"},
            {"contact_id": "C2", "number": "+493012349977"},
            {"contact_id": "C3", "number": "+498912340077"},
        ]

    def _ranges(self, mapping):
        return [
            {"contact_id": cid, "min_records": low, "max_records": high}
            for cid, (low, high) in mapping.items()
        ]

    def test_unique_match_is_confirmed(self):
        verdict = solve(
            [{"masked_id": "M1", "masked_number": "+493012349??7"}],
            self.complete,
            self._ranges({"C1": (0, 2), "C2": (0, 2), "C3": (0, 2)}),
        )
        self.assertEqual(verdict["status"], "feasible")
        self.assertEqual(
            [(a["masked_id"], a["contact_id"]) for a in verdict["confirmed"]],
            [("M1", "C2")],
        )
        self.assertEqual(verdict["candidates"], [])

    def test_ambiguous_matches_stay_candidates(self):
        verdict = solve(
            [{"masked_id": "M1", "masked_number": "+49*0077"}],
            self.complete,
            self._ranges({"C1": (0, 2), "C2": (0, 2), "C3": (0, 2)}),
        )
        self.assertEqual(verdict["confirmed"], [])
        self.assertEqual(
            sorted(a["contact_id"] for a in verdict["candidates"]),
            ["C1", "C3"],
        )

    def test_record_range_forces_an_association(self):
        verdict = solve(
            [
                {"masked_id": "M1", "masked_number": "+493012340??7"},
                {"masked_id": "M2", "masked_number": "+49*0077"},
                {"masked_id": "M3", "masked_number": "+493012349??7"},
            ],
            self.complete,
            self._ranges({"C1": (1, 1), "C2": (1, 1), "C3": (1, 1)}),
        )
        self.assertEqual(verdict["status"], "feasible")
        confirmed = set(
            (a["masked_id"], a["contact_id"]) for a in verdict["confirmed"]
        )
        self.assertIn(("M3", "C2"), confirmed)
        self.assertIn(("M2", "C3"), confirmed)
        self.assertIn(("M1", "C1"), confirmed)
        candidate_edges = set(
            (a["masked_id"], a["contact_id"]) for a in verdict["candidates"]
        )
        self.assertEqual(candidate_edges, {("M2", "C1")})

    def test_capacity_shortfall_is_reported_with_constraints(self):
        verdict = solve(
            [
                {"masked_id": "M1", "masked_number": "+493012340??7"},
                {"masked_id": "M2", "masked_number": "+493012340?77"},
            ],
            self.complete,
            self._ranges({"C1": (1, 1), "C2": (1, 1), "C3": (1, 1)}),
        )
        self.assertEqual(verdict["status"], "infeasible")
        kinds = [c["kind"] for c in verdict["conflicts"]]
        self.assertIn("capacity_shortfall", kinds)
        shortfall = next(c for c in verdict["conflicts"]
                         if c["kind"] == "capacity_shortfall")
        self.assertEqual(shortfall["detail"]["neighbour_contacts"], ["C1"])
        self.assertEqual(shortfall["detail"]["available_capacity"], 1)
        self.assertEqual(shortfall["detail"]["required_assignments"], 2)
        self.assertEqual(shortfall["constraints"]["C1"], [1, 1])

    def test_unreachable_lower_bound_is_reported(self):
        verdict = solve(
            [{"masked_id": "M1", "masked_number": "+493012349??7"}],
            self.complete,
            self._ranges({"C1": (0, 5), "C2": (0, 5), "C3": (3, 5)}),
        )
        self.assertEqual(verdict["status"], "infeasible")
        conflict = next(c for c in verdict["conflicts"]
                        if c["kind"] == "lower_bound_unreachable")
        self.assertEqual(conflict["contact_id"], "C3")
        self.assertEqual(conflict["required_minimum"], 3)
        self.assertEqual(conflict["legal_masked_neighbours"], 0)

    def test_masked_entry_without_candidate(self):
        verdict = solve(
            [{"masked_id": "M1", "masked_number": "+49401234????"}],
            self.complete,
            self._ranges({"C1": (0, 2), "C2": (0, 2), "C3": (0, 2)}),
        )
        self.assertEqual(verdict["status"], "infeasible")
        self.assertEqual(
            [u["masked_id"] for u in verdict["unmatched_masked_entries"]],
            ["M1"],
        )
        self.assertIn(
            "no_candidate", [c["kind"] for c in verdict["conflicts"]]
        )

    def test_brute_force_agrees_with_flow_for_small_instance(self):
        partial = [
            {"masked_id": "M1", "masked_number": "+493012340??7"},
            {"masked_id": "M2", "masked_number": "+49*0077"},
            {"masked_id": "M3", "masked_number": "+493012349??7"},
        ]
        ranges = self._ranges(
            {"C1": (0, 2), "C2": (1, 1), "C3": (0, 1)}
        )
        verdict = solve(partial, self.complete, ranges)
        self.assertEqual(verdict["status"], "feasible")

        contacts = [
            parse_contact(row["contact_id"], row["number"], None)
            for row in self.complete
        ]
        extents = {r["contact_id"]: Extent(r["min_records"], r["max_records"])
                   for r in ranges}
        options = {}
        for row in partial:
            masked = parse_masked(row["masked_id"], row["masked_number"], None)
            options[row["masked_id"]] = [
                c.contact_id for c in structural_candidates(masked, contacts)
            ]

        feasible_edges = set()
        feasible_allocations = []
        for choice in itertools.product(*(options[m] for m in options)):
            allocation = dict(zip(options, choice))
            if not _brute_is_feasible(allocation, extents):
                continue
            feasible_allocations.append(allocation)
            for masked_id, contact_id in allocation.items():
                feasible_edges.add((masked_id, contact_id))

        all_edges = set(
            (masked_id, contact_id)
            for masked_id, ids in options.items()
            for contact_id in ids
        )
        brute_mandatory = set(
            edge for edge in all_edges
            if all(edge in set(allocation.items())
                   for allocation in feasible_allocations)
        )
        flow_mandatory = set(
            (a["masked_id"], a["contact_id"]) for a in verdict["confirmed"]
        )
        self.assertEqual(brute_mandatory, flow_mandatory)
        self.assertEqual(
            feasible_edges,
            flow_mandatory | set(
                (a["masked_id"], a["contact_id"])
                for a in verdict["candidates"]
            ),
        )


class FlowRandomizedTest(unittest.TestCase):

    def test_random_instances_match_brute_force(self):
        import random
        rng = random.Random(20261119)
        from tools.attribution.contact_attribution import _feasible
        for _ in range(300):
            masked_ids = ["M%d" % i for i in range(rng.randint(1, 5))]
            contact_ids = ["C%d" % i for i in range(rng.randint(1, 5))]
            edges = set()
            for masked_id in masked_ids:
                for contact_id in contact_ids:
                    if rng.random() < 0.45:
                        edges.add((masked_id, contact_id))
            extents = {}
            for contact_id in contact_ids:
                low = rng.randint(0, 2)
                high = rng.randint(low, low + 2)
                extents[contact_id] = Extent(low, high)

            expected = _brute_feasible(masked_ids, contact_ids, edges, extents)
            self.assertEqual(
                _feasible(masked_ids, contact_ids, edges, extents), expected
            )
            if not expected:
                continue
            for edge in sorted(edges):
                reduced = set(edges)
                reduced.discard(edge)
                mandatory = not _brute_feasible(
                    masked_ids, contact_ids, reduced, extents
                )
                self.assertEqual(
                    not _feasible(
                        masked_ids, contact_ids, edges, extents,
                        excluded=edge,
                    ),
                    mandatory,
                )


def _brute_feasible(masked_ids, contact_ids, edges, extents):
    options = dict((m, []) for m in masked_ids)
    for masked_id, contact_id in edges:
        options[masked_id].append(contact_id)
    if any(not ids for ids in options.values()):
        return False
    for choice in itertools.product(*(options[m] for m in masked_ids)):
        degrees = dict((c, 0) for c in contact_ids)
        for contact_id in choice:
            degrees[contact_id] += 1
        if all(extents[c].low <= degrees[c] <= extents[c].high
               for c in contact_ids):
            return True
    return False


def _brute_is_feasible(allocation, extents):
    degrees = dict((c, 0) for c in extents)
    for contact_id in allocation.values():
        degrees[contact_id] += 1
    return all(extents[c].low <= degrees[c] <= extents[c].high
               for c in extents)


class InputValidationTest(unittest.TestCase):

    def test_missing_record_range(self):
        verdict = solve(
            [{"masked_id": "M1", "masked_number": "+493012349??7"}],
            [{"contact_id": "C1", "number": "+493012349977"}],
            [],
        )
        self.assertEqual(verdict["status"], "infeasible")
        self.assertTrue(
            any("missing record-count range" in e["error"]
                for e in verdict["input_errors"])
        )

    def test_invalid_range_and_duplicate_ids(self):
        verdict = solve(
            [
                {"masked_id": "M1", "masked_number": "+493012349??7"},
                {"masked_id": "M1", "masked_number": "+493012349??7"},
            ],
            [{"contact_id": "C1", "number": "+493012349977"}],
            [{"contact_id": "C1", "min_records": 3, "max_records": 2}],
        )
        self.assertEqual(verdict["status"], "infeasible")
        messages = [e["error"] for e in verdict["input_errors"]]
        self.assertTrue(any("invalid record-count range" in m for m in messages))
        self.assertTrue(any("duplicate masked_id" in m for m in messages))


class CliTest(unittest.TestCase):

    def test_example_run_writes_verdict(self):
        workdir = tempfile.mkdtemp(dir=_REPO_ROOT)
        try:
            out = os.path.join(workdir, "verdict.json")
            code = main([
                "--partial", os.path.join(EXAMPLES, "partial_directory.csv"),
                "--complete", os.path.join(EXAMPLES, "complete_directory.csv"),
                "--extents", os.path.join(EXAMPLES, "record_ranges.csv"),
                "--out", out,
            ])
            self.assertEqual(code, 0)
            with open(out) as handle:
                verdict = json.load(handle)
            self.assertEqual(verdict["status"], "feasible")
            confirmed = set(
                (a["masked_id"], a["contact_id"])
                for a in verdict["confirmed"]
            )
            self.assertEqual(
                confirmed,
                {("P1", "C2"), ("P3", "C5"), ("P4", "C3")},
            )
        finally:
            shutil.rmtree(workdir)

    def test_csv_columns_are_accepted(self):
        workdir = tempfile.mkdtemp(dir=_REPO_ROOT)
        try:
            partial = os.path.join(workdir, "p.csv")
            complete = os.path.join(workdir, "c.csv")
            extents = os.path.join(workdir, "e.csv")
            with open(partial, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["masked_id", "masked_number", "region"])
                writer.writerow(["M1", "+493012349??7", ""])
            with open(complete, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["contact_id", "number", "region"])
                writer.writerow(["C1", "+493012349977", ""])
            with open(extents, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["contact_id", "min_records", "max_records"])
                writer.writerow(["C1", "1", "1"])
            out = os.path.join(workdir, "v.json")
            self.assertEqual(main([
                "--partial", partial, "--complete", complete,
                "--extents", extents, "--out", out,
            ]), 0)
        finally:
            shutil.rmtree(workdir)


if __name__ == "__main__":
    unittest.main()
