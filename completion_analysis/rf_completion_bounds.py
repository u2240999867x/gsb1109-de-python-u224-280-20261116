#!/usr/bin/env python3
r"""Min/max rooted split (Robinson-Foulds) distance over legal tree completions.

Problem
-------
Two rooted data trees may contain unresolved polytomies and may be missing
leaves with respect to a target leaf set.  A *legal completion* of a data
tree is a fully binary rooted tree on exactly the target leaf set obtained by

  1. attaching every missing leaf at one of the explicitly allowed attachment
     positions (edges of the data tree, or the root edge), and
  2. resolving every polytomy into some binary refinement.

Distance convention (the "clearly stated rooted split distance")
----------------------------------------------------------------
For a completed (binary) tree T on leaf set L, let C(T) be its set of
non-trivial rooted clades (splits): clusters of size 2..|L|-1.  The rooted
split distance between two completions T1, T2 is the symmetric difference

    d(T1, T2) = |C(T1) \ C(T2)| + |C(T2) \ C(T1)|
              = 2*(|L| - 2) - 2*|C(T1) & C(T2)|.

Unresolved polytomies are NEVER scored directly (an unknown split is not a
zero-support split); distances are only ever computed between fully resolved
completions.

Reported endpoints
------------------
The reported minimum and maximum are always attained by an actual pair of
legal completions, returned as witnesses.  We never combine a lower bound
derived from one completion with an upper bound derived from an incompatible
one to fabricate an endpoint.

Statuses
--------
  "exact"             : every legal completion pair was evaluated; min/max are
                        exact and witnessed.
  "budget_exhausted"  : the search budget ran out.  We report the proven
                        bounds together with the best witnessed pairs found:
                          min in [proven_lower, best_witnessed_min]
                          max in [best_witnessed_max, proven_upper]
                        Proven bounds are derived from forced/possible split
                        analysis (see README.md) and are valid for the FULL
                        completion space, not just the explored part.
  "infeasible"        : the constraints admit no legal completion (e.g. a data
                        tree has leaves outside the target set, or a missing
                        leaf has no allowed attachment position).  This is
                        reported separately from budget exhaustion.
  "invalid_input"     : malformed input (e.g. an attachment position clade
                        that matches no node).

Input (JSON)
------------
{
  "target_leaves": ["a", "b", ...],
  "tree1": {"newick": "((a,b),(c,d));",
            "allowed_attachments": [["c", "d"], "ROOT"]},
  "tree2": {"newick": "(a,(b,c,d));",
            "allowed_attachments": [["b", "c", "d"]]},
  "budget": {"max_completions_per_tree": 100000,
             "max_pair_evaluations": 2000000}
}

An attachment position is either "ROOT" (the edge above the root, so the new
leaf becomes sister to the whole tree) or a list of leaf labels giving the
clade of a node u, meaning the edge between u and its parent.
"""

from __future__ import annotations

import itertools
import json
import sys


# ---------------------------------------------------------------------------
# Tree structure and Newick I/O
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("label", "children")

    def __init__(self, label=None, children=None):
        self.label = label
        self.children = children if children is not None else []

    @property
    def is_leaf(self):
        return not self.children


def parse_newick(text):
    s = text.strip()
    if s.endswith(";"):
        s = s[:-1]
    idx = [0]

    def skip_ws():
        while idx[0] < len(s) and s[idx[0]].isspace():
            idx[0] += 1

    def parse_label():
        skip_ws()
        start = idx[0]
        while idx[0] < len(s) and s[idx[0]] not in "(),:;":
            idx[0] += 1
        return s[start:idx[0]].strip()

    def skip_length():
        skip_ws()
        if idx[0] < len(s) and s[idx[0]] == ":":
            idx[0] += 1
            while idx[0] < len(s) and s[idx[0]] not in "(),;":
                idx[0] += 1

    def parse_sub():
        skip_ws()
        if idx[0] >= len(s):
            raise ValueError("unexpected end of Newick string")
        if s[idx[0]] == "(":
            idx[0] += 1
            children = []
            while True:
                children.append(parse_sub())
                skip_ws()
                if idx[0] >= len(s):
                    raise ValueError("unbalanced parentheses")
                c = s[idx[0]]
                if c == ",":
                    idx[0] += 1
                    continue
                if c == ")":
                    idx[0] += 1
                    break
                raise ValueError("unexpected character %r at offset %d" % (c, idx[0]))
            node = Node(children=children)
            parse_label()  # internal label ignored
            skip_length()
            return node
        label = parse_label()
        if not label:
            raise ValueError("empty leaf label at offset %d" % idx[0])
        skip_length()
        return Node(label=label)

    root = parse_sub()
    skip_ws()
    if idx[0] != len(s):
        raise ValueError("trailing characters in Newick string")
    return root


def to_newick(node):
    if node.is_leaf:
        return node.label
    return "(" + ",".join(to_newick(c) for c in node.children) + ")"


# ---------------------------------------------------------------------------
# Clades / splits
# ---------------------------------------------------------------------------

def node_clades(root):
    """Return list of (node, frozenset-of-leaf-labels) for every node."""
    out = []

    def rec(n):
        if n.is_leaf:
            s = frozenset([n.label])
        else:
            s = frozenset().union(*(rec(c) for c in n.children))
        out.append((n, s))
        return s

    rec(root)
    return out


def leaf_set(root):
    return frozenset(n.label for n, _ in node_clades(root) if n.is_leaf)


def split_set(root):
    """Non-trivial rooted clades: sizes 2..n-1.  This is the exact currency
    of the distance convention; trivial splits are shared by every tree on
    the same leaf set and cancel out of the symmetric difference."""
    sets = [s for _, s in node_clades(root)]
    full = max(sets, key=len)
    return frozenset(s for s in sets if 1 < len(s) < len(full))


def split_distance(splits1, splits2):
    return len(splits1 - splits2) + len(splits2 - splits1)


def is_binary(root):
    return all(n.is_leaf or len(n.children) == 2 for n, _ in node_clades(root))


# ---------------------------------------------------------------------------
# Errors / statuses
# ---------------------------------------------------------------------------

class InvalidInput(Exception):
    pass


class Infeasible(Exception):
    pass


# ---------------------------------------------------------------------------
# Completion enumeration
# ---------------------------------------------------------------------------

def _gen_binary_trees(items):
    """Yield all rooted binary trees whose leaves are the given subtree
    objects (order-free; each unordered shape yielded once)."""
    if len(items) == 1:
        yield items[0]
        return
    first, rest = items[0], items[1:]
    for k in range(len(rest)):
        for extra in itertools.combinations(rest, k):
            left_items = [first] + list(extra)
            right_items = [x for x in rest if x not in extra]
            for l in _gen_binary_trees(left_items):
                for r in _gen_binary_trees(right_items):
                    yield Node(children=[l, r])


class _Budget:
    def __init__(self, limit):
        self.limit = limit
        self.exhausted = False

    def cap_list(self, iterator):
        """Materialize at most limit+1 items; flag exhaustion if more exist.
        Sound: if we flag, the true count really exceeds the budget."""
        items = list(itertools.islice(iterator, self.limit + 1))
        if len(items) > self.limit:
            self.exhausted = True
            items = items[: self.limit]
        return items


def _gen_refinements(node, budget):
    """Yield all binary refinements of (the possibly polytomous) node."""
    if node.is_leaf:
        yield Node(label=node.label)
        return
    child_options = []
    for c in node.children:
        opts = budget.cap_list(_gen_refinements(c, budget))
        if not opts:  # pragma: no cover - defensive
            return
        child_options.append(opts)
    for combo in itertools.product(*child_options):
        for bt in _gen_binary_trees(list(combo)):
            yield bt


def _resolve_positions(tree, position_specs):
    """Map attachment position specs to concrete edges.

    Returns list of ("root", None) / ("node", node) tuples."""
    cls = node_clades(tree)
    resolved = []
    for spec in position_specs:
        if spec == "ROOT":
            resolved.append(("root", None))
            continue
        target = frozenset(spec)
        matches = [n for n, s in cls if s == target]
        if not matches:
            raise InvalidInput(
                "attachment position %r matches no node clade" % (spec,))
        if len(matches) > 1:
            raise InvalidInput(
                "attachment position %r matches multiple nodes" % (spec,))
        resolved.append(("node", matches[0]))
    return resolved


def _build_augmented(tree, attach_at_node, attach_at_root):
    """Copy tree, inserting attachment nodes on the chosen edges.

    attach_at_node: dict id(node) -> [new leaf labels]
    attach_at_root: list of new leaf labels for the root edge.
    """
    def rec(n):
        if n.is_leaf:
            return Node(label=n.label)
        children = []
        for c in n.children:
            cc = rec(c)
            extra = attach_at_node.get(id(c))
            if extra:
                cc = Node(children=[cc] + [Node(label=l) for l in extra])
            children.append(cc)
        return Node(children=children)

    new_root = rec(tree)
    if attach_at_root:
        new_root = Node(children=[new_root] + [Node(label=l) for l in attach_at_root])
    return new_root


def enumerate_completions(tree, target, position_specs, budget_limit):
    """Enumerate legal completions of `tree`.

    Returns (completions, exhausted) where completions is a dict
    split_set -> newick string (deduplicated; a binary tree is uniquely
    determined by its split set).
    """
    existing = leaf_set(tree)
    extra = existing - target
    if extra:
        raise Infeasible(
            "tree has leaves outside the target set: %s" % sorted(extra))
    missing = sorted(target - existing)
    positions = _resolve_positions(tree, position_specs)
    if missing and not positions:
        raise Infeasible(
            "missing leaves %s but no allowed attachment positions" % missing)

    budget = _Budget(budget_limit)
    completions = {}

    def generate():
        for assignment in itertools.product(range(len(positions)),
                                            repeat=len(missing)):
            attach_at_node = {}
            attach_at_root = []
            for leaf, pi in zip(missing, assignment):
                kind, node = positions[pi]
                if kind == "root":
                    attach_at_root.append(leaf)
                else:
                    attach_at_node.setdefault(id(node), []).append(leaf)
            aug = _build_augmented(tree, attach_at_node, attach_at_root)
            for refined in _gen_refinements(aug, budget):
                yield refined

    for comp in itertools.islice(generate(), budget_limit + 1):
        completions.setdefault(split_set(comp), to_newick(comp))
    if len(completions) > budget_limit:
        budget.exhausted = True
        # trim to budget, keeping deterministic order
        completions = dict(list(completions.items())[:budget_limit])
    return completions, budget.exhausted, positions


# ---------------------------------------------------------------------------
# Proven bounds (valid over the whole completion space, no enumeration)
# ---------------------------------------------------------------------------

def forced_clades(tree, positions):
    """Clades present in EVERY legal completion of `tree`.

    A clade of the data tree survives every completion iff no allowed
    attachment position lies strictly inside its subtree (refinements only
    ever add splits, never remove them; attachments above or at the node's
    own incoming edge leave its clade intact).  This is exactly the
    "do not treat unknown splits as zero support" direction: only clades
    that genuinely cannot disappear are counted.
    """
    cls = node_clades(tree)
    full = max((s for _, s in cls), key=len)
    pos_nodes = [n for kind, n in positions if kind == "node"]
    forced = set()
    for node, s in cls:
        if len(s) < 2 or s == full:
            continue
        # is any attachment node a *proper* descendant of `node`?
        subtree_ids = set()
        for desc, _ in node_clades(node):
            subtree_ids.add(id(desc))
        if any(id(p) in subtree_ids and p is not node for p in pos_nodes):
            continue
        forced.add(s)
    return forced


def split_possible(clade, tree):
    """Conservative superset test: can `clade` appear in some completion of
    `tree`?  Necessary condition used: its intersection with the existing
    leaves must be compatible with every clade of the data tree (nested or
    disjoint), which is exactly the condition for a split to appear in some
    binary refinement.  Attachment feasibility is ignored, so the test may
    accept impossible splits but never rejects a possible one."""
    existing = leaf_set(tree)
    a = clade & existing
    if len(a) <= 1:
        return True
    for _, c in node_clades(tree):
        inter = a & c
        if inter and inter != a and inter != c:
            return False
    return True


def proven_bounds(tree1, pos1, tree2, pos2, n_target):
    """Return (lower_bound_on_min, upper_bound_on_max) valid over ALL legal
    completion pairs.

    lower_bound_on_min: forced splits of one tree that can appear in no
        completion of the other must contribute to every distance.
    upper_bound_on_max: d = 2(n-2) - 2|C1 & C2| and C1 & C2 always contains
        the forced splits shared by both trees.
    """
    f1 = forced_clades(tree1, pos1)
    f2 = forced_clades(tree2, pos2)
    lower_min = sum(1 for c in f1 if not split_possible(c, tree2))
    lower_min += sum(1 for c in f2 if not split_possible(c, tree1))
    upper_max = 2 * max(n_target - 2, 0) - 2 * len(f1 & f2)
    return lower_min, upper_max


# ---------------------------------------------------------------------------
# Witness validation
# ---------------------------------------------------------------------------

def _prune_to(root, keep):
    def rec(n):
        if n.is_leaf:
            return Node(label=n.label) if n.label in keep else None
        kids = [k for k in (rec(c) for c in n.children) if k is not None]
        if not kids:
            return None
        if len(kids) == 1:
            return kids[0]
        return Node(children=kids)

    return rec(root)


def validate_completion(comp, orig_tree, target, position_specs):
    """Check that `comp` is an actually feasible legal completion:
    binary, correct leaf set, refines the data tree, and every added leaf
    sits at an allowed attachment position."""
    if leaf_set(comp) != frozenset(target):
        return False
    if not is_binary(comp):
        return False
    existing = leaf_set(orig_tree)
    pruned = _prune_to(comp, existing)
    if pruned is None or leaf_set(pruned) != existing:
        return False
    # pruned completion must refine the data tree
    orig_clades = set(s for _, s in node_clades(orig_tree) if len(s) >= 2)
    pruned_clades = set(s for _, s in node_clades(pruned) if len(s) >= 2)
    if not orig_clades <= pruned_clades:
        return False
    # every new leaf must attach at an allowed position
    positions = _resolve_positions(orig_tree, position_specs)
    allowed_clades = set()
    root_allowed = False
    for kind, node in positions:
        if kind == "root":
            root_allowed = True
        else:
            allowed_clades.add(_clade_of(orig_tree, node))
    comp_clades = [s for _, s in node_clades(comp)]
    for x in sorted(frozenset(target) - existing):
        containing = sorted((s for s in comp_clades if x in s), key=len)
        merge = next((s for s in containing if s & existing), None)
        if merge is None:
            return False
        inter = merge & existing
        if inter in allowed_clades:
            continue
        if root_allowed and inter == existing:
            continue
        return False
    return True


def _clade_of(tree, node):
    for n, s in node_clades(tree):
        if n is node:
            return s
    raise KeyError("node not in tree")  # pragma: no cover


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

DEFAULT_BUDGET = {"max_completions_per_tree": 100000,
                  "max_pair_evaluations": 2000000}


def solve(spec):
    target = frozenset(spec["target_leaves"])
    n = len(target)
    budget_spec = dict(DEFAULT_BUDGET)
    budget_spec.update(spec.get("budget", {}))

    if n < 3:
        return {"status": "exact", "n_target_leaves": n,
                "min_distance": 0, "max_distance": 0,
                "note": "fewer than 3 leaves: no non-trivial splits, "
                        "distance is identically 0"}

    trees, completions, exhausted = {}, {}, False
    positions = {}
    infeasible_reasons = []
    for key in ("tree1", "tree2"):
        try:
            tree = parse_newick(spec[key]["newick"])
        except ValueError as exc:
            raise InvalidInput("%s: %s" % (key, exc))
        labels = [s for nd, s in node_clades(tree) if nd.is_leaf]
        if len(set(labels)) != len(labels):
            raise InvalidInput("%s: duplicate leaf labels" % key)
        trees[key] = tree
        try:
            comps, exh, pos = enumerate_completions(
                tree, target, spec[key].get("allowed_attachments", []),
                budget_spec["max_completions_per_tree"])
        except Infeasible as exc:
            infeasible_reasons.append("%s: %s" % (key, exc))
            continue
        completions[key] = comps
        positions[key] = pos
        exhausted = exhausted or exh

    if infeasible_reasons:
        return {"status": "infeasible", "reasons": infeasible_reasons}

    # ---- pair evaluation: min/max only ever come from real pairs ----
    best_min = best_max = None
    witness_min = witness_max = None
    pair_budget = budget_spec["max_pair_evaluations"]
    pairs_done = 0
    items1 = sorted(completions["tree1"].items())
    items2 = sorted(completions["tree2"].items())
    stop = False
    for sp1, nk1 in items1:
        for sp2, nk2 in items2:
            if pairs_done >= pair_budget:
                stop = True
                break
            d = split_distance(sp1, sp2)
            pairs_done += 1
            if best_min is None or d < best_min:
                best_min, witness_min = d, (nk1, nk2)
            if best_max is None or d > best_max:
                best_max, witness_max = d, (nk1, nk2)
        if stop:
            break
    exhausted = exhausted or stop

    lb_min, ub_max = proven_bounds(trees["tree1"], positions["tree1"],
                                   trees["tree2"], positions["tree2"], n)

    def witness_entry(w, d):
        nk1, nk2 = w
        return {"distance": d, "tree1_newick": nk1 + ";",
                "tree2_newick": nk2 + ";",
                "tree1_valid_completion": validate_completion(
                    parse_newick(nk1), trees["tree1"], target,
                    spec["tree1"].get("allowed_attachments", [])),
                "tree2_valid_completion": validate_completion(
                    parse_newick(nk2), trees["tree2"], target,
                    spec["tree2"].get("allowed_attachments", []))}

    if not exhausted:
        # internal consistency: proven bounds must bracket the exact answer
        assert lb_min <= best_min, (lb_min, best_min)
        assert best_max <= ub_max, (best_max, ub_max)
        return {
            "status": "exact",
            "n_target_leaves": n,
            "n_completions_tree1": len(items1),
            "n_completions_tree2": len(items2),
            "pairs_evaluated": pairs_done,
            "min_distance": best_min,
            "min_witness": witness_entry(witness_min, best_min),
            "max_distance": best_max,
            "max_witness": witness_entry(witness_max, best_max),
        }

    return {
        "status": "budget_exhausted",
        "n_target_leaves": n,
        "n_completions_tree1_found": len(items1),
        "n_completions_tree2_found": len(items2),
        "pairs_evaluated": pairs_done,
        "min_distance": {
            "proven_lower_bound": lb_min,
            "best_witnessed": best_min,
            "witness": witness_entry(witness_min, best_min),
        },
        "max_distance": {
            "best_witnessed": best_max,
            "proven_upper_bound": ub_max,
            "witness": witness_entry(witness_max, best_max),
        },
        "note": "budget ran out; endpoints above are proven bounds over the "
                "full completion space plus the best actually witnessed "
                "pairs.  This is distinct from 'infeasible': legal "
                "completions do exist.",
    }


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: rf_completion_bounds.py INPUT.json\n")
        return 2
    with open(argv[1]) as fh:
        spec = json.load(fh)
    try:
        result = solve(spec)
    except InvalidInput as exc:
        result = {"status": "invalid_input", "reason": str(exc)}
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
