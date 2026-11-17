"""Correspondence matching between two snapshots of a list page.

The matcher consumes nodes described by reliable anchors and editable
fields, together with an explicit table of allowed old/new pairings and
the edit cost of each pairing.  It solves a minimum-cost one-to-one
assignment, so an old node can never be claimed by two new nodes and
traversal order is never used as evidence of identity.

When several assignments share the same optimum, only pairs that occur
in every optimal assignment are reported as certain.  Every other edge
that participates in at least one optimum is returned as a candidate,
including the option of remaining unmatched (insertion/deletion).

Positional moves and content corrections are reported independently:
edge costs describe content edits only, while list positions are compared
after the assignment.  Repeated text therefore yields symmetric
candidate sets instead of arbitrary order-based pairings.

Typical usage::

    >>> old = [
    ...     Node("o1", {"id": "1"}, {"title": "Apple"}),
    ...     Node("o2", {"id": "2"}, {"title": "Banana"}),
    ... ]
    >>> new = [
    ...     Node("n2", {"id": "2"}, {"title": "Banana"}),
    ...     Node("n1", {"id": "1"}, {"title": "Apple pie"}),
    ... ]
    >>> result = match(old, new, build_allowed_edges(old, new))
    >>> result.unique
    True
    >>> pair = result.pairs["o1"]
    >>> pair.new_id
    'n1'
    >>> pair.is_move, pair.is_correction
    (True, True)
    >>> [(change.key, change.distance) for change in pair.field_changes]
    [('title', 4)]
"""

from dataclasses import dataclass, field
from math import isclose

__all__ = [
    "Node",
    "AllowedEdge",
    "Candidate",
    "FieldChange",
    "Pairing",
    "Correspondence",
    "match",
    "build_allowed_edges",
    "levenshtein",
]


@dataclass
class Node:
    """A list item in one snapshot.

    ``anchors`` holds reliable identity values that are not expected to
    change (item ids, stable urls, ...).  ``fields`` holds editable
    content whose textual changes count as corrections.
    """

    id: object
    anchors: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AllowedEdge:
    """One permitted structural correspondence with its edit cost."""

    old_id: object
    new_id: object
    cost: float


@dataclass(frozen=True)
class Candidate:
    """A pairing that occurs in at least one optimal assignment.

    A ``new_id`` (or ``old_id``) of ``None`` means leaving the node
    unmatched is itself an optimal alternative.
    """

    old_id: object
    new_id: object
    cost: float


@dataclass(frozen=True)
class FieldChange:
    key: str
    old_value: object
    new_value: object
    distance: int


@dataclass
class Pairing:
    old_id: object
    new_id: object
    cost: float
    old_position: int
    new_position: int
    field_changes: list = field(default_factory=list)
    anchor_changes: list = field(default_factory=list)

    @property
    def is_move(self):
        return self.old_position != self.new_position

    @property
    def is_correction(self):
        return bool(self.field_changes)


@dataclass
class Correspondence:
    pairs: dict
    candidates: dict
    new_candidates: dict
    unmatched_old: list
    unmatched_new: list
    optimal_cost: float
    unique: bool

    def moves(self):
        return [pair for pair in self.pairs.values() if pair.is_move]

    def corrections(self):
        return [pair for pair in self.pairs.values() if pair.is_correction]


def levenshtein(left, right):
    """Character-level edit distance between two strings."""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_left != char_right),
                )
            )
        previous = current
    return previous[-1]


def _hungarian(cost):
    """Minimum-cost perfect assignment on a square cost matrix.

    Returns ``(row_to_column, total_cost)``; unreachable cells carry
    ``float('inf')``.
    """

    size = len(cost)
    inf = float("inf")
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)

    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (size + 1)
        used = [False] * (size + 1)
        while p[j0] != 0:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, size + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * size
    total = 0.0
    for j in range(1, size + 1):
        if p[j]:
            i = p[j] - 1
            assignment[i] = j - 1
            total += cost[i][j - 1]
    return assignment, total


def _equal_cost(left, right):
    if left == float("inf") or right == float("inf"):
        return left == right
    return isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _diff_fields(old_values, new_values):
    changes = []
    for key in sorted(set(old_values) | set(new_values)):
        old_value = old_values.get(key)
        new_value = new_values.get(key)
        if old_value != new_value:
            changes.append(
                FieldChange(
                    key=key,
                    old_value=old_value,
                    new_value=new_value,
                    distance=levenshtein(
                        str(old_value if old_value is not None else ""),
                        str(new_value if new_value is not None else ""),
                    ),
                )
            )
    return changes


def _field_edit_cost(old_node, new_node):
    keys = set(old_node.fields) | set(new_node.fields)
    return sum(
        levenshtein(
            str(old_node.fields.get(key, "")),
            str(new_node.fields.get(key, "")),
        )
        for key in sorted(keys)
    )


def build_allowed_edges(
    old_nodes,
    new_nodes,
    *,
    max_cost=None,
    structural_filter=None,
):
    """Derive allowed correspondences from anchors and field similarity.

    Nodes sharing an anchor value may correspond; any conflicting anchor
    value forbids the edge.  When neither side can prove identity through
    an anchor, field edit distance is used as the cost and ``max_cost``
    lets the caller reject implausible pairings.  ``structural_filter``
    receives ``(old_node, new_node)`` and may veto any edge.
    """

    edges = []
    for old_node in old_nodes:
        for new_node in new_nodes:
            if structural_filter is not None and not structural_filter(
                old_node, new_node
            ):
                continue
            shared = set(old_node.anchors) & set(new_node.anchors)
            if any(
                old_node.anchors[key] != new_node.anchors[key]
                for key in shared
            ):
                continue
            matched = [
                key
                for key in shared
                if old_node.anchors[key] == new_node.anchors[key]
            ]
            if (old_node.anchors or new_node.anchors) and not matched:
                continue
            cost = float(_field_edit_cost(old_node, new_node))
            if max_cost is not None and cost > max_cost:
                continue
            edges.append(AllowedEdge(old_node.id, new_node.id, cost))
    return edges


def match(old_nodes, new_nodes, edges, *, unmatched_cost=None):
    """Compute the optimal one-to-one correspondence.

    ``edges`` is the exhaustive set of allowed pairings and their costs.
    ``unmatched_cost`` is the penalty each side pays when a node stays
    unmatched: deleting an old node or inserting a new node costs the
    penalty once, while leaving both sides of a would-be pair unmatched
    (a deletion plus an insertion) costs twice the penalty.  By default
    it is one above the most expensive edge, so every available real
    correspondence is preferred over an insertion/deletion.
    """

    old_nodes = list(old_nodes)
    new_nodes = list(new_nodes)
    old_index = {node.id: i for i, node in enumerate(old_nodes)}
    new_index = {node.id: j for j, node in enumerate(new_nodes)}
    if len(old_index) != len(old_nodes):
        raise ValueError("old node ids must be unique")
    if len(new_index) != len(new_nodes):
        raise ValueError("new node ids must be unique")

    edge_cost = {}
    for edge in edges:
        if edge.old_id not in old_index or edge.new_id not in new_index:
            raise ValueError(f"edge references unknown node: {edge}")
        edge_cost[(edge.old_id, edge.new_id)] = float(edge.cost)

    n = len(old_nodes)
    m = len(new_nodes)
    if n == 0 and m == 0:
        return Correspondence(
            pairs={},
            candidates={},
            new_candidates={},
            unmatched_old=[],
            unmatched_new=[],
            optimal_cost=0.0,
            unique=True,
        )

    penalty = (
        float(unmatched_cost)
        if unmatched_cost is not None
        else max((cost for cost in edge_cost.values()), default=0.0) + 1.0
    )

    # Square (n+m)x(n+m) matrix: each real old row owns a dummy column
    # for deletions and each real new column owns a dummy row for
    # insertions.  Leaving both sides unmatched therefore costs two
    # independent penalties, while a real pair pays its edge cost once.
    # Infeasible cells use a large finite value because the Hungarian
    # algorithm assumes a complete graph; every node has a feasible
    # dummy cell, so the optimum never needs one.
    size = n + m
    big = 1.0e15

    def dummy_col(i):
        return m + i

    def dummy_row(j):
        return n + j

    def solve(forbid=(), force=None):
        matrix = [[big] * size for _ in range(size)]
        for i in range(n):
            for j in range(m):
                if (i, j) not in forbid:
                    matrix[i][j] = edge_cost.get(
                        (old_nodes[i].id, new_nodes[j].id), big
                    )
            matrix[i][dummy_col(i)] = penalty
        for j in range(m):
            matrix[dummy_row(j)][j] = penalty
        for j in range(m):
            for i in range(n):
                matrix[dummy_row(j)][dummy_col(i)] = 0.0
        if force is not None:
            forced_row, forced_col = force
            for j in range(size):
                if j != forced_col:
                    matrix[forced_row][j] = big
            for i in range(size):
                if i != forced_row:
                    matrix[i][forced_col] = big
        return _hungarian(matrix)

    star_assignment, star_cost = solve()
    star_pairs = {
        i: j
        for i, j in enumerate(star_assignment)
        if i < n and j < m
    }

    certain = {}
    for i, j in star_pairs.items():
        _, forbidden_cost = solve(forbid={(i, j)})
        if not _equal_cost(forbidden_cost, star_cost):
            certain[i] = j

    available = set()
    for old_id, new_id in edge_cost:
        i = old_index[old_id]
        j = new_index[new_id]
        forced_assignment, forced_cost = solve(force=(i, j))
        if forced_assignment[i] == j and _equal_cost(forced_cost, star_cost):
            available.add((i, j))

    row_unmatched = set()
    for i in range(n):
        _, cost = solve(forbid={(i, j) for j in range(m)})
        if _equal_cost(cost, star_cost):
            row_unmatched.add(i)

    col_unmatched = set()
    for j in range(m):
        _, cost = solve(forbid={(i, j) for i in range(n)})
        if _equal_cost(cost, star_cost):
            col_unmatched.add(j)

    pairs = {}
    for i, j in certain.items():
        old_node = old_nodes[i]
        new_node = new_nodes[j]
        pairs[old_node.id] = Pairing(
            old_id=old_node.id,
            new_id=new_node.id,
            cost=edge_cost.get((old_node.id, new_node.id), big),
            old_position=i,
            new_position=j,
            field_changes=_diff_fields(old_node.fields, new_node.fields),
            anchor_changes=_diff_fields(old_node.anchors, new_node.anchors),
        )

    candidates = {}
    for i in range(n):
        if i in certain:
            continue
        options = [
            Candidate(
                old_id=old_nodes[i].id,
                new_id=new_nodes[j].id,
                cost=edge_cost[(old_nodes[i].id, new_nodes[j].id)],
            )
            for j in sorted(jj for ii, jj in available if ii == i)
        ]
        if i in row_unmatched and options:
            options.append(
                Candidate(old_id=old_nodes[i].id, new_id=None, cost=penalty)
            )
        if options:
            candidates[old_nodes[i].id] = options

    new_candidates = {}
    for j in range(m):
        if j in certain.values():
            continue
        options = [
            Candidate(
                old_id=old_nodes[i].id,
                new_id=new_nodes[j].id,
                cost=edge_cost[(old_nodes[i].id, new_nodes[j].id)],
            )
            for i in sorted(ii for ii, jj in available if jj == j)
        ]
        if j in col_unmatched and options:
            options.append(
                Candidate(old_id=None, new_id=new_nodes[j].id, cost=penalty)
            )
        if options:
            new_candidates[new_nodes[j].id] = options

    unmatched_old = [
        old_nodes[i].id
        for i in range(n)
        if i not in certain
        and not any(ii == i for ii, _ in available)
    ]
    unmatched_new = [
        new_nodes[j].id
        for j in range(m)
        if j not in certain.values()
        and not any(jj == j for _, jj in available)
    ]

    return Correspondence(
        pairs=pairs,
        candidates=candidates,
        new_candidates=new_candidates,
        unmatched_old=unmatched_old,
        unmatched_new=unmatched_new,
        optimal_cost=star_cost,
        unique=not candidates and not new_candidates,
    )
