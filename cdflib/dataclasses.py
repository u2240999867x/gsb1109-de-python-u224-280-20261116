from dataclasses import dataclass
from numbers import Number
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np

__all__ = [
    "ADRInfo",
    "CDFInfo",
    "CDRInfo",
    "GDRInfo",
    "VDRInfo",
    "AEDR",
    "VDR",
    "AEDR",
    "AttData",
    "AttEntry",
    "GlobalAttEntry",
    "VariableAttEntry",
    "AttConflict",
]


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
class AttEntry:
    """
    A single attribute entry descriptor record (AEDR).

    Attributes are sparse collections: entry numbers may have holes, so the
    original entry number is always carried explicitly instead of being implied
    by position.
    """

    #: Original entry number written in the AEDR. For a variable attribute this
    #: is the variable number the entry is bound to; for a global attribute it
    #: is the global attribute entry number.
    entry_num: int
    #: Numeric CDF data type (e.g. 52 for CDF_UCHAR, 4 for CDF_INT4).
    data_type: int
    #: Name of the CDF data type.
    data_type_description: str
    #: Number of elements in the entry.
    num_elements: int
    #: The entry value, as a scalar, string, or numpy array.
    value: Union[Number, str, np.ndarray]


@dataclass
class GlobalAttEntry(AttEntry):
    """
    A global attribute entry. ``entry_num`` is the global entry number, which
    may have gaps relative to the surrounding entries.
    """


@dataclass
class VariableAttEntry(AttEntry):
    """
    A variable attribute entry bound to a specific variable.

    The binding is carried as the variable name and r/z scope, so filtering
    or reordering variables (which changes the variable number) does not break
    the link.
    """

    #: Name of the variable this entry is bound to.
    variable: str
    #: ``"zVariable"`` or ``"rVariable"``.
    variable_scope: str


@dataclass
class AttConflict:
    """
    Reports multiple entries that claim the same target in a flattened,
    keyed representation of an attribute.

    CDF never stores two entries with the same target, but a flattening step
    (e.g. collapsing the r and z chains of a variable attribute onto one
    variable-number key) can make them collide. The collision is reported
    instead of silently keeping only the last value.
    """

    #: Target key that collided: an int entry number or a variable name.
    target: Union[int, str]
    #: Every entry claiming the target, in file order.
    entries: List[AttEntry]
    #: Human-readable description of the collision.
    message: str
