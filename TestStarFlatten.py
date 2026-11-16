# Testing of reversible save-frame flattening (src/star_flatten.py)
#
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import unittest

import CifFile
from CifFile.StarFile import StarFile, StarError
from CifFile.star_flatten import flatten_star, unflatten_star, MANIFEST_BLOCK_NAME


def read_star(text):
    return StarFile(datasource=text, from_str=True, grammar="STAR2",
                    scoping="instance")


def canonical_structure(star):
    """A name/path-based description of the nesting plus block contents,
    independent of internal access keys and block order."""
    child_table = star.child_table

    def path(key):
        parts = []
        while key is not None:
            parts.append(child_table[key].block_id.lower())
            key = child_table[key].parent
        return tuple(reversed(parts))

    result = {}
    for key in star.block_input_order:
        contents = []
        for name, value in star.dictionary[key].items():
            if isinstance(value, (list, tuple)):
                contents.append((name.lower(), tuple(str(v) for v in value)))
            else:
                contents.append((name.lower(), str(value)))
        result[path(key)] = sorted(contents)
    return result


def frame_parents(star):
    """Map each frame's printed name to its parent's printed name."""
    child_table = star.child_table
    result = {}
    for key in star.block_input_order:
        info = child_table[key]
        if info.parent is not None:
            result[key] = (info.block_id, child_table[info.parent].block_id)
    return result


class FlattenStructureTestCase(unittest.TestCase):
    """Basic flattening of nested save frames to a single level"""

    def setUp(self):
        self.nested = """
data_root
    _root_item   hello
    save_outer
        _outer_item  1
        save_inner
            _inner_item  2
        save_
        _after_item  3
    save_
    save_second
        _second_item  4
    save_
"""
        self.star = read_star(self.nested)
        self.flat = flatten_star(self.star)

    def testAllFramesAtOneLevel(self):
        """Every save frame is a direct child of the data block"""
        for key, (name, parent) in frame_parents(self.flat).items():
            self.assertEqual(parent, "root")

    def testNoFramesLost(self):
        """All frames survive flattening"""
        nested_frames = set(n for n, p in frame_parents(self.star).items())
        flat_frames = set(n for n, p in frame_parents(self.flat).items())
        self.assertEqual(len(nested_frames), len(flat_frames))

    def testNamesPreservedWithoutCollision(self):
        """Frames keep their original names when nothing collides"""
        names = sorted(n for n, p in frame_parents(self.flat).values())
        self.assertEqual(names, ["inner", "outer", "second"])

    def testContentsPreserved(self):
        """Frame contents are unchanged by flattening"""
        by_name = {}
        for key in self.flat.block_input_order:
            info = self.flat.child_table[key]
            by_name[info.block_id] = self.flat.dictionary[key]
        self.assertEqual(by_name["inner"]["_inner_item"], "2")
        self.assertEqual(by_name["outer"]["_outer_item"], "1")
        self.assertEqual(by_name["root"]["_root_item"], "hello")

    def testManifestPresent(self):
        """The manifest block records names, parents and positions"""
        self.assertIn(MANIFEST_BLOCK_NAME,
                      [i.block_id for i in self.flat.child_table.values()
                       if i.parent is None])
        manifest = self.flat[MANIFEST_BLOCK_NAME]
        records = list(zip(manifest["_star_flatten.frame_name"],
                           manifest["_star_flatten.original_name"],
                           manifest["_star_flatten.parent_frame"],
                           manifest["_star_flatten.parent_block"],
                           manifest["_star_flatten.child_index"]))
        self.assertEqual(len(records), 3)
        by_flat = dict((r[0], r) for r in records)
        self.assertEqual(by_flat["inner"][1], "inner")      # original name
        self.assertEqual(by_flat["inner"][2], "outer")      # parent frame
        self.assertEqual(by_flat["inner"][3], "root")       # data block
        self.assertEqual(by_flat["outer"][2], ".")          # parent is data block
        self.assertEqual(by_flat["second"][4], "1")         # sibling position

    def testSourceUnchanged(self):
        """Flattening does not modify the source file object"""
        self.assertEqual(canonical_structure(self.star),
                         canonical_structure(read_star(self.nested)))


class FlattenCollisionTestCase(unittest.TestCase):
    """Name collisions between frames of one data block"""

    def testActualCollisionRenamed(self):
        """Only genuinely colliding frames are renamed"""
        star = read_star("""
data_one
    save_alpha
        save_dup
            _x  1
        save_
    save_
    save_beta
        save_dup
            _x  2
        save_
    save_
data_two
    save_dup
        _x  3
    save_
""")
        flat = flatten_star(star)
        parents = frame_parents(flat)
        one_names = sorted(n for n, p in parents.values() if p == "one")
        two_names = sorted(n for n, p in parents.values() if p == "two")
        # the collision inside data_one is resolved with new legal names
        self.assertEqual(len(set(one_names)), 4)
        self.assertIn("dup", one_names)
        new_names = [n for n in one_names if n not in ("alpha", "beta", "dup")]
        self.assertEqual(len(new_names), 1)
        self.assertTrue(new_names[0].replace("_", "").isalnum())
        # data_two has no collision: its dup frame keeps its name
        self.assertEqual(two_names, ["dup"])

    def testCaseInsensitiveCollision(self):
        """Names differing only in case collide"""
        star = read_star("""
data_one
    save_alpha
        save_Frame
            _x  1
        save_
    save_
    save_beta
        save_frame
            _x  2
        save_
    save_
""")
        flat = flatten_star(star)
        names = [n.lower() for n, p in frame_parents(flat).values()]
        self.assertEqual(len(names), len(set(names)))


class FlattenReferenceTestCase(unittest.TestCase):
    """Rewriting of designated frame references"""

    def testResolvedReferenceFollowsRename(self):
        """A resolved reference is updated to the frame's new name"""
        star = read_star("""
data_one
    save_alpha
        save_dup
            _x  1
        save_
    save_
    save_beta
        save_dup
            _x  2
        save_
        _ref  dup
    save_
""")
        flat = flatten_star(star, {"_ref": "local"})
        parents = frame_parents(flat)
        # find the reference value in the flattened beta frame
        beta_key = [k for k, (n, p) in parents.items() if n == "beta"][0]
        ref_value = self.flat_value(flat, beta_key, "_ref")
        # beta's child dup is the one renamed (alpha claimed 'dup' first)
        beta_child = [n for k, (n, p) in parents.items()
                      if k != beta_key and n != "alpha" and n != "dup"]
        self.assertEqual(ref_value, beta_child[0])
        # and it still resolves to the frame with _x = 2
        target_key = [k for k, (n, p) in parents.items()
                      if n == ref_value][0]
        self.assertEqual(flat.dictionary[target_key]["_x"], "2")

    def flat_value(self, flat, key, dataname):
        return flat.dictionary[key][dataname]

    def testLocalScopeBeatsDistantSameName(self):
        """A local reference binds locally even when the name exists elsewhere"""
        star = read_star("""
data_one
    save_alpha
        save_config
            _x  alpha_child
        save_
        _ref  config
    save_
    save_beta
        save_config
            _x  beta_child
        save_
    save_
""")
        flat = flatten_star(star, {"_ref": "local"})
        parents = frame_parents(flat)
        alpha_key = [k for k, (n, p) in parents.items() if n == "alpha"][0]
        ref_value = flat.dictionary[alpha_key]["_ref"]
        target_key = [k for k, (n, p) in parents.items() if n == ref_value][0]
        self.assertEqual(flat.dictionary[target_key]["_x"], "alpha_child")

    def testMutualReferences(self):
        """Mutually referencing frames are renamed consistently"""
        star = read_star("""
data_one
    save_group
        save_left
            _partner  right
        save_
        save_right
            _partner  left
        save_
    save_
    save_other
        save_left
            _partner  right
        save_
        save_right
            _partner  left
        save_
    save_
""")
        flat = flatten_star(star, {"_partner": "siblings"})
        manifest = flat[MANIFEST_BLOCK_NAME]
        records = list(zip(manifest["_star_flatten.frame_name"],
                           manifest["_star_flatten.original_name"],
                           manifest["_star_flatten.parent_frame"]))
        by_parent = {}
        for flat_name, original, parent in records:
            by_parent.setdefault(parent, []).append(flat_name)
        group_children = sorted(by_parent["group"])
        other_children = sorted(by_parent["other"])
        self.assertEqual(group_children, ["left", "right"])
        self.assertEqual(len(set(other_children)), 2)
        self.assertNotIn("left", other_children)
        self.assertNotIn("right", other_children)
        # each renamed frame points at its renamed sibling
        parents = frame_parents(flat)
        for key, (name, parent) in parents.items():
            if name in other_children:
                partner = flat.dictionary[key]["_partner"]
                self.assertIn(partner, other_children)
                self.assertNotEqual(partner, name)

    def testUnresolvedReferenceLeftAlone(self):
        """A reference that does not resolve in its scope is not borrowed
        from another level"""
        star = read_star("""
data_one
    save_alpha
        _ref  ghost
    save_
    save_beta
        save_ghost
            _x  1
        save_
    save_
""")
        # 'ghost' exists as beta's child, but the reference in alpha only
        # sees alpha's own children: it must stay unresolved and unchanged
        flat = flatten_star(star, {"_ref": "children"})
        parents = frame_parents(flat)
        alpha_key = [k for k, (n, p) in parents.items() if n == "alpha"][0]
        self.assertEqual(flat.dictionary[alpha_key]["_ref"], "ghost")

    def testAmbiguousReferenceLeftAlone(self):
        """A reference matching several frames in scope is unresolved"""
        star = read_star("""
data_one
    save_alpha
        save_dup
            _x  1
        save_
    save_
    save_beta
        save_dup
            _x  2
        save_
        _ref  dup
    save_
""")
        # global scope sees both 'dup' frames: ambiguous, so untouched
        flat = flatten_star(star, {"_ref": "global"})
        parents = frame_parents(flat)
        beta_key = [k for k, (n, p) in parents.items() if n == "beta"][0]
        self.assertEqual(flat.dictionary[beta_key]["_ref"], "dup")

    def testPlainStringNotRewritten(self):
        """Ordinary strings equal to frame names are not references"""
        star = read_star("""
data_one
    save_alpha
        save_dup
            _x  1
        save_
    save_
    save_beta
        save_dup
            _x  2
        save_
        _description  dup
    save_
""")
        flat = flatten_star(star, {"_ref": "local"})
        parents = frame_parents(flat)
        beta_key = [k for k, (n, p) in parents.items() if n == "beta"][0]
        # _description is not a designated reference dataname
        self.assertEqual(flat.dictionary[beta_key]["_description"], "dup")

    def testLoopColumnReferences(self):
        """References inside loops are rewritten per packet"""
        star = read_star("""
data_one
    save_alpha
        save_dup
            _x  1
        save_
    save_
    save_beta
        save_dup
            _x  2
        save_
    save_
    save_holder
        loop_
            _ref
            _label
            dup   first
            alpha second
            none  third
    save_
""")
        flat = flatten_star(star, {"_ref": "global"})
        parents = frame_parents(flat)
        holder_key = [k for k, (n, p) in parents.items() if n == "holder"][0]
        refs = flat.dictionary[holder_key]["_ref"]
        dup_frames = sorted(n for n, p in parents.values() if "dup" in n)
        # 'dup' is ambiguous in global scope (two frames), so unchanged;
        # 'alpha' resolves and keeps its name; 'none' is unresolved
        self.assertEqual(list(refs), ["dup", "alpha", "none"])


class RoundTripTestCase(unittest.TestCase):
    """Flattening followed by unflattening restores the original"""

    def make_star(self):
        return read_star("""
data_one
    _root_item   hello
    save_alpha
        _a_item  1
        save_dup
            _x  1
            save_deep
                _d  deep
            save_
        save_
        _ref  dup
    save_
    save_beta
        save_dup
            _x  2
        save_
        _ref  dup
        _plain  dup
    save_
data_two
    save_dup
        _x  3
    save_
""")

    def testRoundTripStructure(self):
        """Original nesting, names and contents are restored"""
        star = self.make_star()
        flat = flatten_star(star, {"_ref": "local"})
        restored = unflatten_star(flat)
        self.assertEqual(canonical_structure(restored),
                         canonical_structure(star))

    def testRoundTripThroughText(self):
        """The flattened file is self-contained: write out, reparse, restore"""
        star = self.make_star()
        flat = flatten_star(star, {"_ref": "local"})
        text = flat.WriteOut()
        # the flattened text contains no nested save frames
        reparsed = read_star(text)
        for key, (name, parent) in frame_parents(reparsed).items():
            self.assertNotEqual(parent, None)
            self.assertIsNone(reparsed.child_table[
                [k for k in reparsed.block_input_order
                 if reparsed.child_table[k].block_id == parent][0]].parent)
        restored = unflatten_star(reparsed)
        self.assertEqual(canonical_structure(restored),
                         canonical_structure(star))

    def testUnflattenIgnoresPhysicalOrder(self):
        """Parentage comes from the manifest, not block order"""
        star = self.make_star()
        flat = flatten_star(star, {"_ref": "local"})
        # scramble the physical order of blocks
        import random
        random.seed(42)
        scrambled = list(flat.block_input_order)
        random.shuffle(scrambled)
        flat.block_input_order = scrambled
        restored = unflatten_star(flat)
        self.assertEqual(canonical_structure(restored),
                         canonical_structure(star))

    def testUnflattenWithoutManifestFails(self):
        """A file without a manifest cannot be unflattened"""
        star = read_star("data_plain\n  _a  1\n")
        self.assertRaises(StarError, unflatten_star, star)

    def testReferenceValuesRestored(self):
        """Rewritten references return to their original values"""
        star = self.make_star()
        flat = flatten_star(star, {"_ref": "local"})
        restored = unflatten_star(flat)
        refs = []
        for key in restored.block_input_order:
            block = restored.dictionary[key]
            if "_ref" in block:
                refs.append(block["_ref"])
        self.assertEqual(refs, ["dup", "dup"])


if __name__ == "__main__":
    unittest.main()
