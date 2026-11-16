"""Core analysis: attribute IF-MIB counters to statistic items via ifStack.

Principles
----------
- The ifStack graph is a DAG in the general case; multiple parents
  (an interface stacked on several lower layers) are supported.
- Only collected ifStack relations are used. Interfaces are never
  classified by name pattern (e.g. "Gi0/1.100" is NOT assumed to be a
  sub-interface of "Gi0/1").
- Cycles, missing roles and incomplete aggregation member data do not
  abort the report: the determinable range is listed and the affected
  interfaces are marked indeterminate / uncovered with explicit reasons.
- Counters are read-only input; nothing here mutates them.
"""

from __future__ import annotations

from .model import ItemSpec, Plan

AGGREGATE_IF_TYPES = frozenset({"ieee8023adLag"})

HIGHER_OF_METERING = "higher-layer of metering interface"
LOWER_OF_METERING = "lower-layer member of metering interface"


def _scc(nodes, adj):
    """Tarjan's strongly connected components (recursive; node counts are small)."""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    components = []
    counter = [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(comp)

    for v in nodes:
        if v not in index:
            visit(v)
    return components


def _reachable(start, adj, blocked):
    """Transitive reachability from `start`, never traversing blocked nodes."""
    seen = set()
    work = [start]
    while work:
        v = work.pop()
        for w in adj.get(v, ()):
            if w in blocked or w in seen:
                continue
            seen.add(w)
            work.append(w)
    seen.discard(start)
    return seen


def _matches(if_index, info, spec):
    if not (spec.if_indexes or spec.if_types or spec.roles):
        return True
    if if_index in spec.if_indexes:
        return True
    if info is not None:
        if info.if_type is not None and info.if_type in spec.if_types:
            return True
        if info.role is not None and info.role in spec.roles:
            return True
    return False


def build_report(plan: Plan) -> dict:
    info = {i.if_index: i for i in plan.interfaces}
    counter_indexes = set(plan.counters)

    nodes = set(info) | counter_indexes
    up = {}
    down = {}
    for link in plan.stack:
        up.setdefault(link.lower, set()).add(link.higher)
        down.setdefault(link.higher, set()).add(link.lower)
        nodes.add(link.higher)
        nodes.add(link.lower)

    warnings = []
    notes = []

    # --- cycle detection on the directed higher->lower graph -----------------
    directed = {n: set(down.get(n, ())) for n in nodes}
    components = _scc(sorted(nodes), directed)
    cycle_groups = [sorted(c) for c in components if len(c) > 1]
    for n in sorted(nodes):
        if n in directed.get(n, ()):
            cycle_groups.append([n])
    cycle_nodes = set()
    for group in cycle_groups:
        cycle_nodes.update(group)
        warnings.append(
            "ifStack cycle detected among interfaces %s; stacking relations "
            "of these interfaces are not trusted" % group
        )

    # --- metering layer ------------------------------------------------------
    metering = set()
    for n in sorted(nodes):
        if n in plan.layer.if_indexes:
            metering.add(n)
        elif info.get(n) is not None and info[n].if_type in plan.layer.if_types:
            metering.add(n)
    effective_metering = metering - cycle_nodes
    for m in sorted(metering & cycle_nodes):
        warnings.append(
            "metering-layer interface %d lies in an ifStack cycle; it is "
            "excluded from metering until the cycle is resolved" % m
        )

    blocked = cycle_nodes
    uppers = {m: _reachable(m, up, blocked) for m in effective_metering}
    lowers = {m: _reachable(m, down, blocked) for m in effective_metering}

    # --- statistic items -----------------------------------------------------
    items = plan.items or (ItemSpec(name="all"),)
    item_members = {}
    for spec in items:
        members = sorted(
            m for m in effective_metering if _matches(m, info.get(m), spec)
        )
        item_members[spec.name] = members
        if not members:
            warnings.append(
                "statistic item '%s' selects no metering-layer interface" % spec.name
            )

    item_of = {}
    for spec in items:
        for m in item_members[spec.name]:
            if m in item_of:
                warnings.append(
                    "metering interface %d is selected by both '%s' and '%s'; "
                    "its counters would be counted in both items"
                    % (m, item_of[m], spec.name)
                )
            else:
                item_of[m] = spec.name

    # --- conflicts between metering interfaces -------------------------------
    reported_pairs = set()
    for m in sorted(effective_metering):
        stacked = sorted((uppers[m] | lowers[m]) & (effective_metering - {m}))
        for other in stacked:
            pair = (min(m, other), max(m, other))
            if pair in reported_pairs:
                continue
            reported_pairs.add(pair)
            warnings.append(
                "metering interfaces %d and %d are stacked on each other; "
                "counting both double-counts their shared traffic" % pair
            )

    # --- exclusion relations --------------------------------------------------
    excluded_via = {}
    for m in sorted(effective_metering):
        for u in sorted(uppers[m]):
            if u in effective_metering:
                continue
            excluded_via.setdefault(u, []).append(
                {"metering": m, "relation": HIGHER_OF_METERING}
            )
        for l in sorted(lowers[m]):
            if l in effective_metering:
                continue
            excluded_via.setdefault(l, []).append(
                {"metering": m, "relation": LOWER_OF_METERING}
            )

    for idx, via in sorted(excluded_via.items()):
        items_covering = {item_of.get(v["metering"]) for v in via} - {None}
        if len(items_covering) > 1:
            warnings.append(
                "interface %d is covered by metering interfaces of multiple "
                "items %s (multi-parent stacking); its traffic is represented "
                "in each of them" % (idx, sorted(items_covering))
            )

    # --- per-sample assignment -------------------------------------------------
    assignments = {}
    cycle_group_of = {}
    for group in cycle_groups:
        for n in group:
            cycle_group_of[n] = group

    for idx in sorted(counter_indexes):
        if idx in cycle_nodes:
            assignments[idx] = {
                "disposition": "indeterminate",
                "reason": "part of ifStack cycle %s" % cycle_group_of[idx],
            }
        elif idx in effective_metering:
            item = item_of.get(idx)
            if item is not None:
                assignments[idx] = {"disposition": "metered", "item": item}
            else:
                assignments[idx] = {
                    "disposition": "uncovered",
                    "detail": "metering-layer interface not selected by any "
                    "statistic item (role/type missing or not matched)",
                }
                warnings.append(
                    "metering-layer interface %d is not selected by any "
                    "statistic item" % idx
                )
        elif idx in excluded_via:
            assignments[idx] = {
                "disposition": "excluded",
                "via": excluded_via[idx],
            }
        else:
            if idx in info:
                detail = "no ifStack relation to any metering interface"
            else:
                detail = "no interface record (ifType/role unknown) and no " "ifStack relation"
            assignments[idx] = {
                "disposition": "uncovered",
                "detail": detail + "; not attributed by interface name",
            }

    # --- aggregation member completeness --------------------------------------
    for n in sorted(nodes):
        entry = info.get(n)
        if entry is None or entry.if_type not in AGGREGATE_IF_TYPES:
            continue
        members = sorted(down.get(n, ()))
        label = "%d (%s)" % (n, entry.if_name or "unnamed")
        if not members:
            warnings.append(
                "aggregate interface %s has no member data in ifStack; only "
                "the aggregate itself is determinable, potential member "
                "counters stay uncovered and are not inferred by name" % label
            )
        else:
            notes.append(
                "aggregate interface %s members as collected: %s; membership "
                "completeness cannot be verified from the collected data"
                % (label, members)
            )

    for m in sorted(effective_metering):
        if m not in counter_indexes:
            warnings.append("metering interface %d has no counter sample" % m)

    # --- assemble report -------------------------------------------------------
    uncovered = [
        {"ifIndex": idx, "detail": a["detail"]}
        for idx, a in sorted(assignments.items())
        if a["disposition"] == "uncovered"
    ]
    indeterminate = [
        {"ifIndex": idx, "reason": a["reason"]}
        for idx, a in sorted(assignments.items())
        if a["disposition"] == "indeterminate"
    ]

    item_reports = []
    for spec in items:
        members = item_members[spec.name]
        member_set = set(members)
        used = []
        for m in members:
            entry = info.get(m)
            used.append(
                {
                    "ifIndex": m,
                    "ifName": entry.if_name if entry else None,
                    "counters": (
                        dict(plan.counters[m]) if m in plan.counters else None
                    ),
                }
            )
        excluded = []
        for idx, via in sorted(excluded_via.items()):
            own_via = [v for v in via if v["metering"] in member_set]
            if own_via and idx in counter_indexes:
                entry = info.get(idx)
                excluded.append(
                    {
                        "ifIndex": idx,
                        "ifName": entry.if_name if entry else None,
                        "via": own_via,
                    }
                )
        item_reports.append(
            {
                "name": spec.name,
                "used": used,
                "excluded": excluded,
                "uncovered": uncovered + indeterminate,
            }
        )

    return {
        "device": plan.device,
        "metering_layer": {
            "ifTypes": sorted(plan.layer.if_types),
            "ifIndexes": sorted(plan.layer.if_indexes),
        },
        "metering_interfaces": sorted(effective_metering),
        "items": item_reports,
        "uncovered": uncovered,
        "indeterminate": indeterminate,
        "assignments": {str(k): v for k, v in sorted(assignments.items())},
        "counters": {str(k): dict(v) for k, v in sorted(plan.counters.items())},
        "warnings": warnings,
        "notes": notes,
    }


def diff_reports(old: dict, new: dict) -> dict:
    """Audit which original samples are re-attributed after a layer switch.

    Counter values are echoed read-only from the reports; they are never
    modified.
    """
    old_assignments = old.get("assignments", {})
    new_assignments = new.get("assignments", {})
    reassigned = []
    unchanged = []
    for idx in sorted(set(old_assignments) | set(new_assignments), key=int):
        before = old_assignments.get(idx)
        after = new_assignments.get(idx)
        counters = new.get("counters", {}).get(idx) or old.get("counters", {}).get(idx)
        record = {"ifIndex": int(idx), "counters": counters}
        if before != after:
            record["from"] = before
            record["to"] = after
            reassigned.append(record)
        else:
            record["assignment"] = after
            unchanged.append(record)
    return {
        "device": new.get("device", old.get("device")),
        "metering_layer_from": old.get("metering_layer"),
        "metering_layer_to": new.get("metering_layer"),
        "reassigned": reassigned,
        "unchanged": unchanged,
    }
