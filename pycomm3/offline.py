# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Ian Ottoway <ian@ottoway.dev>
# Copyright (c) 2014 Agostino Ruscito <ruscito@gmail@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Offline access planning for Logix tags.

An :class:`OfflineAccessPlanner` is built from a *locked* tag catalog (the
dict produced by a tag list upload / ``tags_json`` export).  It resolves a
user supplied tag reference against that catalog only:

* program scope, controller scope and nested structure members are followed
  through the exact type definition stored for that tag;
* display names and symbol identities are kept separately;
* array dimensionality, every subscript and the requested element count are
  validated together, so a request can never silently read past a member
  boundary into the next field;
* anything the locked catalog cannot prove is marked ``unresolved`` and gets
  no request path/bytes - there is no fuzzy, name-similarity based guessing.

No network access is performed: the planner only renders the CIP symbolic /
instance-id request paths that :class:`~pycomm3.logix_driver.LogixDriver`
would place on the wire, plus a human-checkable explanation of each segment.
"""

import json
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .cip import ClassCode, DataSegment, LogicalSegment, PADDED_EPATH, DataTypes
from .exceptions import DataError

__all__ = [
    "OfflineAccessPlanner",
    "OfflinePlan",
    "PathSegment",
    "load_tag_catalog",
]

# A path token is either a bare symbol (``Program:foo``, ``member`` ...) or an
# array subscript group (``[0]`` / ``[1,2,0]``).  Anything not consumed by the
# scan (``tag..x``, ``tag[`` ...) makes the path malformed.
_TOKEN_RE = re.compile(
    r"(?P<symbol>[^\.\[\],]+)|(?P<array>\[(?P<indices>[^\[\]]*)\])|(?P<sep>\.)"
)
_EXPLICIT_ELEMENTS_RE = re.compile(r"\{(?P<count>\d+)\}$")


class PathSegment(NamedTuple):
    """
    One explained step of a resolved request path.

    :param kind: ``scope``, ``symbol``, ``member``, ``array``, ``instance`` or ``bit``
    :param name: symbol/member name the step refers to (empty for index steps)
    :param detail: human readable explanation of the step
    :param indices: array subscripts attached to the step, if any
    :param offset: byte offset reached within the enclosing structure, if known
    """

    kind: str
    name: str
    detail: str
    indices: Optional[Tuple[int, ...]] = None
    offset: Optional[int] = None


class OfflinePlan(NamedTuple):
    """
    The offline access plan for a single tag reference.

    :param request: tag reference as supplied by the caller (display name)
    :param resolved: ``True`` only when the locked catalog proves the whole path
    :param status: ``"resolved"`` or ``"unresolved"``
    :param reasons: validation failures for an unresolved plan, empty otherwise
    :param display_name: user facing name, never rewritten or aliased
    :param symbol_identity: identity fields taken from the catalog entry
    :param segments: explained path segments, in wire order
    :param plc_tag: tag string the driver would send (bit suffix removed)
    :param dimensions: dimensions declared on the base tag (``[0, 0, 0]`` when scalar)
    :param indices: flat list of all validated subscripts
    :param bit: addressed bit within the leaf element, if any
    :param elements: requested element count
    :param element_size: size in bytes of one leaf element
    :param request_length: total byte span the read would cover
    :param boundary: member-array boundary check details, when applicable
    :param request_path: effective encoded CIP request path (instance-id or symbolic)
    :param symbolic_request_path: always-symbolic encoded CIP request path
    :param instance_request_path: instance-id encoded path, or ``None``
    """

    request: str
    resolved: bool
    status: str
    reasons: Tuple[str, ...]
    display_name: Optional[str]
    symbol_identity: Optional[Dict[str, Any]]
    segments: Tuple[PathSegment, ...]
    plc_tag: Optional[str]
    dimensions: Optional[Tuple[int, ...]]
    indices: Tuple[int, ...]
    bit: Optional[int]
    elements: Optional[int]
    element_size: Optional[int]
    request_length: Optional[int]
    boundary: Optional[Dict[str, Any]]
    request_path: Optional[bytes]
    symbolic_request_path: Optional[bytes]
    instance_request_path: Optional[bytes]

    def __bool__(self):
        return self.resolved

    @property
    def request_path_hex(self) -> Optional[str]:
        if self.request_path is None:
            return None
        return self.request_path.hex(" ")

    @property
    def symbolic_request_path_hex(self) -> Optional[str]:
        if self.symbolic_request_path is None:
            return None
        return self.symbolic_request_path.hex(" ")

    @property
    def instance_request_path_hex(self) -> Optional[str]:
        if self.instance_request_path is None:
            return None
        return self.instance_request_path.hex(" ")


def load_tag_catalog(path: str) -> Dict[str, dict]:
    """
    Load a locked tag catalog previously exported via ``LogixDriver.tags_json``.
    """
    with open(path, "r", encoding="utf-8") as f:
        catalog = json.load(f)
    if not isinstance(catalog, dict):
        raise DataError("Tag catalog must be a JSON object of tag definitions")
    return catalog


class _Token(NamedTuple):
    kind: str
    name: str
    indices: Tuple[int, ...]


class _Cursor(NamedTuple):
    node: dict
    kind: str  # 'base' or 'member'
    name: str
    offset: int  # absolute byte offset of this node inside the root structure
    siblings: Optional[Dict[str, dict]] = None
    enclosing_size: Optional[int] = None


class OfflineAccessPlanner:
    """
    Builds offline, checkable read plans from a locked tag catalog.

    :param tags: tag catalog, e.g. ``LogixDriver.tags`` / :func:`load_tag_catalog`
    :param use_instance_ids: instance-id policy the driver would use (v21+ targets)
    """

    def __init__(self, tags: Dict[str, dict], use_instance_ids: bool = True):
        self._tags = dict(tags)
        self._use_instance_ids = use_instance_ids

    def plan_all(
        self, requests: Sequence[str], elements: Optional[int] = None
    ) -> List[OfflinePlan]:
        """Plan several tag references, preserving input order."""
        return [self.plan(request, elements) for request in requests]

    def plan(self, request: str, elements: Optional[int] = None) -> OfflinePlan:
        """
        Resolve one tag reference against the locked catalog.

        A plan is always returned.  When the catalog cannot prove the full path
        (unknown tag/member, bad subscripts, a length crossing a member boundary,
        malformed syntax ...) ``resolved`` is ``False``, the failures are listed
        in ``reasons`` and every encoded request path is ``None``.
        """
        if not isinstance(request, str) or not request.strip():
            return self._unresolved(request, (), ("empty or non-string tag request",))

        reasons: List[str] = []
        explicit_elements = self._extract_explicit_elements(request, elements, reasons)
        if explicit_elements is not None and explicit_elements < 1:
            reasons.append("element count must be a positive integer")

        tokens, scan_ok = self._tokenize(request)
        if not scan_ok or not tokens:
            reasons.append("malformed tag path, could not tokenize the full reference")
            return self._unresolved(request, (), reasons)

        scope, program, base_key, base_name, header_count = self._split_scope(
            tokens, reasons
        )
        base_def = self._lookup_base(base_key, reasons)
        if base_def is None:
            return self._unresolved(
                request, tuple(self._explain_tokens(tokens)), reasons
            )

        identity = {
            "scope": scope,
            "program": program,
            "catalog_key": base_key,
            "tag_name": base_def.get("tag_name"),
            "instance_id": base_def.get("instance_id"),
            "symbol_address": base_def.get("symbol_address"),
            "symbol_object_address": base_def.get("symbol_object_address"),
        }

        walk = self._walk(
            tokens,
            header_count,
            scope,
            program,
            base_name,
            base_def,
            explicit_elements,
            reasons,
        )
        segments, indices, bit, plc_tag, elements_out, size, length, boundary = walk

        if reasons:
            return OfflinePlan(
                request=request,
                resolved=False,
                status="unresolved",
                reasons=tuple(dict.fromkeys(reasons)),
                display_name=request,
                symbol_identity=identity,
                segments=tuple(segments),
                plc_tag=None,
                dimensions=tuple(base_def.get("dimensions", [0, 0, 0])),
                indices=tuple(indices),
                bit=bit,
                elements=elements_out,
                element_size=size,
                request_length=length,
                boundary=boundary,
                request_path=None,
                symbolic_request_path=None,
                instance_request_path=None,
            )

        try:
            symbolic_path, instance_path = self._encode_paths(
                scope, program, base_name, base_def, tokens, header_count, bit
            )
        except DataError as err:
            reasons.append(f"could not encode a CIP request path: {err}")
            return OfflinePlan(
                request=request,
                resolved=False,
                status="unresolved",
                reasons=tuple(dict.fromkeys(reasons)),
                display_name=request,
                symbol_identity=identity,
                segments=tuple(segments),
                plc_tag=None,
                dimensions=tuple(base_def.get("dimensions", [0, 0, 0])),
                indices=tuple(indices),
                bit=bit,
                elements=elements_out,
                element_size=size,
                request_length=length,
                boundary=boundary,
                request_path=None,
                symbolic_request_path=None,
                instance_request_path=None,
            )

        effective_path = (
            instance_path
            if instance_path is not None and self._use_instance_ids
            else symbolic_path
        )

        return OfflinePlan(
            request=request,
            resolved=True,
            status="resolved",
            reasons=(),
            display_name=request,
            symbol_identity=identity,
            segments=tuple(segments),
            plc_tag=plc_tag,
            dimensions=tuple(base_def.get("dimensions", [0, 0, 0])),
            indices=tuple(indices),
            bit=bit,
            elements=elements_out,
            element_size=size,
            request_length=length,
            boundary=boundary,
            request_path=effective_path,
            symbolic_request_path=symbolic_path,
            instance_request_path=instance_path,
        )

    # ------------------------------------------------------------------
    # parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_explicit_elements(
        request: str, elements: Optional[int], reasons: List[str]
    ) -> Optional[int]:
        match = _EXPLICIT_ELEMENTS_RE.search(request)
        if match:
            if elements is not None:
                reasons.append(
                    "element count specified both with brace syntax and argument"
                )
            return int(match.group("count"))
        return elements

    @staticmethod
    def _tokenize(request: str) -> Tuple[List[_Token], bool]:
        text = _EXPLICIT_ELEMENTS_RE.sub("", request)
        tokens: List[_Token] = []
        pos = 0
        ok = True
        expect_operand = True
        for match in _TOKEN_RE.finditer(text):
            if match.start() != pos:
                ok = False
                break
            pos = match.end()
            if match.lastgroup == "sep":
                # dots must separate two operands: no leading, trailing or
                # consecutive dots (``.tag``, ``tag.``, ``tag..member``)
                if expect_operand:
                    ok = False
                    break
                expect_operand = True
                continue
            if expect_operand is False and match.lastgroup == "symbol":
                # two symbols with no separator cannot happen with this grammar
                ok = False
                break
            expect_operand = False
            if match.lastgroup == "symbol":
                tokens.append(_Token("symbol", match.group("symbol"), ()))
            else:
                raw = match.group("indices")
                parts = raw.split(",")
                if not raw or any(not part.strip() for part in parts):
                    ok = False
                    break
                try:
                    indices = tuple(int(part.strip()) for part in parts)
                except ValueError:
                    ok = False
                    break
                tokens.append(_Token("array", "", indices))
        if pos != len(text) or expect_operand:
            ok = False
        return tokens, ok

    @staticmethod
    def _split_scope(
        tokens: List[_Token], reasons: List[str]
    ) -> Tuple[str, Optional[str], Optional[str], Optional[str], int]:
        first = tokens[0]
        if first.kind != "symbol":
            reasons.append("tag reference must start with a tag or Program: scope name")
            return "controller", None, None, None, 1

        if not first.name.startswith("Program:"):
            return "controller", None, first.name, first.name, 1

        # 'Program:prog.tag' is lexed as two symbol tokens around the dot
        if len(tokens) < 2 or tokens[1].kind != "symbol":
            reasons.append(
                "malformed program scope, expected 'Program:{program}.{tag}'"
            )
            return "program", None, None, None, 1

        program = first.name[len("Program:") :]
        if not program:
            reasons.append("malformed program scope, empty program name")
        leaf = tokens[1].name
        base_key = f"Program:{program}.{leaf}"
        return f"program:{program}", program, base_key, leaf, 2

    def _lookup_base(self, base_key: Optional[str], reasons: List[str]) -> Optional[dict]:
        if base_key is None:
            return None
        definition = self._tags.get(base_key)
        if definition is None:
            # exact catalog match only: similar names must never be substituted
            reasons.append(
                f"unknown tag {base_key!r}: no entry in the locked tag catalog"
            )
            return None
        return definition

    # ------------------------------------------------------------------
    # catalog walk / validation
    # ------------------------------------------------------------------

    def _walk(
        self,
        tokens: List[_Token],
        header_count: int,
        scope: str,
        program: Optional[str],
        base_name: str,
        base_def: dict,
        explicit_elements: Optional[int],
        reasons: List[str],
    ) -> Tuple[
        List[PathSegment],
        List[int],
        Optional[int],
        Optional[str],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[Dict[str, Any]],
    ]:
        segments: List[PathSegment] = []
        all_indices: List[int] = []
        bit: Optional[int] = None
        boundary: Optional[Dict[str, Any]] = None

        if program is not None:
            segments.append(
                PathSegment(
                    "scope",
                    f"Program:{program}",
                    f"program-scoped tag inside {program!r}; resolved only from that "
                    "program's catalog entries",
                )
            )

        segments.append(
            PathSegment("symbol", base_name, self._describe_base(base_def), offset=0)
        )

        cursor = _Cursor(base_def, "base", base_name, 0)
        plc_tag_text = ".".join(token.name for token in tokens[:header_count])
        remaining = tokens[header_count:]

        while remaining:
            token = remaining.pop(0)

            if token.kind == "array":
                cursor, boundary = self._check_array(
                    cursor, token.indices, segments, all_indices, reasons
                )
                plc_tag_text += "[" + ",".join(str(i) for i in token.indices) + "]"
                continue

            if self._is_struct(cursor.node):
                child, siblings, enclosing_size = self._lookup_member(
                    cursor, token.name, reasons
                )
                if child is None:
                    break
                cursor = _Cursor(
                    child,
                    "member",
                    token.name,
                    cursor.offset + child["offset"],
                    siblings,
                    enclosing_size,
                )
                segments.append(
                    PathSegment(
                        "member",
                        token.name,
                        self._describe_member(child),
                        offset=cursor.offset,
                    )
                )
                plc_tag_text += "." + token.name
                continue

            if token.name.isdigit():
                if bit is not None:
                    reasons.append("more than one bit selector in the tag reference")
                    break
                bit = int(token.name)
                leaf_size = self._node_size(cursor.node)
                if leaf_size is None or not 0 <= bit < leaf_size * 8:
                    reasons.append(
                        f"bit {bit} is outside the {cursor.name!r} element "
                        f"({leaf_size * 8 if leaf_size else 0} bits available)"
                    )
                segments.append(
                    PathSegment(
                        "bit",
                        token.name,
                        f"bit {bit} of {cursor.name!r}; the whole containing element "
                        "is still requested",
                        offset=cursor.offset,
                    )
                )
                plc_tag_text += "." + token.name
                continue

            reasons.append(
                f"{token.name!r} is not a member of {self._type_name(cursor.node)!r}; "
                "no same-named type from another program or tag is used to fill it"
            )
            break

        elements_out, size, length, boundary = self._finalize_length(
            cursor, explicit_elements, boundary, bit, reasons
        )

        if reasons:
            plc_tag = None
        elif bit is not None:
            plc_tag = plc_tag_text.rsplit(".", 1)[0]
        else:
            plc_tag = plc_tag_text
        return segments, all_indices, bit, plc_tag, elements_out, size, length, boundary

    def _check_array(
        self,
        cursor: _Cursor,
        indices: Tuple[int, ...],
        segments: List[PathSegment],
        all_indices: List[int],
        reasons: List[str],
    ) -> Tuple[_Cursor, Optional[Dict[str, Any]]]:
        node = cursor.node

        if cursor.kind == "base":
            dim = int(node.get("dim", 0))
            dimensions = tuple(node.get("dimensions", [0, 0, 0])[:dim])
            if dim == 0:
                reasons.append(f"{cursor.name!r} is not an array")
                return cursor, None
            if len(indices) != dim:
                reasons.append(
                    f"{cursor.name!r} declares {dim} dimension(s) {list(dimensions)}, "
                    f"but {len(indices)} subscript(s) were supplied"
                )
            bounds_ok = len(indices) == dim
            for axis, (index, bound) in enumerate(zip(indices, dimensions)):
                if not 0 <= index < bound:
                    reasons.append(
                        f"subscript {index} on dimension {axis} of {cursor.name!r} "
                        f"is outside [0, {bound - 1}]"
                    )
                    bounds_ok = False
            if bounds_ok:
                linear = self._linear_index(indices, dimensions)
                all_indices.extend(indices)
                segments.append(
                    PathSegment(
                        "array",
                        cursor.name,
                        f"subscript {list(indices)} -> linear element {linear} of "
                        f"{self._product(dimensions)} (dims {list(dimensions)})",
                        indices=indices,
                        offset=cursor.offset,
                    )
                )
                return cursor, {
                    "container": cursor.name,
                    "dimensions": dimensions,
                    "start_index": linear,
                    "capacity": self._product(dimensions),
                }
            return cursor, None

        capacity = int(node.get("array", 0))
        if capacity == 0:
            reasons.append(f"member {cursor.name!r} is not an array")
            return cursor, None
        if len(indices) != 1:
            reasons.append(
                f"structure member {cursor.name!r} is a 1-dimensional array, got "
                f"{len(indices)} subscript(s)"
            )
            return cursor, None

        index = indices[0]
        if not 0 <= index < capacity:
            reasons.append(
                f"subscript {index} of member {cursor.name!r} is outside [0, {capacity - 1}]"
            )
            return cursor, None

        size = self._node_size(node)
        all_indices.append(index)
        segments.append(
            PathSegment(
                "array",
                cursor.name,
                f"subscript [{index}] of {capacity}-element member array {cursor.name!r} "
                f"({self._type_name(node)} element)",
                indices=indices,
                offset=cursor.offset + index * (size or 0),
            )
        )
        return cursor, {
            "container": cursor.name,
            "dimensions": (capacity,),
            "start_index": index,
            "capacity": capacity,
            "element_size": size,
            "member_local_offset": node["offset"],
        }

    def _lookup_member(
        self, cursor: _Cursor, name: str, reasons: List[str]
    ) -> Tuple[Optional[dict], Optional[Dict[str, dict]], Optional[int]]:
        data_type = cursor.node["data_type"]
        siblings = data_type.get("internal_tags", {})
        child = siblings.get(name)
        if child is None:
            reasons.append(
                f"member {name!r} does not exist on type "
                f"{self._type_name(cursor.node)!r} in tag {cursor.name!r}; known members "
                "cannot be back-filled from another program's same-named type"
            )
            return None, siblings, None
        enclosing_size = (data_type.get("template") or {}).get("structure_size")
        return child, siblings, enclosing_size

    def _finalize_length(
        self,
        cursor: _Cursor,
        explicit_elements: Optional[int],
        boundary: Optional[Dict[str, Any]],
        bit: Optional[int],
        reasons: List[str],
    ) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[Dict[str, Any]]]:
        elements = 1 if explicit_elements is None else explicit_elements
        size = self._node_size(cursor.node)
        if size is None:
            reasons.append(f"cannot determine element size for {cursor.name!r}")
            return elements, None, None, boundary

        node = cursor.node
        is_base_array = cursor.kind == "base" and int(node.get("dim", 0)) > 0
        is_member_array = cursor.kind == "member" and int(node.get("array", 0)) > 0
        is_array = is_base_array or is_member_array

        if not is_array and elements != 1:
            reasons.append(f"{cursor.name!r} is scalar; {elements} elements were requested")

        if is_base_array:
            dim = int(node.get("dim", 0))
            dimensions = tuple(node.get("dimensions", [0, 0, 0])[:dim])
            capacity = self._product(dimensions)
            start = boundary["start_index"] if boundary else 0
            if elements > capacity - start:
                reasons.append(
                    f"request of {elements} element(s) from index {start} exceeds the "
                    f"{capacity - start} element(s) remaining in {cursor.name!r}"
                )

        if is_member_array:
            capacity = int(node.get("array", 0))
            start = boundary["start_index"] if boundary else 0
            boundary = boundary or {
                "container": cursor.name,
                "dimensions": (capacity,),
                "start_index": start,
                "capacity": capacity,
                "element_size": size,
                "member_local_offset": node["offset"],
            }

            if elements > capacity - start:
                reasons.append(
                    f"request of {elements} element(s) from index {start} exceeds the "
                    f"{capacity - start} element(s) remaining in member {cursor.name!r}"
                )

            local_start = node["offset"] + start * size
            local_end = node["offset"] + (start + elements) * size
            boundary["local_span"] = (local_start, local_end)
            boundary["span"] = (
                cursor.offset + start * size,
                cursor.offset + (start + elements) * size,
            )
            boundary["requested_elements"] = elements

            sibling_offsets = sorted(
                sibling["offset"]
                for sibling in (cursor.siblings or {}).values()
                if sibling["offset"] > node["offset"]
            )
            if sibling_offsets:
                next_sibling = sibling_offsets[0]
                boundary["next_member_offset"] = next_sibling
                if local_end > next_sibling:
                    reasons.append(
                        f"request would read {local_end - next_sibling} byte(s) past member "
                        f"{cursor.name!r} into the next field at local offset {next_sibling}; "
                        "cross-member reads are not permitted"
                    )

            if cursor.enclosing_size is not None and local_end > cursor.enclosing_size:
                reasons.append(
                    "request extends past the enclosing structure size "
                    f"({cursor.enclosing_size} bytes)"
                )

        if bit is not None and elements != 1:
            reasons.append("bit access cannot be combined with a multi-element count")

        if boundary is not None and "requested_elements" not in boundary:
            boundary["requested_elements"] = elements

        length = elements * size
        return elements, size, length, boundary

    # ------------------------------------------------------------------
    # path encoding
    # ------------------------------------------------------------------

    def _encode_paths(
        self,
        scope: str,
        program: Optional[str],
        base_name: str,
        base_def: dict,
        tokens: List[_Token],
        header_count: int,
        bit: Optional[int],
    ) -> Tuple[bytes, Optional[bytes]]:
        symbolic_segments: List[Any] = [DataSegment(base_name)]
        instance_segments: Optional[List[Any]] = None

        if program is not None:
            # program tags are always addressed symbolically, even on v21+
            symbolic_segments = [
                DataSegment(f"Program:{program}"),
                DataSegment(base_name),
            ]
        elif base_def.get("instance_id"):
            instance_segments = [
                LogicalSegment(ClassCode.symbol_object, "class_id"),
                LogicalSegment(base_def["instance_id"], "instance_id"),
            ]

        body = tokens[header_count:]
        if bit is not None and body and body[-1].kind == "symbol" and body[-1].name.isdigit():
            body = body[:-1]

        for token in body:
            if token.kind == "array":
                for index in token.indices:
                    member_segment = LogicalSegment(int(index), "member_id")
                    symbolic_segments.append(member_segment)
                    if instance_segments is not None:
                        instance_segments.append(member_segment)
            else:
                symbolic_segments.append(DataSegment(token.name))
                if instance_segments is not None:
                    instance_segments.append(DataSegment(token.name))

        symbolic_path = PADDED_EPATH.encode(symbolic_segments, length=True)
        instance_path = (
            PADDED_EPATH.encode(instance_segments, length=True)
            if instance_segments is not None
            else None
        )
        return symbolic_path, instance_path

    # ------------------------------------------------------------------
    # small catalog helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_struct(node: dict) -> bool:
        return node.get("tag_type") == "struct"

    @staticmethod
    def _type_name(node: dict) -> str:
        if node.get("data_type_name"):
            return node["data_type_name"]
        data_type = node.get("data_type")
        if isinstance(data_type, dict):
            return data_type.get("name")
        return data_type

    def _node_size(self, node: dict) -> Optional[int]:
        if self._is_struct(node):
            template = node["data_type"].get("template") or {}
            return template.get("structure_size")
        type_name = node.get("data_type_name")
        try:
            return DataTypes[type_name].size
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _product(values: Sequence[int]) -> int:
        total = 1
        for value in values:
            total *= value
        return total or 1

    @staticmethod
    def _linear_index(indices: Sequence[int], dimensions: Sequence[int]) -> int:
        linear = 0
        stride = 1
        for index, bound in zip(reversed(indices), reversed(dimensions)):
            linear += index * stride
            stride *= bound
        return linear

    def _describe_base(self, node: dict) -> str:
        dim = int(node.get("dim", 0))
        if dim:
            dims = list(node.get("dimensions", [0, 0, 0])[:dim])
            return (
                f"base {node['tag_type']} tag of {self._type_name(node)}, "
                f"{dim}-dimensional array {dims}"
            )
        return f"base {node['tag_type']} tag of {self._type_name(node)}"

    def _describe_member(self, node: dict) -> str:
        if self._is_struct(node):
            text = f"structure member of {self._type_name(node)} at offset {node['offset']}"
        else:
            text = f"{self._type_name(node)} member at offset {node['offset']}"
        if node.get("array", 0):
            text += f", array of {node['array']} element(s)"
        if "bit" in node:
            text += f", bit {node['bit']} of its container byte/word"
        return text

    @staticmethod
    def _explain_tokens(tokens: List[_Token]) -> Tuple[PathSegment, ...]:
        return tuple(
            PathSegment(
                "array" if token.kind == "array" else "symbol",
                token.name,
                "unresolved token",
                indices=token.indices or None,
            )
            for token in tokens
        )

    @staticmethod
    def _unresolved(
        request: str,
        segments: Tuple[PathSegment, ...],
        reasons: Sequence[str],
    ) -> OfflinePlan:
        return OfflinePlan(
            request=request,
            resolved=False,
            status="unresolved",
            reasons=tuple(dict.fromkeys(reasons)),
            display_name=request if isinstance(request, str) else None,
            symbol_identity=None,
            segments=segments,
            plc_tag=None,
            dimensions=None,
            indices=(),
            bit=None,
            elements=None,
            element_size=None,
            request_length=None,
            boundary=None,
            request_path=None,
            symbolic_request_path=None,
            instance_request_path=None,
        )
