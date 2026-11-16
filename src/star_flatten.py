"""
Reversible flattening of nested STAR save frames.

Upstream STAR (STAR2) data may contain arbitrarily nested save frames,
while some downstream consumers accept only a single level of save
frames directly beneath each data block.  This module produces a
*flattened* copy of such a file, embedding enough provenance in the
flattened file itself to reconstruct the original nesting later:

* every save frame is moved directly beneath its top-level data block;
* frames whose names would collide within their data block receive
  new, legal, unique names; all other frames keep their original
  names;
* a ``data_star_flatten_manifest`` block appended to the flattened
  file records, for every frame, its new name, its original name, its
  original parent and its position amongst its siblings, so that
  :func:`unflatten_star` can restore the original structure without
  guessing parentage from the physical order of the flattened file;
* datanames nominated by the caller as carrying save-frame references
  (together with the scope in which those references resolve) have
  their values updated, so that every reference that resolved before
  flattening still points at the same frame afterwards.  Values under
  all other datanames are never touched, even when they happen to
  coincide with a frame name, and references that cannot be resolved
  within their declared scope are left unchanged rather than being
  attached to a same-named frame from another level.

The reference specification passed to :func:`flatten_star` is a
mapping (or iterable of pairs) of ``dataname -> scope`` where ``scope``
is one of:

``global``
    resolve against every save frame in the file;
``root``
    resolve against every save frame beneath the same top-level data
    block as the referencing block;
``local``
    resolve against the referencing frame itself, its siblings and its
    direct children (for a data block, its direct child frames);
``siblings``
    resolve against frames sharing the referencing frame's parent;
``children``
    resolve against the referencing block's direct child frames only.

A reference value that matches exactly one frame name within its scope
is *resolved*; a value matching zero or several frames is left
untouched.
"""

from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import

import collections

try:
    from .StarFile import StarFile, StarBlock, StarError
except ImportError:                      # direct (non-package) use
    from StarFile import StarFile, StarBlock, StarError

__all__ = ["flatten_star", "unflatten_star", "MANIFEST_BLOCK_NAME", "SCOPES"]

#: Default name of the data block that records the flattening provenance.
MANIFEST_BLOCK_NAME = "star_flatten_manifest"

MANIFEST_VERSION = "1.0"

#: Scopes in which a frame reference may be resolved.
SCOPES = ("global", "root", "local", "siblings", "children")

_VERSION_ITEM = "_star_flatten.version"
_FRAME_ITEMS = ("_star_flatten.frame_name",
                "_star_flatten.original_name",
                "_star_flatten.parent_frame",
                "_star_flatten.parent_block",
                "_star_flatten.child_index")
_REF_ITEMS = ("_star_flatten.reference_dataname",
              "_star_flatten.reference_scope")

_NO_PARENT = "."        # manifest marker: the parent is the data block itself

_FrameRecord = collections.namedtuple("_FrameRecord",
                                      ["flat_name", "original", "parent",
                                       "root", "child_index"])


def _normalise_ref_spec(ref_spec):
    """Return `ref_spec` as a list of validated (dataname, scope) pairs."""
    if hasattr(ref_spec, "items"):
        ref_spec = list(ref_spec.items())
    normalised = []
    for entry in ref_spec:
        if isinstance(entry, dict):
            dataname = entry["dataname"]
            scope = entry.get("scope", "global")
        else:
            dataname, scope = entry
        scope = scope.lower()
        if scope not in SCOPES:
            raise StarError("Unknown frame reference scope {}".format(repr(scope)))
        normalised.append((dataname, scope))
    return normalised


class _FrameTree(object):
    """Structural description of the save frames in a block collection."""

    def __init__(self, star):
        self.star = star
        self.child_table = star.child_table
        self.order = [k for k in star.block_input_order if k in self.child_table]
        self.roots = [k for k in self.order if self.child_table[k].parent is None]
        self.frames = [k for k in self.order if self.child_table[k].parent is not None]
        self.root_set = set(self.roots)
        self.root_of = {}
        for key in self.frames:
            ancestor = self.child_table[key].parent
            while ancestor not in self.root_set:
                ancestor = self.child_table[ancestor].parent
            self.root_of[key] = ancestor
        self.children_of = {}
        for key in self.frames:
            self.children_of.setdefault(self.child_table[key].parent, []).append(key)
        self.original = {k: self.child_table[k].block_id for k in self.frames}
        self.child_index = {}
        for parent, children in self.children_of.items():
            for position, child in enumerate(children):
                self.child_index[child] = position

    def scope_frames(self, context, scope):
        """Return the frame keys visible from `context` under `scope`."""
        if scope == "global":
            return list(self.frames)
        if scope == "root":
            root = context if context in self.root_set else self.root_of[context]
            return [k for k in self.frames if self.root_of[k] == root]
        if scope == "children":
            return list(self.children_of.get(context, []))
        if scope == "siblings":
            if context in self.root_set:
                return []
            return list(self.children_of.get(self.child_table[context].parent, []))
        if scope == "local":
            found = list(self.children_of.get(context, []))
            if context not in self.root_set:
                found += self.children_of.get(self.child_table[context].parent, [])
            return found
        raise StarError("Unknown frame reference scope {}".format(repr(scope)))


def _iter_item_values(block, dataname):
    """Yield (index, value) for every value stored under `dataname` in
    `block`.  `index` is None for a scalar value, otherwise the position
    of the value in its loop column or compound value."""
    if dataname not in block:
        return
    value = block[dataname]
    if isinstance(value, (list, tuple)):
        for index, one_value in enumerate(value):
            yield index, one_value
    else:
        yield None, value


def _resolve_reference(tree, context, value, scope):
    """Return the key of the unique frame that `value` refers to from
    `context` under `scope`, or None if the reference does not resolve."""
    if not isinstance(value, str):
        return None
    matches = [k for k in tree.scope_frames(context, scope)
               if tree.original[k].lower() == value.lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def _assign_flat_names(tree, ref_spec, manifest_name):
    """Compute the name each frame will have in the flattened file.

    A frame keeps its original name unless that name is already used by
    another frame beneath the same data block.  Invented replacement
    names are legal (whitespace-free), unique within the whole file, and
    are chosen to differ from every existing frame name and every value
    found in a reference-carrying dataname, so that the renaming can be
    reversed unambiguously on unflattening.
    """
    reference_values = set()
    for context in tree.order:
        block = tree.star[context]
        for dataname, scope in ref_spec:
            for index, value in _iter_item_values(block, dataname):
                if isinstance(value, str):
                    reference_values.add(value.lower())
    reserved = set(name.lower() for name in tree.original.values())
    reserved.update(reference_values)
    reserved.add(manifest_name.lower())
    used_per_root = {root: set() for root in tree.roots}
    flat_names = {}
    for key in tree.frames:
        root = tree.root_of[key]
        original = tree.original[key]
        if original.lower() not in used_per_root[root]:
            candidate = original
        else:
            parent = tree.child_table[key].parent
            base = tree.child_table[parent].block_id
            candidate = "{}_{}".format(base, original)
            extra = 2
            while candidate.lower() in used_per_root[root] or \
                    candidate.lower() in reserved:
                candidate = "{}_{}_{}".format(base, original, extra)
                extra += 1
        used_per_root[root].add(candidate.lower())
        reserved.add(candidate.lower())
        flat_names[key] = candidate
    return flat_names


def _plan_reference_rewrites(tree, ref_spec, flat_names):
    """Return a list of (context, dataname, index, old, new) tuples
    describing the reference values that must change so that every
    resolved reference still points at the same frame after flattening.
    Unresolved references are never rewritten."""
    rewrites = []
    for context in tree.order:
        block = tree.star[context]
        for dataname, scope in ref_spec:
            if dataname not in block:
                continue
            for index, value in _iter_item_values(block, dataname):
                target = _resolve_reference(tree, context, value, scope)
                if target is None:
                    continue
                new_value = flat_names[target]
                if new_value.lower() != value.lower():
                    rewrites.append((context, dataname, index, value, new_value))
    return rewrites


def _rewrite_item_value(block, dataname, index, new_value):
    """Replace the value stored under `dataname` in `block` (element
    `index` if it is a list) with `new_value`, in place."""
    lower = dataname.lower()
    stored = block.block[lower]
    if not isinstance(stored, list):
        stored = list(stored)
        block.block[lower] = stored
    if index is None:
        if stored[0] is not None and not isinstance(stored[0], (list, tuple)):
            stored[0] = new_value
        else:
            stored[1] = new_value
        return
    for slot in (0, 1):
        candidate = stored[slot]
        if isinstance(candidate, list) and len(candidate) > index:
            candidate[index] = new_value
            return
    raise StarError("Cannot locate value {} of {} for rewriting".format(index, dataname))


def _copy_block(block):
    """Return an independent copy of a StarBlock (deep enough that
    in-place value rewrites cannot affect the source)."""
    new_block = StarBlock()
    new_block.block = {}
    for key, pair in block.block.items():
        new_pair = []
        for stored in pair:
            if isinstance(stored, list):
                new_pair.append(list(stored))
            else:
                new_pair.append(stored)
        new_block.block[key] = new_pair
    new_block.loops = {loop_no: list(names) for loop_no, names in block.loops.items()}
    new_block.item_order = list(block.item_order)
    new_block.true_case = dict(block.true_case)
    new_block.formatting_hints = dict(block.formatting_hints)
    new_block.characterset = block.characterset
    new_block.char_check = block.char_check
    new_block.maxnamelength = block.maxnamelength
    new_block.maxoutlength = block.maxoutlength
    new_block.wraplength = block.wraplength
    new_block.overwrite = block.overwrite
    return new_block


def _unique_key(collection, proposed):
    """Return a lowercase access key based on `proposed` that is unused
    in `collection`, following the library convention of appending '+'."""
    key = proposed.lower()
    while key in collection.lower_keys:
        key += "+"
    return key


def _insert_block(collection, key, block_id, parent_key, contents):
    """Insert `contents` into `collection` under `key`, printing as
    `block_id` and nested beneath `parent_key` (None for top level)."""
    collection.dictionary[key] = contents
    collection.lower_keys.add(key)
    collection.block_input_order.append(key)
    collection.child_table[key] = collection.PC(block_id, parent_key)
    if parent_key is None:
        collection.visible_keys.append(key)


def _add_manifest(flat, tree, flat_names, ref_spec, manifest_name):
    """Append the provenance block recording original frame names,
    parents and sibling positions, plus the reference specification."""
    manifest = StarBlock()
    manifest[_VERSION_ITEM] = MANIFEST_VERSION
    rows = []
    for key in tree.frames:
        parent = tree.child_table[key].parent
        if parent in tree.root_set:
            parent_name = _NO_PARENT
            root_id = tree.child_table[parent].block_id
        else:
            parent_name = flat_names[parent]
            root_id = tree.child_table[tree.root_of[key]].block_id
        rows.append((flat_names[key], tree.original[key], parent_name,
                     root_id, str(tree.child_index[key])))
    for item, column in zip(_FRAME_ITEMS, zip(*rows)):
        manifest[item] = list(column)
    manifest.CreateLoop(list(_FRAME_ITEMS))
    if ref_spec:
        manifest[_REF_ITEMS[0]] = [dataname for dataname, scope in ref_spec]
        manifest[_REF_ITEMS[1]] = [scope for dataname, scope in ref_spec]
        manifest.CreateLoop(list(_REF_ITEMS))
    key = _unique_key(flat, manifest_name)
    _insert_block(flat, key, manifest_name, None, manifest)


def flatten_star(star, ref_spec=(), manifest_name=MANIFEST_BLOCK_NAME,
                 output_grammar="2.0"):
    """Return a new StarFile in which every save frame of `star` appears
    directly beneath its top-level data block.

    `ref_spec` nominates the datanames that carry save-frame references
    and the scope in which each resolves (see the module docstring).
    The returned file carries a manifest block from which
    :func:`unflatten_star` restores the original nesting, original frame
    names and original reference values.
    """
    ref_spec = _normalise_ref_spec(ref_spec)
    tree = _FrameTree(star)
    root_ids = set(tree.child_table[root].block_id.lower() for root in tree.roots)
    base_name = manifest_name
    extra = 2
    while manifest_name.lower() in root_ids:
        manifest_name = "{}_{}".format(base_name, extra)
        extra += 1
    flat_names = _assign_flat_names(tree, ref_spec, manifest_name)
    rewrites = _plan_reference_rewrites(tree, ref_spec, flat_names)
    flat = StarFile(scoping="instance")
    flat.set_grammar(output_grammar)
    key_map = {}
    for old_key in tree.roots + tree.frames:
        info = tree.child_table[old_key]
        contents = _copy_block(star.dictionary[old_key])
        if info.parent is None:
            new_key = _unique_key(flat, info.block_id)
            _insert_block(flat, new_key, info.block_id, None, contents)
        else:
            new_key = _unique_key(flat, flat_names[old_key])
            _insert_block(flat, new_key, flat_names[old_key],
                          key_map[tree.root_of[old_key]], contents)
        key_map[old_key] = new_key
    for context, dataname, index, old_value, new_value in rewrites:
        _rewrite_item_value(flat.dictionary[key_map[context]], dataname,
                            index, new_value)
    if tree.frames:
        _add_manifest(flat, tree, flat_names, ref_spec, manifest_name)
    if getattr(star, "characterset", None):
        flat.set_characterset(star.characterset)
    flat.scoping = "instance"            # recompute visible keys
    return flat


def _find_manifest(flat, manifest_name):
    """Return the key of the manifest block in `flat`, or None."""
    if manifest_name is not None:
        for key in flat.block_input_order:
            info = flat.child_table[key]
            if info.parent is None and info.block_id.lower() == manifest_name.lower():
                return key
        return None
    for key in flat.block_input_order:
        info = flat.child_table[key]
        if info.parent is None and _FRAME_ITEMS[0] in flat.dictionary[key]:
            return key
    return None


def _manifest_column(manifest, item):
    """Return the values of `item` in the manifest block as a list."""
    value = manifest[item]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _read_manifest(manifest):
    """Extract frame records and reference datanames from the manifest."""
    missing = [item for item in _FRAME_ITEMS if item not in manifest]
    if missing:
        raise StarError("Flattening manifest is missing items {}".format(repr(missing)))
    columns = [_manifest_column(manifest, item) for item in _FRAME_ITEMS]
    records = []
    for row in zip(*columns):
        records.append(_FrameRecord(flat_name=row[0], original=row[1],
                                    parent=row[2], root=row[3],
                                    child_index=int(row[4])))
    ref_datanames = []
    if _REF_ITEMS[0] in manifest:
        ref_datanames = _manifest_column(manifest, _REF_ITEMS[0])
    return records, ref_datanames


def unflatten_star(flat, manifest_name=None, output_grammar="STAR2"):
    """Rebuild the original nested structure from a StarFile produced by
    :func:`flatten_star`.

    Parentage is taken from the embedded manifest block, never from the
    physical order of blocks in the flattened file.  Reference values
    that were rewritten during flattening are restored to the original
    frame names.  Raises StarError if no manifest is present or the
    manifest does not match the frames actually found.
    """
    manifest_key = _find_manifest(flat, manifest_name)
    if manifest_key is None:
        raise StarError("No flattening manifest block found: "
                        "this file was not produced by flatten_star")
    records, ref_datanames = _read_manifest(flat.dictionary[manifest_key])
    out = StarFile(scoping="instance")
    out.set_grammar(output_grammar)
    root_keys = {}
    root_order = []
    for old_key in flat.block_input_order:
        info = flat.child_table[old_key]
        if info.parent is None and old_key != manifest_key:
            new_key = _unique_key(out, info.block_id)
            _insert_block(out, new_key, info.block_id, None,
                          _copy_block(flat.dictionary[old_key]))
            root_keys[info.block_id.lower()] = new_key
            root_order.append(new_key)
    # locate every frame in the flattened file by (root, printed name)
    flat_frame_keys = {}
    for old_key in flat.block_input_order:
        info = flat.child_table[old_key]
        if info.parent is not None:
            ancestor = info.parent
            while flat.child_table[ancestor].parent is not None:
                ancestor = flat.child_table[ancestor].parent
            root_id = flat.child_table[ancestor].block_id.lower()
            flat_frame_keys[(root_id, info.block_id.lower())] = old_key
    # check the manifest against reality before rebuilding
    record_by_name = {}
    for record in records:
        tag = (record.root.lower(), record.flat_name.lower())
        if tag in record_by_name:
            raise StarError("Manifest lists frame {} of data block {} twice".format(
                record.flat_name, record.root))
        if record.root.lower() not in root_keys:
            raise StarError("Manifest refers to unknown data block {}".format(
                record.root))
        if tag not in flat_frame_keys:
            raise StarError("Manifest refers to missing save frame {} of "
                            "data block {}".format(record.flat_name, record.root))
        record_by_name[tag] = record
    unlisted = set(flat_frame_keys) - set(record_by_name)
    if unlisted:
        raise StarError("Save frames missing from the manifest: {}".format(
            repr(sorted(unlisted))))
    # recreate the frames, then attach them to their recorded parents
    new_frame_keys = {}
    for record in records:
        tag = (record.root.lower(), record.flat_name.lower())
        new_key = _unique_key(out, record.original)
        out.dictionary[new_key] = _copy_block(flat.dictionary[flat_frame_keys[tag]])
        out.lower_keys.add(new_key)
        new_frame_keys[tag] = new_key
    for record in records:
        tag = (record.root.lower(), record.flat_name.lower())
        if record.parent == _NO_PARENT:
            parent_key = root_keys[record.root.lower()]
        else:
            parent_key = new_frame_keys[(record.root.lower(), record.parent.lower())]
        out.child_table[new_frame_keys[tag]] = out.PC(record.original, parent_key)
    # restore the original order of frames within each parent
    children = {}
    for record in records:
        tag = (record.root.lower(), record.flat_name.lower())
        parent_key = out.child_table[new_frame_keys[tag]].parent
        children.setdefault(parent_key, []).append(
            (record.child_index, new_frame_keys[tag]))
    ordered = []

    def emit(key):
        ordered.append(key)
        for position, child in sorted(children.get(key, [])):
            emit(child)

    for root_key in root_order:
        emit(root_key)
    out.block_input_order = ordered
    # reverse the reference rewrites: values equal to an invented flat
    # name are restored to the frame's original name
    renamed = {}
    for record in records:
        if record.flat_name.lower() != record.original.lower():
            renamed[record.flat_name.lower()] = record.original
    if renamed:
        for key in out.block_input_order:
            block = out.dictionary[key]
            for dataname in ref_datanames:
                if dataname not in block:
                    continue
                pending = []
                for index, value in _iter_item_values(block, dataname):
                    if isinstance(value, str) and value.lower() in renamed:
                        pending.append((index, renamed[value.lower()]))
                for index, original in pending:
                    _rewrite_item_value(block, dataname, index, original)
    if getattr(flat, "characterset", None):
        out.set_characterset(flat.characterset)
    out.scoping = "instance"
    return out


def main(argv=None):
    """Command line interface: flatten or unflatten a STAR file."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Reversibly flatten nested STAR save frames to a "
                    "single level, or restore the original nesting.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    flat_parser = subparsers.add_parser(
        "flatten", help="produce a single-level copy of a STAR2 file")
    flat_parser.add_argument("infile")
    flat_parser.add_argument("outfile")
    flat_parser.add_argument(
        "--ref", action="append", default=[], metavar="DATANAME:SCOPE",
        help="dataname carrying save-frame references and the scope in "
             "which they resolve (one of {}; may be repeated)".format(", ".join(SCOPES)))
    unflat_parser = subparsers.add_parser(
        "unflatten", help="restore the original nested structure")
    unflat_parser.add_argument("infile")
    unflat_parser.add_argument("outfile")
    args = parser.parse_args(argv)
    if args.command == "flatten":
        ref_spec = []
        for ref in args.ref:
            dataname, _, scope = ref.partition(":")
            ref_spec.append((dataname, scope or "global"))
        star = StarFile(datasource=args.infile, grammar="STAR2", scoping="instance")
        result = flatten_star(star, ref_spec)
    else:
        star = StarFile(datasource=args.infile, scoping="instance")
        result = unflatten_star(star)
    with open(args.outfile, "w") as out_file:
        out_file.write(result.WriteOut())


if __name__ == "__main__":
    main()
