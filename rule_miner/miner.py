"""Mine one common extraction rule set from several annotated DOM pages."""

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable

from lxml import html as lxml_html
from lxml.cssselect import CSSSelector, SelectorError


class RuleMinerError(Exception):
    """Base error for rule mining inputs."""


@dataclass(frozen=True)
class Ref:
    """Stable reference to an annotated node inside a page."""

    page: str
    label: str
    element: Any


@dataclass
class Page:
    name: str
    tree: Any
    roots: list[Any]
    required: dict[str, set[Any]]
    excluded: set[Any]
    ordinal: dict[int, Any]
    by_nid: dict[str, Any]

    def label_of(self, element: Any) -> str:
        nid = element.get("data-nid")
        if nid is not None:
            return nid
        for index, candidate in self.ordinal.items():
            if candidate is element:
                return f"#{index}"
        return "<?>"

    def ref_of(self, element: Any) -> Ref:
        return Ref(self.name, self.label_of(element), element)


@dataclass
class Candidate:
    field: str
    selector: str
    cost: float
    root: str | None = None
    parsed: CSSSelector | None = None
    error: str | None = None


@dataclass
class RootChoice:
    selector: str | None
    cost: float
    roots_by_page: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class Evaluation:
    candidate: Candidate
    feasible: bool
    reasons: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Solution:
    rules: dict[str, str]
    total_cost: float
    root_selector: str | None
    root_cost: float
    field_costs: dict[str, float]


def parse_page(
    name: str,
    markup: str | Any,
    required: dict[str, Iterable[Any]],
    excluded: Iterable[Any] = (),
    entity_roots: Iterable[Any] | None = None,
) -> Page:
    """Build a Page from HTML markup and annotation references.

    Node references may be ``data-nid`` values (with or without a leading
    ``#``), integer document-order ordinals, or CSS selector strings that
    resolve to exactly one element.
    """
    if isinstance(markup, str):
        tree = lxml_html.fromstring(markup)
    else:
        tree = markup
    elements = list(tree.iter())
    ordinal = {index: element for index, element in enumerate(elements)}
    by_nid = {
        element.get("data-nid"): element
        for element in elements
        if element.get("data-nid") is not None
    }

    def resolve(ref: Any) -> Any:
        if hasattr(ref, "tag"):
            return ref
        if isinstance(ref, int):
            if ref not in ordinal:
                raise RuleMinerError(f"page {name}: ordinal {ref} does not exist")
            return ordinal[ref]
        text = str(ref)
        key = text[1:] if text.startswith("#") else text
        if key in by_nid:
            return by_nid[key]
        try:
            matched = tree.cssselect(text)
        except SelectorError as exc:
            raise RuleMinerError(
                f"page {name}: cannot resolve ref {text!r}: {exc}"
            ) from exc
        if len(matched) != 1:
            raise RuleMinerError(
                f"page {name}: ref {text!r} matches {len(matched)} nodes, "
                "expected exactly one"
            )
        return matched[0]

    if entity_roots is None:
        roots = [tree]
    else:
        roots = [resolve(ref) for ref in entity_roots]
    required_sets = {
        field_name: {resolve(ref) for ref in refs}
        for field_name, refs in required.items()
    }
    excluded_set = {resolve(ref) for ref in excluded}
    return Page(name, tree, roots, required_sets, excluded_set, ordinal, by_nid)


def _parse_candidate(raw: dict[str, Any]) -> Candidate:
    try:
        field_name = raw["field"]
        selector = raw["selector"]
        cost = float(raw["cost"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuleMinerError(f"invalid candidate {raw!r}: {exc}") from exc
    root = raw.get("root")
    candidate = Candidate(field_name, selector, cost, root)
    try:
        candidate.parsed = CSSSelector(selector)
    except SelectorError as exc:
        candidate.error = str(exc)
    return candidate


def _parse_root_candidate(raw: dict[str, Any]) -> Candidate:
    try:
        selector = raw["selector"]
        cost = float(raw["cost"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuleMinerError(f"invalid root candidate {raw!r}: {exc}") from exc
    candidate = Candidate("", selector, cost)
    try:
        candidate.parsed = CSSSelector(selector)
    except SelectorError as exc:
        candidate.error = str(exc)
    return candidate


def _is_descendant(node: Any, ancestor: Any) -> bool:
    current = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.getparent()
    return False


def _containing_roots(node: Any, roots: list[Any]) -> list[Any]:
    return [root for root in roots if _is_descendant(node, root)]


def evaluate_candidate(
    candidate: Candidate, pages: list[Page], root_choice: RootChoice
) -> Evaluation:
    """Check one field selector against every page under one root choice."""
    evaluation = Evaluation(candidate, True)
    if candidate.error is not None:
        evaluation.feasible = False
        for page in pages:
            evaluation.reasons[page.name] = [
                f"invalid selector: {candidate.error}"
            ]
        return evaluation

    for page in pages:
        page_reasons: list[str] = []
        roots = root_choice.roots_by_page[page.name]
        matched: set[Any] = set()
        outside_roots: set[Any] = set()
        for root in roots:
            for node in candidate.parsed(root):
                matched.add(node)
        for node in candidate.parsed(page.tree):
            if node not in matched:
                outside_roots.add(node)

        wanted = page.required.get(candidate.field, set())
        missing = [page.label_of(node) for node in wanted - matched]
        hit_excluded = [
            page.label_of(node) for node in matched & page.excluded
        ]
        stray = [page.label_of(node) for node in outside_roots]
        if missing:
            page_reasons.append("missing required: " + ", ".join(sorted(missing)))
        if hit_excluded:
            page_reasons.append("hits excluded: " + ", ".join(sorted(hit_excluded)))
        if stray:
            page_reasons.append(
                "matches outside entity root: " + ", ".join(sorted(stray))
            )
        if page_reasons:
            evaluation.feasible = False
            evaluation.reasons[page.name] = page_reasons
    return evaluation


def _build_root_choices(
    pages: list[Page], root_candidates: list[Candidate] | None
) -> tuple[list[RootChoice], dict[str, list[str]]]:
    """Return feasible root choices plus failure reasons for the others."""
    failures: dict[str, list[str]] = {}
    if not root_candidates:
        choice = RootChoice(None, 0.0, {page.name: page.roots for page in pages})
        return [choice], failures

    choices: list[RootChoice] = []
    for candidate in root_candidates:
        if candidate.error is not None:
            failures[candidate.selector] = [
                f"invalid selector: {candidate.error}"
            ]
            continue
        roots_by_page: dict[str, list[Any]] = {}
        reasons: list[str] = []
        for page in pages:
            matched = set(candidate.parsed(page.tree))
            wanted = set(page.roots)
            if not wanted <= matched:
                missing = [page.label_of(node) for node in wanted - matched]
                reasons.append(
                    f"page {page.name}: missing entity root "
                    + ", ".join(sorted(missing))
                )
            bad_excluded = matched & page.excluded
            if bad_excluded:
                labels = [page.label_of(node) for node in bad_excluded]
                reasons.append(
                    f"page {page.name}: root hits excluded "
                    + ", ".join(sorted(labels))
                )
            roots_by_page[page.name] = list(matched)
        if reasons:
            failures[candidate.selector] = reasons
        else:
            choices.append(
                RootChoice(candidate.selector, candidate.cost, roots_by_page)
            )
    return choices, failures


def mine_rules(
    pages: list[Page],
    candidates: list[dict[str, Any]] | list[Candidate],
    root_candidates: list[dict[str, Any]] | list[Candidate] | None = None,
) -> dict[str, Any]:
    """Find the minimum-cost common rule set.

    The same selectors are applied to every page; one selector is chosen per
    field, all extractions stay inside a common entity root, required nodes
    must be covered and excluded nodes must never be matched.
    """
    if not pages:
        raise RuleMinerError("at least one annotated page is required")
    field_candidates = [
        item if isinstance(item, Candidate) else _parse_candidate(item)
        for item in candidates
    ]
    parsed_roots = None
    if root_candidates is not None:
        parsed_roots = [
            item if isinstance(item, Candidate) else _parse_root_candidate(item)
            for item in root_candidates
        ]

    fields = sorted({candidate.field for candidate in field_candidates})
    annotated = set()
    for page in pages:
        annotated.update(page.required)
    missing_fields = sorted(set(annotated) - set(fields))
    if missing_fields:
        raise RuleMinerError(
            "no candidate offered for annotated fields: "
            + ", ".join(missing_fields)
        )

    root_choices, root_failures = _build_root_choices(pages, parsed_roots)
    if not root_choices:
        return {
            "status": "infeasible",
            "solutions": [],
            "ties": False,
            "conflicts": _root_conflicts(root_failures),
        }

    grouping_conflicts = _check_record_grouping(pages)
    if grouping_conflicts:
        return {
            "status": "infeasible",
            "solutions": [],
            "ties": False,
            "conflicts": grouping_conflicts,
        }

    best: list[Solution] = []
    diagnostics: dict[str, list[dict[str, Any]]] = {}
    for root_choice in sorted(root_choices, key=lambda item: (item.cost, item.selector or "")):
        feasible_by_field: dict[str, list[tuple[Candidate, Evaluation]]] = {}
        field_reports: list[dict[str, Any]] = []
        for field_name in fields:
            pool = [
                candidate
                for candidate in field_candidates
                if candidate.field == field_name
                and (candidate.root is None or candidate.root == root_choice.selector)
            ]
            feasible: list[tuple[Candidate, Evaluation]] = []
            for candidate in sorted(pool, key=lambda item: (item.cost, item.selector)):
                evaluation = evaluate_candidate(candidate, pages, root_choice)
                if evaluation.feasible:
                    feasible.append((candidate, evaluation))
                else:
                    field_reports.append(
                        {
                            "field": field_name,
                            "selector": candidate.selector,
                            "cost": candidate.cost,
                            "reasons": evaluation.reasons,
                        }
                    )
            feasible_by_field[field_name] = feasible

        if any(not pool for pool in feasible_by_field.values()):
            diagnostics[root_choice.selector or "<implicit root>"] = field_reports
            continue

        options = [feasible_by_field[field_name] for field_name in fields]
        for combo in product(*options):
            rules = {candidate.field: candidate.selector for candidate, _ in combo}
            field_costs = {
                candidate.field: candidate.cost for candidate, _ in combo
            }
            total = root_choice.cost + sum(field_costs.values())
            solution = Solution(
                rules=rules,
                total_cost=total,
                root_selector=root_choice.selector,
                root_cost=root_choice.cost,
                field_costs=field_costs,
            )
            if not best or total < best[0].total_cost:
                best = [solution]
            elif total == best[0].total_cost:
                best.append(solution)

    if not best:
        return {
            "status": "infeasible",
            "solutions": [],
            "ties": False,
            "conflicts": _field_conflicts(pages, field_candidates, diagnostics),
        }

    unique = _deduplicate_solutions(best)
    return {
        "status": "optimal",
        "solutions": [_solution_to_dict(solution) for solution in unique],
        "ties": len(unique) > 1,
        "conflicts": [],
    }


def _deduplicate_solutions(solutions: list[Solution]) -> list[Solution]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    ordered = sorted(
        solutions,
        key=lambda item: (
            item.root_selector or "",
            tuple(sorted(item.rules.items())),
        ),
    )
    result = []
    for solution in ordered:
        key = (solution.root_selector or "", tuple(sorted(solution.rules.items())))
        if key not in seen:
            seen.add(key)
            result.append(solution)
    return result


def _solution_to_dict(solution: Solution) -> dict[str, Any]:
    return {
        "root": solution.root_selector,
        "rules": dict(sorted(solution.rules.items())),
        "root_cost": solution.root_cost,
        "field_costs": dict(sorted(solution.field_costs.items())),
        "total_cost": solution.total_cost,
    }


def _check_record_grouping(pages: list[Page]) -> list[dict[str, Any]]:
    """Flag required nodes that no annotated entity root can own."""
    conflicts: list[dict[str, Any]] = []
    for page in pages:
        for field_name, nodes in sorted(page.required.items()):
            for node in sorted(nodes, key=page.label_of):
                if not _containing_roots(node, page.roots):
                    conflicts.append(
                        {
                            "kind": "field_outside_entity_root",
                            "page": page.name,
                            "field": field_name,
                            "node": page.label_of(node),
                        }
                    )
    return conflicts


def _root_conflicts(failures: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {"kind": "entity_root", "selector": selector, "reasons": reasons}
        for selector, reasons in sorted(failures.items())
    ]


def _field_conflicts(
    pages: list[Page],
    candidates: list[Candidate],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for root_label, reports in sorted(diagnostics.items()):
        for report in reports:
            conflicts.append({"kind": "field", "root": root_label, **report})

    required_nodes: dict[str, list[Ref]] = {}
    excluded_refs = [
        page.ref_of(node) for page in pages for node in page.excluded
    ]
    for page in pages:
        for field_name, nodes in page.required.items():
            required_nodes.setdefault(field_name, []).extend(
                page.ref_of(node) for node in nodes
            )

    for field_name, refs in sorted(required_nodes.items()):
        pool = [candidate for candidate in candidates if candidate.field == field_name]
        for required_ref in refs:
            for excluded_ref in excluded_refs:
                covering = [
                    candidate.selector
                    for candidate in pool
                    if _covers(candidate, required_ref)
                    and _covers(candidate, excluded_ref)
                ]
                if covering and not any(
                    _covers(candidate, required_ref)
                    and not _covers(candidate, excluded_ref)
                    for candidate in pool
                ):
                    conflicts.append(
                        {
                            "kind": "required_vs_excluded",
                            "field": field_name,
                            "required": f"{required_ref.page}:{required_ref.label}",
                            "excluded": f"{excluded_ref.page}:{excluded_ref.label}",
                            "selectors_forced_to_match_both": sorted(covering),
                        }
                    )
    return conflicts


def _covers(candidate: Candidate, ref: Ref) -> bool:
    if candidate.parsed is None:
        return False
    root = ref.element.getroottree().getroot()
    return any(node is ref.element for node in candidate.parsed(root))
