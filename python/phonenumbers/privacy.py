"""Privacy-preserving release of regional phone-number category statistics.

A directory holds phone-number records classified by region and by number
category.  Statistics derived from such a directory may only be published as
aggregate counts over groups that

 - contain at least a configured minimum number of records (the threshold),
 - never expose an individual number, and
 - only merge regions along the levels of an explicitly allowed hierarchy.

This module turns the directory into non-overlapping published groups that
cover every input record, choosing the permitted grouping with the smallest
total information loss (publication cost is only used to break ties).
Records whose category cannot be told apart as fixed-line or mobile stay in
an explicit ``UNCERTAIN`` category: such records are never forced into the
mobile category and categories are never merged with each other.

A published group always corresponds to a complete cell of the region
hierarchy: merging to a cell publishes *every* more specific region below
that cell as one group.  Partial merges (a merged label that silently omits
some of its member regions) are deliberately refused, because they make the
label misleading and leave subtotals that could be subtracted to reconstruct
a smaller group.  The legal partitions are therefore the cuts of the
hierarchy forest, which are mutually exclusive by construction.

Information loss of a group is the number of records it contains multiplied
by the hierarchy level of its cell (level 0, an unmerged group, costs no
loss), so the plan generalizes as little as possible.  Publication cost is
charged once per published region cell; cells absent from the ``release_costs``
mapping default to their hierarchy level.  The plan minimizes total
information loss first and only uses total publication cost to break ties.

The result always reconciles against the original directory: when a release
is possible, the published group counts sum to exactly the number of input
records.  Only the single, mutually exclusive partition is returned -- no
extra subtotals are emitted, because subtotals could be subtracted to
reconstruct smaller groups.  When no legal partition exists, no group counts
are released at all and the ranges that cannot reach the threshold under any
allowed merge are reported instead.

Example:

>>> from phonenumbers.privacy import (
...     DirectoryRecord, RegionLevel, NumberCategory,
...     build_release_plan)
>>> records = [
...     DirectoryRecord("Berlin", NumberCategory.FIXED_LINE, 30),
...     DirectoryRecord("Berlin", NumberCategory.MOBILE, 4),
...     DirectoryRecord("Munich", NumberCategory.MOBILE, 6),
...     DirectoryRecord("Berlin", NumberCategory.UNCERTAIN, 3),
...     DirectoryRecord("Munich", NumberCategory.UNCERTAIN, 2),
... ]
>>> hierarchy = [
...     RegionLevel("Berlin", 0),
...     RegionLevel("Munich", 0),
...     RegionLevel("Germany", 1, "Berlin"),
...     RegionLevel("Germany", 1, "Munich"),
... ]
>>> plan = build_release_plan(records, hierarchy, threshold=5)
>>> plan.publishable
True
>>> plan.total_count
45
>>> [(g.region, g.number_category, g.count, g.is_merged)
...  for g in plan.groups]  # doctest: +NORMALIZE_WHITESPACE
[('Berlin', 'fixed_line', 30, False),
 ('Germany', 'fixed_or_mobile', 5, True),
 ('Germany', 'mobile', 10, True)]
>>> plan.reconciled
True
"""

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import namedtuple


__all__ = ['NumberCategory', 'DirectoryRecord', 'RegionLevel',
           'PublishedGroup', 'UnpublishableRange', 'ReleasePlan',
           'build_release_plan', 'check_reconciliation']


class NumberCategory(object):
    """Number categories that a directory record can belong to.

    ``UNCERTAIN`` is a first-class category rather than a missing value:
    records that cannot be classified as either fixed-line or mobile must be
    published under ``UNCERTAIN`` and are never assigned to ``MOBILE``.
    """
    FIXED_LINE = "fixed_line"
    MOBILE = "mobile"
    # Fixed-line versus mobile cannot be distinguished for these records.
    UNCERTAIN = "fixed_or_mobile"


class DirectoryRecord(namedtuple("DirectoryRecord",
                                 ["region", "number_category", "count"])):
    """A single raw group of the directory.

    Only aggregate counts are accepted; individual phone numbers are never
    needed or held.

    Arguments:
    region -- The most specific region the records are known under.
    number_category -- One of the ``NumberCategory`` constants.
    count -- Number of records in this raw group (a positive integer).
    """
    __slots__ = ()


class RegionLevel(namedtuple("RegionLevel",
                             ["region", "level", "parent"])):
    """An allowed publication level for a region.

    Arguments:
    region -- The region label used for the (possibly merged) group.
    level -- Hierarchy level, 0 is the most specific (unmerged) level.
    parent -- Region label at the next more specific level, or ``None`` at
              level 0.  Merging is only allowed along these parent links.
    """
    __slots__ = ()

    def __new__(cls, region, level, parent=None):
        return super(RegionLevel, cls).__new__(cls, region, level, parent)


class PublishedGroup(namedtuple("PublishedGroup",
                                ["region_level", "region", "number_category",
                                 "count", "is_merged", "information_loss",
                                 "release_cost"])):
    """A group of the mutually exclusive published partition.

    Arguments:
    region_level -- Hierarchy level of the group (0 means unmerged).
    region -- Region label under which the group is published.
    number_category -- The ``NumberCategory`` of every record in the group.
    count -- Number of records covered by the group (at least the threshold
             when the plan is publishable).
    is_merged -- Whether the group merges records from more specific regions.
    information_loss -- Loss charged for the grouping of this group.
    release_cost -- Publication cost charged for the group's region cell.
    """
    __slots__ = ()


class UnpublishableRange(namedtuple("UnpublishableRange",
                                    ["region", "number_category", "count",
                                     "reason"])):
    """A raw range that no legal grouping can bring above the threshold.

    The count is retained for internal verification only.  When a plan is not
    publishable no group counts may be released, so this report must stay
    internal rather than being published as a statistic.

    Arguments:
    region -- The most specific region of the withheld records.
    number_category -- The ``NumberCategory`` of the withheld records.
    count -- Number of withheld records.
    reason -- Human-readable explanation of why publication is impossible.
    """
    __slots__ = ()


class ReleasePlan(namedtuple("ReleasePlan",
                             ["groups", "total_count",
                              "total_information_loss", "total_release_cost",
                              "publishable", "unpublishable",
                              "reconciliation_total"])):
    """The outcome of planning a privacy-preserving release.

    Arguments:
    groups -- Tuple of ``PublishedGroup`` objects, a mutually exclusive
              partition of every record when the plan is publishable; empty
              otherwise.
    total_count -- Number of records in the original directory.
    total_information_loss -- Summed information loss over the groups.
    total_release_cost -- Summed publication cost over the groups.
    publishable -- Whether every record could be placed in a group that
                   reaches the threshold.
    unpublishable -- Tuple of ``UnpublishableRange`` objects; empty when the
                     plan is fully publishable.
    reconciliation_total -- Number of records accounted for by the plan,
                            which equals ``total_count`` by construction.
    """
    __slots__ = ()

    @property
    def reconciled(self):
        """Whether the plan totals reconcile against the directory."""
        return self.reconciliation_total == self.total_count


def check_reconciliation(plan):
    """Assert that a plan accounts for the directory without losing records.

    For a publishable plan the published group counts must sum to the
    directory total.  For an unpublishable plan the whole directory is
    withheld (the plan releases zero groups) and the withheld total must
    match the directory total.  Raises ``AssertionError`` otherwise.
    """
    if plan.publishable:
        counted = sum(group.count for group in plan.groups)
        assert counted == plan.total_count, (
            "release plan covers %d records but directory holds %d"
            % (counted, plan.total_count))
        assert not plan.unpublishable
    else:
        assert not plan.groups, (
            "an unpublishable plan must not release any group counts")
        withheld = sum(item.count for item in plan.unpublishable)
        assert withheld <= plan.total_count
        # The blocking ranges explain why nothing is released; the withheld
        # total reconciles against the directory even when other ranges are
        # individually safe, because those safe ranges are withheld too.
        assert plan.reconciliation_total == plan.total_count, (
            "withheld release covers %d records but directory holds %d"
            % (plan.reconciliation_total, plan.total_count))
    return True


def _validate_inputs(records, hierarchy, threshold, release_costs):
    if not isinstance(threshold, int) or threshold < 1:
        raise ValueError("threshold must be a positive integer, got %r"
                         % (threshold,))
    if release_costs is None:
        release_costs = {}
    if not isinstance(release_costs, dict):
        raise ValueError("release_costs must be a dict keyed by "
                         "(region, level)")

    valid_categories = (NumberCategory.FIXED_LINE,
                        NumberCategory.MOBILE,
                        NumberCategory.UNCERTAIN)
    raw_counts = {}
    for record in records:
        if not isinstance(record, DirectoryRecord):
            raise ValueError("every record must be a DirectoryRecord, "
                             "got %r" % (record,))
        if record.number_category not in valid_categories:
            raise ValueError("unknown number category %r"
                             % (record.number_category,))
        if not isinstance(record.count, int) or record.count < 1:
            raise ValueError("record count must be a positive integer, "
                             "got %r" % (record.count,))
        key = (record.region, record.number_category)
        if key in raw_counts:
            raise ValueError("duplicate directory record for %r" % (key,))
        raw_counts[key] = record.count

    # The hierarchy is validated as a forest.  Each RegionLevel entry at a
    # level above 0 links one child cell (named by its parent region label) to
    # a merged target cell; several entries may target the same merged cell to
    # express a many-to-one merge, and hierarchy depths may be uneven.  A
    # child cell may roll up into at most one target, which keeps every
    # record in exactly one subtree and makes the resulting partition
    # mutually exclusive.
    all_cells = set()
    label_to_cell = {}
    child_to_parent = {}
    children_of = {}

    def declare_cell(cell_key):
        if cell_key not in children_of:
            children_of[cell_key] = []
        all_cells.add(cell_key)
        label = cell_key[0]
        if label in label_to_cell and label_to_cell[label] != cell_key:
            raise ValueError("region label %r used at different hierarchy "
                             "levels" % label)
        label_to_cell[label] = cell_key

    entries = []
    for entry in hierarchy:
        if not isinstance(entry, RegionLevel):
            raise ValueError("every hierarchy entry must be a RegionLevel, "
                             "got %r" % (entry,))
        if not isinstance(entry.level, int) or entry.level < 0:
            raise ValueError("hierarchy level must be a non-negative "
                             "integer, got %r" % (entry.level,))
        target = (entry.region, entry.level)
        declare_cell(target)
        entries.append(entry)
        if entry.level == 0 and entry.parent is not None:
            raise ValueError("level 0 region %r must not declare a parent"
                             % entry.region)

    # Parent labels are resolved in a second pass so entry order does not
    # matter; each label identifies exactly one declared cell.
    for entry in entries:
        if entry.level == 0:
            continue
        if entry.parent is None:
            raise ValueError("region %r at level %d must declare its parent "
                             "region" % (entry.region, entry.level))
        if entry.parent not in label_to_cell:
            raise ValueError("region %r at level %d references undeclared "
                             "parent region %r"
                             % (entry.region, entry.level, entry.parent))
        child = label_to_cell[entry.parent]
        target = (entry.region, entry.level)
        if child[1] >= target[1]:
            raise ValueError("hierarchy link %r -> %r does not go to a more "
                             "general level" % (child, target))
        if child in child_to_parent and child_to_parent[child] != target:
            raise ValueError("region %r may only roll up into one merged "
                             "cell, found %r and %r"
                             % (child[0], child_to_parent[child], target))
        child_to_parent[child] = target
        if child not in children_of[target]:
            children_of[target].append(child)

    # Every record region needs a level-0 hierarchy cell.
    for region, _category in raw_counts:
        if (region, 0) not in all_cells:
            raise ValueError("region %r has no level 0 hierarchy entry"
                             % region)

    # Resolve publication costs for every declared cell.  Cells absent from
    # release_costs default to their hierarchy level, so merging is never
    # free by accident while unmerged publication costs nothing.
    cost_of = {}
    for cell_key in all_cells:
        if cell_key in release_costs:
            cost = release_costs[cell_key]
            if cost < 0:
                raise ValueError("release cost for %r is negative"
                                 % (cell_key,))
        else:
            cost = cell_key[1]
        cost_of[cell_key] = cost

    return raw_counts, child_to_parent, children_of, cost_of


def build_release_plan(records, hierarchy, threshold, release_costs=None):
    """Build the minimum-loss, threshold-safe partition of a directory.

    Arguments:
    records -- Iterable of ``DirectoryRecord`` objects holding the raw
               (region, category, count) groups of the directory.  Individual
               phone numbers are never needed nor accepted.
    hierarchy -- Iterable of ``RegionLevel`` objects describing every region
                 merge that is allowed.
    threshold -- Minimum number of records a published group must contain.
    release_costs -- Optional dict mapping ``(region, level)`` cells to their
                     publication cost.  Cells absent from the dict default to
                     their hierarchy level.

    Returns a ``ReleasePlan``.  When every record can be covered by groups of
    at least ``threshold`` records, ``groups`` is a mutually exclusive
    partition ordered deterministically and the plan is publishable.
    Otherwise ``publishable`` is ``False``, ``groups`` is empty and
    ``unpublishable`` lists the raw ranges that cannot be released; the
    whole directory is withheld in that case and no subtotals are produced.
    """
    raw_counts, child_to_parent, children_of, cost_of = _validate_inputs(
        records, hierarchy, threshold, release_costs)

    total_count = sum(raw_counts.values())
    if not raw_counts:
        return ReleasePlan(groups=(), total_count=0,
                           total_information_loss=0, total_release_cost=0,
                           publishable=True, unpublishable=(),
                           reconciliation_total=0)

    categories = sorted(set(category for (_region, category) in raw_counts))
    # A root has no parent link; roots include both level-0 regions that are
    # never merged and top-level merged cells.
    roots = sorted(cell_key for cell_key in children_of
                   if cell_key not in child_to_parent)

    # Per-category record count for every cell, accumulated from the level-0
    # leaves up to the merge roots.
    count_at_cell = {}

    def accumulate(cell_key, category):
        memo_key = (cell_key, category)
        if memo_key in count_at_cell:
            return count_at_cell[memo_key]
        if cell_key[1] == 0:
            count = raw_counts.get((cell_key[0], category), 0)
        else:
            count = sum(accumulate(child, category)
                        for child in children_of[cell_key])
        count_at_cell[memo_key] = count
        return count

    for category in categories:
        for root in roots:
            accumulate(root, category)

    # Dynamic programming per category over the hierarchy forest.  At each
    # cell the choice is either STOP (publish the cell as one merged group,
    # legal only when its count reaches the threshold) or DESCEND (partition
    # into the child cells).  Categories are solved independently, so
    # UNCERTAIN records can never be reclassified as mobile.
    # Option values are (information_loss, release_cost, stop_flag); the
    # lexicographic order keeps information loss as the primary objective.
    best = {}
    stop_here = {}

    def solve(cell_key, category):
        memo_key = (cell_key, category)
        if memo_key in best:
            return best[memo_key]
        level = cell_key[1]
        count = count_at_cell.get(memo_key, 0)
        children = children_of[cell_key]

        # An empty cell holds no records for this category (and neither does
        # anything below it, since a cell count includes all descendants), so
        # it needs no group and must not force its siblings to merge.
        if count == 0:
            best[memo_key] = (0, 0)
            stop_here[memo_key] = False
            return best[memo_key]

        options = []
        if count >= threshold:
            options.append((count * level, cost_of[cell_key], True))
        if children:
            child_values = [solve(child, category) for child in children]
            if all(value is not None for value in child_values):
                child_loss = sum(value[0] for value in child_values)
                child_cost = sum(value[1] for value in child_values)
                options.append((child_loss, child_cost, False))
        if not options:
            best[memo_key] = None
            return None
        options.sort(key=lambda option: (option[0], option[1],
                                         not option[2]))
        selected = options[0]
        best[memo_key] = (selected[0], selected[1])
        stop_here[memo_key] = selected[2]
        return best[memo_key]

    feasible = True
    for category in categories:
        for root in roots:
            if solve(root, category) is None:
                feasible = False

    if not feasible:
        unpublishable = []

        def collect_withheld(cell_key, category):
            if solve(cell_key, category) is not None:
                return
            if cell_key[1] == 0:
                count = raw_counts.get((cell_key[0], category), 0)
                if count:
                    unpublishable.append(UnpublishableRange(
                        region=cell_key[0], number_category=category,
                        count=count,
                        reason=("no allowed merge along the region "
                                "hierarchy reaches the threshold of %d "
                                "records" % threshold)))
                return
            for child in children_of[cell_key]:
                collect_withheld(child, category)

        for category in categories:
            for root in roots:
                collect_withheld(root, category)

        plan = ReleasePlan(groups=(), total_count=total_count,
                           total_information_loss=0, total_release_cost=0,
                           publishable=False,
                           unpublishable=tuple(unpublishable),
                           reconciliation_total=total_count)
        check_reconciliation(plan)
        return plan

    # Walk the chosen options back out into concrete, non-overlapping groups.
    groups = []

    def emit(cell_key, category):
        memo_key = (cell_key, category)
        count = count_at_cell.get(memo_key, 0)
        if count == 0:
            return
        if stop_here[memo_key]:
            level = cell_key[1]
            groups.append(PublishedGroup(
                region_level=level, region=cell_key[0],
                number_category=category, count=count,
                is_merged=level > 0,
                information_loss=count * level,
                release_cost=cost_of[cell_key]))
            return
        for child in children_of[cell_key]:
            emit(child, category)

    for category in categories:
        for root in roots:
            emit(root, category)

    groups.sort(key=lambda group: (group.region_level, group.region,
                                   group.number_category))
    groups_tuple = tuple(groups)
    total_loss = sum(group.information_loss for group in groups_tuple)
    total_cost = sum(group.release_cost for group in groups_tuple)
    plan = ReleasePlan(groups=groups_tuple, total_count=total_count,
                       total_information_loss=total_loss,
                       total_release_cost=total_cost,
                       publishable=True, unpublishable=(),
                       reconciliation_total=total_count)
    check_reconciliation(plan)
    return plan
