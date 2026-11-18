"""Fixture builders for the dependency-repair tests.

The shipped sample files are all internally consistent, so the repair tests
need small purpose-built CDFs.  Every fixture is written with ``cdfwrite`` and
re-read through ``cdflib`` to make sure it behaves like a real file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np

from cdflib import cdfwrite

GLOBAL_ATTRS = {
    "Project": "Repair Bench",
    "Source_name": "Repair Bench>Fixture",
    "Discipline": "Space Physics>Planetary Physics>Particles",
    "Data_type": "L2>Level 2 Data",
    "Descriptor": "fixture",
    "Data_version": "01",
    "Logical_file_id": "repair_fixture",
    "Logical_source": "repair_fixture",
    "Logical_source_description": "Synthetic dependency-repair fixture",
    "PI_name": "Test PI",
    "PI_affiliation": "Test Lab",
    "TEXT": "Synthetic fixture",
    "Instrument_type": "Particles (space)",
    "Mission_group": "Repair Bench",
}

SUPPORT_ATTRS = {
    "CATDESC": "coordinate",
    "FIELDNAM": "coordinate",
    "FORMAT": "E12.2",
    "UNITS": "unit",
    "VALIDMIN": 0.0,
    "VALIDMAX": 1.0e9,
    "VAR_TYPE": "support_data",
}

DATA_ATTRS = {
    "CATDESC": "measurement",
    "FIELDNAM": "measurement",
    "FORMAT": "E12.2",
    "UNITS": "count",
    "VALIDMIN": 0.0,
    "VALIDMAX": 1.0e9,
    "VAR_TYPE": "data",
}


def _var(name: str, data_type: int, data, dim_sizes: Sequence[int], rec_vary: bool = True) -> Dict:
    return {
        "spec": {
            "Variable": name,
            "Data_Type": data_type,
            "Num_Elements": 1,
            "Rec_Vary": rec_vary,
            "Dim_Sizes": list(dim_sizes),
            "Sparse": "no_sparse",
            "Compress": 0,
        },
        "data": data,
    }


def write_cdf(path: Path, variables: Iterable[Dict], global_attrs: Dict | None = None) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    with cdfwrite.CDF(str(path), cdf_spec={"Majority": "Row_major", "Encoding": 6, "Checksum": False}, delete=True) as cdf:
        cdf.write_globalattrs(
            {key: {0: value} for key, value in (global_attrs or GLOBAL_ATTRS).items()}
        )
        for variable in variables:
            cdf.write_var(variable["spec"], var_attrs=variable.get("attrs", {}), var_data=variable["data"])
    return path


def epoch_variable(name: str, records: int, start: float = 6.3e13) -> Dict:
    values = np.linspace(start, start + 1000.0 * max(records - 1, 1), records)
    return {
        **_var(name, 31, values, (), True),
        "attrs": {
            "CATDESC": "epoch",
            "FIELDNAM": name,
            "UNITS": "ms",
            "MONOTON": "INCREASE",
            "VAR_TYPE": "support_data",
            "DISPLAY_TYPE": "time_series",
            "DEPEND_0": name,
            "FILLVAL": -1.0e31,
            "FORMAT": "E25.18",
        },
    }


def coordinate(name: str, values, attrs: Dict | None = None, dim_sizes: Sequence[int] | None = None) -> Dict:
    array = np.asarray(values, dtype=np.float32)
    sizes = list(dim_sizes) if dim_sizes is not None else list(array.shape)
    base = dict(SUPPORT_ATTRS)
    base["CATDESC"] = name
    base["FIELDNAM"] = name
    base.update(attrs or {})
    return {**_var(name, 44, array, sizes, rec_vary=False), "attrs": base}


def data_variable(name: str, shape: Tuple[int, ...], attrs: Dict | None = None, data_type: int = 44) -> Dict:
    """Build a record-varying variable; ``shape[0]`` is the record count."""

    array = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape) + 1.0
    base = dict(DATA_ATTRS)
    base["CATDESC"] = name
    base["FIELDNAM"] = name
    base.update(attrs or {})
    return {**_var(name, data_type, array, shape[1:], rec_vary=True), "attrs": base}


def label_variable(name: str, labels: Sequence[str]) -> Dict:
    array = np.asarray([f"{label:<16}" for label in labels])
    return {
        **_var(name, 51, array, [len(labels)], rec_vary=False),
        "attrs": {
            "CATDESC": name,
            "FIELDNAM": name,
            "VAR_TYPE": "metadata",
            "DICT_KEY": "label",
        },
    }


def build_axis_swap_fixture(path: Path, wrong: bool = True) -> Path:
    """A 2-D spectrogram whose two axes are both 4 long.

    Correct wiring is ``DEPEND_1=angle_axis`` and ``DEPEND_2=energy_axis``.
    With ``wrong=True`` the two targets are exchanged, and lengths alone cannot
    detect it -- only the value ranges and descriptive metadata can.
    """

    variables = [
        epoch_variable("Epoch", 5),
        coordinate("angle_axis", [10, 20, 30, 40], {"CATDESC": "pitch angle", "UNITS": "degrees"}),
        coordinate("energy_axis", [100, 200, 300, 400], {"CATDESC": "energy channel", "UNITS": "eV"}),
        label_variable("angle_labels", ["a0", "a1", "a2", "a3"]),
        label_variable("energy_labels", ["e0", "e1", "e2", "e3"]),
        data_variable(
            "flux",
            (5, 4, 4),
            {
                "DISPLAY_TYPE": "spectrogram>xx=angle_axis,y=energy_axis",
                "DEPEND_0": "Epoch",
                "DEPEND_1": "energy_axis" if wrong else "angle_axis",
                "DEPEND_2": "angle_axis" if wrong else "energy_axis",
                "LABL_PTR_1": "angle_labels",
                "LABL_PTR_2": "energy_labels",
            },
        ),
    ]
    return write_cdf(path, variables)


def build_ambiguous_fixture(path: Path) -> Path:
    """Two equally good replacements for one broken edge.

    ``counts`` currently points at a three-element axis although its own axis
    has four values.  Both ``angle_a`` and ``angle_b`` are legal four-element
    angle coordinates, so the cheapest repair is ambiguous and must be
    reported as a candidate rather than applied.
    """

    variables = [
        epoch_variable("Epoch", 4),
        coordinate("broken_axis", [10, 20, 30], {"CATDESC": "pitch angle", "UNITS": "degrees"}),
        coordinate("angle_a", [10, 20, 30, 40], {"CATDESC": "pitch angle A", "UNITS": "degrees"}),
        coordinate("angle_b", [15, 25, 35, 45], {"CATDESC": "pitch angle B", "UNITS": "degrees"}),
        data_variable(
            "counts",
            (4, 4),
            {
                "DISPLAY_TYPE": "spectrogram",
                "DEPEND_0": "Epoch",
                "DEPEND_1": "broken_axis",
            },
        ),
    ]
    return write_cdf(path, variables)


def build_infeasible_fixture(path: Path) -> Path:
    """Two constraints that cannot both hold.

    ``flux`` needs four values on axis 1 but its only candidate supplies
    three, and its label pointer requires five.  No substitution is legal.
    """

    variables = [
        epoch_variable("Epoch", 4),
        coordinate("angle_short", [10, 20, 30], {"CATDESC": "pitch angle", "UNITS": "degrees"}),
        label_variable("angle_labels", ["a0", "a1", "a2", "a3", "a4"]),
        data_variable(
            "flux",
            (4, 4),
            {
                "DISPLAY_TYPE": "spectrogram",
                "DEPEND_0": "Epoch",
                "DEPEND_1": "angle_short",
                "LABL_PTR_1": "angle_labels",
            },
        ),
    ]
    return write_cdf(path, variables)


def build_several_references_fixture(path: Path, wrong_edge: bool = False) -> Path:
    """One coordinate referenced by many variables; at most one edge is wrong.

    With ``wrong_edge=True`` only ``energy_spectra`` points at the pitch axis.
    It carries an ``energy`` label pointer, so the mistake is detectable while
    ``flux_a`` and ``flux_b`` legitimately keep using the pitch axis.
    """

    spectra_depend = "pitch" if wrong_edge else "energy"
    variables = [
        epoch_variable("Epoch", 3),
        coordinate("pitch", [10, 20, 30, 40], {"CATDESC": "pitch angle", "UNITS": "degrees"}),
        coordinate("energy", [1.0e2, 2.0e2, 3.0e2, 4.0e2], {"CATDESC": "energy", "UNITS": "eV"}),
        label_variable("energy_labels", ["e0", "e1", "e2", "e3"]),
        data_variable("flux_a", (3, 4), {"DEPEND_0": "Epoch", "DEPEND_1": "pitch", "DISPLAY_TYPE": "spectrogram"}),
        data_variable("flux_b", (3, 4), {"DEPEND_0": "Epoch", "DEPEND_1": "pitch", "DISPLAY_TYPE": "spectrogram"}),
        data_variable(
            "energy_spectra",
            (3, 4),
            {
                "DEPEND_0": "Epoch",
                "DEPEND_1": spectra_depend,
                "LABL_PTR_1": "energy_labels",
                "DISPLAY_TYPE": "spectrogram",
                "CATDESC": "energy spectra",
                "DICT_KEY": "particle_flux>energy",
            },
        ),
    ]
    return write_cdf(path, variables)


def build_permutation_fixture(path: Path, swapped: bool = True) -> Path:
    """A 3-D variable whose axes may be permuted as a group.

    ``pitch``, ``energy`` and ``sector`` all have four values, so the only
    evidence separating them is their values and metadata.  The provider
    allows the axes of ``cube`` to be permuted; label pointers pin which
    coordinate belongs on which axis.
    """

    variables = [
        epoch_variable("Epoch", 3),
        coordinate("pitch", [10, 20, 30, 40], {"CATDESC": "pitch angle", "UNITS": "degrees"}),
        coordinate("energy", [1.0e2, 2.0e2, 3.0e2, 4.0e2], {"CATDESC": "energy", "UNITS": "eV"}),
        coordinate("sector", [1, 2, 3, 4], {"CATDESC": "sector index"}),
        label_variable("pitch_labels", ["p0", "p1", "p2", "p3"]),
        label_variable("energy_labels", ["e0", "e1", "e2", "e3"]),
        label_variable("sector_labels", ["s0", "s1", "s2", "s3"]),
        data_variable(
            "cube",
            (3, 4, 4, 4),
            {
                "DEPEND_0": "Epoch",
                "DEPEND_1": "energy" if swapped else "pitch",
                "DEPEND_2": "pitch" if swapped else "energy",
                "DEPEND_3": "sector",
                "LABL_PTR_1": "pitch_labels",
                "LABL_PTR_2": "energy_labels",
                "LABL_PTR_3": "sector_labels",
                "DISPLAY_TYPE": "spectrogram",
            },
        ),
    ]
    return write_cdf(path, variables)
