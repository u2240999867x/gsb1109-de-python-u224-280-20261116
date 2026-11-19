from .numberrange import NumberRange

__all__: list[str]

RANGE_VALID: str
RANGE_PENDING: str
RANGE_INVALID: str
RANGE_STATUSES: tuple[str, ...]

class ClassifiedRange:
    number_range: NumberRange
    status: str
    number_type: int | None
    def __init__(self, number_range: NumberRange, status: str, number_type: int | None = ...) -> None: ...
    @property
    def number_count(self) -> int: ...
    def to_dict(self) -> dict[str, object]: ...
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ClassifiedRange: ...

class RangeRuleClassifier:
    region_code: str
    def __init__(self, region_code: str) -> None: ...
    def metadata_for_code(self, country_code: int) -> object: ...
    def classify(self, number_range: NumberRange) -> list[ClassifiedRange]: ...
