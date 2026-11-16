# Tests for src/crystal_directory.py
from __future__ import print_function, division, absolute_import

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'src'))

import crystal_directory as cd


def write_temp_cif(text):
    handle, path = tempfile.mkstemp(suffix='.cif')
    with os.fdopen(handle, 'w') as stream:
        stream.write(text)
    return path


BASIC_CIF = """
data_test
_cell_length_a 10.0
_cell_length_b 10.0
_cell_length_c 10.0
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _symmetry_equiv_pos_as_xyz
    'x, y, z'
    '-x, -y, -z'
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.10 0.10 0.10
    O1 0.25 0.10 0.10
    N1 0.40 0.40 0.40
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_site_symmetry_2
    _geom_bond_distance
    C1 O1 . 1.500
    C1 N1 2_555 8.660
"""


class RealFileTestCase(unittest.TestCase):
    """The shipped small-molecule CIF must link completely."""

    def testC13H22O3(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'tests', 'C13H22O3.cif')
        directory = cd.build_directory(path)
        self.assertEqual(len(directory.atoms), 76)
        self.assertEqual(len(directory.bonds), 80)
        bad = [b for b in directory.bonds if b.status != 'ok']
        self.assertEqual(bad, [])
        for bond in directory.bonds:
            self.assertIsNotNone(bond.computed_distance)
            self.assertAlmostEqual(bond.declared_distance,
                                   bond.computed_distance, places=2)
            self.assertIn('geom_bond row', bond.source)


class EndpointResolutionTestCase(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            os.remove(path)

    def build(self, text):
        path = write_temp_cif(text)
        self.paths.append(path)
        return cd.build_directory(path)

    def test_basic_link_and_symop(self):
        directory = self.build(BASIC_CIF)
        self.assertEqual(len(directory.atoms), 3)
        self.assertEqual(len(directory.bonds), 2)
        first, second = directory.bonds
        self.assertEqual(first.status, 'ok')
        self.assertEqual(first.end1.atom.label, 'C1')
        self.assertEqual(first.end2.atom.label, 'O1')
        self.assertAlmostEqual(first.computed_distance, 1.5, places=6)
        # second bond uses symmetry operation 2 (-x,-y,-z) on N1
        self.assertEqual(second.status, 'ok')
        self.assertEqual(second.end2.symop.op_id, '2')
        pos = second.end2.fract_position()
        self.assertAlmostEqual(pos[0], -0.4, places=6)
        expected = 10.0 * math.sqrt(0.5 ** 2 + 0.5 ** 2 + 0.5 ** 2)
        self.assertAlmostEqual(second.computed_distance, expected, places=6)

    def test_duplicate_labels_are_ambiguous_not_first_row(self):
        text = """
data_dup
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _atom_site_label
    _atom_site_label_alt_id
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 A 0.10 0.10 0.10
    C1 B 0.50 0.50 0.50
    O1 . 0.25 0.10 0.10
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    C1 O1 1.5
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertEqual(bond.status, 'ambiguous-endpoint')
        self.assertIsNone(bond.end1.atom)
        self.assertEqual(len(bond.end1.candidates), 2)
        # the confirmed atoms must survive the ambiguous bond
        self.assertEqual(len(directory.atoms), 3)

    def test_alt_id_disambiguates(self):
        text = """
data_dup2
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _atom_site_label
    _atom_site_label_alt_id
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 A 0.10 0.10 0.10
    C1 B 0.50 0.50 0.50
    O1 . 0.25 0.10 0.10
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_alt_id_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    C1 A O1 1.5
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertEqual(bond.status, 'ok')
        self.assertEqual(bond.end1.atom.alt_id, 'A')
        self.assertAlmostEqual(bond.computed_distance, 1.5, places=6)

    def test_model_number_disambiguates(self):
        text = """
data_models
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _atom_site_label
    _atom_site_pdbx_PDB_model_num
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    CA 1 0.10 0.10 0.10
    CA 2 0.60 0.10 0.10
    CB 1 0.25 0.10 0.10
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    CA CB 1.5
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertEqual(bond.status, 'ambiguous-endpoint')
        self.assertEqual(len(bond.end1.candidates), 2)


class SymmetryTestCase(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            os.remove(path)

    def build(self, text):
        path = write_temp_cif(text)
        self.paths.append(path)
        return cd.build_directory(path)

    def test_translation_shift(self):
        text = """
data_shift
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _space_group_symop_id
    _space_group_symop_operation_xyz
    1 'x, y, z'
    2 '-x, -y, -z'
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.05 0.05 0.05
    C2 0.95 0.05 0.05
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_site_symmetry_2
    _geom_bond_distance
    C1 C2 2_655 1.414
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertEqual(bond.status, 'ok')
        pos = bond.end2.fract_position()
        # -x,-y,-z applied to (0.95,0.05,0.05) then shifted by (+1,0,0)
        self.assertAlmostEqual(pos[0], 0.05, places=6)
        self.assertAlmostEqual(pos[1], -0.05, places=6)
        self.assertAlmostEqual(bond.computed_distance,
                               10.0 * math.sqrt(0.02), places=6)

    def test_missing_symop_declaration_leaves_unresolved_reference(self):
        text = """
data_nosym
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.10 0.10 0.10
    O1 0.25 0.10 0.10
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_site_symmetry_2
    _geom_bond_distance
    C1 O1 2_555 1.5
    C1 O1 . 1.5
"""
        directory = self.build(text)
        first, second = directory.bonds
        self.assertEqual(first.status, 'unresolved-symop')
        self.assertIsNone(first.computed_distance)
        # the identity-coded bond still verifies fine
        self.assertEqual(second.status, 'ok')
        self.assertAlmostEqual(second.computed_distance, 1.5, places=6)
        self.assertEqual(len(directory.atoms), 2)

    def test_undeclared_op_id_leaves_unresolved_reference(self):
        text = """
data_badid
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _symmetry_equiv_pos_as_xyz
    'x, y, z'
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.10 0.10 0.10
    O1 0.25 0.10 0.10
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_site_symmetry_2
    _geom_bond_distance
    C1 O1 7_555 1.5
"""
        directory = self.build(text)
        self.assertEqual(directory.bonds[0].status, 'unresolved-symop')


class CellMetricTestCase(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            os.remove(path)

    def build(self, text):
        path = write_temp_cif(text)
        self.paths.append(path)
        return cd.build_directory(path)

    def test_triclinic_metric_not_cartesian_difference(self):
        # gamma = 60 deg: metric distance of (0.1, 0.1, 0) is
        # 10*sqrt(0.01+0.01+2*0.01*cos60) = 10*sqrt(0.03) ~ 1.732,
        # whereas a plain Cartesian difference of the fractional
        # coordinates would give sqrt(0.02) ~ 0.141.
        text = """
data_tric
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 60
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.00 0.00 0.00
    C2 0.10 0.10 0.00
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    C1 C2 1.732
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertAlmostEqual(bond.computed_distance,
                               10.0 * math.sqrt(0.03), places=6)
        self.assertEqual(bond.status, 'ok')

    def test_no_cell_means_unverified_not_cartesian_guess(self):
        text = """
data_nocell
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.00 0.00 0.00
    C2 0.10 0.10 0.00
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    C1 C2 1.5
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertIsNone(bond.computed_distance)
        self.assertEqual(bond.status, 'unverified')

    def test_length_mismatch_flagged_but_bond_kept(self):
        text = """
data_mismatch
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.00 0.00 0.00
    C2 0.15 0.00 0.00
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    C1 C2 2.500
"""
        directory = self.build(text)
        bond = directory.bonds[0]
        self.assertEqual(bond.status, 'length-mismatch')
        self.assertAlmostEqual(bond.computed_distance, 1.5, places=6)


class FaultToleranceTestCase(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            os.remove(path)

    def build(self, text):
        path = write_temp_cif(text)
        self.paths.append(path)
        return cd.build_directory(path)

    def test_bad_bonds_do_not_drop_atoms_or_other_bonds(self):
        text = """
data_bad
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
    _atom_site_label
    _atom_site_fract_x
    _atom_site_fract_y
    _atom_site_fract_z
    C1 0.10 0.10 0.10
    O1 0.25 0.10 0.10
    N1 0.40 0.10 0.10
loop_
    _geom_bond_atom_site_label_1
    _geom_bond_atom_site_label_2
    _geom_bond_distance
    C1 O1 1.5
    C1 ZZ9 1.5
    O1 N1 notanumber
    C1 N1 3.0
"""
        directory = self.build(text)
        # all confirmed atoms survive
        self.assertEqual(len(directory.atoms), 3)
        # all four bond rows are still reported
        self.assertEqual(len(directory.bonds), 4)
        good = [b for b in directory.bonds if b.status == 'ok']
        self.assertEqual(len(good), 2)
        self.assertEqual(directory.bonds[1].status, 'unknown-atom')
        self.assertEqual(directory.bonds[2].status, 'bad-distance')
        # the good bonds are still verified
        self.assertAlmostEqual(good[0].computed_distance, 1.5, places=6)
        self.assertAlmostEqual(good[1].computed_distance, 3.0, places=6)


class SymOpParsingTestCase(unittest.TestCase):
    def test_parse_common_forms(self):
        op = cd.parse_symop_xyz('2', '-x, 1/2+y, -z')
        pos = op.apply((0.3, 0.2, 0.1))
        self.assertAlmostEqual(pos[0], -0.3)
        self.assertAlmostEqual(pos[1], 0.7)
        self.assertAlmostEqual(pos[2], -0.1)

    def test_parse_symmetry_code(self):
        self.assertEqual(cd.parse_symmetry_code('.'), (None, (0, 0, 0)))
        self.assertEqual(cd.parse_symmetry_code('2_555'), ('2', (0, 0, 0)))
        self.assertEqual(cd.parse_symmetry_code('3_465'), ('3', (-1, 1, 0)))
        self.assertEqual(cd.parse_symmetry_code('4'), ('4', (0, 0, 0)))

    def test_su_handling(self):
        self.assertAlmostEqual(cd.strip_su('9.812(2)'), 9.812)
        self.assertAlmostEqual(cd.su_of('9.812(2)'), 0.002)
        self.assertAlmostEqual(cd.su_of('1.524(4)'), 0.004)


if __name__ == '__main__':
    unittest.main()
