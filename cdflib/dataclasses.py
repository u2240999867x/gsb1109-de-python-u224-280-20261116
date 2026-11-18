from dataclasses import dataclass, field
from numbers import Number
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

import numpy as np

__all__ = [
    "ADRInfo",
    "CDFInfo",
    "CDRInfo",
    "GDRInfo",
    "VDRInfo",
    "AEDR",
    "VDR",
    "AttData",
    "SparseValue",
    "SparseData",
    "SPARSE_WRITTEN",
    "SPARSE_PAD",
    "SPARSE_PREVIOUS",
    "SPARSE_MISSING",
]

#: A logical record whose value is physically stored in the CDF file.
SPARSE_WRITTEN = "written"
#: A logical record generated from the variable's pad value.
SPARSE_PAD = "pad"
#: A logical record that reuses the value of an earlier physical record.
SPARSE_PREVIOUS = "previous"
#: A logical record with no physical value and no usable previous value.
SPARSE_MISSING = "missing"


@dataclass
class ADRInfo:
    scope: int
    next_adr_loc: int
    attribute_number: int
    num_gr_entry: int
    max_gr_entry: int
    num_z_entry: int
    max_z_entry: int
    first_z_entry: int
    first_gr_entry: int
    name: str


@dataclass
class CDFInfo:
    """
    CDF information.
    """

    #: Path to the CDF.
    CDF: Union[str, Path]
    #: CDF version.
    Version: str
    #: Endianness of the CDF.
    Encoding: int
    #: Row/column majority.
    Majority: str
    #: zVariable names.
    rVariables: List[str]
    #: rVariable names.
    zVariables: List[str]
    #: List of dictionary objects that map attribute_name to scope.
    Attributes: List[Dict[str, str]]
    Copyright: str
    #: Checksum indicator.
    Checksum: bool
    #: Number of dimensions for rVariables.
    Num_rdim: int
    #: Dimensional sizes for rVariables.
    rDim_sizes: List[int]
    #: If CDF is compressed at file level.
    Compressed: bool
    #: Last updated leap second table.
    LeapSecondUpdate: Optional[int] = None


@dataclass
class CDRInfo:
    encoding: int
    copyright_: str
    version: str
    majority: int
    format_: bool
    md5: bool
    post25: bool


@dataclass
class GDRInfo:
    first_zvariable: int
    first_rvariable: int
    first_adr: int
    num_zvariables: int
    num_rvariables: int
    num_attributes: int
    rvariables_num_dims: int
    rvariables_dim_sizes: List[int]
    eof: int
    leapsecond_updated: Optional[int] = None


@dataclass
class VDRInfo:
    """
    Variable data record info.
    """

    #: Name of the variable.
    Variable: str
    #: Variable number.
    Num: int
    #: Variable type: zVariable or rVariable.
    Var_Type: str
    #: Variable CDF data type.
    Data_Type: int
    Data_Type_Description: str
    #: Number of elements of the variable.
    Num_Elements: int
    #: Dimensionality of variable record.
    Num_Dims: int
    #: Shape of the variable record.
    Dim_Sizes: List[int]
    Sparse: str
    #: Maximum written variable number (0-based).
    Last_Rec: int
    #: Record variance.
    Rec_Vary: int
    #: Dimensional variance(s).
    Dim_Vary: Union[List[int], List[bool]]  #: a doc
    Compress: int
    #: Padded value (if set).
    Pad: Optional[Union[str, np.ndarray]] = None
    #: Blocking factor (if variable is compressed).
    Block_Factor: Optional[int] = None


@dataclass
class AEDR:
    entry: Union[str, np.ndarray]
    data_type: int
    num_elements: int
    next_aedr: int
    entry_num: int
    num_strings: Optional[int] = None


@dataclass
class VDR:
    data_type: int
    section_type: int
    next_vdr_location: int
    variable_number: int
    head_vxr: int
    last_vxr: int
    max_rec: int
    name: str
    num_dims: int
    dim_sizes: List[int]
    compression_bool: bool
    compression_level: int
    blocking_factor: int
    dim_vary: Union[List[int], List[bool]]
    record_vary: int
    num_elements: int
    sparse: int
    pad: Optional[Union[str, np.ndarray]] = None


@dataclass
class AttData:
    """
    Attribute data.
    """

    #: Number of bytes for each entry value.
    Item_Size: int
    #: CDF data type.
    Data_Type: str
    #: Number of values extracted.
    Num_Items: int
    #: Data as a scalar value, a numpy array or a string.
    Data: Union[Number, str, np.ndarray]


@dataclass
class SparseValue:
    """
    Provenance for one logical sample of a variable.

    ``status`` is one of ``"written"``, ``"pad"``, ``"previous"`` or
    ``"missing"``.  ``source_record`` is the zero-based record that physically
    supplied the value: the record itself for ``"written"``, and the earlier
    physical record for ``"previous"``.  It is ``None`` when no record in the
    file supplies a value.
    """

    status: str
    source_record: Optional[int] = None
    #: Zero-based record number within the file the sample was read from.
    record: int = 0

    @property
    def is_written(self) -> bool:
        return self.status == SPARSE_WRITTEN

    @property
    def is_virtual(self) -> bool:
        return self.status in (SPARSE_PAD, SPARSE_PREVIOUS, SPARSE_MISSING)

    #: Alias for :attr:`source_record`.
    @property
    def source(self) -> Optional[int]:
        return self.source_record

    #: Alias for :attr:`status`.
    @property
    def kind(self) -> str:
        return self.status


@dataclass
class SparseData:
    """
    Logical variable data together with per-sample provenance.
    """

    variable: str
    #: Logical values, one per requested record.
    data: np.ndarray
    #: One SparseValue per logical record, in the same order as ``data``.
    provenance: List[SparseValue] = field(default_factory=list)
    #: Name or path of the file the values were read from.
    source_file: Optional[str] = None
    #: First record of the returned window.
    start_rec: int = 0

    def __len__(self) -> int:
        return len(self.provenance)

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, item):
        return self.data[item]

    def __array__(self, dtype=None):
        return np.asarray(self.data, dtype=dtype)

    @property
    def samples(self) -> List[SparseValue]:
        """Alias for :attr:`provenance`."""
        return self.provenance

    @property
    def statuses(self) -> List[str]:
        return [sample.status for sample in self.provenance]

    @property
    def source_records(self) -> List[Optional[int]]:
        return [sample.source_record for sample in self.provenance]

    @property
    def written(self) -> np.ndarray:
        """Boolean mask selecting physically written records."""
        return np.asarray([sample.is_written for sample in self.provenance], dtype=bool)

    @property
    def virtual(self) -> np.ndarray:
        """Boolean mask selecting synthesized or missing records."""
        return ~self.written

    def to_dataframe(self):
        """Return one row per logical sample as a pandas DataFrame."""
        import pandas as pd

        return pd.DataFrame(list(self.records()))

    def records(self) -> Iterator[Dict[str, object]]:
        """Yield one flat dictionary per logical sample."""
        values = np.asarray(self.data)
        for index, sample in enumerate(self.provenance):
            yield {
                "variable": self.variable,
                "record": sample.record,
                "value": values[index],
                "status": sample.status,
                "source_record": sample.source_record,
                "source_file": self.source_file,
            }
