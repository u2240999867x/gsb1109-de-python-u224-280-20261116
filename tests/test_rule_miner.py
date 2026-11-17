import json
import subprocess
import sys
from pathlib import Path

import pytest

from rule_miner import RuleMinerError, mine_rules, parse_page
from rule_miner.cli import main as cli_main


PAGE_A = """
<html><body>
  <h2 class="ad" data-nid="noise-a">Ad heading</h2>
  <section class="card" data-nid="root-a">
    <h2 class="title" data-nid="title-a">Product A</h2>
    <span class="price" data-nid="price-a">10</span>
    <span class="ad" data-nid="ad-a">sponsored</span>
  </section>
</body></html>
"""

PAGE_B = """
<html><body>
  <div>
    <h2 class="ad" data-nid="noise-b">Ad heading</h2>
    <section class="card" data-nid="root-b">
      <h2 class="title" data-nid="title-b">Product B</h2>
      <span class="price" data-nid="price-b">20</span>
      <span class="ad" data-nid="ad-b">sponsored</span>
    </section>
  </div>
</body></html>
"""


def make_pages(markup_a=PAGE_A, markup_b=PAGE_B, roots=("root-a", "root-b")):
    return [
        parse_page(
            "a",
            markup_a,
            required={"title": ["title-a"], "price": ["price-a"]},
            excluded=["ad-a", "noise-a"],
            entity_roots=[roots[0]],
        ),
        parse_page(
            "b",
            markup_b,
            required={"title": ["title-b"], "price": ["price-b"]},
            excluded=["ad-b", "noise-b"],
            entity_roots=[roots[1]],
        ),
    ]


CANDIDATES = [
    {"field": "title", "selector": "h2", "cost": 1},
    {"field": "title", "selector": ".card .title", "cost": 3},
    {"field": "title", "selector": ".title", "cost": 2},
    {"field": "price", "selector": "span", "cost": 1},
    {"field": "price", "selector": ".card .price", "cost": 3},
    {"field": "price", "selector": ".price", "cost": 2},
]

ROOT_CANDIDATES = [
    {"selector": "section", "cost": 1},
    {"selector": ".card", "cost": 2},
]


def test_unique_minimum_cost_common_rule_set():
    result = mine_rules(make_pages(), CANDIDATES, ROOT_CANDIDATES)

    assert result["status"] == "optimal"
    assert result["ties"] is False
    assert len(result["solutions"]) == 1
    solution = result["solutions"][0]
    assert solution["root"] == "section"
    assert solution["rules"] == {"price": ".price", "title": ".title"}
    assert solution["total_cost"] == 1 + 2 + 2


def test_cheap_selectors_rejected_when_they_hit_excluded_nodes():
    pages = make_pages()
    result = mine_rules(pages, CANDIDATES, ROOT_CANDIDATES)
    solution = result["solutions"][0]

    assert "h2" not in solution["rules"].values()
    assert "span" not in solution["rules"].values()


def test_same_rules_apply_to_every_page():
    pages = make_pages()
    result = mine_rules(pages, CANDIDATES, ROOT_CANDIDATES)
    rules = result["solutions"][0]["rules"]

    for page in pages:
        for field_name, selector in rules.items():
            matched = {page.label_of(node) for node in page.tree.cssselect(selector)}
            assert page.required[field_name] <= {
                node for node in page.tree.cssselect(selector)
            }
            assert not (set(page.tree.cssselect(selector)) & page.excluded)
            assert field_name in rules


def test_ties_are_all_returned_in_stable_order():
    candidates = [
        {"field": "title", "selector": ".title", "cost": 2},
        {"field": "title", "selector": "h2.title", "cost": 2},
        {"field": "price", "selector": ".price", "cost": 2},
    ]
    first = mine_rules(make_pages(), candidates, ROOT_CANDIDATES)
    second = mine_rules(make_pages(), list(reversed(candidates)), ROOT_CANDIDATES)

    assert first["status"] == "optimal"
    assert first["ties"] is True
    assert len(first["solutions"]) == 2
    assert [sol["rules"]["title"] for sol in first["solutions"]] == [
        ".title",
        "h2.title",
    ]
    assert first["solutions"] == second["solutions"]


def test_field_must_stay_inside_entity_root():
    page_a = parse_page(
        "a",
        PAGE_A,
        required={"title": ["title-a"]},
        excluded=["ad-a", "noise-a"],
        entity_roots=["root-a"],
    )
    page_b = parse_page(
        "b",
        PAGE_B,
        required={"title": ["title-b"]},
        excluded=["ad-b", "noise-b"],
        entity_roots=["root-b"],
    )
    stray_markup = PAGE_B.replace(
        '<h2 class="ad" data-nid="noise-b">Ad heading</h2>',
        '<h2 class="title" data-nid="noise-b">Ad heading</h2>',
    )
    page_b_stray = parse_page(
        "b",
        stray_markup,
        required={"title": ["title-b"]},
        excluded=["ad-b"],
        entity_roots=["root-b"],
    )

    ok = mine_rules(
        [page_a, page_b],
        [{"field": "title", "selector": ".title", "cost": 1}],
        [{"selector": ".card", "cost": 1}],
    )
    assert ok["status"] == "optimal"

    blocked = mine_rules(
        [page_a, page_b_stray],
        [{"field": "title", "selector": ".title", "cost": 1}],
        [{"selector": ".card", "cost": 1}],
    )
    assert blocked["status"] == "infeasible"
    reason = json.dumps(blocked["conflicts"], ensure_ascii=False)
    assert "outside entity root" in reason
    assert "noise-b" in reason


def test_root_candidate_must_cover_annotated_roots_on_every_page():
    bad_roots = [{"selector": ".missing", "cost": 1}]
    result = mine_rules(make_pages(), CANDIDATES, bad_roots)

    assert result["status"] == "infeasible"
    conflict = result["conflicts"][0]
    assert conflict["kind"] == "entity_root"
    assert conflict["selector"] == ".missing"
    assert "root-a" in json.dumps(conflict)
    assert "root-b" in json.dumps(conflict)


def test_infeasible_when_no_selector_covers_all_pages():
    broken_b = PAGE_B.replace('class="title"', 'class="headline"')
    pages = make_pages(markup_b=broken_b)
    result = mine_rules(pages, CANDIDATES, ROOT_CANDIDATES)

    assert result["status"] == "infeasible"
    assert result["solutions"] == []
    dumped = json.dumps(result["conflicts"], ensure_ascii=False)
    assert "title-b" in dumped
    assert "missing required" in dumped


def test_required_node_also_excluded_is_reported_as_conflict():
    pages = [
        parse_page(
            "a",
            PAGE_A,
            required={"title": ["title-a"]},
            excluded=["title-a"],
            entity_roots=["root-a"],
        )
    ]
    result = mine_rules(
        pages,
        [{"field": "title", "selector": ".title", "cost": 1}],
        [{"selector": ".card", "cost": 1}],
    )

    assert result["status"] == "infeasible"
    kinds = {conflict["kind"] for conflict in result["conflicts"]}
    assert "required_vs_excluded" in kinds


def test_implicit_root_uses_whole_document():
    pages = make_pages()
    result = mine_rules(pages, CANDIDATES)

    assert result["status"] == "optimal"
    solution = result["solutions"][0]
    assert solution["root"] is None
    assert solution["root_cost"] == 0
    assert solution["rules"] == {"price": ".price", "title": ".title"}


def test_multiple_entity_roots_keep_each_record_together():
    markup = """
    <html><body>
      <section class="card" data-nid="root-1">
        <h2 class="title" data-nid="t1">One</h2>
        <span class="price" data-nid="p1">1</span>
      </section>
      <section class="card" data-nid="root-2">
        <h2 class="title" data-nid="t2">Two</h2>
        <span class="price" data-nid="p2">2</span>
      </section>
    </body></html>
    """
    page = parse_page(
        "multi",
        markup,
        required={"title": ["t1", "t2"], "price": ["p1", "p2"]},
        excluded=[],
        entity_roots=["root-1", "root-2"],
    )
    result = mine_rules(
        [page],
        [
            {"field": "title", "selector": ".title", "cost": 2},
            {"field": "price", "selector": ".price", "cost": 2},
        ],
        [{"selector": ".card", "cost": 1}],
    )

    assert result["status"] == "optimal"
    solution = result["solutions"][0]
    assert solution["rules"] == {"price": ".price", "title": ".title"}
    from lxml.cssselect import CSSSelector

    roots = CSSSelector(".card")(page.tree)
    records = [
        {
            "title": [el.text for el in CSSSelector(".title")(root)],
            "price": [el.text for el in CSSSelector(".price")(root)],
        }
        for root in roots
    ]
    assert records == [
        {"title": ["One"], "price": ["1"]},
        {"title": ["Two"], "price": ["2"]},
    ]


def test_required_node_outside_every_entity_root_is_conflict():
    markup = """
    <html><body>
      <h2 class="title" data-nid="loose">Loose</h2>
      <section class="card" data-nid="root-1">
        <h2 class="title" data-nid="t1">One</h2>
      </section>
    </body></html>
    """
    page = parse_page(
        "multi",
        markup,
        required={"title": ["t1", "loose"]},
        excluded=[],
        entity_roots=["root-1"],
    )
    result = mine_rules(
        [page],
        [{"field": "title", "selector": ".title", "cost": 2}],
        [{"selector": ".card", "cost": 1}],
    )

    assert result["status"] == "infeasible"
    conflict = next(
        item
        for item in result["conflicts"]
        if item["kind"] == "field_outside_entity_root"
    )
    assert conflict["node"] == "loose"


def test_invalid_candidate_selector_is_rejected_not_raised():
    candidates = CANDIDATES + [
        {"field": "title", "selector": "???", "cost": 0.1}
    ]
    result = mine_rules(make_pages(), candidates, ROOT_CANDIDATES)

    assert result["status"] == "optimal"
    assert "???" not in result["solutions"][0]["rules"].values()


def test_missing_field_candidate_raises():
    with pytest.raises(RuleMinerError):
        mine_rules(
            make_pages(),
            [{"field": "title", "selector": ".title", "cost": 1}],
        )


def test_json_file_and_cli(tmp_path):
    problem = {
        "pages": [
            {
                "name": "a",
                "html": PAGE_A,
                "required": {"title": ["title-a"], "price": ["price-a"]},
                "excluded": ["ad-a", "noise-a"],
                "entity_roots": ["root-a"],
            },
            {
                "name": "b",
                "html": PAGE_B,
                "required": {"title": ["title-b"], "price": ["price-b"]},
                "excluded": ["ad-b", "noise-b"],
                "entity_roots": ["root-b"],
            },
        ],
        "candidates": CANDIDATES,
        "root_candidates": ROOT_CANDIDATES,
    }
    path = tmp_path / "problem.json"
    path.write_text(json.dumps(problem), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "rule_miner", str(path), "--format", "json"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["solutions"][0]["rules"] == {
        "price": ".price",
        "title": ".title",
    }

    text_proc = subprocess.run(
        [sys.executable, "-m", "rule_miner", str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Unique optimum" in text_proc.stdout
    assert ".title" in text_proc.stdout


def test_cli_returns_nonzero_on_infeasible(tmp_path):
    problem = {
        "pages": [
            {
                "name": "a",
                "html": PAGE_A,
                "required": {"title": ["title-a"]},
                "excluded": ["title-a"],
                "entity_roots": ["root-a"],
            }
        ],
        "candidates": [{"field": "title", "selector": ".title", "cost": 1}],
        "root_candidates": [{"selector": ".card", "cost": 1}],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(problem), encoding="utf-8")

    rc = cli_main([str(path), "--format", "json"])
    assert rc == 1
