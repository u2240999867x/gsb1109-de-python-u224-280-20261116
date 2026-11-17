"""Cross-file correlation of atomic sites from crystallographic CIF data.

This module associates the *atomic sites* described in two structure
descriptions (typically two CIF data blocks, possibly from different files).
A site is described by its original label, element, fractional coordinates,
disorder occupancy and the unit cell it belongs to.  Two sites are only
allowed to correspond when they carry the same element and the same disorder
signature; merely sharing a position (including positions made equal by a
periodic boundary translation) is never sufficient to merge them.

Symmetry is handled explicitly.  Each structure supplies the symmetry
operations it allows.  The full orbit of every site is generated (modulo
lattice translations) and every retained relation keeps the complete
*equivalence evidence*: which symmetry operation from each structure
produced the matching images, the symmetry-expanded fractional coordinates
and the integer lattice translation that connects the two images across a
periodic boundary.

When more than one global site-to-site correspondence exists, all perfect
matchings are enumerated.  The result separates relations that are fixed in
every solution (``certain``) from the groups of sites that can be exchanged
between solutions (``ambiguous_groups``), and every candidate edge keeps its
evidence even when it only occurs in some of the solutions.

The module only depends on :mod:`numpy` and on :mod:`CifFile` for reading
CIF data blocks; structures can equally be built directly from plain Python
sequences.
"""
from __future__ import absolute_import, print_function

import re
from itertools import product

import numpy as np

from .CifFile_module import get_number_with_esd

# Default tolerances.  Fractional coordinates are compared modulo one.
DEFAULT_FRACTIONAL_TOL = 1.0e-3
# Grid used to recognise symmetry images that coincide on special positions.
_ORBIT_GRID = 1.0e-8
# Safety limit on the number of explicitly enumerated global matchings.
DEFAULT_MATCHING_CAP = 1000

_SYMOP_TAGS = (
    "_space_group_symop_operation_xyz",  # CIF 2.0 / DDLm
    "_symmetry_equiv_pos_as_xyz",        # CIF 1.1
)
_FRAC_TAGS = ("_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z")
_CELL_TAGS = (
    ("_cell_length_a", "a"),
    ("_cell_length_b", "b"),
    ("_cell_length_c", "c"),
    ("_cell_angle_alpha", "alpha"),
    ("_cell_angle_beta", "beta"),
    ("_cell_angle_gamma", "gamma"),
)
_MISSING = (".", "?", "")


class AtomSiteError(ValueError):
    """Raised when structure data cannot be interpreted for correlation."""


# ---------------------------------------------------------------------------
# Symmetry operation handling
# ---------------------------------------------------------------------------

_TERM_RE = re.compile(r"[+-]?[^+-]+")
_ELEMENT_RE = re.compile(r"([A-Za-z]{1,3})")


def _parse_number(token):
    """Parse an integer, decimal or simple fraction (e.g. ``1/2``)."""
    token = token.strip()
    if "/" in token:
        numerator, denominator = token.split("/", 1)
        return float(numerator) / float(denominator)
    return float(token)


def parse_symop(xyz):
    """Parse a symmetry operation expressed as ``'x, -y+1/2, z'``.

    Returns ``(rotation, translation)`` where ``rotation`` is a 3x3 matrix
    and ``translation`` is a length-3 vector such that the transformed
    fractional coordinate ``x'`` satisfies ``x' = rotation @ x + t``.
    Components may be linear combinations of several axes (e.g. ``x-y``).
    """
    if not isinstance(xyz, str):
        raise AtomSiteError("Symmetry operation must be a string, got %r" % (xyz,))
    components = [part.strip().lower() for part in xyz.split(",")]
    if len(components) != 3:
        raise AtomSiteError("Symmetry operation %r does not have 3 components" % xyz)
    rotation = np.zeros((3, 3), dtype=float)
    translation = np.zeros(3, dtype=float)
    axis_index = {"x": 0, "y": 1, "z": 2}
    try:
        for row, component in enumerate(components):
            compact = component.replace(" ", "")
            if not compact:
                raise AtomSiteError("Empty component in symmetry operation %r" % xyz)
            consumed = 0
            for term in _TERM_RE.findall(compact):
                consumed += len(term)
                term = term.strip()
                variable = None
                for axis in ("x", "y", "z"):
                    if term.endswith(axis):
                        variable = axis
                        break
                if variable is not None:
                    prefix = term[:-1]
                    if prefix in ("", "+"):
                        coefficient = 1.0
                    elif prefix == "-":
                        coefficient = -1.0
                    else:
                        coefficient = _parse_number(prefix)
                    rotation[row, axis_index[variable]] += coefficient
                else:
                    translation[row] += _parse_number(term)
            if consumed != len(compact):
                raise AtomSiteError("Cannot parse symmetry operation %r" % xyz)
    except (ValueError, ArithmeticError) as error:
        raise AtomSiteError(
            "Cannot parse symmetry operation %r: %s" % (xyz, error))
    return rotation, translation


def apply_symop(rotation, translation, frac):
    """Apply a symmetry operation to a fractional coordinate."""
    return rotation @ np.asarray(frac, dtype=float) + translation


def wrap_fractional(frac):
    """Wrap a fractional coordinate into [0, 1) and report the shift.

    Returns ``(wrapped, shift)`` where ``shift`` is the integer lattice
    translation added to the input to obtain the wrapped coordinate.
    """
    frac = np.asarray(frac, dtype=float)
    shift = -np.floor(frac).astype(int)
    return frac + shift, shift


def _orbit_key(frac):
    return tuple(int(round(v / _ORBIT_GRID)) for v in frac)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def lattice_matrix(cell):
    """Build the matrix converting fractional to Cartesian coordinates.

    ``cell`` is a mapping with keys ``a, b, c, alpha, beta, gamma`` with
    angles in degrees.  Columns of the returned matrix are the lattice
    vectors.
    """
    if cell is None:
        return None
    a = float(cell["a"])
    b = float(cell["b"])
    c = float(cell["c"])
    alpha = np.radians(float(cell["alpha"]))
    beta = np.radians(float(cell["beta"]))
    gamma = np.radians(float(cell["gamma"]))
    cos_a, cos_b, cos_g = np.cos(alpha), np.cos(beta), np.cos(gamma)
    sin_g = np.sin(gamma)
    matrix = np.array(
        [
            [a, b * cos_g, c * cos_b],
            [0.0, b * sin_g, c * (cos_a - cos_b * cos_g) / sin_g],
            [0.0, 0.0, c * np.sqrt(1.0 - cos_b ** 2
                                   - ((cos_a - cos_b * cos_g) / sin_g) ** 2)],
        ]
    )
    return matrix


def pbc_distance(fractional_delta, cell):
    """Cartesian length of a fractional vector under periodic boundaries."""
    if cell is None:
        return None
    return float(np.linalg.norm(lattice_matrix(cell) @ np.asarray(fractional_delta)))


def wrapped_delta(coords_a, coords_b):
    """Shortest fractional vector from ``coords_b`` to ``coords_a``.

    Returns ``(delta, lattice_shift)`` with each component of ``delta`` in
    [-0.5, 0.5) and ``lattice_shift`` the integer vector satisfying
    ``coords_a - coords_b = delta + lattice_shift``.
    """
    raw = np.asarray(coords_a, dtype=float) - np.asarray(coords_b, dtype=float)
    # Half-open range [-0.5, 0.5): ties at +0.5 map to -0.5 deterministically.
    lattice_shift = np.floor(raw + 0.5).astype(int)
    delta = raw - lattice_shift
    return delta, lattice_shift


def _as_number(value):
    """Convert a CIF numeric token (possibly with an ESD) to a float."""
    if isinstance(value, (int, float, np.floating)):
        return float(value)
    number, _esd = get_number_with_esd(str(value).strip())
    if number is None:
        return None
    return number


def normalize_element(symbol):
    """Normalise an element symbol, stripping charge/valence suffixes."""
    if symbol is None:
        return None
    match = _ELEMENT_RE.match(str(symbol).strip())
    if not match:
        return None
    return match.group(1).capitalize()


def _element_from_label(label):
    match = _ELEMENT_RE.match(str(label).strip())
    return match.group(1).capitalize() if match else None


def _is_missing(value):
    return value is None or str(value).strip().lower() in _MISSING


# ---------------------------------------------------------------------------
# Structure description
# ---------------------------------------------------------------------------

class Structure(object):
    """A self-contained description of the sites in one structure.

    Attributes
    ----------
    name : str
        Identifier used in reports and evidence.
    labels : list of str
        Original site labels (``_atom_site_label``), always preserved.
    elements : list of str
        Normalised element symbols, aligned with :attr:`labels`.
    fracs : numpy.ndarray
        Fractional coordinates with shape ``(n_sites, 3)``.
    cell : dict or None
        Unit-cell parameters with angles in degrees.
    symops : list of str
        Allowed symmetry operations as xyz expressions, including identity.
    disorder : list of tuple or None
        ``(assembly, group)`` disorder signature per site, or ``None``.
    occupancies : list of float or None
        Site occupancies when available.
    """

    def __init__(self, labels, elements, fracs, name="structure", cell=None,
                 symops=None, disorder=None, occupancies=None):
        labels = [str(label) for label in labels]
        fracs = np.asarray(fracs, dtype=float)
        if fracs.ndim != 2 or fracs.shape[1] != 3:
            raise AtomSiteError("Fractional coordinates must have shape (n, 3)")
        if len(labels) != fracs.shape[0]:
            raise AtomSiteError("Number of labels and coordinates differ")
        elements = [normalize_element(element) for element in elements]
        if any(element is None for element in elements):
            raise AtomSiteError("Every site requires an identifiable element")
        if disorder is None:
            disorder = [None] * len(labels)
        if len(disorder) != len(labels):
            raise AtomSiteError("Disorder list length does not match site count")
        disorder = [self._disorder_signature(signature) for signature in disorder]
        if symops is None or len(symops) == 0:
            symops = ["x, y, z"]
        parsed = [parse_symop(operation) for operation in symops]
        if occupancies is not None and len(occupancies) != len(labels):
            raise AtomSiteError("Occupancy list length does not match site count")
        self.name = str(name)
        self.labels = labels
        self.elements = elements
        self.fracs = fracs
        self.cell = cell
        self.symops = list(symops)
        self._parsed_symops = parsed
        self.disorder = disorder
        self.occupancies = occupancies

    @staticmethod
    def _disorder_signature(signature):
        if signature is None:
            return None
        assembly, group = signature
        assembly = None if _is_missing(assembly) else str(assembly).strip()
        group = None if _is_missing(group) else str(group).strip()
        if assembly is None and group is None:
            return None
        return (assembly, group)

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_cif_block(cls, block, name=None):
        """Build a :class:`Structure` from a PyCIFRW ``CifBlock``/``StarBlock``."""
        if name is None:
            name = getattr(block, "name", "block")
        labels = cls._block_values(block, "_atom_site_label")
        if labels is None:
            raise AtomSiteError("Block %s contains no _atom_site_label loop" % name)
        labels = [str(label) for label in labels]
        n_sites = len(labels)

        type_symbols = cls._block_values(block, "_atom_site_type_symbol")
        elements = []
        for index in range(n_sites):
            element = None
            if type_symbols is not None and not _is_missing(type_symbols[index]):
                element = normalize_element(type_symbols[index])
            if element is None:
                element = _element_from_label(labels[index])
            if element is None:
                raise AtomSiteError(
                    "Cannot determine element for site %s in block %s"
                    % (labels[index], name))
            elements.append(element)

        coordinates = np.zeros((n_sites, 3), dtype=float)
        for axis, tag in enumerate(_FRAC_TAGS):
            values = cls._block_values(block, tag)
            if values is None or len(values) != n_sites:
                raise AtomSiteError("Missing %s in block %s" % (tag, name))
            for index, token in enumerate(values):
                number = _as_number(token)
                if number is None:
                    raise AtomSiteError(
                        "Site %s has no numeric %s value" % (labels[index], tag))
                coordinates[index, axis] = number

        assemblies = cls._block_values(block, "_atom_site_disorder_assembly")
        groups = cls._block_values(block, "_atom_site_disorder_group")
        disorder = []
        for index in range(n_sites):
            assembly = assemblies[index] if assemblies is not None else None
            group = groups[index] if groups is not None else None
            disorder.append(cls._disorder_signature((assembly, group)))

        occupancies = cls._block_values(block, "_atom_site_occupancy")
        if occupancies is not None:
            parsed_occupancy = []
            for token in occupancies:
                number = _as_number(token) if not _is_missing(token) else None
                parsed_occupancy.append(number)
            occupancies = parsed_occupancy

        cell = None
        cell_values = {}
        complete = True
        for tag, key in _CELL_TAGS:
            token = cls._block_scalar(block, tag)
            number = _as_number(token) if token is not None else None
            if number is None:
                complete = False
                break
            cell_values[key] = number
        if complete:
            cell = cell_values

        symops = None
        for tag in _SYMOP_TAGS:
            values = cls._block_values(block, tag)
            if values:
                symops = [str(operation).strip() for operation in values]
                break

        return cls(labels, elements, coordinates, name=name, cell=cell,
                   symops=symops, disorder=disorder, occupancies=occupancies)

    @classmethod
    def from_cif(cls, source, block=None):
        """Read a structure from a CIF file path or readable object."""
        from .CifFile_module import ReadCif

        cif = ReadCif(source)
        if block is None:
            block_name = cif.keys()[0]
        else:
            block_name = block
        return cls.from_cif_block(cif[block_name], name=block_name)

    @staticmethod
    def _block_values(block, tag):
        if tag not in block:
            return None
        value = block[tag]
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _block_scalar(block, tag):
        if tag not in block:
            return None
        value = block[tag]
        if isinstance(value, list):
            return value[0] if value else None
        return value

    # -- geometry ----------------------------------------------------------

    def orbit(self, index):
        """Return the symmetry orbit of one site modulo lattice translations.

        Each image is a dictionary with the wrapped fractional coordinate and
        a ``sources`` list recording every ``(operation index, operation xyz,
        lattice shift)`` that generates it, preserving equivalence evidence
        on special positions.
        """
        base = self.fracs[index]
        images = {}
        for op_index, (rotation, translation) in enumerate(self._parsed_symops):
            raw = apply_symop(rotation, translation, base)
            wrapped, shift = wrap_fractional(raw)
            key = _orbit_key(wrapped)
            source = (op_index, self.symops[op_index], tuple(int(v) for v in shift))
            if key in images:
                images[key]["sources"].append(source)
            else:
                images[key] = {
                    "coords": wrapped,
                    "sources": [source],
                }
        return list(images.values())

    def can_correspond(self, index, other, other_index):
        """Element and disorder occupancy compatibility guard."""
        if self.elements[index] != other.elements[other_index]:
            return False
        if self.disorder[index] != other.disorder[other_index]:
            return False
        return True


# ---------------------------------------------------------------------------
# Candidate relations and global matching
# ---------------------------------------------------------------------------

def _candidate_edges(structure_a, structure_b, fractional_tol, cartesian_tol):
    """Build all admissible A->B candidate edges with full evidence."""
    orbits_a = [structure_a.orbit(i) for i in range(len(structure_a.labels))]
    orbits_b = [structure_b.orbit(j) for j in range(len(structure_b.labels))]
    edges = {}
    for i in range(len(structure_a.labels)):
        for j in range(len(structure_b.labels)):
            if not structure_a.can_correspond(i, structure_b, j):
                continue
            evidence = []
            for image_a, image_b in product(orbits_a[i], orbits_b[j]):
                delta, shift = wrapped_delta(image_a["coords"], image_b["coords"])
                fractional_distance = float(np.linalg.norm(delta))
                if fractional_distance > fractional_tol:
                    continue
                distance_a = pbc_distance(delta, structure_a.cell)
                distance_b = pbc_distance(delta, structure_b.cell)
                cart_distances = [d for d in (distance_a, distance_b)
                                  if d is not None]
                if cartesian_tol is not None:
                    if not cart_distances or max(cart_distances) > cartesian_tol:
                        continue
                for source_a, source_b in product(image_a["sources"],
                                                  image_b["sources"]):
                    evidence.append({
                        "a_operation_index": source_a[0],
                        "a_operation": source_a[1],
                        "a_lattice_shift": source_a[2],
                        "b_operation_index": source_b[0],
                        "b_operation": source_b[1],
                        "b_lattice_shift": source_b[2],
                        "a_image": tuple(float(v) for v in image_a["coords"]),
                        "b_image": tuple(float(v) for v in image_b["coords"]),
                        "boundary_shift": tuple(int(v) for v in shift),
                        "fractional_delta": tuple(float(v) for v in delta),
                        "fractional_distance": fractional_distance,
                        "cartesian_distance": (max(cart_distances)
                                               if cart_distances else None),
                    })
            if evidence:
                edges[(i, j)] = evidence
    return edges


def _relation(structure_a, structure_b, i, j, evidence):
    distances = [entry["fractional_distance"] for entry in evidence]
    cart_distances = [entry["cartesian_distance"] for entry in evidence
                      if entry["cartesian_distance"] is not None]
    return {
        "a_index": i,
        "b_index": j,
        "a_label": structure_a.labels[i],
        "b_label": structure_b.labels[j],
        "element": structure_a.elements[i],
        "disorder": structure_a.disorder[i],
        "fractional_distance": min(distances),
        "cartesian_distance": min(cart_distances) if cart_distances else None,
        "evidence": evidence,
    }


def _enumerate_matchings(adjacency, n_a, n_b, cap):
    """Enumerate maximum-cardinality bipartite matchings with MRV search."""
    candidates = dict(adjacency)
    solutions = []
    matching = {}
    used_b = set()
    overflow = [False]

    def search():
        free_a = [i for i in range(n_a) if i not in matching and candidates.get(i)]
        if not free_a:
            if len(solutions) < cap:
                solutions.append(dict(matching))
            else:
                overflow[0] = True
            return
        node = min(free_a, key=lambda i: len([j for j in candidates[i]
                                              if j not in used_b]))
        options = [j for j in candidates[node] if j not in used_b]
        if not options:
            if len(solutions) < cap:
                solutions.append(dict(matching))
            else:
                overflow[0] = True
            return
        for j in options:
            matching[node] = j
            used_b.add(j)
            search()
            used_b.discard(j)
            del matching[node]

    search()
    truncated = overflow[0]

    # Keep only maximum-cardinality solutions (partial matching case).
    best = max((len(solution) for solution in solutions), default=0)
    maximum = [solution for solution in solutions if len(solution) == best]
    return maximum, truncated, best


def _connected_components(variable_edges, n_a, n_b):
    graph = {}
    for i, j in variable_edges:
        graph.setdefault(("a", i), set()).add(("b", j))
        graph.setdefault(("b", j), set()).add(("a", i))
    seen = set()
    components = []
    for node in graph:
        if node in seen:
            continue
        stack = [node]
        component = {"a_indices": [], "b_indices": []}
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            side, index = current
            component["a_indices" if side == "a" else "b_indices"].append(index)
            stack.extend(graph[current] - seen)
        component["a_indices"].sort()
        component["b_indices"].sort()
        components.append(component)
    return components


class CorrespondenceResult(object):
    """Outcome of correlating two structures.

    Attributes
    ----------
    status : str
        One of ``'unique'`` (single perfect matching), ``'ambiguous'``
        (several perfect matchings), ``'partial'`` (a unique maximum matching
        that does not cover every site) or ``'ambiguous_partial'``.
    certain : list of dict
        Relations present in *every* enumerated global solution.
    ambiguous_groups : list of dict
        Connected groups of sites whose pairing changes between solutions,
        including every alternative relation and its evidence.
    unmatched_a, unmatched_b : list of str
        Labels that no maximum matching can cover.
    solutions : list of list of dict
        All enumerated global matchings (up to the configured cap).
    truncated : bool
        ``True`` if the matching cap was reached.
    """

    def __init__(self, structure_a, structure_b, edges, solutions, truncated,
                 matching_size, fractional_tol, cartesian_tol):
        self.structure_a = structure_a
        self.structure_b = structure_b
        self.edges = edges
        self.solutions = solutions
        self.truncated = truncated
        self.matching_size = matching_size
        self.fractional_tol = fractional_tol
        self.cartesian_tol = cartesian_tol
        self.certain = []
        self.ambiguous_groups = []
        self.unmatched_a = []
        self.unmatched_b = []
        self.status = ""
        self._analyse()

    def _analyse(self):
        structure_a, structure_b = self.structure_a, self.structure_b
        solution_edge_sets = [frozenset(solution.items())
                              for solution in self.solutions]
        if solution_edge_sets:
            common = set.intersection(*(set(edges) for edges in
                                        solution_edge_sets))
        else:
            common = set()
        union = set()
        for edges in solution_edge_sets:
            union.update(edges)
        variable = union - common

        # Certainty is established over the collected solutions.  When
        # ``truncated`` is set, further uncollected solutions may exist and
        # callers must treat this list as provisional.
        certain_edges = common
        variable = union - common

        self.certain = [_relation(structure_a, structure_b, i, j,
                                  self.edges[(i, j)])
                        for (i, j) in sorted(certain_edges)]

        components = _connected_components(variable,
                                           len(structure_a.labels),
                                           len(structure_b.labels))
        for component in components:
            group = {
                "a_labels": [structure_a.labels[i] for i in component["a_indices"]],
                "b_labels": [structure_b.labels[j] for j in component["b_indices"]],
                "alternatives": [],
            }
            group_edges = [(i, j) for (i, j) in sorted(variable)
                           if i in component["a_indices"]]
            for (i, j) in group_edges:
                group["alternatives"].append(
                    _relation(structure_a, structure_b, i, j,
                              self.edges[(i, j)]))
            self.ambiguous_groups.append(group)

        covered_a = {i for solution in self.solutions for i in solution}
        covered_b = {j for solution in self.solutions for j in solution.values()}
        self.unmatched_a = [structure_a.labels[i]
                            for i in range(len(structure_a.labels))
                            if i not in covered_a]
        self.unmatched_b = [structure_b.labels[j]
                            for j in range(len(structure_b.labels))
                            if j not in covered_b]

        perfect = (self.matching_size == len(structure_a.labels)
                   == len(structure_b.labels))
        if len(self.solutions) <= 1 and not self.truncated:
            self.status = "unique" if perfect else "partial"
        else:
            self.status = "ambiguous" if perfect else "ambiguous_partial"

    def to_dict(self):
        return {
            "status": self.status,
            "structure_a": self.structure_a.name,
            "structure_b": self.structure_b.name,
            "truncated": self.truncated,
            "matching_size": self.matching_size,
            "certain": self.certain,
            "ambiguous_groups": self.ambiguous_groups,
            "unmatched_a": self.unmatched_a,
            "unmatched_b": self.unmatched_b,
            "solutions": [
                [_relation(self.structure_a, self.structure_b, i, j,
                           self.edges[(i, j)]) for i, j in sorted(solution.items())]
                for solution in self.solutions
            ],
        }

    def __repr__(self):
        return ("CorrespondenceResult(status=%r, certain=%d, ambiguous_groups=%d, "
                "unmatched_a=%d, unmatched_b=%d, solutions=%d%s)"
                % (self.status, len(self.certain), len(self.ambiguous_groups),
                   len(self.unmatched_a), len(self.unmatched_b),
                   len(self.solutions), ", truncated" if self.truncated else ""))


def correlate_structures(structure_a, structure_b, fractional_tol=DEFAULT_FRACTIONAL_TOL,
                         cartesian_tol=None, matching_cap=DEFAULT_MATCHING_CAP):
    """Correlate the atomic sites of two structures.

    Parameters
    ----------
    structure_a, structure_b : Structure or CIF block
        Either :class:`Structure` objects or PyCIFRW data blocks, in which
        case :meth:`Structure.from_cif_block` is used.
    fractional_tol : float
        Maximum norm of the wrapped fractional-coordinate difference for an
        admissible relation (default 1e-3).
    cartesian_tol : float or None
        When given, relations must also be within this Cartesian distance in
        Angstrom under both supplied unit cells.
    matching_cap : int
        Maximum number of global matchings to enumerate explicitly.

    Returns
    -------
    CorrespondenceResult
    """
    if not isinstance(structure_a, Structure):
        structure_a = Structure.from_cif_block(structure_a, name="structure_a")
    if not isinstance(structure_b, Structure):
        structure_b = Structure.from_cif_block(structure_b, name="structure_b")
    if cartesian_tol is not None and (structure_a.cell is None
                                      or structure_b.cell is None):
        raise AtomSiteError(
            "cartesian_tol requires complete unit-cell parameters in both "
            "structures")
    edges = _candidate_edges(structure_a, structure_b, fractional_tol,
                             cartesian_tol)
    adjacency = {}
    for (i, j) in edges:
        adjacency.setdefault(i, []).append(j)
    for i in adjacency:
        adjacency[i].sort()
    solutions, truncated, best = _enumerate_matchings(
        adjacency, len(structure_a.labels), len(structure_b.labels),
        matching_cap)
    return CorrespondenceResult(structure_a, structure_b, edges, solutions,
                                truncated, best, fractional_tol, cartesian_tol)
