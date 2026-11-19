# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

import os

import pyomo.common.unittest as unittest
import pyomo.environ as pyo
from pyomo.common.tempfiles import TempfileManager

from pyomo.contrib.datadiagnose import (
    DataDiagnoseError,
    find_conflicting_lines,
    find_conflicts_in_text,
    scan_data_units,
)
from pyomo.contrib.datadiagnose.__main__ import main


def _domain_model():
    # set I is constrained to live inside set J; both are data-driven
    model = pyo.AbstractModel()
    model.J = pyo.Set()
    model.I = pyo.Set(within=model.J)
    return model


def _param_validation_model():
    model = pyo.AbstractModel()
    model.I = pyo.Set(initialize=["a"])
    model.q = pyo.Param(model.I, default=100)

    def _validate(instance, value, index):
        return value <= instance.q[index]

    model.p = pyo.Param(model.I, validate=_validate)
    return model


def _write(path, content):
    with open(path, "w") as data_file:
        data_file.write(content)


class TestScanDataUnits(unittest.TestCase):
    def test_line_ranges_and_locations(self):
        text = (
            "# a comment\n"
            "set J := a\n"
            "  b;\n"
            "\n"
            "set I := a c;\n"
        )
        units = scan_data_units(text, source_file="model.dat")
        self.assertEqual(len(units), 2)
        self.assertEqual((units[0].line_start, units[0].line_end), (2, 3))
        self.assertEqual((units[1].line_start, units[1].line_end), (5, 5))
        self.assertEqual(units[0].source_file, "model.dat")
        self.assertIn("b;", units[0].text)

    def test_namespace_is_tagged(self):
        text = "namespace ns1 {\nset J := a;\n}\nset I := a;\n"
        units = scan_data_units(text, source_file="model.dat")
        self.assertEqual([unit.namespace for unit in units], ["ns1", None])

    def test_unbalanced_brackets_rejected(self):
        with self.assertRaises(DataDiagnoseError):
            scan_data_units("set J := ( a ;", source_file="bad.dat")

    def test_include_rejected(self):
        with self.assertRaises(DataDiagnoseError):
            scan_data_units("include other.dat;\n", source_file="model.dat")

    def test_store_rejected(self):
        with self.assertRaises(DataDiagnoseError):
            scan_data_units("store foo.csv;\n", source_file="model.dat")

    def test_control_markers_are_skipped(self):
        text = "data;\nset J := a;\nend;\n"
        units = scan_data_units(text, source_file="model.dat")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].line_start, 2)

    def test_block_comments_and_quotes(self):
        text = '/* multi\nline comment */\nset J := "a;b" c;\n'
        units = scan_data_units(text, source_file="model.dat")
        self.assertEqual(len(units), 1)
        self.assertEqual((units[0].line_start, units[0].line_end), (3, 3))


class TestFindConflicts(unittest.TestCase):
    def setUp(self):
        TempfileManager.push()
        self.tempdir = TempfileManager.create_tempdir()

    def tearDown(self):
        TempfileManager.pop()

    def _dat(self, content):
        path = os.path.join(self.tempdir, "data.dat")
        _write(path, content)
        return path

    def test_two_line_irreducible_conflict(self):
        dat = self._dat("set J := a b;\nset I := a c;\n")
        result = find_conflicting_lines(_domain_model(), dat)

        self.assertFalse(result.exhausted)
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertTrue(group.proven_minimal)
        self.assertEqual(len(group.lines), 2)
        self.assertEqual(
            [line.line_start for line in group.lines], [1, 2]
        )
        self.assertEqual(group.error_type, "ValueError")

        # Minimality w.r.t. the *same* conflict: dropping either line no
        # longer reproduces this exact error signature.
        target_signature = group.signature
        for dropped in range(2):
            kept_text = "\n".join(
                group.lines[k].text for k in range(2) if k != dropped
            )
            sub = find_conflicts_in_text(
                _domain_model(), kept_text, source_file=dat
            )
            self.assertNotIn(
                target_signature,
                [candidate.signature for candidate in sub.groups],
            )

    def test_each_line_alone_is_clean(self):
        # q alone is valid and p alone falls back to the default q=100,
        # so neither line conflicts by itself; only their combination does.
        dat = self._dat("param q := a 10;\nparam p := a 50;\n")
        result = find_conflicting_lines(_param_validation_model(), dat)
        group = result.groups[0]
        self.assertTrue(group.proven_minimal)
        self.assertEqual(len(group.lines), 2)

        for kept in range(2):
            alone = group.lines[kept].text + "\n"
            sub = find_conflicts_in_text(
                _param_validation_model(), alone, source_file=dat
            )
            self.assertEqual(sub.groups, [])

    def test_single_line_conflict_is_minimal(self):
        model = pyo.AbstractModel()
        model.p = pyo.Param(within=pyo.NonNegativeReals)
        dat = self._dat("param p := -5;\n")
        result = find_conflicting_lines(model, dat)

        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertTrue(group.proven_minimal)
        self.assertEqual(len(group.lines), 1)
        self.assertEqual(group.lines[0].location().split(":")[1], "1")

    def test_multiple_independent_conflicts(self):
        dat = self._dat(
            "set J := a b;\nset I := a c;\nparam extra := 1 2;\n"
        )
        result = find_conflicting_lines(_domain_model(), dat)

        self.assertEqual(len(result.groups), 2)
        self.assertTrue(all(group.proven_minimal for group in result.groups))
        sizes = sorted(len(group.lines) for group in result.groups)
        self.assertEqual(sizes, [1, 2])
        # the original file position is retained on every reported line
        for group in result.groups:
            for line in group.lines:
                self.assertEqual(line.source_file, dat)
                self.assertGreaterEqual(line.line_end, line.line_start)

    def test_clean_file_has_no_groups(self):
        dat = self._dat("set J := a b;\nset I := a;\n")
        result = find_conflicting_lines(_domain_model(), dat)
        self.assertEqual(result.groups, [])
        self.assertFalse(result.exhausted)

    def test_budget_distinguishes_candidate_from_proven(self):
        dat = self._dat("set J := a b;\nset I := a c;\n")

        full = find_conflicting_lines(_domain_model(), dat)
        proven_checks = full.checks_used
        self.assertTrue(full.groups[0].proven_minimal)

        starved = find_conflicting_lines(
            _domain_model(), dat, budget=proven_checks - 1
        )
        self.assertTrue(starved.exhausted)
        self.assertEqual(len(starved.groups), 1)
        self.assertFalse(starved.groups[0].proven_minimal)
        # the candidate still reproduces the same error signature
        self.assertEqual(
            starved.groups[0].signature, full.groups[0].signature
        )

    def test_budget_large_enough_proves_minimality(self):
        dat = self._dat("set J := a b;\nset I := a c;\n")
        result = find_conflicting_lines(_domain_model(), dat, budget=1000)
        self.assertFalse(result.exhausted)
        self.assertTrue(result.groups[0].proven_minimal)

    def test_each_probe_uses_fresh_instance_state(self):
        dat = self._dat(
            "param q := a 10;\nparam p := a 50;\n"
        )
        model = _param_validation_model()
        result = find_conflicting_lines(model, dat)

        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertTrue(group.proven_minimal)
        self.assertEqual(len(group.lines), 2)
        # the abstract model itself was never constructed or mutated
        self.assertFalse(model.is_constructed())
        # a second run from scratch gives the identical proven group
        again = find_conflicting_lines(_param_validation_model(), dat)
        self.assertEqual(
            [line.line_start for line in again.groups[0].lines],
            [line.line_start for line in group.lines],
        )

    def test_multiline_statement_location(self):
        dat = self._dat("set J := a\nb;\nset I := a c;\n")
        result = find_conflicting_lines(_domain_model(), dat)
        group = result.groups[0]
        set_j_line = next(line for line in group.lines if "set J" in line.text)
        self.assertEqual((set_j_line.line_start, set_j_line.line_end), (1, 2))

    def test_namespace_lines_replay(self):
        dat = self._dat(
            "namespace ns1 {\nset J := a b;\nset I := a c;\n}\n"
        )
        result = find_conflicting_lines(_domain_model(), dat)
        self.assertEqual(len(result.groups), 1)
        self.assertTrue(result.groups[0].proven_minimal)
        self.assertEqual(
            [line.namespace for line in result.groups[0].lines],
            ["ns1", "ns1"],
        )

    def test_explicit_namespace_selection(self):
        dat = self._dat(
            "namespace good {\nset J := a;\nset I := a;\n}\n"
            "namespace bad {\nset J := a b;\nset I := a c;\n}\n"
        )
        result = find_conflicting_lines(
            _domain_model(), dat, namespaces=["good"]
        )
        self.assertEqual(result.groups, [])

        result = find_conflicting_lines(
            _domain_model(), dat, namespaces=["bad"]
        )
        self.assertEqual(len(result.groups), 1)
        self.assertTrue(result.groups[0].proven_minimal)

    def test_unknown_namespace_rejected(self):
        dat = self._dat("set J := a;\n")
        with self.assertRaises(DataDiagnoseError):
            find_conflicting_lines(
                _domain_model(), dat, namespaces=["missing"]
            )

    def test_concrete_model_rejected(self):
        concrete = pyo.ConcreteModel()
        concrete.x = pyo.Var()
        dat = self._dat("set J := a;\n")
        with self.assertRaises(DataDiagnoseError):
            find_conflicting_lines(concrete, dat)

    def test_invalid_budget(self):
        dat = self._dat("set J := a;\n")
        with self.assertRaises(ValueError):
            find_conflicting_lines(_domain_model(), dat, budget=0)

    def test_no_solver_needed_with_objective_present(self):
        model = _domain_model()
        model.x = pyo.Var()
        model.c = pyo.Constraint(expr=model.x >= 0)
        model.o = pyo.Objective(expr=model.x)
        dat = self._dat("set J := a b;\nset I := a c;\n")
        # construction-only diagnosis succeeds without any solver
        result = find_conflicting_lines(model, dat)
        self.assertTrue(result.groups[0].proven_minimal)


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        TempfileManager.push()
        self.tempdir = TempfileManager.create_tempdir()

    def tearDown(self):
        TempfileManager.pop()

    def _model_file(self, content):
        path = os.path.join(self.tempdir, "model.py")
        _write(path, content)
        return path

    def _data_file(self, content):
        path = os.path.join(self.tempdir, "data.dat")
        _write(path, content)
        return path

    def test_cli_reports_conflict(self):
        model_file = self._model_file(
            "import pyomo.environ as pyo\n"
            "model = pyo.AbstractModel()\n"
            "model.J = pyo.Set()\n"
            "model.I = pyo.Set(within=model.J)\n"
        )
        data_file = self._data_file("set J := a b;\nset I := a c;\n")
        exit_code = main([model_file, data_file])
        self.assertEqual(exit_code, 1)

    def test_cli_clean_file(self):
        model_file = self._model_file(
            "import pyomo.environ as pyo\n"
            "model = pyo.AbstractModel()\n"
            "model.J = pyo.Set()\n"
        )
        data_file = self._data_file("set J := a b;\n")
        exit_code = main([model_file, data_file])
        self.assertEqual(exit_code, 0)

    def test_cli_budget_exhausted(self):
        model_file = self._model_file(
            "import pyomo.environ as pyo\n"
            "model = pyo.AbstractModel()\n"
            "model.J = pyo.Set()\n"
            "model.I = pyo.Set(within=model.J)\n"
        )
        data_file = self._data_file("set J := a b;\nset I := a c;\n")
        exit_code = main([model_file, data_file, "--budget", "1"])
        self.assertEqual(exit_code, 2)

    def test_cli_missing_model_file(self):
        data_file = self._data_file("set J := a;\n")
        exit_code = main(
            [os.path.join(self.tempdir, "absent.py"), data_file]
        )
        self.assertEqual(exit_code, 3)
