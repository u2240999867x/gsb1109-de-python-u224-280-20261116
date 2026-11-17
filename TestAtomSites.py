# Tests for cross-file atomic site correlation (CifFile.atom_sites)
#
# To maximise python3/python2 compatibility
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import os
import unittest

import numpy as np

import CifFile
from CifFile import ReadCif
from CifFile.atom_sites import (
    AtomSiteError,
    Structure,
    correlate_structures,
    lattice_matrix,
    parse_symop,
    pbc_distance,
    wrap_fractional,
    wrapped_delta,
)

CUBIC = dict(a=5.0, b=5.0, c=5.0, alpha=90.0, beta=90.0, gamma=90.0)
CIF_FILE = os.path.join(os.path.dirname(__file__), "tests", "C13H22O3.cif")


def line_structure(labels, elements, x_coordinates, **kwargs):
    """Build a structure whose sites differ only along fractional x."""
    fracs = [[x, 0.0, 0.0] for x in x_coordinates]
    return Structure(labels, elements, fracs, **kwargs)


class PackageExportTestCase(unittest.TestCase):
    def test_top_level_exports(self):
        self.assertIs(CifFile.Structure, Structure)
        self.assertIs(CifFile.correlate_structures, correlate_structures)
        self.assertTrue(issubclass(CifFile.AtomSiteError, ValueError))


class SymopParsingTestCase(unittest.TestCase):
    def test_identity(self):
        rotation, translation = parse_symop("x, y, z")
        np.testing.assert_array_equal(rotation, np.eye(3))
        np.testing.assert_array_equal(translation, np.zeros(3))

    def test_inversion_with_translation(self):
        rotation, translation = parse_symop("-x, -y+1/2, -z+1")
        np.testing.assert_array_equal(rotation, -np.eye(3))
        np.testing.assert_allclose(translation, [0.0, 0.5, 1.0])

    def test_combined_axes(self):
        rotation, translation = parse_symop("x-y+1/2, y, -x")
        np.testing.assert_array_equal(
            rotation, [[1.0, -1.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]])
        np.testing.assert_allclose(translation, [0.5, 0.0, 0.0])

    def test_bad_operation(self):
        with self.assertRaises(AtomSiteError):
            parse_symop("x, y")
        with self.assertRaises(AtomSiteError):
            parse_symop(42)

    def test_numeric_failure_is_wrapped(self):
        with self.assertRaises(AtomSiteError):
            parse_symop("x/0, y, z")


class GeometryTestCase(unittest.TestCase):
    def test_wrap_fractional(self):
        wrapped, shift = wrap_fractional([-0.2, 1.2, 0.4])
        np.testing.assert_allclose(wrapped, [0.8, 0.2, 0.4])
        self.assertEqual(tuple(shift), (1, -1, 0))

    def test_wrapped_delta_boundaries(self):
        # Ties at +/-0.5 map to -0.5 under the half-open [-0.5, 0.5) rule.
        delta, shift = wrapped_delta([0.01, 0.5, 0.5], [0.99, 0.0, 0.9])
        np.testing.assert_allclose(delta, [0.02, -0.5, -0.4], atol=1e-12)
        self.assertEqual(tuple(shift), (-1, 1, 0))

    def test_lattice_and_pbc_distance(self):
        matrix = lattice_matrix(CUBIC)
        np.testing.assert_allclose(matrix @ np.array([1.0, 0.0, 0.0]),
                                   [5.0, 0.0, 0.0], atol=1e-12)
        self.assertAlmostEqual(pbc_distance([0.1, 0.0, 0.0], CUBIC), 0.5)
        self.assertIsNone(pbc_distance([0.1, 0.0, 0.0], None))


class OrbitTestCase(unittest.TestCase):
    def test_orbit_count(self):
        structure = line_structure(
            ["Fe1"], ["Fe"], [0.1],
            symops=["x, y, z", "-x, -y, -z"])
        orbit = structure.orbit(0)
        images = sorted(tuple(image["coords"]) for image in orbit)
        np.testing.assert_allclose(images, [[0.1, 0.0, 0.0],
                                            [0.9, 0.0, 0.0]], atol=1e-12)

    def test_special_position_keeps_all_evidence(self):
        structure = line_structure(
            ["Ti1"], ["Ti"], [0.0],
            symops=["x, y, z", "-x, -y, -z"])
        orbit = structure.orbit(0)
        self.assertEqual(len(orbit), 1)
        operations = [source[1] for source in orbit[0]["sources"]]
        self.assertEqual(operations, ["x, y, z", "-x, -y, -z"])


class CorrelationTestCase(unittest.TestCase):
    def test_unique_correlation_preserves_labels(self):
        structure_a = line_structure(["Fe1", "O1"], ["Fe", "O"],
                                     [0.1, 0.3], name="A")
        structure_b = line_structure(["FeX", "OX"], ["Fe", "O"],
                                     [0.1001, 0.2999], name="B")
        result = correlate_structures(structure_a, structure_b)
        self.assertEqual(result.status, "unique")
        pairs = [(relation["a_label"], relation["b_label"])
                 for relation in result.certain]
        self.assertEqual(sorted(pairs), [("Fe1", "FeX"), ("O1", "OX")])

    def test_periodic_boundary_correspondence(self):
        structure_a = line_structure(["O1"], ["O"], [0.0004], cell=CUBIC)
        structure_b = line_structure(["O2"], ["O"], [0.9998], cell=CUBIC)
        result = correlate_structures(structure_a, structure_b)
        self.assertEqual(result.status, "unique")
        relation = result.certain[0]
        evidence = relation["evidence"][0]
        self.assertEqual(evidence["boundary_shift"], (-1, 0, 0))
        np.testing.assert_allclose(evidence["fractional_delta"],
                                   [0.0006, 0.0, 0.0], atol=1e-12)
        self.assertAlmostEqual(relation["cartesian_distance"], 0.003)

    def test_different_elements_at_same_position_not_merged(self):
        structure_a = line_structure(["Fe1"], ["Fe"], [0.25])
        structure_b = line_structure(["O1"], ["O"], [0.25])
        result = correlate_structures(structure_a, structure_b)
        self.assertEqual(result.certain, [])
        self.assertEqual(result.unmatched_a, ["Fe1"])
        self.assertEqual(result.unmatched_b, ["O1"])

    def test_mutually_exclusive_occupancy_not_merged(self):
        structure_a = line_structure(
            ["FeA", "FeB"], ["Fe", "Fe"], [0.4, 0.4],
            disorder=[("A", "1"), ("A", "2")])
        structure_b = line_structure(
            ["FeC", "FeD"], ["Fe", "Fe"], [0.4, 0.4],
            disorder=[("A", "1"), ("A", "2")])
        result = correlate_structures(structure_a, structure_b)
        self.assertEqual(result.status, "unique")
        pairs = sorted((relation["a_label"], relation["b_label"])
                       for relation in result.certain)
        self.assertEqual(pairs, [("FeA", "FeC"), ("FeB", "FeD")])
        # The cross-group alternatives must not be present as edges.
        edges = {(relation["a_label"], relation["b_label"])
                 for relation in result.certain}
        self.assertNotIn(("FeA", "FeD"), edges)
        self.assertNotIn(("FeB", "FeC"), edges)

    def test_disorder_versus_ordered_sites_are_distinct(self):
        ordered = line_structure(["Fe1"], ["Fe"], [0.4])
        disordered = line_structure(
            ["Fe2"], ["Fe"], [0.4], disorder=[("A", "1")])
        result = correlate_structures(ordered, disordered)
        self.assertEqual(result.certain, [])
        self.assertEqual(result.unmatched_a, ["Fe1"])
        self.assertEqual(result.unmatched_b, ["Fe2"])

    def test_symmetry_evidence_recorded(self):
        structure_a = line_structure(
            ["Fe1"], ["Fe"], [0.1],
            symops=["x, y, z", "-x, -y, -z"])
        structure_b = line_structure(
            ["Fe2"], ["Fe"], [0.9],
            symops=["x, y, z", "-x, -y, -z"])
        result = correlate_structures(structure_a, structure_b)
        relation = result.certain[0]
        op_pairs = {(entry["a_operation"], entry["b_operation"])
                    for entry in relation["evidence"]}
        # The base sites are inversion partners, so the equivalence is
        # witnessed by two complementary operation combinations.
        self.assertEqual(
            op_pairs,
            {("x, y, z", "-x, -y, -z"), ("-x, -y, -z", "x, y, z")})
        for entry in relation["evidence"]:
            np.testing.assert_allclose(entry["a_image"], entry["b_image"],
                                       atol=1e-12)

    def test_ambiguous_matchings_separated_from_certain(self):
        structure_a = line_structure(
            ["Fe1", "Fe2", "Fe3", "Fe4"], ["Fe"] * 4,
            [0.1, 0.5, 0.9, 0.9002])
        structure_b = line_structure(
            ["FeA", "FeB", "FeC", "FeD"], ["Fe"] * 4,
            [0.1, 0.5, 0.9, 0.9002])
        result = correlate_structures(structure_a, structure_b)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.solutions), 2)
        certain_pairs = sorted((r["a_label"], r["b_label"])
                               for r in result.certain)
        self.assertEqual(certain_pairs, [("Fe1", "FeA"), ("Fe2", "FeB")])
        self.assertEqual(len(result.ambiguous_groups), 1)
        group = result.ambiguous_groups[0]
        self.assertEqual(group["a_labels"], ["Fe3", "Fe4"])
        self.assertEqual(group["b_labels"], ["FeC", "FeD"])
        self.assertEqual(len(group["alternatives"]), 4)
        for alternative in group["alternatives"]:
            self.assertTrue(alternative["evidence"])

    def test_partial_matching_reports_unmatched(self):
        structure_a = line_structure(["Fe1", "Fe2"], ["Fe", "Fe"],
                                     [0.1, 0.6])
        structure_b = line_structure(["FeA"], ["Fe"], [0.1])
        result = correlate_structures(structure_a, structure_b)
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.certain), 1)
        self.assertEqual(result.unmatched_a, ["Fe2"])
        self.assertEqual(result.unmatched_b, [])

    def test_matching_cap_truncation(self):
        structure_a = line_structure(
            ["Fe1", "Fe2", "Fe3"], ["Fe"] * 3, [0.2, 0.2, 0.2])
        structure_b = line_structure(
            ["FeA", "FeB", "FeC"], ["Fe"] * 3, [0.2, 0.2, 0.2])
        result = correlate_structures(structure_a, structure_b,
                                      matching_cap=2)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.solutions), 2)
        self.assertEqual(result.status, "ambiguous")

    def test_cap_above_solution_count_is_not_truncated(self):
        structure_a = line_structure(
            ["Fe1", "Fe2"], ["Fe", "Fe"], [0.2, 0.2])
        structure_b = line_structure(
            ["FeA", "FeB"], ["Fe", "Fe"], [0.2, 0.2])
        result = correlate_structures(structure_a, structure_b,
                                      matching_cap=10)
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.solutions), 2)

    def test_cartesian_tolerance(self):
        structure_a = line_structure(["Fe1"], ["Fe"], [0.1], cell=CUBIC)
        structure_b = line_structure(["Fe2"], ["Fe"], [0.101], cell=CUBIC)
        accepted = correlate_structures(structure_a, structure_b,
                                        fractional_tol=0.01,
                                        cartesian_tol=0.1)
        self.assertEqual(len(accepted.certain), 1)
        rejected = correlate_structures(structure_a, structure_b,
                                        cartesian_tol=1e-6)
        self.assertEqual(rejected.certain, [])

    def test_cartesian_tolerance_requires_cell(self):
        structure_a = line_structure(["Fe1"], ["Fe"], [0.1])
        structure_b = line_structure(["Fe2"], ["Fe"], [0.101])
        with self.assertRaises(AtomSiteError):
            correlate_structures(structure_a, structure_b,
                                 cartesian_tol=0.1)


class CifBackedTestCase(unittest.TestCase):
    def setUp(self):
        self.cif = ReadCif(CIF_FILE)
        self.block = self.cif["ii"]

    def test_structure_from_block_parses_esds(self):
        structure = Structure.from_cif_block(self.block, name="mol")
        self.assertEqual(len(structure.labels), 76)
        self.assertEqual(structure.elements[0], "C")
        self.assertEqual(structure.labels[0], "C1A")
        np.testing.assert_allclose(structure.fracs[0],
                                   [0.7141, 0.2593, 0.9184])
        self.assertEqual(structure.symops, ["x, y, z", "-x, -y, -z"])
        self.assertAlmostEqual(structure.cell["a"], 9.812)

    def test_from_cif_file_constructor(self):
        structure = Structure.from_cif(CIF_FILE, block="ii")
        self.assertEqual(structure.name, "ii")
        self.assertEqual(len(structure.labels), 76)

    def test_self_correlation_is_unique(self):
        structure = Structure.from_cif_block(self.block)
        result = correlate_structures(structure, structure)
        self.assertEqual(result.status, "unique")
        self.assertEqual(len(result.certain), len(structure.labels))
        for relation in result.certain:
            self.assertEqual(relation["a_label"], relation["b_label"])
            self.assertEqual(relation["fractional_distance"], 0.0)

    def test_reordered_translated_copy(self):
        structure_a = Structure.from_cif_block(self.block, name="mol")
        order = list(range(len(structure_a.labels)))[::-1]
        translated = structure_a.fracs[order] + np.array(
            [1.0002, 1.0005, 2.0])
        structure_b = Structure(
            [label + "r" for label in
             [structure_a.labels[i] for i in order]],
            [structure_a.elements[i] for i in order],
            translated, name="mol2", cell=structure_a.cell,
            symops=structure_a.symops)
        result = correlate_structures(structure_a, structure_b,
                                      cartesian_tol=0.05)
        self.assertEqual(result.status, "unique")
        for relation in result.certain:
            self.assertEqual(relation["a_label"] + "r", relation["b_label"])
            self.assertEqual(relation["evidence"][0]["a_operation"],
                             "x, y, z")
        c1a = next(relation for relation in result.certain
                   if relation["a_label"] == "C1A")
        shifts = {entry["b_lattice_shift"] for entry in c1a["evidence"]}
        self.assertIn((-1, -1, -2), shifts)

    def test_block_without_atom_sites_raises(self):
        empty = self.cif["global"]
        with self.assertRaises(AtomSiteError):
            Structure.from_cif_block(empty)

    def test_result_to_dict_is_serializable(self):
        structure = Structure.from_cif_block(self.block)
        result = correlate_structures(structure, structure)
        payload = result.to_dict()
        self.assertEqual(payload["status"], "unique")
        self.assertEqual(payload["structure_a"], structure.name)
        self.assertEqual(len(payload["certain"]), len(structure.labels))
        self.assertIn("evidence", payload["certain"][0])


if __name__ == "__main__":
    unittest.main()
