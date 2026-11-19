import datetime
from typing import Any

from .phonenumber import PhoneNumber
from .phonenumberutil import ValidationResult

__all__: list[str]

class ReachabilityStatus:
    REACHABLE: str
    NOT_REACHABLE: str
    UNKNOWN: str
    CONFLICT: str
    NO_EVIDENCE: str

class ReceiptOutcome:
    REACHABLE: str
    NOT_REACHABLE: str
    UNKNOWN: str

def contact_point_for_number(numobj: PhoneNumber, extension: str | None = ...) -> ContactScope: ...
def length_possibility(numobj: PhoneNumber) -> int: ...
def length_is_possible(length_result: int) -> bool: ...

class ContactScope:
    _mutable: bool
    contact_point: str
    extension: str | None
    def __init__(self, contact_point: str, extension: str | None = ...) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __ne__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

class MetadataAssessment:
    _mutable: bool
    valid: bool
    region: str | None
    number_type: int
    library_version: Any
    assessed_at: datetime.datetime
    def __init__(
        self,
        valid: bool,
        region: str | None,
        number_type: int,
        library_version: Any,
        assessed_at: datetime.datetime,
    ) -> None: ...

class BusinessReceipt:
    _mutable: bool
    scope: ContactScope
    outcome: str
    version: Any
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    supersedes_version: Any
    version_namespace: Any
    holder_epoch: int
    library_version: Any
    recorded_at: datetime.datetime
    notes: str | None
    def __init__(
        self,
        scope: ContactScope,
        outcome: str,
        version: Any,
        effective_from: datetime.datetime,
        effective_to: datetime.datetime | None = ...,
        supersedes_version: Any = ...,
        version_namespace: Any = ...,
        holder_epoch: int = ...,
        library_version: Any = ...,
        recorded_at: datetime.datetime | None = ...,
        notes: str | None = ...,
    ) -> None: ...
    def effective_at(self, at: datetime.datetime) -> bool: ...

class ReachabilityConclusion:
    _mutable: bool
    scope: ContactScope
    at: datetime.datetime
    status: str
    length_result: int | None
    metadata_valid: bool | None
    receipts: tuple[BusinessReceipt, ...]
    withdrawn_receipts: tuple[BusinessReceipt, ...]
    conflicting_receipts: tuple[BusinessReceipt, ...]
    reasons: tuple[str, ...]
    def __init__(
        self,
        scope: ContactScope,
        at: datetime.datetime,
        status: str,
        length_result: int | None = ...,
        metadata_valid: bool | None = ...,
        receipts: tuple[BusinessReceipt, ...] | None = ...,
        withdrawn_receipts: tuple[BusinessReceipt, ...] | None = ...,
        conflicting_receipts: tuple[BusinessReceipt, ...] | None = ...,
        reasons: tuple[str, ...] | None = ...,
    ) -> None: ...

class ContactRecord:
    contact_point: str
    def __init__(self, contact_point: str) -> None: ...
    def record_length(self, assessed_at: datetime.datetime, numobj: PhoneNumber, library_version: Any) -> int: ...
    def record_metadata(self, assessed_at: datetime.datetime, numobj: PhoneNumber, library_version: Any) -> MetadataAssessment: ...
    def mark_transfer(self, at: datetime.datetime) -> None: ...
    def add_receipt(self, receipt: BusinessReceipt) -> BusinessReceipt: ...
    @property
    def receipts(self) -> list[BusinessReceipt]: ...
    @property
    def transfers(self) -> list[datetime.datetime]: ...
    def latest_length(self, at: datetime.datetime) -> tuple[datetime.datetime, int, Any] | None: ...
    def latest_metadata(self, at: datetime.datetime) -> MetadataAssessment | None: ...
    def effective_receipts(self, at: datetime.datetime, extension: str | None) -> list[BusinessReceipt]: ...

class ReachabilityRegistry:
    library_version: Any
    def __init__(self, library_version: Any) -> None: ...
    def upgrade_library(self, new_version: Any) -> None: ...
    def record_for(self, contact_point: str) -> ContactRecord: ...
    def record_length(self, scope: ContactScope, assessed_at: datetime.datetime, numobj: PhoneNumber, library_version: Any = ...) -> int: ...
    def record_metadata(self, scope: ContactScope, assessed_at: datetime.datetime, numobj: PhoneNumber, library_version: Any = ...) -> MetadataAssessment: ...
    def add_receipt(self, receipt: BusinessReceipt) -> BusinessReceipt: ...
    def mark_transfer(self, contact_point: str, at: datetime.datetime) -> None: ...
    def conclusion_for(self, scope: ContactScope, at: datetime.datetime) -> ReachabilityConclusion: ...
