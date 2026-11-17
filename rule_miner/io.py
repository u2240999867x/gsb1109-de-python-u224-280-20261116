"""JSON input loading for the rule miner."""

import json
from pathlib import Path
from typing import Any

from .miner import Page, RuleMinerError, parse_page


def load_problem(path: str | Path) -> tuple[list[Page], list[dict[str, Any]], Any]:
    """Load pages, field candidates and optional root candidates from JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    base_dir = Path(path).parent
    pages = [
        _load_page(raw_page, base_dir) for raw_page in data.get("pages", [])
    ]
    candidates = data.get("candidates")
    if not candidates:
        raise RuleMinerError("input must contain at least one candidate")
    root_candidates = data.get("root_candidates")
    return pages, candidates, root_candidates


def _load_page(raw: dict[str, Any], base_dir: Path) -> Page:
    name = raw.get("name") or "page"
    if "html" in raw:
        markup = raw["html"]
    elif "html_file" in raw:
        markup = (base_dir / raw["html_file"]).read_text(encoding="utf-8")
    else:
        raise RuleMinerError(f"page {name}: provide 'html' or 'html_file'")
    required = raw.get("required", {})
    if not required:
        raise RuleMinerError(f"page {name}: at least one required field is needed")
    return parse_page(
        name=name,
        markup=markup,
        required=required,
        excluded=raw.get("excluded", ()),
        entity_roots=raw.get("entity_roots"),
    )
