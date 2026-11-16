# crystal_directory.py
#
# Link a CIF's _atom_site list with its _geom_bond table and report, for
# every bond, both endpoints and where the information came from.
#
# Design rules (see task statement):
#   * Atom labels are matched together with their alternate-location id,
#     model number and symmetry context.  If several _atom_site rows share
#     a label and the bond record cannot be disambiguated, the endpoint is
#     reported as ambiguous - the first matching row is never silently
#     picked.
#   * Symmetry codes in _geom_bond_site_symmetry_* are interpreted using
#     the symmetry operations declared in the same data block
#     (_space_group_symop_operation_xyz or _symmetry_equiv_pos_as_xyz).
#     If the file declares no operations and a non-identity code is used,
#     the reference is left unresolved instead of being guessed.
#   * When unit-cell parameters are given, bond lengths are recomputed
#     with the cell metric applied to fractional coordinates.  A plain
#     Euclidean difference of fractional coordinates is never used as a
#     substitute for the cell metric.
#   * A single malformed bond must not make already-confirmed atoms or
#     the remaining bonds disappear: every row is processed independently
#     and failures are recorded per bond.

from __future__ import print_function, division, absolute_import

import argparse
import json
import math
import re
import sys

from CifFile import ReadCif

NULL_VALUES = ('.', '?')

# Tolerance (Angstrom) used when comparing a declared _geom_bond_distance
# with the distance recomputed from coordinates and the cell metric.
DEFAULT_LENGTH_TOLERANCE = 0.05


class CrystalDirectoryError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')


def is_null(value):
    return value is None or str(value).strip() in NULL_VALUES


def strip_su(value):
    """Return the numeric part of a CIF value such as '9.812(2)'."""
    if value is None:
        raise CrystalDirectoryError('missing numeric value')
    match = _NUMBER_RE.match(str(value).strip())
    if not match:
        raise CrystalDirectoryError('not a number: %r' % (value,))
    return float(match.group(0))


def su_of(value, default=0.0):
    """Return the standard uncertainty of '9.812(2)' -> 0.002."""
    text = str(value).strip()
    match = re.match(r'^[+-]?(?:\d+(?:\.(\d*))?|\.\d+)\((\d+)\)$', text)
    if not match:
        return default
    decimals = len(match.group(1) or '')
    return int(match.group(2)) * 10 ** (-decimals)


_CATEGORY_RE = re.compile(r'^(_[^_]+)_')


def _get_any(block, names):
    """Return the first present item among CIF1/CIF2 spelling variants."""
    for name in names:
        variants = [name]
        match = _CATEGORY_RE.match(name)
        if match:
            variants.append(match.group(1) + '.' + name[match.end():])
        for variant in variants:
            for candidate in (variant, variant.lower()):
                try:
                    return block[candidate]
                except KeyError:
                    continue
    return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# ---------------------------------------------------------------------------
# Symmetry operations
# ---------------------------------------------------------------------------

class SymOp(object):
    """A symmetry operation acting on fractional coordinates."""

    def __init__(self, op_id, xyz, rotation, translation):
        self.op_id = str(op_id)
        self.xyz = xyz
        self.rotation = rotation        # 3x3 tuple of tuples
        self.translation = translation  # length-3 tuple

    def apply(self, fract, extra_translation=(0, 0, 0)):
        out = []
        for row in range(3):
            val = self.translation[row] + extra_translation[row]
            for col in range(3):
                val += self.rotation[row][col] * fract[col]
            out.append(val)
        return tuple(out)

    def __repr__(self):
        return 'SymOp(%s, %r)' % (self.op_id, self.xyz)


_TERM_RE = re.compile(r'[+-]?[^+-]+')


def _parse_fraction(text):
    text = text.strip()
    if '/' in text:
        num, den = text.split('/', 1)
        return float(num) / float(den)
    return float(text)


def _parse_component(component):
    """Parse one component of an xyz symop string into (coeffs, constant)."""
    coeffs = {'x': 0.0, 'y': 0.0, 'z': 0.0}
    constant = 0.0
    for term in _TERM_RE.findall(component.replace(' ', '')):
        sign = 1.0
        if term[0] == '+':
            term = term[1:]
        elif term[0] == '-':
            sign = -1.0
            term = term[1:]
        if term and term[-1] in 'xyz':
            axis = term[-1]
            factor = term[:-1]
            coeffs[axis] += sign * (_parse_fraction(factor) if factor else 1.0)
        elif term:
            constant += sign * _parse_fraction(term)
    return coeffs, constant


def parse_symop_xyz(op_id, xyz):
    """Parse an '_*_symop_operation_xyz' style string into a SymOp."""
    parts = [p for p in re.split(',', str(xyz).strip()) if p.strip() != '']
    if len(parts) != 3:
        raise CrystalDirectoryError('cannot parse symmetry operation %r' % (xyz,))
    rotation = []
    translation = []
    for part in parts:
        coeffs, constant = _parse_component(part)
        rotation.append((coeffs['x'], coeffs['y'], coeffs['z']))
        translation.append(constant)
    return SymOp(op_id, str(xyz).strip(), tuple(rotation), tuple(translation))


_SYMCODE_RE = re.compile(r'^\s*(\d+)(?:_(\d)(\d)(\d))?\s*$')


def parse_symmetry_code(code):
    """Split a '_geom_bond_site_symmetry_*' code.

    Returns (symop_id, (tx, ty, tz)) where the translations are relative
    to the 555 home cell.  Null/empty codes map to the identity reference.
    """
    if code is None:
        return None, (0, 0, 0)
    text = str(code).strip()
    if text in NULL_VALUES or text == '':
        return None, (0, 0, 0)
    match = _SYMCODE_RE.match(text)
    if not match:
        raise CrystalDirectoryError('cannot parse symmetry code %r' % (code,))
    op_id = match.group(1)
    if match.group(2) is None:
        return op_id, (0, 0, 0)
    shifts = tuple(int(d) - 5 for d in match.groups()[1:])
    return op_id, shifts


def collect_symops(block):
    """Collect declared symmetry operations from a data block.

    Returns (ops_by_id, declared) where ``declared`` says whether the file
    declares any operations at all.
    """
    ops = {}
    xyz_list = _get_any(block, ['_space_group_symop_operation_xyz'])
    id_list = _get_any(block, ['_space_group_symop_id'])
    if xyz_list is None:
        xyz_list = _get_any(block, ['_symmetry_equiv_pos_as_xyz'])
        id_list = _get_any(block, ['_symmetry_equiv_pos_site_id'])
    xyz_values = _as_list(xyz_list)
    id_values = _as_list(id_list)
    for index, xyz in enumerate(xyz_values):
        op_id = str(id_values[index]) if index < len(id_values) else str(index + 1)
        try:
            ops[op_id] = parse_symop_xyz(op_id, xyz)
        except CrystalDirectoryError:
            # A single unparseable operation must not kill the rest.
            continue
    return ops, bool(xyz_values)


# ---------------------------------------------------------------------------
# Unit cell metric
# ---------------------------------------------------------------------------

class CellMetric(object):
    """Metric of the crystallographic unit cell for fractional coordinates."""

    def __init__(self, a, b, c, alpha, beta, gamma):
        self.parameters = (a, b, c, alpha, beta, gamma)
        ar, br, gr = (math.radians(v) for v in (alpha, beta, gamma))
        cos_a, cos_b, cos_g = math.cos(ar), math.cos(br), math.cos(gr)
        sin_g = math.sin(gr)
        if abs(sin_g) < 1e-12:
            raise CrystalDirectoryError('degenerate cell: sin(gamma) ~ 0')
        ax = a
        bx, by = b * cos_g, b * sin_g
        cx = c * cos_b
        cy = c * (cos_a - cos_b * cos_g) / sin_g
        cz2 = c * c - cx * cx - cy * cy
        cz = math.sqrt(cz2) if cz2 > 0 else 0.0
        # Columns are the a, b, c basis vectors in Cartesian space.
        self.matrix = ((ax, bx, cx),
                       (0.0, by, cy),
                       (0.0, 0.0, cz))

    def to_cartesian(self, fract):
        m = self.matrix
        return (m[0][0] * fract[0] + m[0][1] * fract[1] + m[0][2] * fract[2],
                m[1][0] * fract[0] + m[1][1] * fract[1] + m[1][2] * fract[2],
                m[2][0] * fract[0] + m[2][1] * fract[1] + m[2][2] * fract[2])

    def distance(self, fract1, fract2):
        delta = tuple(f2 - f1 for f1, f2 in zip(fract1, fract2))
        cart = self.to_cartesian(delta)
        return math.sqrt(sum(v * v for v in cart))


def build_cell_metric(block):
    """Build a CellMetric from _cell_length/_cell_angle items, or None."""
    names = ['_cell_length_a', '_cell_length_b', '_cell_length_c',
             '_cell_angle_alpha', '_cell_angle_beta', '_cell_angle_gamma']
    values = []
    for name in names:
        raw = _get_any(block, [name])
        if raw is None or is_null(raw):
            return None
        values.append(strip_su(raw))
    return CellMetric(*values)


# ---------------------------------------------------------------------------
# Atom sites
# ---------------------------------------------------------------------------

class AtomSite(object):
    def __init__(self, label, alt_id, model_num, fract, cartn, row, block_name):
        self.label = label
        self.alt_id = alt_id
        self.model_num = model_num
        self.fract = fract
        self.cartn = cartn
        self.row = row
        self.block_name = block_name

    def describe(self):
        parts = [self.label]
        if self.alt_id:
            parts.append('alt=%s' % self.alt_id)
        if self.model_num:
            parts.append('model=%s' % self.model_num)
        return ' '.join(parts)

    @property
    def source(self):
        return 'atom_site row %d of block %s' % (self.row, self.block_name)

    def __repr__(self):
        return 'AtomSite(%s)' % self.describe()


def collect_atoms(block, block_name):
    """Parse the _atom_site loop.  Bad rows are skipped, not fatal."""
    labels = _as_list(_get_any(block, ['_atom_site_label']))
    atoms = []
    problems = []
    if not labels:
        return atoms, problems
    alt_ids = _as_list(_get_any(block, ['_atom_site_label_alt_id']))
    if not alt_ids:
        assemblies = _as_list(_get_any(block, ['_atom_site_disorder_assembly']))
        groups = _as_list(_get_any(block, ['_atom_site_disorder_group']))
        alt_ids = []
        for index in range(len(labels)):
            assembly = assemblies[index] if index < len(assemblies) else None
            group = groups[index] if index < len(groups) else None
            if is_null(assembly) and is_null(group):
                alt_ids.append(None)
            else:
                alt_ids.append('%s%s' % ('' if is_null(assembly) else assembly,
                                         '' if is_null(group) else group))
    models = _as_list(_get_any(block, ['_atom_site_pdbx_PDB_model_num',
                                       '_atom_site.pdbx_PDB_model_num']))
    fx = _as_list(_get_any(block, ['_atom_site_fract_x']))
    fy = _as_list(_get_any(block, ['_atom_site_fract_y']))
    fz = _as_list(_get_any(block, ['_atom_site_fract_z']))
    cx = _as_list(_get_any(block, ['_atom_site_cartn_x']))
    cy = _as_list(_get_any(block, ['_atom_site_cartn_y']))
    cz = _as_list(_get_any(block, ['_atom_site_cartn_z']))
    for row, label in enumerate(labels):
        try:
            fract = None
            if row < len(fx) and row < len(fy) and row < len(fz) \
                    and not any(is_null(v) for v in (fx[row], fy[row], fz[row])):
                fract = (strip_su(fx[row]), strip_su(fy[row]), strip_su(fz[row]))
            cartn = None
            if row < len(cx) and row < len(cy) and row < len(cz) \
                    and not any(is_null(v) for v in (cx[row], cy[row], cz[row])):
                cartn = (strip_su(cx[row]), strip_su(cy[row]), strip_su(cz[row]))
            alt = alt_ids[row] if row < len(alt_ids) else None
            if is_null(alt):
                alt = None
            model = models[row] if row < len(models) else None
            if is_null(model):
                model = None
            atoms.append(AtomSite(str(label), alt, model, fract, cartn,
                                  row, block_name))
        except Exception as exc:  # keep the other rows
            problems.append('atom_site row %d skipped: %s' % (row, exc))
    return atoms, problems


# ---------------------------------------------------------------------------
# Bonds
# ---------------------------------------------------------------------------

class BondEnd(object):
    """One resolved (or unresolved) bond endpoint."""

    def __init__(self, label, sym_code):
        self.label = label
        self.sym_code = sym_code
        self.atom = None            # AtomSite once resolved
        self.candidates = []        # possible AtomSites when ambiguous
        self.symop = None           # SymOp applied
        self.translation = (0, 0, 0)
        self.status = 'unresolved'
        self.note = ''

    @property
    def resolved(self):
        return self.atom is not None and self.status == 'ok'

    def fract_position(self):
        if self.atom is None or self.atom.fract is None:
            return None
        if self.symop is None:
            return self.atom.fract
        return self.symop.apply(self.atom.fract, self.translation)

    def describe(self):
        if self.atom is None:
            if self.candidates:
                return '%s (ambiguous: %d candidates)' % (self.label,
                                                          len(self.candidates))
            return '%s (unresolved)' % self.label
        text = self.atom.describe()
        if self.symop is not None and not self._is_plain_identity():
            text += ' symop %s [%s]' % (self.symop.op_id, self.symop.xyz)
            if self.translation != (0, 0, 0):
                text += ' shift %s' % (self.translation,)
        return text

    def _is_plain_identity(self):
        if self.symop is None:
            return True
        if self.translation != (0, 0, 0):
            return False
        identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        return self.symop.rotation == identity \
            and self.symop.translation == (0.0, 0.0, 0.0)


class BondRecord(object):
    def __init__(self, row, block_name):
        self.row = row
        self.block_name = block_name
        self.end1 = None
        self.end2 = None
        self.declared_distance = None
        self.declared_distance_su = 0.0
        self.publ_flag = None
        self.computed_distance = None
        self.status = 'ok'
        self.problems = []

    @property
    def source(self):
        return 'geom_bond row %d of block %s' % (self.row, self.block_name)

    def add_problem(self, status, message):
        if self.status == 'ok':
            self.status = status
        self.problems.append(message)


def _resolve_endpoint(label, alt, model, atoms_by_label):
    """Match an endpoint against the atom list.

    Returns (atom, candidates, status, note).  Never silently picks the
    first row when several atoms share the label.
    """
    candidates = list(atoms_by_label.get(label, []))
    if not candidates:
        return None, [], 'unknown-atom', 'no atom_site row with label %r' % label
    if alt is not None:
        matched = [a for a in candidates if a.alt_id == alt]
        if matched:
            candidates = matched
    if model is not None:
        matched = [a for a in candidates if a.model_num == model]
        if matched:
            candidates = matched
    if len(candidates) == 1:
        return candidates[0], candidates, 'ok', ''
    descriptions = ', '.join(a.describe() for a in candidates)
    return None, candidates, 'ambiguous-endpoint', \
        'label %r matches %d atom_site rows (%s); refine with alt id or model' \
        % (label, len(candidates), descriptions)


def _apply_symmetry(end, symops, symops_declared):
    """Attach the SymOp referenced by the endpoint's symmetry code."""
    try:
        op_id, shifts = parse_symmetry_code(end.sym_code)
    except CrystalDirectoryError as exc:
        end.status = 'bad-symmetry-code'
        end.note = str(exc)
        return
    end.translation = shifts
    if op_id is None:
        op_id = '1'
    if op_id in symops:
        end.symop = symops[op_id]
        return
    if not symops_declared:
        # The file declares no operations: leave the reference unresolved
        # rather than guessing what the code means.
        if op_id == '1' and shifts == (0, 0, 0):
            end.symop = None  # identity is safe without a declaration
            return
        end.status = 'unresolved-symop'
        end.note = ('symmetry code %r used but the file declares no '
                    'symmetry operations' % (end.sym_code,))
        return
    end.status = 'unresolved-symop'
    end.note = ('symmetry operation id %r is not declared in this block'
                % (op_id,))


def collect_bonds(block, block_name, atoms_by_label, symops, symops_declared,
                  cell_metric, tolerance):
    """Parse the _geom_bond loop row by row; failures stay local."""
    labels1 = _as_list(_get_any(block, ['_geom_bond_atom_site_label_1']))
    labels2 = _as_list(_get_any(block, ['_geom_bond_atom_site_label_2']))
    bonds = []
    problems = []
    if not labels1 and not labels2:
        return bonds, problems
    count = max(len(labels1), len(labels2))
    sym1 = _as_list(_get_any(block, ['_geom_bond_site_symmetry_1']))
    sym2 = _as_list(_get_any(block, ['_geom_bond_site_symmetry_2']))
    dist = _as_list(_get_any(block, ['_geom_bond_distance']))
    publ = _as_list(_get_any(block, ['_geom_bond_publ_flag']))
    alt1 = _as_list(_get_any(block, ['_geom_bond_atom_site_label_alt_id_1',
                                     '_geom_bond_label_alt_id_1']))
    alt2 = _as_list(_get_any(block, ['_geom_bond_atom_site_label_alt_id_2',
                                     '_geom_bond_label_alt_id_2']))
    model1 = _as_list(_get_any(block, ['_geom_bond_atom_site_pdbx_PDB_model_num_1',
                                       '_geom_bond_pdbx_PDB_model_num_1']))
    model2 = _as_list(_get_any(block, ['_geom_bond_atom_site_pdbx_PDB_model_num_2',
                                       '_geom_bond_pdbx_PDB_model_num_2']))

    def _col(values, row):
        value = values[row] if row < len(values) else None
        return None if is_null(value) else value

    for row in range(count):
        bond = BondRecord(row, block_name)
        try:
            for which, labels, syms, alts, models in (
                    (1, labels1, sym1, alt1, model1),
                    (2, labels2, sym2, alt2, model2)):
                end = BondEnd(_col(labels, row), _col(syms, row))
                if end.label is None:
                    end.status = 'missing-label'
                    end.note = 'no atom label given'
                else:
                    atom, cands, status, note = _resolve_endpoint(
                        end.label, _col(alts, row), _col(models, row),
                        atoms_by_label)
                    end.atom = atom
                    end.candidates = cands
                    end.status = status
                    end.note = note
                    if atom is not None:
                        _apply_symmetry(end, symops, symops_declared)
                setattr(bond, 'end%d' % which, end)
                if end.status != 'ok':
                    bond.add_problem(end.status,
                                     'end %d: %s' % (which, end.note))
            raw_dist = _col(dist, row)
            if raw_dist is not None:
                try:
                    bond.declared_distance = strip_su(raw_dist)
                    bond.declared_distance_su = su_of(dist[row])
                except CrystalDirectoryError as exc:
                    bond.add_problem('bad-distance',
                                     'distance %r unreadable: %s'
                                     % (raw_dist, exc))
            bond.publ_flag = _col(publ, row)
            _check_length(bond, cell_metric, tolerance)
        except Exception as exc:  # one bad row must not kill the others
            bond.add_problem('error', 'row %d failed: %s' % (row, exc))
        bonds.append(bond)
    return bonds, problems


def _check_length(bond, cell_metric, tolerance):
    """Recompute the bond length with the cell metric, if possible."""
    end1, end2 = bond.end1, bond.end2
    if end1 is None or end2 is None:
        return
    if not (end1.resolved and end2.resolved):
        return
    fract1 = end1.fract_position()
    fract2 = end2.fract_position()
    if cell_metric is not None and fract1 is not None and fract2 is not None:
        # The cell metric applied to fractional coordinates is the only
        # acceptable way to measure here.
        bond.computed_distance = cell_metric.distance(fract1, fract2)
    elif end1.symop is None and end2.symop is None \
            and end1.atom.cartn is not None and end2.atom.cartn is not None:
        # Genuine Cartesian coordinates from the file (no cell needed).
        bond.computed_distance = math.sqrt(sum(
            (a - b) ** 2 for a, b in zip(end1.atom.cartn, end2.atom.cartn)))
    else:
        if bond.status == 'ok':
            bond.status = 'unverified'
        bond.problems.append('bond length cannot be verified: '
                             'no cell metric or Cartesian coordinates')
        return
    if bond.declared_distance is not None:
        allowed = tolerance + 3.0 * bond.declared_distance_su
        if abs(bond.computed_distance - bond.declared_distance) > allowed:
            bond.add_problem(
                'length-mismatch',
                'declared %.4f vs computed %.4f (tolerance %.4f)'
                % (bond.declared_distance, bond.computed_distance, allowed))


# ---------------------------------------------------------------------------
# Directory assembly
# ---------------------------------------------------------------------------

class CrystalDirectory(object):
    def __init__(self, path, block_name, atoms, bonds, symops,
                 symops_declared, cell_metric, problems):
        self.path = path
        self.block_name = block_name
        self.atoms = atoms
        self.bonds = bonds
        self.symops = symops
        self.symops_declared = symops_declared
        self.cell_metric = cell_metric
        self.problems = problems

    def to_dict(self):
        return {
            'file': self.path,
            'block': self.block_name,
            'cell': (list(self.cell_metric.parameters)
                     if self.cell_metric else None),
            'symops_declared': self.symops_declared,
            'symops': {k: v.xyz for k, v in sorted(self.symops.items())},
            'atoms': [{'label': a.label, 'alt_id': a.alt_id,
                       'model': a.model_num, 'fract': a.fract,
                       'cartn': a.cartn, 'source': a.source}
                      for a in self.atoms],
            'bonds': [{'row': b.row,
                       'end1': b.end1.describe() if b.end1 else None,
                       'end2': b.end2.describe() if b.end2 else None,
                       'declared_distance': b.declared_distance,
                       'computed_distance': b.computed_distance,
                       'status': b.status,
                       'problems': b.problems,
                       'source': b.source}
                      for b in self.bonds],
            'problems': self.problems,
        }

    def report(self):
        lines = []
        lines.append('Crystal directory: %s block %s' % (self.path,
                                                          self.block_name))
        if self.cell_metric is not None:
            a, b, c, al, be, ga = self.cell_metric.parameters
            lines.append('Cell: a=%g b=%g c=%g alpha=%g beta=%g gamma=%g'
                         % (a, b, c, al, be, ga))
        else:
            lines.append('Cell: not given (bond lengths unverified)')
        if self.symops_declared:
            lines.append('Symmetry operations declared: %d'
                         % len(self.symops))
        else:
            lines.append('Symmetry operations declared: none')
        lines.append('Atoms confirmed: %d' % len(self.atoms))
        lines.append('Bonds: %d' % len(self.bonds))
        for bond in self.bonds:
            end1 = bond.end1.describe() if bond.end1 else '?'
            end2 = bond.end2.describe() if bond.end2 else '?'
            dist = '-'
            if bond.declared_distance is not None:
                dist = '%.4f' % bond.declared_distance
            calc = '-'
            if bond.computed_distance is not None:
                calc = '%.4f' % bond.computed_distance
            lines.append('  #%d %s -- %s  declared=%s computed=%s  [%s]  (%s)'
                         % (bond.row, end1, end2, dist, calc,
                            bond.status, bond.source))
            for problem in bond.problems:
                lines.append('      ! %s' % problem)
        if self.problems:
            lines.append('Problems:')
            for problem in self.problems:
                lines.append('  ! %s' % problem)
        return '\n'.join(lines)


def build_directory(path, block_name=None, tolerance=DEFAULT_LENGTH_TOLERANCE):
    """Build a CrystalDirectory for one data block of a CIF file."""
    cif = ReadCif(path)
    names = list(cif.keys())
    if block_name is None:
        candidates = [n for n in names if n.lower() != 'global']
        if not candidates:
            raise CrystalDirectoryError('no data blocks in %s' % path)
        block_name = candidates[0]
    if block_name not in names:
        lowered = {n.lower(): n for n in names}
        if block_name.lower() in lowered:
            block_name = lowered[block_name.lower()]
        else:
            raise CrystalDirectoryError('block %r not in %s (have: %s)'
                                        % (block_name, path,
                                           ', '.join(names)))
    block = cif[block_name]
    problems = []
    atoms, atom_problems = collect_atoms(block, block_name)
    problems.extend(atom_problems)
    atoms_by_label = {}
    for atom in atoms:
        atoms_by_label.setdefault(atom.label, []).append(atom)
    symops, symops_declared = collect_symops(block)
    try:
        cell_metric = build_cell_metric(block)
    except CrystalDirectoryError as exc:
        cell_metric = None
        problems.append('cell metric unavailable: %s' % exc)
    bonds, bond_problems = collect_bonds(block, block_name, atoms_by_label,
                                         symops, symops_declared,
                                         cell_metric, tolerance)
    problems.extend(bond_problems)
    return CrystalDirectory(path, block_name, atoms, bonds, symops,
                            symops_declared, cell_metric, problems)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Link _atom_site with _geom_bond and report each '
                    "bond's endpoints and source.")
    parser.add_argument('ciffile', help='CIF file to read')
    parser.add_argument('--block', default=None,
                        help='data block name (default: first non-global)')
    parser.add_argument('--tolerance', type=float,
                        default=DEFAULT_LENGTH_TOLERANCE,
                        help='bond-length tolerance in Angstrom')
    parser.add_argument('--json', action='store_true',
                        help='emit JSON instead of a text report')
    args = parser.parse_args(argv)
    directory = build_directory(args.ciffile, block_name=args.block,
                                tolerance=args.tolerance)
    if args.json:
        print(json.dumps(directory.to_dict(), indent=2))
    else:
        print(directory.report())
    return 0


if __name__ == '__main__':
    sys.exit(main())
