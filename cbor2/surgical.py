"""
Surgical, byte-span-preserving editing of a single CBOR data item.

This module implements a two-phase "authorized rewrite" workflow:

``plan``
    Parse the original message into a byte-span tree, resolve the target
    field, and compute the exact rewrite scope: the target span, all length
    fields and shared-reference (tag 28/29) relationships that the edit
    touches.  If honoring the edit would force changes outside the
    authorized span (dangling or renumbered shared references, ancestor
    item-count changes), the blocking relations are reported and nothing
    is written.

``apply``
    Re-read the original, verify it is byte-identical to the version the
    plan was made from (otherwise stop), splice in the replacement so that
    every byte outside the authorized span is preserved, and verify the
    new copy: structure, preserved spans, shared-reference resolutions and
    the edited value must all check out -- "still parses" is not enough.

Shared values (tag 28/29) may be referenced from several places, so the
caller must state explicitly whether an *occurrence* (one reference site)
or the *shared target* (the shareable definition, affecting every
reference) is being edited.  The two are never mixed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterator

from . import _decoder
from ._decoder import CBORDecoder, loads
from ._encoder import dumps
from ._types import CBORDecodeValueError, CBORTag

__all__ = [
    "BlockingError",
    "OriginalChangedError",
    "SurgicalError",
    "VerificationError",
    "apply_edit",
    "plan_edit",
]

PLAN_VERSION = 1


class SurgicalError(Exception):
    """Base class for surgical editing failures."""


class ParseError(SurgicalError):
    """The message is not a single, well-formed CBOR data item."""


class PathResolutionError(SurgicalError):
    """The target path does not address an item in the message."""


class BlockingError(SurgicalError):
    """
    The edit would force changes outside the authorized rewrite scope.

    ``relations`` is a list of dicts describing each blocking relation
    (dangling reference, renumbered reference, invalid replacement
    reference, ...).  Nothing is written when this is raised.
    """

    def __init__(self, relations: list[dict[str, Any]]):
        self.relations = relations
        super().__init__(f"edit is blocked by {len(relations)} relation(s)")


class OriginalChangedError(SurgicalError):
    """The original message changed since the plan was made; stop."""


class VerificationError(SurgicalError):
    """The new copy failed post-edit verification; it was not written."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        failed = [c["name"] for c in report["checks"] if not c["ok"]]
        super().__init__(f"verification failed: {', '.join(failed)}")


#
# Byte-span parser
#
@dataclass
class _Item:
    start: int  # offset of the initial byte
    end: int  # offset one past the last byte
    major: int
    arg: int | None  # decoded argument; None for indefinite length
    children: list[_Item]


@dataclass
class _Document:
    root: _Item
    shareables: list[_Item]  # tag 28 items, in shareable-index order
    sharedrefs: list[tuple[_Item, int]]  # (tag 29 item, referenced index)


class _Parser:
    """Parse one CBOR data item, recording exact byte spans and sharing info."""

    def __init__(self, data: bytes):
        self.data = data
        self.shareables: list[_Item] = []
        self.sharedrefs: list[tuple[_Item, int]] = []

    def parse(self) -> _Document:
        item = self._item(0)
        if item.end != len(self.data):
            raise ParseError(f"trailing {len(self.data) - item.end} byte(s) after the data item")
        return _Document(item, self.shareables, self.sharedrefs)

    def _byte(self, pos: int) -> int:
        try:
            return self.data[pos]
        except IndexError:
            raise ParseError("unexpected end of input") from None

    def _item(self, pos: int) -> _Item:
        start = pos
        initial = self._byte(pos)
        pos += 1
        major = initial >> 5
        subtype = initial & 0x1F
        arg: int | None
        if subtype < 24:
            arg = subtype
        elif subtype == 24:
            arg = self._byte(pos)
            pos += 1
        elif subtype == 25:
            arg = int.from_bytes(self.data[pos : pos + 2], "big")
            if pos + 2 > len(self.data):
                raise ParseError("truncated argument")
            pos += 2
        elif subtype == 26:
            if pos + 4 > len(self.data):
                raise ParseError("truncated argument")
            arg = int.from_bytes(self.data[pos : pos + 4], "big")
            pos += 4
        elif subtype == 27:
            if pos + 8 > len(self.data):
                raise ParseError("truncated argument")
            arg = int.from_bytes(self.data[pos : pos + 8], "big")
            pos += 8
        elif subtype == 31:
            if major in (2, 3, 4, 5):
                arg = None
            elif major == 7:
                raise ParseError("break marker outside an indefinite-length item")
            else:
                raise ParseError("indefinite length is not valid for a tag")
        else:
            raise ParseError(f"reserved additional information {subtype}")

        item = _Item(start, pos, major, arg, [])
        if major == 6 and arg == 28:
            # The shareable index is assigned at the tag head, before the
            # content is parsed, so nested shareables get later indices.
            self.shareables.append(item)

        children: list[_Item] = []
        if major in (0, 1):
            pass
        elif major in (2, 3):
            if arg is None:
                while self._byte(pos) != 0xFF:
                    chunk = self._item(pos)
                    if chunk.major != major or chunk.arg is None:
                        raise ParseError("invalid chunk in indefinite-length string")
                    children.append(chunk)
                    pos = chunk.end
                pos += 1
            else:
                if pos + arg > len(self.data):
                    raise ParseError("truncated string payload")
                pos += arg
        elif major == 4:
            if arg is None:
                while self._byte(pos) != 0xFF:
                    child = self._item(pos)
                    children.append(child)
                    pos = child.end
                pos += 1
            else:
                for _ in range(arg):
                    child = self._item(pos)
                    children.append(child)
                    pos = child.end
        elif major == 5:
            if arg is None:
                while self._byte(pos) != 0xFF:
                    key = self._item(pos)
                    value = self._item(key.end)
                    children.extend((key, value))
                    pos = value.end
                pos += 1
            else:
                for _ in range(arg * 2):
                    child = self._item(pos)
                    children.append(child)
                    pos = child.end
        elif major == 6:
            child = self._item(pos)
            children.append(child)
            pos = child.end
        elif major == 7:
            if subtype == 24:
                pos += 1
            elif subtype == 25:
                pos += 2
            elif subtype == 26:
                pos += 4
            elif subtype == 27:
                pos += 8
            if pos > len(self.data):
                raise ParseError("truncated simple/float value")
        item.children = children
        item.end = pos

        if major == 6 and arg == 29:
            if child.major != 0:
                raise ParseError("shared reference index must be an unsigned integer")
            self.sharedrefs.append((item, child.arg or 0))
        return item


#
# Target path resolution
#
def _unescape(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def _split_path(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PathResolutionError(f"path must be empty or start with '/': {path!r}")
    return [_unescape(segment) for segment in path.split("/")[1:]]


@dataclass
class _Resolved:
    node: _Item
    # Steps for navigating the *decoded* value to the same spot:
    # ("index", n) / ("key", segment) / ("tag", transparent)
    decoded_steps: list[tuple[str, Any]]


def _resolve_path(doc: _Document, data: bytes, path: str) -> _Resolved:
    segments = _split_path(path)
    node = doc.root
    steps: list[tuple[str, Any]] = []
    for segment in segments:
        if node.major == 4:
            if not segment.isdigit() or int(segment) >= len(node.children):
                raise PathResolutionError(f"no array element {segment!r} at offset {node.start}")
            node = node.children[int(segment)]
            steps.append(("index", int(segment)))
        elif node.major == 5:
            matches = []
            for i in range(0, len(node.children), 2):
                key_item = node.children[i]
                key = loads(data[key_item.start : key_item.end])
                if key == segment or (
                    segment.lstrip("-").isdigit()
                    and isinstance(key, int)
                    and not isinstance(key, bool)
                    and key == int(segment)
                ):
                    matches.append(node.children[i + 1])
            if not matches:
                raise PathResolutionError(f"no map key {segment!r} at offset {node.start}")
            if len(matches) > 1:
                raise PathResolutionError(f"map key {segment!r} is ambiguous")
            node = matches[0]
            steps.append(("key", segment))
        elif node.major == 6 and node.arg != 29:
            if segment != "0":
                raise PathResolutionError(f"a tag only has content step '0', got {segment!r}")
            steps.append(("tag", node.arg == 28))
            node = node.children[0]
        else:
            raise PathResolutionError(f"cannot descend into the item at offset {node.start}")
    return _Resolved(node, steps)


def _shareable_index(doc: _Document, item: _Item) -> int:
    for index, shareable in enumerate(doc.shareables):
        if shareable is item:
            return index
    raise SurgicalError("internal error: shareable not registered")


def _sharedref_index(doc: _Document, item: _Item) -> int:
    for ref_item, index in doc.sharedrefs:
        if ref_item is item:
            return index
    raise SurgicalError("internal error: shared reference not registered")


#
# Decoding helpers for verification
#
class _RecordingDecoder(CBORDecoder):
    """Decoder that records every shared-reference resolution, in order."""

    def __init__(self, fp: BytesIO):
        super().__init__(fp)
        self.resolutions: list[tuple[int, Any]] = []

    def decode_sharedref(self) -> Any:
        value = self._decode(unshared=True)
        try:
            shared = self._shareables[value]
        except IndexError:
            raise CBORDecodeValueError(f"shared reference {value} not found") from None
        if shared is None:
            raise CBORDecodeValueError(f"shared value {value} has not been initialized")
        self.resolutions.append((value, shared))
        return shared


@contextlib.contextmanager
def _structural_decoding() -> Iterator[None]:
    """
    Decode tags (other than 28/29) as plain CBORTag wrappers so that decoded
    navigation lines up with the byte-level structure.
    """
    saved = _decoder.semantic_decoders
    _decoder.semantic_decoders = {
        28: saved[28],
        29: saved[29],
    }
    try:
        yield
    finally:
        _decoder.semantic_decoders = saved


def _decode_recording(data: bytes) -> tuple[Any, list[tuple[int, Any]], list[Any]]:
    with _structural_decoding():
        decoder = _RecordingDecoder(BytesIO(data))
        value = decoder.decode()
    return value, decoder.resolutions, decoder._shareables


def _safe_equal(a: Any, b: Any, seen: set[tuple[int, int]] | None = None) -> bool:
    """Structural equality that tolerates cyclic shared values."""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and a != a and b != b:
        return True  # NaN
    if seen is None:
        seen = set()
    if isinstance(a, (list, tuple)):
        key = (id(a), id(b))
        if key in seen:
            return True
        seen.add(key)
        return len(a) == len(b) and all(_safe_equal(x, y, seen) for x, y in zip(a, b))
    if isinstance(a, dict):
        key = (id(a), id(b))
        if key in seen:
            return True
        seen.add(key)
        if len(a) != len(b):
            return False
        for k, v in a.items():
            try:
                other = b[k]
            except KeyError:
                return False
            if not _safe_equal(v, other, seen):
                return False
        return True
    if isinstance(a, CBORTag):
        return a.tag == b.tag and _safe_equal(a.value, b.value, seen)
    return bool(a == b)


def _navigate(value: Any, steps: list[tuple[str, Any]]) -> Any:
    for kind, arg in steps:
        if kind == "index":
            value = value[arg]
        elif kind == "key":
            if isinstance(value, dict) and arg in value:
                value = value[arg]
            else:
                value = value[int(arg)]
        else:  # "tag"
            if not arg:  # non-transparent tag: unwrap the CBORTag
                value = value.value
            # transparent (tag 28): invisible in the decoded value
    return value


#
# Planning
#
def _replacement_document(replacement: bytes) -> _Document:
    try:
        return _Parser(replacement).parse()
    except ParseError as exc:
        raise SurgicalError(f"replacement is not a single CBOR data item: {exc}")


def _changed_byte_count(old: bytes, new: bytes) -> int:
    overlap = min(len(old), len(new))
    diff = sum(1 for i in range(overlap) if old[i] != new[i])
    return diff + abs(len(old) - len(new))


def plan_edit(
    data: bytes,
    path: str,
    mode: str,
    replacement: bytes,
) -> dict[str, Any]:
    """
    Compute the rewrite scope for replacing the item at ``path`` in ``data``.

    :param data: the original CBOR message (a single data item)
    :param path: JSON-Pointer-like path to the target field
        (``/key/0``; tag content is addressed with the step ``0``)
    :param mode: ``"occurrence"`` to replace only the addressed item (which
        must not lie inside a shared value), or ``"shared"`` to edit the
        shared target: the content of the shareable (tag 28) the addressed
        item defines, references, or is a field of -- affecting every
        reference to it
    :param replacement: the replacement encoded as exactly one CBOR data item
    :return: a JSON-serializable plan dict
    :raises BlockingError: if the edit would force changes outside the
        authorized span; nothing is written in that case
    """
    if mode not in ("occurrence", "shared"):
        raise SurgicalError(f"invalid mode {mode!r}")

    doc = _Parser(data).parse()
    resolved = _resolve_path(doc, data, path)
    node = resolved.node

    # Innermost shareable whose content contains the target, if any.  Any
    # edit in there edits the shared target, never a single occurrence.
    containing: tuple[int, _Item] | None = None
    for index, shareable in enumerate(doc.shareables):
        content = shareable.children[0]
        if content.start <= node.start and node.end <= content.end:
            if containing is None or content.start >= containing[1].start:
                containing = (index, content)

    shared_index: int | None = None
    if mode == "occurrence":
        if containing is not None:
            raise SurgicalError(
                f"the target is part of shared value #{containing[0]}; editing "
                "it would change every reference to that value -- use mode "
                "'shared' to edit the shared target, or address a reference "
                "occurrence instead"
            )
        start, end = node.start, node.end
    else:
        if node.major == 6 and node.arg == 28:
            shared_index = _shareable_index(doc, node)
            content = node.children[0]
            start, end = content.start, content.end
        elif node.major == 6 and node.arg == 29:
            shared_index = _sharedref_index(doc, node)
            content = doc.shareables[shared_index].children[0]
            start, end = content.start, content.end
        elif containing is not None:
            # Partial edit of the shared target's content.
            shared_index = containing[0]
            start, end = node.start, node.end
        else:
            raise SurgicalError(
                "mode 'shared' requires the target to be a shareable (tag 28), "
                "a shared reference (tag 29), or a field inside a shared value"
            )

    repl_doc = _replacement_document(replacement)

    defs_before_span = sum(1 for s in doc.shareables if s.start < start)
    old_defs = [i for i, s in enumerate(doc.shareables) if start <= s.start < end]
    new_defs = len(repl_doc.shareables)
    shift = new_defs - len(old_defs)
    last_old_def = old_defs[-1] if old_defs else -1

    blocking: list[dict[str, Any]] = []
    affected_shared_refs: list[int] = []
    for ref_item, index in doc.sharedrefs:
        if start <= ref_item.start < end:
            continue  # removed together with the edit span: authorized
        if old_defs and old_defs[0] <= index <= last_old_def:
            relation: dict[str, Any] = {
                "kind": "dangling-reference",
                "offset": ref_item.start,
                "references_shareable": index,
                "reason": "reference outside the authorized span resolves to a "
                "shareable defined inside it",
            }
            if mode == "occurrence" and node.major == 6 and node.arg == 28:
                relation["hint"] = "use mode 'shared' to edit the shared target instead"
            blocking.append(relation)
        elif shift and index > last_old_def:
            blocking.append(
                {
                    "kind": "renumbered-reference",
                    "offset": ref_item.start,
                    "references_shareable": index,
                    "reason": f"shareable indices shift by {shift:+d}; rewriting "
                    "this reference is outside the authorized span",
                }
            )
        elif mode == "shared" and index == shared_index:
            affected_shared_refs.append(ref_item.start)

    for ref_item, index in repl_doc.sharedrefs:
        defs_before_ref = sum(1 for s in repl_doc.shareables if s.start < ref_item.start)
        visible = defs_before_span + defs_before_ref
        if index >= visible:
            blocking.append(
                {
                    "kind": "invalid-replacement-reference",
                    "replacement_offset": ref_item.start,
                    "references_shareable": index,
                    "reason": f"only {visible} shareable(s) are defined before this "
                    "reference in the new copy",
                }
            )

    if blocking:
        raise BlockingError(blocking)

    old_span = data[start:end]
    plan: dict[str, Any] = {
        "version": PLAN_VERSION,
        "input": {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "path": path,
        "mode": mode,
        "target": {
            "start": start,
            "end": end,
            "length": end - start,
            "hex": old_span.hex(),
            "shared_index": shared_index,
        },
        "replacement": {
            "length": len(replacement),
            "hex": replacement.hex(),
        },
        "delta": len(replacement) - len(old_span),
        "length_fields": {
            "ancestor_item_counts": "unchanged (one item replaced by one item)",
            "target_head": "rewritten inside the authorized span only",
        },
        "shareables": {
            "defined_before_span": defs_before_span,
            "defined_in_span": old_defs,
            "defined_in_replacement": new_defs,
            "index_shift": shift,
        },
        "references": {
            "removed_in_span": [r.start for r, _ in doc.sharedrefs if start <= r.start < end],
            "affected_by_shared_edit": affected_shared_refs,
        },
        "decoded_steps": resolved.decoded_steps,
        "changed_byte_estimate": _changed_byte_count(old_span, replacement),
    }
    return plan


#
# Apply + verify
#
def _verify(
    original: bytes,
    new: bytes,
    plan: dict[str, Any],
    expected_value: Any,
) -> dict[str, Any]:
    start = plan["target"]["start"]
    end = plan["target"]["end"]
    replacement = bytes.fromhex(plan["replacement"]["hex"])
    delta = plan["delta"]
    mode = plan["mode"]
    shared_index = plan["target"]["shared_index"]

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    check("prefix-preserved", new[:start] == original[:start])
    check("suffix-preserved", new[start + len(replacement) :] == original[end:])

    try:
        new_doc = _Parser(new).parse()
        check("structure-valid", True)
    except ParseError as exc:
        check("structure-valid", False, str(exc))
        return {"checks": checks, "ok": False}

    try:
        _orig_value, orig_refs, orig_shareables = _decode_recording(original)
        new_value, new_refs, new_shareables = _decode_recording(new)
        check("decodes", True)
    except Exception as exc:
        check("decodes", False, f"{type(exc).__name__}: {exc}")
        return {"checks": checks, "ok": False}

    orig_ref_by_offset = {}
    for (ref_item, index), (r_index, resolved) in zip(
        _Parser(original).parse().sharedrefs, orig_refs
    ):
        orig_ref_by_offset[ref_item.start] = (index, resolved)

    # Every reference outside the authorized span must resolve exactly as
    # before -- except references to an intentionally edited shared target.
    defs_before_span = plan["shareables"]["defined_before_span"]
    new_defs = plan["shareables"]["defined_in_replacement"]
    shift = plan["shareables"]["index_shift"]
    reference_failures: list[str] = []
    for (ref_item, index), (r_index, resolved) in zip(new_doc.sharedrefs, new_refs):
        offset = ref_item.start
        if start <= offset < start + len(replacement):
            # Reference inside the replacement: must resolve to the shareable
            # at the same table index, and consistently with the original
            # where the index maps to a pre-existing shareable.
            if index >= len(new_shareables) or resolved is not new_shareables[index]:
                reference_failures.append(
                    f"replacement reference at {offset} does not resolve to shareable #{index}"
                )
                continue
            if index < defs_before_span:
                mapped: int | None = index
            elif index >= defs_before_span + new_defs:
                mapped = index - shift
            else:
                mapped = None  # defined inside the replacement: no counterpart
            if mapped is not None and not _safe_equal(resolved, orig_shareables[mapped]):
                reference_failures.append(
                    f"replacement reference at {offset} to shareable #{index} "
                    f"differs from the original value"
                )
            continue
        orig_offset = offset if offset < start else offset - delta
        original_ref = orig_ref_by_offset.get(orig_offset)
        if original_ref is None:
            reference_failures.append(f"no original reference at offset {orig_offset}")
            continue
        orig_index, orig_resolved = original_ref
        if orig_index != index:
            reference_failures.append(f"reference at {offset} renumbered: {orig_index} -> {index}")
        elif mode == "shared" and index == shared_index:
            if not _safe_equal(resolved, new_shareables[shared_index]):
                reference_failures.append(
                    f"reference at {offset} does not resolve to the edited shared target #{index}"
                )
        elif not _safe_equal(resolved, orig_resolved):
            reference_failures.append(f"reference at {offset} to shareable #{index} changed value")
    check("references-preserved", not reference_failures, "; ".join(reference_failures))

    # The edited field must hold the new value.  The decoded steps lead to
    # the target in both modes (tag 28 is transparent in decoded values).
    if expected_value is not None:
        try:
            actual = _navigate(new_value, plan["decoded_steps"])
            ok = _safe_equal(actual, expected_value)
            check("value-updated", ok, "" if ok else "value mismatch")
        except (KeyError, IndexError, TypeError) as exc:
            check("value-updated", False, f"cannot navigate to target: {exc}")
    if mode == "shared" and shared_index is not None:
        check(
            "shared-target-defined",
            shared_index < len(new_shareables) and new_shareables[shared_index] is not None,
        )

    return {"checks": checks, "ok": all(c["ok"] for c in checks)}


def apply_edit(
    data: bytes,
    plan: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    """
    Apply a plan produced by :func:`plan_edit` to ``data``.

    Stops without writing anything if the original changed since planning,
    or if the new copy fails verification.

    :return: ``(new_bytes, verification_report)``
    """
    if plan.get("version") != PLAN_VERSION:
        raise SurgicalError(f"unsupported plan version {plan.get('version')!r}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != plan["input"]["sha256"] or len(data) != plan["input"]["size"]:
        raise OriginalChangedError(
            "the original message changed since the plan was made; replan first"
        )

    start = plan["target"]["start"]
    end = plan["target"]["end"]
    replacement = bytes.fromhex(plan["replacement"]["hex"])
    new = data[:start] + replacement + data[end:]

    # The expected value, if it can be decoded standalone (a replacement
    # with references into the surrounding document is verified through the
    # reference checks instead).
    try:
        with _structural_decoding():
            expected_value: Any = loads(replacement)
    except Exception:
        expected_value = None

    report = _verify(data, new, plan, expected_value)
    report["changed_bytes"] = _changed_byte_count(data[start:end], replacement)
    if not report["ok"]:
        raise VerificationError(report)
    return new, report


#
# Command line interface
#
def _read_replacement(options: argparse.Namespace) -> bytes:
    if options.value_json is not None:
        return dumps(json.loads(options.value_json))
    return bytes.fromhex(options.value_cbor_hex)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m cbor2.surgical",
        description="Surgically edit one field of a CBOR message, preserving "
        "the original encoding of everything else.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="compute the rewrite scope")
    plan_parser.add_argument("input", help="the original CBOR message")
    plan_parser.add_argument("--path", required=True, help="path to the target field")
    plan_parser.add_argument(
        "--mode",
        choices=["occurrence", "shared"],
        default="occurrence",
        help="edit only this occurrence, or the shared target (tag 28 content)",
    )
    value_group = plan_parser.add_mutually_exclusive_group(required=True)
    value_group.add_argument("--value-json", help="replacement value as JSON")
    value_group.add_argument(
        "--value-cbor-hex", help="replacement as a hex-encoded CBOR data item"
    )
    plan_parser.add_argument("-o", "--plan-out", help="write the plan to this file")

    apply_parser = subparsers.add_parser("apply", help="apply a plan and verify")
    apply_parser.add_argument("plan", help="plan file produced by the plan command")
    apply_parser.add_argument(
        "--input", help="original CBOR message (default: recorded in the plan)"
    )
    apply_parser.add_argument("-o", "--output", required=True, help="where to write the new copy")

    options = parser.parse_args(argv)

    if options.command == "plan":
        with open(options.input, "rb") as f:
            data = f.read()
        replacement = _read_replacement(options)
        try:
            plan = plan_edit(data, options.path, options.mode, replacement)
        except BlockingError as exc:
            json.dump({"error": "blocked", "blocking": exc.relations}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            raise SystemExit(2)
        except SurgicalError as exc:
            raise SystemExit(f"error: {exc}")
        plan["input_path"] = options.input
        out = json.dumps(plan, indent=2) + "\n"
        if options.plan_out:
            with open(options.plan_out, "w") as f:
                f.write(out)
        else:
            sys.stdout.write(out)
    else:
        with open(options.plan) as f:
            plan = json.load(f)
        input_path = options.input or plan.get("input_path")
        if not input_path:
            raise SystemExit("error: no input given and none recorded in the plan")
        with open(input_path, "rb") as f:
            data = f.read()
        try:
            new, report = apply_edit(data, plan)
        except OriginalChangedError as exc:
            raise SystemExit(f"error: {exc}") from exc
        except VerificationError as exc:
            json.dump(exc.report, sys.stdout, indent=2)
            sys.stdout.write("\n")
            raise SystemExit(4) from exc
        except SurgicalError as exc:
            raise SystemExit(f"error: {exc}")
        with open(options.output, "wb") as f:
            f.write(new)
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover
    main()
