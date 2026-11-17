import itertools
import random

import pytest

from pyquery.listdiff import (
    AllowedEdge,
    Candidate,
    Node,
    build_allowed_edges,
    levenshtein,
    match,
)


def make_pair(old_id, new_id, cost=0.0):
    return AllowedEdge(old_id, new_id, cost)


class TestLevenshtein:
    def test_basic(self):
        assert levenshtein("kitten", "sitting") == 3
        assert levenshtein("", "abc") == 3
        assert levenshtein("abc", "") == 3
        assert levenshtein("same", "same") == 0


class TestBuildAllowedEdges:
    def test_shared_anchor_allows_edge(self):
        old = [Node("o1", {"id": "1"}, {"title": "Apple"})]
        new = [Node("n1", {"id": "1"}, {"title": "Apple pie"})]
        edges = build_allowed_edges(old, new)
        assert edges == [AllowedEdge("o1", "n1", 4.0)]

    def test_conflicting_anchor_forbids_edge(self):
        old = [Node("o1", {"id": "1"})]
        new = [Node("n1", {"id": "2"})]
        assert build_allowed_edges(old, new) == []

    def test_disjoint_anchor_namespaces_forbid_edge(self):
        old = [Node("o1", {"sku": "x"})]
        new = [Node("n1", {"url": "y"})]
        assert build_allowed_edges(old, new) == []

    def test_max_cost_filters_expensive_edits(self):
        old = [Node("o1", {}, {"title": "abc"})]
        new = [Node("n1", {}, {"title": "xyz"})]
        assert build_allowed_edges(old, new, max_cost=2) == []
        edges = build_allowed_edges(old, new, max_cost=3)
        assert len(edges) == 1

    def test_structural_filter_can_veto(self):
        old = [Node("o1", {"id": "1"}, {"kind": "card"})]
        new = [Node("n1", {"id": "1"}, {"kind": "banner"})]
        edges = build_allowed_edges(
            old,
            new,
            structural_filter=lambda o, n: o.fields.get("kind")
            == n.fields.get("kind"),
        )
        assert edges == []


class TestMatch:
    def test_correction_without_move_is_distinguished(self):
        old = [
            Node("o1", {"id": "1"}, {"title": "Apple"}),
            Node("o2", {"id": "2"}, {"title": "Banana"}),
        ]
        new = [
            Node("n1", {"id": "1"}, {"title": "Apple pie"}),
            Node("n2", {"id": "2"}, {"title": "Banana"}),
        ]
        result = match(old, new, build_allowed_edges(old, new))

        assert set(result.pairs) == {"o1", "o2"}
        assert result.unique is True
        pairing = result.pairs["o1"]
        assert pairing.new_id == "n1"
        assert pairing.is_correction is True
        assert pairing.is_move is False
        assert pairing.field_changes[0].key == "title"
        assert result.moves() == []
        assert [p.old_id for p in result.corrections()] == ["o1"]

    def test_block_move_without_correction(self):
        old = [
            Node("o1", {"id": "1"}, {"title": "Alpha"}),
            Node("o2", {"id": "2"}, {"title": "Beta"}),
            Node("o3", {"id": "3"}, {"title": "Gamma"}),
        ]
        new = [
            Node("n2", {"id": "2"}, {"title": "Beta"}),
            Node("n1", {"id": "1"}, {"title": "Alpha"}),
            Node("n3", {"id": "3"}, {"title": "Gamma"}),
        ]
        result = match(old, new, build_allowed_edges(old, new))

        assert result.pairs["o1"].new_id == "n1"
        assert result.pairs["o1"].is_move is True
        assert result.pairs["o1"].is_correction is False
        assert result.pairs["o3"].is_move is False
        assert result.corrections() == []
        assert sorted(p.old_id for p in result.moves()) == ["o1", "o2"]

    def test_insertion_and_deletion_stay_unmatched(self):
        old = [
            Node("o1", {"id": "1"}, {"title": "Alpha"}),
            Node("o2", {"id": "2"}, {"title": "Beta"}),
        ]
        new = [
            Node("n1", {"id": "1"}, {"title": "Alpha"}),
            Node("n3", {"id": "3"}, {"title": "Gamma"}),
        ]
        result = match(old, new, build_allowed_edges(old, new))

        assert result.pairs["o1"].new_id == "n1"
        assert result.unmatched_old == ["o2"]
        assert result.unmatched_new == ["n3"]
        assert result.candidates == {}
        assert result.new_candidates == {}

    def test_duplicate_text_is_not_paired_by_traversal_order(self):
        old = [
            Node("o1", {}, {"title": "Same"}),
            Node("o2", {}, {"title": "Same"}),
        ]
        new = [
            Node("n1", {}, {"title": "Same"}),
            Node("n2", {}, {"title": "Same"}),
        ]
        result = match(old, new, build_allowed_edges(old, new))

        assert result.pairs == {}
        assert result.unique is False
        assert {c.new_id for c in result.candidates["o1"]} == {"n1", "n2"}
        assert {c.new_id for c in result.candidates["o2"]} == {"n1", "n2"}
        assert {c.old_id for c in result.new_candidates["n1"]} == {
            "o1",
            "o2",
        }
        assert {c.old_id for c in result.new_candidates["n2"]} == {
            "o1",
            "o2",
        }

    def test_anchor_breaks_tie_between_duplicate_texts(self):
        old = [
            Node("o1", {"id": "1"}, {"title": "Same"}),
            Node("o2", {"id": "2"}, {"title": "Same"}),
        ]
        new = [
            Node("n2", {"id": "2"}, {"title": "Same"}),
            Node("n1", {"id": "1"}, {"title": "Same"}),
        ]
        result = match(old, new, build_allowed_edges(old, new))

        assert result.unique is True
        assert result.pairs["o1"].new_id == "n1"
        assert result.pairs["o2"].new_id == "n2"
        assert result.pairs["o1"].is_move is True

    def test_no_old_node_is_claimed_twice(self):
        old = [
            Node("o1", {}, {"title": "X"}),
            Node("o2", {}, {"title": "X"}),
        ]
        new = [
            Node("n1", {}, {"title": "X"}),
            Node("n2", {}, {"title": "Y"}),
        ]
        edges = [
            make_pair("o1", "n1", 0.0),
            make_pair("o2", "n1", 0.0),
            make_pair("o1", "n2", 1.0),
            make_pair("o2", "n2", 1.0),
        ]
        result = match(old, new, edges)

        assert result.pairs == {}
        for options in result.candidates.values():
            new_ids = [c.new_id for c in options]
            assert len(new_ids) == len(set(new_ids))
        claimed = [
            c.new_id
            for options in result.candidates.values()
            for c in options
        ]
        assert sorted(claimed) == ["n1", "n1", "n2", "n2"]

    def test_disallowed_pairing_is_infeasible(self):
        old = [Node("o1", {"id": "1"}), Node("o2", {"id": "2"})]
        new = [Node("n1", {"id": "1"}), Node("n2", {"id": "2"})]
        result = match(old, new, [make_pair("o1", "n1")])

        assert result.pairs["o1"].new_id == "n1"
        assert result.unmatched_old == ["o2"]
        assert result.unmatched_new == ["n2"]

    def test_forced_expensive_match_loses_to_unmatched(self):
        old = [Node("o1", {}, {"title": "aaaa"})]
        new = [Node("n1", {}, {"title": "bbbb"})]
        result = match(
            old,
            new,
            [make_pair("o1", "n1", 10.0)],
            unmatched_cost=2.0,
        )
        assert result.pairs == {}
        assert result.unmatched_old == ["o1"]
        assert result.unmatched_new == ["n1"]

    def test_unmatched_is_listed_as_candidate_when_tied(self):
        old = [Node("o1", {}, {"title": "abc"})]
        new = [Node("n1", {}, {"title": "xyz"})]
        # Deletion plus insertion costs two penalties, which ties the edge.
        result = match(
            old,
            new,
            [make_pair("o1", "n1", 3.0)],
            unmatched_cost=1.5,
        )
        assert result.pairs == {}
        assert Candidate("o1", None, 1.5) in result.candidates["o1"]
        assert Candidate(None, "n1", 1.5) in result.new_candidates["n1"]

    def test_combined_insert_edit_and_block_move(self):
        old = [
            Node("o1", {"id": "1"}, {"title": "News A"}),
            Node("o2", {"id": "2"}, {"title": "News B"}),
            Node("o3", {"id": "3"}, {"title": "News C"}),
            Node("o4", {"id": "4"}, {"title": "News D"}),
        ]
        new = [
            Node("n5", {"id": "5"}, {"title": "Fresh"}),
            Node("n3", {"id": "3"}, {"title": "News C"}),
            Node("n4", {"id": "4"}, {"title": "News D moved"}),
            Node("n1", {"id": "1"}, {"title": "News A"}),
            Node("n2", {"id": "2"}, {"title": "News B"}),
        ]
        result = match(old, new, build_allowed_edges(old, new))

        assert result.unique is True
        assert result.unmatched_new == ["n5"]
        assert result.unmatched_old == []
        moved = {p.old_id for p in result.moves()}
        assert moved == {"o1", "o2", "o3", "o4"}
        corrected = {p.old_id for p in result.corrections()}
        assert corrected == {"o4"}
        pair_o4 = result.pairs["o4"]
        assert pair_o4.new_id == "n4"
        assert pair_o4.is_move and pair_o4.is_correction

    def test_unknown_node_in_edge_raises(self):
        with pytest.raises(ValueError):
            match([Node("o1")], [Node("n1")], [make_pair("oX", "n1")])

    def test_duplicate_node_ids_raise(self):
        with pytest.raises(ValueError):
            match([Node("o1"), Node("o1")], [Node("n1")], [])

    def test_empty_snapshots(self):
        result = match([], [], [])
        assert result.pairs == {}
        assert result.optimal_cost == 0.0
        assert result.unique is True

    def test_one_sided_snapshots_are_all_insertions_or_deletions(self):
        old = [Node("o1", {"id": "1"}), Node("o2", {"id": "2"})]
        deleted = match(old, [], [])
        assert deleted.unmatched_old == ["o1", "o2"]
        assert deleted.unmatched_new == []
        assert deleted.pairs == {}

        inserted = match([], old, [])
        assert inserted.unmatched_new == ["o1", "o2"]
        assert inserted.unmatched_old == []
        assert inserted.pairs == {}

    def test_field_and_anchor_changes_are_separately_reported(self):
        old = [Node("o1", {"id": "1"}, {"title": "A"})]
        new = [Node("n1", {"id": "9"}, {"title": "B"})]
        result = match(old, new, [make_pair("o1", "n1", 1.0)])
        pairing = result.pairs["o1"]
        assert pairing.field_changes[0].key == "title"
        assert pairing.anchor_changes[0].key == "id"
        assert pairing.anchor_changes[0].old_value == "1"
        assert pairing.anchor_changes[0].new_value == "9"


class TestOptimumConsensusAgainstBruteForce:
    def _brute_force(self, n, m, edge_cost, penalty):
        size = n + m
        big = 1.0e15
        matrix = [[big] * size for _ in range(size)]
        for i in range(n):
            for j in range(m):
                if (i, j) in edge_cost:
                    matrix[i][j] = edge_cost[(i, j)]
            matrix[i][m + i] = penalty
        for j in range(m):
            matrix[n + j][j] = penalty
        for j in range(m):
            for i in range(n):
                matrix[n + j][m + i] = 0.0

        optimum = None
        optimal_perms = []
        for perm in itertools.permutations(range(size)):
            total = sum(matrix[i][perm[i]] for i in range(size))
            if total >= big:
                continue
            if optimum is None or total < optimum:
                optimum = total
                optimal_perms = [perm]
            elif total == optimum:
                optimal_perms.append(perm)

        pair_sets = [
            frozenset(
                (i, perm[i])
                for i in range(n)
                if perm[i] < m
            )
            for perm in optimal_perms
        ]
        certain = set.intersection(*(set(pairs) for pairs in pair_sets))
        available = set.union(*(set(pairs) for pairs in pair_sets))
        row_unmatched = {
            i
            for perm in optimal_perms
            for i in range(n)
            if perm[i] >= m
        }
        col_unmatched = {
            j
            for perm in optimal_perms
            for j in range(m)
            if perm.index(j) >= n
        }
        return optimum, certain, available, row_unmatched, col_unmatched

    @pytest.mark.parametrize("seed", range(200))
    def test_random_graphs_match_exhaustive_enumeration(self, seed):
        rng = random.Random(seed)
        n = rng.randint(1, 4)
        m = rng.randint(1, 4)
        old = [Node(f"o{i}") for i in range(n)]
        new = [Node(f"n{j}") for j in range(m)]

        edges = []
        edge_cost = {}
        for i in range(n):
            for j in range(m):
                if rng.random() < 0.6:
                    cost = float(rng.randint(0, 3))
                    edges.append(AllowedEdge(f"o{i}", f"n{j}", cost))
                    edge_cost[(i, j)] = cost

        penalty = (
            rng.choice([0.5, 1.0, 2.0, 4.0])
            if rng.random() < 0.5
            else None
        )
        result = match(old, new, edges, unmatched_cost=penalty)
        expected_penalty = (
            penalty
            if penalty is not None
            else max((e.cost for e in edges), default=0.0) + 1.0
        )

        optimum, certain, available, row_unmatched, col_unmatched = (
            self._brute_force(n, m, edge_cost, expected_penalty)
        )

        assert result.optimal_cost == pytest.approx(optimum)

        assert {
            (int(p.old_id[1:]), int(p.new_id[1:]))
            for p in result.pairs.values()
        } == certain

        observed_available = {
            (int(c.old_id[1:]), int(c.new_id[1:]))
            for options in result.candidates.values()
            for c in options
            if c.new_id is not None
        }
        assert observed_available == available - certain

        assert {
            int(oid[1:])
            for oid, options in result.candidates.items()
            if any(c.new_id is None for c in options)
        } == row_unmatched & {i for i, _ in available}

        assert {
            int(nid[1:])
            for nid, options in result.new_candidates.items()
            if any(c.old_id is None for c in options)
        } == col_unmatched & {j for _, j in available}

        assert [int(oid[1:]) for oid in result.unmatched_old] == sorted(
            i
            for i in range(n)
            if i not in {ii for ii, _ in certain}
            and not any(ii == i for ii, _ in available)
        )
        assert [int(nid[1:]) for nid in result.unmatched_new] == sorted(
            j
            for j in range(m)
            if j not in {jj for _, jj in certain}
            and not any(jj == j for _, jj in available)
        )
