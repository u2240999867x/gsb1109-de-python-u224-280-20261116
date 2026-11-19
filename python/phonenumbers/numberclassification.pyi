from .phonenumber import PhoneNumber
from .util import UnicodeMixin

class NumberCategory:
    ORDINARY_NUMBER: str
    REGIONAL_SHORT_CODE: str
    ENTERPRISE_EXTENSION: str
    UNKNOWN: str

class NumberReach:
    INTERNATIONAL: str
    REGIONAL: str
    ENTERPRISE_ONLY: str
    EMERGENCY_ONLY: str
    UNKNOWN: str

class NumberConclusion(UnicodeMixin):
    number: PhoneNumber | None
    category: str
    reach: str
    region: str | None
    organization_domain: str | None
    extension: str | None
    evidence: tuple[str, ...]
    cost: str | None
    def __init__(self, number: PhoneNumber | None, category: str, reach: str,
                 region: str | None, organization_domain: str | None,
                 extension: str | None, evidence: tuple[str, ...],
                 cost: str | None = ...) -> None: ...
    def to_dict(self) -> dict[str, object]: ...
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> NumberConclusion: ...

class NumberClassification(UnicodeMixin):
    raw_input: str
    organization_domain: str | None
    region: str | None
    roaming: bool
    conclusions: tuple[NumberConclusion, ...]
    def __init__(self, raw_input: str, organization_domain: str | None,
                 region: str | None, roaming: bool,
                 conclusions: tuple[NumberConclusion, ...]) -> None: ...
    @property
    def categories(self) -> tuple[str, ...]: ...
    @property
    def reaches(self) -> tuple[str, ...]: ...
    def to_dict(self) -> dict[str, object]: ...
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> NumberClassification: ...

def classify_number(number: str, region: str | None = ..., roaming: bool = ...,
                    organization_domain: str | None = ...) -> NumberClassification: ...
def classify_numbers(numbers: list[str] | tuple[str, ...], region: str | None = ...,
                     roaming: bool = ...,
                     organization_domain: str | None = ...) -> tuple[NumberClassification, ...]: ...
