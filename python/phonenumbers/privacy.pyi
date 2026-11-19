__all__: list[str]

class NumberCategory:
    FIXED_LINE: str
    MOBILE: str
    UNCERTAIN: str

class DirectoryRecord:
    region: str
    number_category: str
    count: int
    def __new__(cls, region: str, number_category: str, count: int) -> DirectoryRecord: ...

class RegionLevel:
    region: str
    level: int
    parent: str | None
    def __new__(
        cls, region: str, level: int, parent: str | None = ...
    ) -> RegionLevel: ...

class PublishedGroup:
    region_level: int
    region: str
    number_category: str
    count: int
    is_merged: bool
    information_loss: int
    release_cost: int

class UnpublishableRange:
    region: str
    number_category: str
    count: int
    reason: str

class ReleasePlan:
    groups: tuple[PublishedGroup, ...]
    total_count: int
    total_information_loss: int
    total_release_cost: int
    publishable: bool
    unpublishable: tuple[UnpublishableRange, ...]
    reconciliation_total: int
    @property
    def reconciled(self) -> bool: ...

def check_reconciliation(plan: ReleasePlan) -> bool: ...
def build_release_plan(
    records: list[DirectoryRecord] | tuple[DirectoryRecord, ...],
    hierarchy: list[RegionLevel] | tuple[RegionLevel, ...],
    threshold: int,
    release_costs: dict[tuple[str, int], int] | None = ...,
) -> ReleasePlan: ...
