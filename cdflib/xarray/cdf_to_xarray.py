import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

import numpy as np
import numpy.typing as npt
import xarray as xr

from cdflib import CDF
from cdflib.dataclasses import AttData, VDRInfo
from cdflib.epochs import CDFepoch as cdfepoch
from cdflib.logging import logger

ISTP_TO_XARRAY_ATTRS = {"FIELDNAM": "standard_name", "LABLAXIS": "long_name", "UNITS": "units"}

_DEPEND_REGEX = re.compile(r"^depend_(\d+)$", re.IGNORECASE)


def _find_xarray_plotting_values(var_att_dict: Dict[str, str]) -> Dict[str, str]:
    """
    This is a simple function that looks through a variable attribute dictionary for ISTP attributes that are similar
    to ones used natively by Xarray, specifically their plotting routines.  If some are found, this returns a dictionary
    object of this new xarray attributes
    :param var_att_dict: A dictionary of attributes that a variable has
    :return:a dictionary of attributes that should be added to the created XArray DataArray
    """
    xarray_att_dict: Dict[str, str] = {}
    if not var_att_dict:
        return xarray_att_dict
    for key, value in var_att_dict.items():
        if key in ISTP_TO_XARRAY_ATTRS:
            xarray_att_dict[ISTP_TO_XARRAY_ATTRS[key]] = value
    return xarray_att_dict


def _convert_cdf_time_types(
    data: npt.ArrayLike, atts: Dict[str, AttData], properties: VDRInfo, to_datetime: bool = False, to_unixtime: bool = False
) -> Tuple[npt.NDArray, Dict[str, Any]]:
    """
    # Converts CDF time types into either datetime objects, unixtime, or nothing
    # If nothing, ALL CDF_EPOCH16 types are converted to CDF_EPOCH, because xarray can't handle int64s
    """
    data = np.atleast_1d(data)
    # Convert all data in the "data" variable to unixtime or datetime if needed
    data_type = properties.Data_Type_Description
    if len(data) == 0 or data_type not in ("CDF_EPOCH", "CDF_EPOCH16", "CDF_TIME_TT2000"):
        new_data = data
    else:
        data = np.atleast_1d(np.squeeze(data))
        if to_datetime:
            new_data = np.atleast_1d(cdfepoch.to_datetime(data))
            if "UNITS" in atts:
                atts["UNITS"].Data = "Datetime (UTC)"
        elif to_unixtime:
            new_data = np.atleast_1d(cdfepoch.unixtime(data))
            if "UNITS" in atts:
                atts["UNITS"].Data = "seconds"
        else:
            if data_type == "CDF_EPOCH16":
                new_data = np.atleast_1d(cdfepoch.compute(cdfepoch.breakdown(data)[0:7]))
            else:
                new_data = data

    # Convert all the attributes in the "atts" dictionary to unixtime or datetime if needed
    new_atts: Dict[str, Any] = {}
    time_converted_attrs = []
    for att in atts:
        attr_data_type = atts[att].Data_Type
        attr_data = atts[att].Data
        if attr_data_type not in ("CDF_EPOCH", "CDF_EPOCH16", "CDF_TIME_TT2000"):
            new_atts[att] = attr_data
        else:
            if to_datetime:
                time_converted_attrs.append(att)
                new_atts[att] = cdfepoch.to_datetime(np.array(attr_data))
            elif to_unixtime:
                time_converted_attrs.append(att)
                new_atts[att] = cdfepoch.unixtime(np.array(attr_data))
            else:
                if attr_data_type == "CDF_EPOCH16":
                    new_atts[att] = cdfepoch.compute(cdfepoch.breakdown(np.array(attr_data))[0:7])
                else:
                    new_atts[att] = attr_data

    # This is a bit of a hack, these data types are ambiguous
    # Lets add an attribute so at least we retain some information about what these numbers represent
    if data_type in ("CDF_EPOCH", "CDF_REAL4", "CDF_REAL8"):
        new_atts["CDF_DATA_TYPE"] = data_type

    # Another hack for now, if we convert the data to datetime or unixtime we want to have a memory of what it was before
    if data_type in ("CDF_EPOCH", "CDF_EPOCH16") and (to_datetime or to_unixtime):
        new_atts["CDF_DATA_TYPE"] = data_type
    elif data_type == "CDF_TIME_TT2000" and to_unixtime:
        new_atts["CDF_DATA_TYPE"] = data_type

    # Yet another hack, if we convert the attributes too we need to somehow keep track of what was converted
    if time_converted_attrs:
        new_atts["TIME_ATTRS"] = time_converted_attrs

    return new_data, new_atts


def _convert_cdf_to_dicts(
    filename: Union[str, Path], to_datetime: bool = False, to_unixtime: bool = False
) -> Tuple[Dict[str, List[Union[str, np.ndarray]]], Dict[str, Any], Dict[str, npt.NDArray], Dict[str, VDRInfo]]:
    # Open the CDF file
    # Converts the entire CDF file into python dictionary objects

    cdf_file = CDF(filename, string_encoding="latin-1")
    cdf_info = cdf_file.cdf_info()
    all_cdf_variables = cdf_info.rVariables + cdf_info.zVariables

    # Gather all Global Attributes
    try:
        gatt = cdf_file.globalattsget()
    except BaseException:
        gatt = {}

    # Gather all information about the CDF file, and store in the below dictionaries
    variable_data: Dict[str, npt.NDArray] = {}
    variable_attributes: Dict[str, Any] = {}
    variable_properties: Dict[str, VDRInfo] = {}

    for var_name in all_cdf_variables:
        var_attribute_list = cdf_file.varattsget(var_name)
        var_data_temp: Dict[str, Union[str, np.ndarray]] = {}
        var_atts_temp: Dict[str, AttData] = {}
        for att in var_attribute_list:
            var_atts_temp[att] = cdf_file.attget(att, var_name)
        variable_properties[var_name] = cdf_file.varinq(var_name)
        # Gather the actual variable data
        if variable_properties[var_name].Last_Rec < 0:
            var_data_temp[var_name] = np.array([])
        else:
            var_data_temp[var_name] = cdf_file.varget(var_name)

        variable_data[var_name], variable_attributes[var_name] = _convert_cdf_time_types(
            var_data_temp[var_name], var_atts_temp, variable_properties[var_name], to_datetime=to_datetime, to_unixtime=to_unixtime
        )

    return gatt, variable_attributes, variable_data, variable_properties


def _find_dependency_cycles(depends: Dict[str, Dict[int, str]]) -> List[List[str]]:
    """
    Finds groups of variables whose DEPEND_n attributes point at each other in a circle.
    Returns a list of cycles, each cycle being the list of variable names involved.
    """
    index_of: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    cycles: List[List[str]] = []
    counter = 0

    for start in depends:
        if start in index_of:
            continue
        todo: List[Tuple[str, Iterator[str]]] = [(start, iter(depends[start].values()))]
        while todo:
            node, successors = todo[-1]
            if node not in index_of:
                index_of[node] = counter
                lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            for successor in successors:
                if successor not in depends:
                    continue
                if successor not in index_of:
                    todo.append((successor, iter(depends[successor].values())))
                    descended = True
                    break
                if successor in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[successor])
            if descended:
                continue
            todo.pop()
            if todo:
                parent = todo[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cycles.append(component)
    return cycles


class _DimensionResolver:
    """
    Establishes the xarray dimension names of every CDF variable strictly from the
    DEPEND_n attributes and the variable declarations (Rec_Vary, Last_Rec, Dim_Sizes).

    Dimension and coordinate relationships are never inferred from matching data
    lengths: two variables of the same length only share a dimension when a
    DEPEND_n attribute (or the variable's own role as a declared dependency) says so.
    Missing dependencies, circular dependencies, and dimension contradictions are
    reported with the specific variables involved, and only the offending
    dependency is ignored so that all other variables still import.
    """

    def __init__(
        self,
        all_variable_data: Dict[str, npt.NDArray],
        all_variable_attributes: Dict[str, Any],
        all_variable_properties: Dict[str, VDRInfo],
    ) -> None:
        self._data = {var_name: np.asarray(data) for var_name, data in all_variable_data.items()}
        self._props = all_variable_properties
        self._depends = self._collect_dependencies(all_variable_attributes)
        # Variables that are pointed to by a DEPEND_n attribute of another variable
        self.depend_targets: Set[str] = {target for edges in self._depends.values() for target in edges.values()}
        # Variables that were verified to be the coordinate of another variable
        self.coordinate_variables: Set[str] = set()
        self._dims_cache: Dict[str, Tuple[str, ...]] = {}
        self._resolving: Set[str] = set()
        self._taken_dim_names: Set[str] = set(all_variable_properties)

    def _collect_dependencies(self, all_variable_attributes: Dict[str, Any]) -> Dict[str, Dict[int, str]]:
        depends: Dict[str, Dict[int, str]] = {}
        for var_name, atts in all_variable_attributes.items():
            edges: Dict[int, str] = {}
            for att, target in atts.items():
                match = _DEPEND_REGEX.match(str(att))
                if match is None:
                    continue
                depend_number = int(match.group(1))
                if not isinstance(target, str) or target not in self._props:
                    logger.warning(
                        f"Variable {var_name} lists {att} as {target}, but no variable by that name was found. "
                        f"The dependency will be ignored for {var_name}."
                    )
                    continue
                if target == var_name:
                    logger.warning(
                        f"Variable {var_name} lists itself as its {att} dependency. The dependency will be ignored."
                    )
                    continue
                edges[depend_number] = target
            if edges:
                depends[var_name] = edges

        for cycle in _find_dependency_cycles(depends):
            members = ", ".join(sorted(cycle))
            logger.warning(
                f"Circular DEPEND_n dependency detected between variables: {members}. "
                f"The dependencies between these variables will be ignored."
            )
            cycle_members = set(cycle)
            for var_name in cycle:
                remaining = {n: target for n, target in depends[var_name].items() if target not in cycle_members}
                if remaining:
                    depends[var_name] = remaining
                else:
                    del depends[var_name]
        return depends

    def _private_dimension(self, var_name: str, suffix: str) -> str:
        # Creates a dimension name that belongs to this variable alone, so that
        # unrelated variables are never merged onto the same dimension
        dim_name = f"{var_name}_{suffix}"
        counter = 1
        while dim_name in self._taken_dim_names:
            dim_name = f"{var_name}_{suffix}_{counter}"
            counter += 1
        self._taken_dim_names.add(dim_name)
        return dim_name

    def dims_for(self, var_name: str) -> Tuple[str, ...]:
        if var_name in self._dims_cache:
            return self._dims_cache[var_name]
        if var_name in self._resolving:
            # Unreachable unless the dependency graph changed under us, but never recurse forever
            logger.warning(f"Circular DEPEND_n dependency detected while determining the dimensions of {var_name}.")
            return ()
        self._resolving.add(var_name)
        dims = self._compute_dims(var_name)
        self._resolving.discard(var_name)
        self._dims_cache[var_name] = dims
        return dims

    def _compute_dims(self, var_name: str) -> Tuple[str, ...]:
        props = self._props[var_name]
        if props.Last_Rec < 0:
            # No records were ever written for this variable
            return ()
        data = self._data[var_name]
        dims = []
        if props.Rec_Vary:
            non_record_axes = max(data.ndim - 1, 0)
            dims.append(self._record_dimension(var_name, non_record_axes))
            for axis in range(1, non_record_axes + 1):
                dims.append(self._axis_dimension(var_name, axis, int(data.shape[axis]), True, non_record_axes))
        else:
            for axis, axis_length in enumerate(props.Dim_Sizes):
                dims.append(self._axis_dimension(var_name, axis, int(axis_length), False, len(props.Dim_Sizes)))
        return tuple(dims)

    def _record_dimension(self, var_name: str, non_record_axes: int) -> str:
        edges = self._depends.get(var_name, {})
        if var_name in self.depend_targets and non_record_axes == 0:
            # This variable is itself the record coordinate of other variables
            return var_name
        if 0 in edges:
            target = edges[0]
            if self._validate_depend_0(var_name, target):
                self.coordinate_variables.add(target)
                return target
        return self._private_dimension(var_name, "record")

    def _validate_depend_0(self, var_name: str, target: str) -> bool:
        target_props = self._props[target]
        target_data = self._data[target]
        if not (target_props.Rec_Vary and target_props.Last_Rec >= 0):
            logger.warning(
                f"Variable {var_name} lists DEPEND_0 as {target}, but {target} is not record-varying. "
                f"The dependency will be ignored for {var_name}."
            )
            return False
        if target_data.ndim != 1:
            logger.warning(
                f"Variable {var_name} lists DEPEND_0 as {target}, but {target} is not one-dimensional. "
                f"The dependency will be ignored for {var_name}."
            )
            return False
        if target_data.shape[0] != self._data[var_name].shape[0]:
            logger.warning(
                f"Variable {var_name} lists DEPEND_0 as {target}, but {var_name} has {self._data[var_name].shape[0]} "
                f"records while {target} has {target_data.shape[0]}. The dependency will be ignored for {var_name}."
            )
            return False
        return True

    def _axis_dimension(self, var_name: str, axis: int, axis_length: int, record_varying: bool, non_record_axes: int) -> str:
        depend_number = axis if record_varying else axis + 1
        edges = self._depends.get(var_name, {})
        target = edges.get(depend_number)
        if target is None and not record_varying:
            # There may be occasions where there was no time-varying record identified, but the users intended
            # for it to exist.  Thus, all the DEPEND_n's are off by 1.  We should still try to incorporate those.
            fallback = edges.get(depend_number - 1)
            if fallback is not None:
                logger.warning(
                    f"Variable {var_name} has no record dimension, but lists DEPEND_{depend_number - 1} as {fallback}. "
                    f"It will be applied to dimension {axis} of {var_name} for convenience."
                )
                target = fallback
                depend_number = depend_number - 1
        if target is not None:
            dim_name = self._depend_dimension(var_name, target, depend_number, axis, axis_length, record_varying)
            if dim_name is not None:
                return dim_name
        if var_name in self.depend_targets and non_record_axes == 1:
            if record_varying:
                # Two-dimensional (record-varying) coordinates get a single named non-record dimension
                return self._private_dimension(var_name, "dim")
            # One-dimensional dependencies are their own dimension
            return var_name
        return self._private_dimension(var_name, f"dim{axis}")

    def _depend_dimension(
        self, var_name: str, target: str, depend_number: int, axis: int, axis_length: int, record_varying: bool
    ) -> Optional[str]:
        target_props = self._props[target]
        target_data = self._data[target]
        target_record_varying = bool(target_props.Rec_Vary) and target_props.Last_Rec >= 0
        if not target_record_varying:
            if len(target_props.Dim_Sizes) == 1:
                if int(target_props.Dim_Sizes[0]) != axis_length:
                    logger.warning(
                        f"Variable {var_name} lists DEPEND_{depend_number} as {target}, but dimension {axis} of "
                        f"{var_name} has length {axis_length} while {target} has length {target_props.Dim_Sizes[0]}. "
                        f"The dependency will be ignored for {var_name}."
                    )
                    return None
                self.coordinate_variables.add(target)
                return target
        elif target_data.ndim == 2:
            # A dependency that varies from record to record, such as an energy axis that
            # changes over time.  It is kept as a two-dimensional coordinate spanning its
            # own record dimension and its declared non-record dimension.
            if int(target_data.shape[1]) != axis_length:
                logger.warning(
                    f"Variable {var_name} lists DEPEND_{depend_number} as {target}, but dimension {axis} of "
                    f"{var_name} has length {axis_length} while {target} has length {target_data.shape[1]}. "
                    f"The dependency will be ignored for {var_name}."
                )
                return None
            if record_varying and int(target_data.shape[0]) != int(self._data[var_name].shape[0]):
                logger.warning(
                    f"Variable {var_name} lists DEPEND_{depend_number} as {target}, but {var_name} has "
                    f"{self._data[var_name].shape[0]} records while {target} has {target_data.shape[0]}. "
                    f"The dependency will be ignored for {var_name}."
                )
                return None
            target_dims = self.dims_for(target)
            if len(target_dims) != 2:
                logger.warning(
                    f"Variable {var_name} lists DEPEND_{depend_number} as {target}, but the dimensions of {target} "
                    f"could not be determined. The dependency will be ignored for {var_name}."
                )
                return None
            self.coordinate_variables.add(target)
            return target_dims[1]
        logger.warning(
            f"Variable {var_name} lists DEPEND_{depend_number} as {target}, but {target} has an unsupported shape "
            f"{target_data.shape} for a coordinate variable. The dependency will be ignored for {var_name}."
        )
        return None


def _discover_uncertainty_variables(varatts: Dict[str, Any]) -> Dict[str, str]:
    # This loops through the variable attributes to discover which variables are the labels of other variables
    # Unfortunately, there is no easy way to tell this by looking at the label variable itself
    # This returns a KEY:VALUE pair, with the LABEL VARIABLE corresponding to which dimension it covers.

    list_of_label_vars = {}

    for v in varatts:
        if "DELTA_PLUS_VAR" in varatts[v].keys():
            list_of_label_vars[varatts[v]["DELTA_PLUS_VAR"]] = v
        if "DELTA_MINUS_VAR" in varatts[v].keys():
            list_of_label_vars[varatts[v]["DELTA_MINUS_VAR"]] = v
    return list_of_label_vars


def _discover_label_variables(
    varatts: Dict[str, Any], all_variable_properties: Dict[str, VDRInfo], all_variable_data: Dict[str, npt.NDArray]
) -> Dict[str, str]:
    # This loops through the variable attributes to discover which variables are the labels of other variables
    # Unfortunately, there is no easy way to tell this by looking at the label variable itself
    # This returns a KEY:VALUE pair, with the LABEL VARIABLE corresponding to which dimension it covers.

    list_of_label_vars: Dict[str, str] = {}

    for v in varatts:
        label_keys = [x for x in list(varatts[v].keys()) if x.startswith("LABL_PTR_")]
        for lab in label_keys:
            label_dependency = "DEPEND_" + lab[-1]
            if label_dependency not in varatts[v]:
                continue
            depend_var_name = varatts[v][label_dependency]

            if depend_var_name not in all_variable_properties:
                logger.warning(
                    f"Warning, variable {v} points to {depend_var_name} as {label_dependency}, but {depend_var_name} does not exist."
                )
                continue

            if (
                all_variable_properties[depend_var_name].Dim_Sizes
                and len(all_variable_properties[v].Dim_Sizes) > 0
                and (
                    all_variable_properties[depend_var_name].Dim_Sizes[0] == all_variable_properties[v].Dim_Sizes[int(lab[-1]) - 1]
                )
            ):
                if all_variable_data[depend_var_name].size == 0:
                    continue
            else:
                continue

            if varatts[v][lab] not in varatts:
                logger.warning(
                    f"Warning, variable {v} points to {varatts[v][lab]} as label {lab}, but {varatts[v][lab]} does not exist\n."
                    f"Setting {varatts[v][lab]} as long_name instead."
                )
                varatts[v]["LABLAXIS"] = varatts[v][lab]
                varatts[v]["long_name"] = varatts[v][lab]
                continue

            list_of_label_vars[varatts[v][lab]] = depend_var_name

    return list_of_label_vars


def _convert_fillvals_to_nan(var_data: npt.NDArray, var_atts: Dict[str, Any], var_properties: VDRInfo) -> npt.NDArray:
    if var_atts is None:
        return var_data
    if var_data is None:
        return var_data

    new_data = var_data
    if "FILLVAL" in var_atts:
        if var_properties.Data_Type_Description in (
            "CDF_FLOAT",
            "CDF_REAL4",
            "CDF_DOUBLE",
            "CDF_REAL8",
            "CDF_TIME_TT2000",
            "CDF_EPOCH",
            "CDF_EPOCH16",
        ):
            if new_data.size > 1:
                if new_data[new_data == var_atts["FILLVAL"]].size != 0:
                    if new_data.dtype.type == np.datetime64:  # type: ignore
                        new_data[new_data == var_atts["FILLVAL"]] = np.datetime64("nat")
                    else:
                        new_data[new_data == var_atts["FILLVAL"]] = np.nan
            elif new_data.size == 1:
                if new_data == var_atts["FILLVAL"]:
                    if new_data.dtype.type == np.datetime64:  # type: ignore
                        new_data[new_data == var_atts["FILLVAL"]] = np.array(np.datetime64("nat"))
                    else:
                        new_data[new_data == var_atts["FILLVAL"]] = np.array(np.nan)
    return new_data


def _reformat_variable_dims_and_data(
    var_dims: List[str], var_data: Optional[np.ndarray]
) -> Tuple[Union[str, List[str]], npt.NDArray]:
    if len(var_dims) > 0 and var_data is None:
        var_data = np.array([])

    # For some reason, there are times when the actual shape of the data doesn't match the dimensions listed.
    if var_data is not None:
        if len(np.array(var_data).shape) > len(var_dims):
            var_data = np.squeeze(var_data)
        if len(np.array(var_data).shape) < len(var_dims):
            var_data = np.expand_dims(var_data, axis=0)

    # Check if both dimensions and data are empty, if so, set the dimension to the empty dimension
    if var_data.size == 0 and not len(var_dims):
        var_dims_: Union[str, List[str]] = "dim_empty"
    else:
        var_dims_ = var_dims

    return var_dims_, var_data


def _resolve_dimension_conflicts(created_vars: Dict[str, xr.Variable]) -> None:
    """
    Safety net guaranteeing that a dimension name is never used with two different lengths.

    This should not normally trigger, because dimensions are established from DEPEND_n
    attributes and variable declarations rather than guessed from matching lengths.
    If a conflict is found anyway, the affected variables are reported and their
    conflicting dimension is made private to them.  Data values are never reindexed,
    reordered, or unioned to make conflicting coordinates fit.
    """
    dim_sizes: Dict[str, int] = {}
    dim_owners: Dict[str, str] = {}

    # Variables that are their own dimension get the first claim on the dimension length
    for var_name, var in created_vars.items():
        for dim, size in zip(var.dims, var.shape):
            if dim == var_name:
                dim_sizes[dim] = size
                dim_owners[dim] = var_name

    for var_name, var in created_vars.items():
        new_dims: List[str] = []
        changed = False
        for dim, size in zip(var.dims, var.shape):
            dim = str(dim)
            conflict = dim in dim_sizes and dim_sizes[dim] != size
            misused = dim != var_name and dim in created_vars and created_vars[dim].dims != (dim,)
            if conflict or misused:
                new_dim = f"{var_name}_{dim}"
                counter = 1
                while new_dim in dim_sizes or new_dim in created_vars:
                    new_dim = f"{var_name}_{dim}_{counter}"
                    counter += 1
                if conflict:
                    logger.warning(
                        f"Variable {var_name} uses dimension {dim} with length {size}, but variable "
                        f"{dim_owners[dim]} already uses it with length {dim_sizes[dim]}. "
                        f"The dimension of {var_name} will be renamed to {new_dim}. "
                        f"Data values are left untouched."
                    )
                else:
                    logger.warning(
                        f"Variable {var_name} uses dimension {dim}, but variable {dim} does not span that "
                        f"dimension itself. The dimension of {var_name} will be renamed to {new_dim}. "
                        f"Data values are left untouched."
                    )
                dim_sizes[new_dim] = size
                dim_owners[new_dim] = var_name
                new_dims.append(new_dim)
                changed = True
            else:
                dim_sizes.setdefault(dim, size)
                dim_owners.setdefault(dim, var_name)
                new_dims.append(dim)
        if changed:
            created_vars[var_name] = xr.Variable(tuple(new_dims), var.data, attrs=var.attrs)


def _generate_xarray_data_variables(
    all_variable_data: Dict[str, npt.NDArray],
    all_variable_attributes: Dict[str, Any],
    all_variable_properties: Dict[str, VDRInfo],
    fillval_to_nan: bool,
) -> Tuple[Dict[str, xr.Variable], _DimensionResolver]:
    # Establish all dimension and coordinate relationships from the DEPEND_n
    # attributes and the variable declarations, never from matching data lengths
    resolver = _DimensionResolver(all_variable_data, all_variable_attributes, all_variable_properties)
    created_vars: Dict[str, xr.Variable] = {}

    for var_name in all_variable_data:
        var_dims: List[str] = list(resolver.dims_for(var_name))
        var_atts = all_variable_attributes[var_name]
        var_data = np.array(all_variable_data[var_name])
        var_props = all_variable_properties[var_name]

        # There might be a few tweaks needed to the data or the dimension labels
        var_dims, var_data = _reformat_variable_dims_and_data(var_dims, var_data)

        # Looks for attributes to convert over to the things XArray uses to plot
        additional_variable_attrs = _find_xarray_plotting_values(var_atts)
        var_atts.update(additional_variable_attrs)

        # If the user wants to convert all the FILLVAL values to NaNs, we got them covered
        if fillval_to_nan:
            var_data = _convert_fillvals_to_nan(var_data, var_atts, var_props)

        # Finally, create the new variable
        created_vars[var_name] = xr.Variable(var_dims, var_data, attrs=var_atts)

    # Make sure no dimension name is used with two conflicting lengths
    _resolve_dimension_conflicts(created_vars)

    return created_vars, resolver


def cdf_to_xarray(filename: str, to_datetime: bool = True, to_unixtime: bool = False, fillval_to_nan: bool = False) -> xr.Dataset:
    """
    This function converts CDF files into XArray Dataset Objects.

    Parameters
    ----------
    filename :  str
        The path to the CDF file to read
    to_datetime : bool, optional
        Whether or not to convert CDF_EPOCH/EPOCH_16/TT2000 to datetime64, or leave them as is
    to_unixtime :  bool, optional
        Whether or not to convert CDF_EPOCH/EPOCH_16/TT2000 to unixtime, or leave them as is
    fillval_to_nan : bool, optional
        If True, any data values that match the FILLVAL attribute for a variable will be set to NaN

    Returns
    -------
    dataset : xarray.Dataset
        An XArray Dataset object containing all of the data and attributes from the CDF file

    Example MMS
    -----------
    ```python
    >>> # Import necessary libraries
    >>> import cdflib.xarray
    >>> import xarray as xr
    >>> import os
    >>> import urllib.request

    >>> # Download a CDF file
    >>> fname = 'mms2_fgm_srvy_l2_20160809_v4.47.0.cdf'
    >>> url = ("https://lasp.colorado.edu/maven/sdc/public/data/sdc/web/cdflib_testing/mms2_fgm_srvy_l2_20160809_v4.47.0.cdf")
    >>> if not os.path.exists(fname):
    >>>     urllib.request.urlretrieve(url, fname)

    >>> # Load in and display the CDF file
    >>> mms_data = cdflib.xarray.cdf_to_xarray("mms2_fgm_srvy_l2_20160809_v4.47.0.cdf", to_unixtime=True, fillval_to_nan=True)

    >>> # Show off XArray functionality

    >>> # Slice the data using built in XArray functions
    >>> mms_data2 = mms_data.isel(dim0=0)
    >>> # Plot the sliced data using built in XArray functions
    >>> mms_data2['mms2_fgm_b_gse_srvy_l2'].plot()
    >>> # Zoom in on the slices data in time using built in XArray functions
    >>> mms_data3 = mms_data2.isel(Epoch=slice(716000,717000))
    >>> # Plot the zoomed in sliced data using built in XArray functionality
    >>> mms_data3['mms2_fgm_srvy_l2'].plot()
    ```

    Example THEMIS
    --------------
    ```python
    >>> # Import necessary libraries
    >>> import cdflib.xarray
    >>> import xarray as xr
    >>> import os
    >>> import urllib.request

    >>> # Download a CDF file
    >>> fname = 'thg_l2_mag_amd_20070323_v01.cdf'
    >>> url = ("https://lasp.colorado.edu/maven/sdc/public/data/sdc/web/cdflib_testing/thg_l2_mag_amd_20070323_v01.cdf")
    >>> if not os.path.exists(fname):
    >>>     urllib.request.urlretrieve(url, fname)

    >>> # Load in and display the CDF file
    >>> thg_data = cdflib.xarray.cdf_to_xarray(fname, to_unixtime=True, fillval_to_nan=True)
    ```

    Processing Steps
    ----------------
        1. For each variable in the CDF file
            1. Determine the name of the dimension that spans the data "records"
                - A variable that is itself the DEPEND_0 of other variables is its own dimension
                - A declared DEPEND_0 attribute points to the appropriate dimension, after validating
                  it against the target variable's declaration
                - If neither of the above, a private dimension is created for the variable.  Dimensions
                  are never shared between variables simply because their lengths happen to match
            2. Determine the name of the other dimensions of the variable, if they exist
                - The DEPEND_n attributes declare the appropriate dimension for that variable
                - One-dimensional dependencies name the dimension after the dependency variable
                - Two-dimensional (record-varying) dependencies, such as energy axes that change over
                  time, are kept as two-dimensional coordinates spanning their own record dimension
                  and their declared non-record dimension
                - If no DEPEND_n is declared, a private dimension is created for the variable
            3. Gather all attributes that belong to the variable
            4. Add a few attributes that enable better plotting with built-in xarray functions (name, units, etc)
            5. Optionally, convert FILLVALs to NaNs in the data
            6. Optionally, convert CDF_EPOCH/EPOCH16/TT2000 variables to unixtime or datetime
            7. Create an XArray Variable object using the dimensions determined in steps 1 and 2, the attributes from steps 3 and 4, and then the variable data
        2. Gather all the Variable objects created in the first step, and separate them into data variables or coordinate variables
        3. Missing dependencies, circular dependencies, and dimension contradictions are reported with the
           specific variables involved; only the offending dependency is ignored, so all other variables
           are still imported
        4. Data is never reindexed, reordered, or unioned to make coordinates fit: record i in the CDF
           file remains at position i along the record dimension
        5. Gather all global scope attributes in the CDF file
        6. Create an XArray Dataset objects with the data variables, coordinate variables, and global attributes.
    """

    if to_datetime and to_unixtime:
        to_datetime = False

    # Convert the CDF file into a series of dicts, so we don't need to keep reading the file
    global_attributes, all_variable_attributes, all_variable_data, all_variable_properties = _convert_cdf_to_dicts(
        filename, to_datetime=to_datetime, to_unixtime=to_unixtime
    )

    created_vars, resolver = _generate_xarray_data_variables(
        all_variable_data, all_variable_attributes, all_variable_properties, fillval_to_nan
    )

    label_variables = _discover_label_variables(all_variable_attributes, all_variable_properties, all_variable_data)
    uncertainty_variables = _discover_uncertainty_variables(all_variable_attributes)

    coordinate_variables = resolver.coordinate_variables
    depend_targets = resolver.depend_targets

    # Determine which dimensions are coordinates vs actual data
    # Variables are considered coordinates if one of the other dimensions depends on them.
    # Otherwise, they are considered data coordinates.
    created_coord_vars: Dict[str, xr.Variable] = {}
    created_data_vars: Dict[str, xr.Variable] = {}
    for var_name in created_vars:
        if var_name in label_variables:
            # If these are label variables, we'll deal with these later when the DEPEND variables come up
            continue
        elif var_name in coordinate_variables or (var_name in depend_targets and created_vars[var_name].dims == (var_name,)):
            # If these are DEPEND variables, add them to the DataSet coordinates
            created_coord_vars[var_name] = created_vars[var_name]
            # Check if these coordinate variable have associated labels
            for lab in label_variables:
                if label_variables[lab] == var_name:  # Found one! Label "lab" covers the dimension "var_name"
                    if len(created_vars[lab].dims) == len(created_vars[var_name].dims):
                        if created_vars[lab].size != created_vars[var_name].size:
                            logger.warning(
                                f"Warning, label variable {lab} does not match the expected dimension sizes of {var_name}"
                            )
                        else:
                            created_vars[lab].dims = created_vars[var_name].dims
                    else:
                        if created_vars[lab].shape[0] != created_vars[var_name].shape[-1]:  # type: ignore
                            logger.warning(
                                f"Warning, label variable {lab} does not match the expected dimension sizes of {var_name}"
                            )
                        else:
                            created_vars[lab].dims = (created_vars[var_name].dims[-1],)
                    # Add the labels to the coordinates as well
                    created_coord_vars[lab] = created_vars[lab]
        elif var_name in uncertainty_variables and uncertainty_variables[var_name] in created_vars:
            # If there is an uncertainty variable, link it to the uncertainty along a dimension
            if created_vars[var_name].size == created_vars[uncertainty_variables[var_name]].size:
                created_vars[var_name].dims = created_vars[uncertainty_variables[var_name]].dims
            created_data_vars[var_name] = created_vars[var_name]
        else:
            created_data_vars[var_name] = created_vars[var_name]

    # Create the XArray DataSet Object!
    return xr.Dataset(data_vars=created_data_vars, coords=created_coord_vars, attrs=global_attributes)
