#!/usr/bin/env python3
"""Tests for the offline icon pack planner (xdg.IconPack)."""

import json
import os
import shutil
import stat
import tempfile
import unittest

from xdg.IconPack import (IconPack, IconTarget, select_icon_pack,
                          build_icon_pack, plan_icon_pack)
from xdg.IconPack import main as iconpack_main


def write(path, data=b"icon"):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "wb") as stream:
        stream.write(data)


def theme_index(root, name, directories, inherits=None, extra=""):
    lines = ["[Icon Theme]", "Name=%s" % name]
    if inherits:
        lines.append("Inherits=%s" % inherits)
    lines.append("Directories=%s" % ",".join(directories))
    if extra:
        lines.append(extra)
    for directory in directories:
        lines.append("")
        lines.append("[%s]" % directory)
    return "\n".join(lines) + "\n"


class ThemeBuilder(object):
    """Create small real icon themes in a temporary icon root."""

    def __init__(self, root):
        self.root = root

    def directory(self, theme, subdir, size, kind="Fixed", scale=None,
                  threshold=None, min_size=None, max_size=None):
        path = os.path.join(self.root, theme, subdir)
        if not os.path.isdir(path):
            os.makedirs(path)
        lines = ["[%s]" % subdir, "Size=%d" % size, "Type=%s" % kind]
        if scale is not None:
            lines.append("Scale=%d" % scale)
        if threshold is not None:
            lines.append("Threshold=%d" % threshold)
        if min_size is not None:
            lines.append("MinSize=%d" % min_size)
        if max_size is not None:
            lines.append("MaxSize=%d" % max_size)
        return path, "\n".join(lines) + "\n"

    def theme(self, name, inherits=None, hidden=False):
        path = os.path.join(self.root, name)
        if not os.path.isdir(path):
            os.makedirs(path)
        return ThemeSpec(self, name)

    def finish(self, spec):
        header = ["[Icon Theme]", "Name=%s" % spec.name]
        if spec.inherits:
            header.append("Inherits=%s" % spec.inherits)
        if spec.hidden:
            header.append("Hidden=true")
        header.append("Directories=%s" % ",".join(spec.directories))
        body = "\n".join(header + [""] + spec.sections)
        with open(os.path.join(self.root, spec.name, "index.theme"), "w") as stream:
            stream.write(body + "\n")


class ThemeSpec(object):
    def __init__(self, builder, name):
        self.builder = builder
        self.name = name
        self.inherits = None
        self.hidden = False
        self.directories = []
        self.sections = []

    def inherit(self, *names):
        self.inherits = ",".join(names)
        return self

    def fixed(self, subdir, size, icons=(), scale=None):
        path, section = self.builder.directory(
            self.name, subdir, size, kind="Fixed", scale=scale)
        self.directories.append(subdir)
        self.sections.append(section)
        for icon in icons:
            write(os.path.join(path, icon + ".png"),
                  ("%s/%s/%s" % (self.name, subdir, icon)).encode("ascii"))
        return self

    def threshold(self, subdir, size, threshold, icons=(), scale=None):
        path, section = self.builder.directory(
            self.name, subdir, size, kind="Threshold", threshold=threshold,
            scale=scale)
        self.directories.append(subdir)
        self.sections.append(section)
        for icon in icons:
            write(os.path.join(path, icon + ".png"),
                  ("%s/%s/%s" % (self.name, subdir, icon)).encode("ascii"))
        return self

    def scalable(self, subdir, size, min_size, max_size, icons=(),
                 extension="svg", scale=None):
        path, section = self.builder.directory(
            self.name, subdir, size, kind="Scalable", min_size=min_size,
            max_size=max_size, scale=scale)
        self.directories.append(subdir)
        self.sections.append(section)
        for icon in icons:
            write(os.path.join(path, icon + "." + extension),
                  ("%s/%s/%s" % (self.name, subdir, icon)).encode("ascii"))
        return self

    def build(self):
        self.builder.finish(self)
        return self


class IconPackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "icons")
        os.makedirs(self.root)
        self.builder = ThemeBuilder(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def select(self, targets, theme, **kwargs):
        kwargs.setdefault("search_paths", [self.root])
        kwargs.setdefault("include_fallback", False)
        return select_icon_pack(targets, theme=theme, **kwargs)

    def paths(self, result):
        return sorted(entry["path"].split(os.sep + "icons" + os.sep, 1)[-1]
                      for entry in result.selected)


class SizeRuleTest(IconPackTestCase):
    def test_fixed_requires_exact_size_and_scale(self):
        spec = self.builder.theme("Fixed")
        spec.fixed("16x16/apps", 16, ["a"])
        spec.fixed("24x24/apps", 24, ["a"])
        spec.fixed("24x24@2/apps", 24, ["a"], scale=2)
        spec.build()

        result = self.select([{"name": "a", "size": 24, "scale": 1}], "Fixed")
        self.assertEqual(result.total_volume,
                         len("Fixed/24x24/apps/a"))
        self.assertIn("Fixed/24x24/apps/a.png", result.assignments[0]["path"])
        self.assertEqual(result.assignments[0]["directory_scale"], 1)

    def test_threshold_uses_declared_threshold_and_tolerance(self):
        spec = self.builder.theme("Threshold")
        spec.threshold("apps", 24, 2, ["a"])
        spec.build()

        # The theme's threshold already permits 26; a target tolerance of 1
        # permits 27, but 28 remains out of range.
        result = self.select([{"name": "a", "size": 27, "tolerance": 1}],
                             "Threshold")
        self.assertTrue(result.ok)

        result = self.select([{"name": "a", "size": 27, "tolerance": 0}],
                             "Threshold")
        self.assertFalse(result.ok)
        self.assertEqual(len(result.no_size_match), 1)

    def test_scalable_directory_range_and_scale(self):
        spec = self.builder.theme("Scalable")
        spec.scalable("scalable/apps", 128, 16, 256, ["a"])
        spec.scalable("scalable@2/apps", 128, 16, 256, ["a"], scale=2)
        spec.build()

        result = self.select([{"name": "a", "size": 48, "scale": 2}], "Scalable")
        self.assertTrue(result.ok)
        self.assertIn("scalable@2/apps/a.svg", result.assignments[0]["path"])

        result = self.select([{"name": "a", "size": 512, "scale": 2}],
                             "Scalable")
        self.assertFalse(result.ok)
        self.assertEqual(result.assignments[0]["status"], "no_size_match")

    def test_requested_extension_is_respected(self):
        spec = self.builder.theme("Vector")
        spec.fixed("32x32/apps", 32, ["a"])
        spec.scalable("scalable/apps", 32, 8, 512, ["a"])
        spec.build()

        result = self.select([{"name": "a.svg", "size": 32}], "Vector")
        self.assertTrue(result.ok)
        self.assertTrue(result.assignments[0]["path"].endswith(".svg"))

        result = self.select([{"name": "a.png", "size": 32}], "Vector")
        self.assertTrue(result.ok)
        self.assertTrue(result.assignments[0]["path"].endswith(".png"))

    def test_missing_requested_extension_is_not_renamed(self):
        spec = self.builder.theme("Formats")
        spec.fixed("32x32/apps", 32, ["a"])
        spec.build()

        result = self.select([{"name": "a.svg", "size": 32}], "Formats")
        self.assertFalse(result.ok)
        self.assertEqual(result.assignments[0]["status"], "missing")
        self.assertEqual(result.selected, [])
        self.assertEqual([entry["target"] for entry in result.missing], ["a.svg"])


class InheritanceTest(IconPackTestCase):
    def test_inheritance_precedence_is_declaration_order(self):
        base = self.builder.theme("Base")
        base.fixed("16x16/apps", 16, ["shared", "base-only"]).build()
        middle = self.builder.theme("Middle")
        middle.inherit("Base")
        middle.fixed("16x16/apps", 16, ["shared"]).build()
        top = self.builder.theme("Top")
        top.inherit("Middle", "Base")
        top.fixed("16x16/apps", 16, ["shared"]).build()

        result = self.select(
            [{"name": "shared", "size": 16},
             {"name": "base-only", "size": 16}], "Top")
        self.assertTrue(result.ok)
        self.assertIn("Top/16x16/apps/shared.png", result.assignments[0]["path"])
        self.assertIn("Base/16x16/apps/base-only.png",
                      result.assignments[1]["path"])

    def test_same_icon_in_another_theme_keeps_its_source(self):
        first = self.builder.theme("First")
        first.fixed("16x16/apps", 16, ["a"]).build()
        second = self.builder.theme("Second")
        second.fixed("16x16/apps", 16, ["a"]).build()

        result = self.select([{"name": "a", "size": 16}], "Second")
        self.assertIn("Second/16x16/apps/a.png", result.assignments[0]["path"])
        result = self.select([{"name": "a", "size": 16}], "First")
        self.assertIn("First/16x16/apps/a.png", result.assignments[0]["path"])

    def test_inheritance_cycle_terminates(self):
        alpha = self.builder.theme("Alpha")
        alpha.inherit("Beta")
        alpha.fixed("16x16/apps", 16, ["a"]).build()
        beta = self.builder.theme("Beta")
        beta.inherit("Alpha")
        beta.fixed("16x16/apps", 16, ["b"]).build()

        result = self.select(
            [{"name": "a", "size": 16}, {"name": "b", "size": 16}], "Alpha")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.selected), 2)

    def test_missing_theme_is_reported(self):
        result = self.select([{"name": "a", "size": 16}], "DoesNotExist")
        self.assertFalse(result.ok)
        self.assertEqual(result.theme_errors[0]["type"], "theme_missing")
        self.assertEqual(result.missing[0]["target"], "a")


class DiagnosticsTest(IconPackTestCase):
    def test_missing_file_is_distinct(self):
        spec = self.builder.theme("Solid")
        spec.fixed("16x16/apps", 16, ["present"]).build()

        result = self.select(
            [{"name": "present", "size": 16}, {"name": "absent", "size": 16}],
            "Solid")
        self.assertTrue(result.ok is False)
        self.assertEqual([x["target"] for x in result.missing], ["absent"])
        self.assertEqual(result.unreadable, [])
        self.assertEqual(result.no_size_match, [])
        self.assertEqual(result.assignments[0]["status"], "selected")
        self.assertEqual(result.assignments[1]["status"], "missing")

    def test_no_size_match_is_distinct(self):
        spec = self.builder.theme("WrongSize")
        spec.fixed("16x16/apps", 16, ["a"]).build()

        result = self.select([{"name": "a", "size": 48}], "WrongSize")
        self.assertEqual(result.assignments[0]["status"], "no_size_match")
        self.assertEqual(result.missing, [])
        self.assertEqual(result.unreadable, [])
        self.assertEqual(len(result.no_size_match), 1)
        self.assertEqual(result.no_size_match[0]["target"], "a")

    def test_unreadable_file_is_distinct(self):
        spec = self.builder.theme("Protected")
        spec.fixed("16x16/apps", 16, ["a"]).build()
        path = os.path.join(self.root, "Protected", "16x16/apps/a.png")
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("running as a user which can read mode-000 files")
            result = self.select([{"name": "a", "size": 16}], "Protected")
            self.assertEqual(result.assignments[0]["status"], "unreadable")
            self.assertEqual(result.missing, [])
            self.assertEqual(result.no_size_match, [])
            self.assertEqual(len(result.unreadable), 1)
            self.assertEqual(result.unreadable[0]["type"], "unreadable")
        finally:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def test_unsatisfiable_target_keeps_other_targets(self):
        spec = self.builder.theme("Partial")
        spec.fixed("16x16/apps", 16, ["good"])
        spec.fixed("24x24/apps", 24, ["too-small"])
        spec.build()

        result = self.select(
            [{"name": "good", "size": 16}, {"name": "too-small", "size": 48},
             {"name": "unknown", "size": 16}], "Partial")
        self.assertFalse(result.ok)
        self.assertEqual([entry["name"] for entry in result.satisfied], ["good"])
        self.assertEqual([entry["name"] for entry in result.unmet],
                         ["too-small", "unknown"])
        self.assertEqual([entry["name"] for entry in result.gaps],
                         ["too-small", "unknown"])
        self.assertEqual(len(result.selected), 1)


class OptimisationTest(IconPackTestCase):
    def test_shared_file_is_packed_once(self):
        spec = self.builder.theme("Shared")
        spec.fixed("24x24/apps", 24, ["a", "b", "c"]).build()

        result = self.select(
            [{"name": "a", "size": 24}, {"name": "b", "size": 24},
             {"name": "c", "size": 24}], "Shared")
        self.assertEqual(len(result.selected), 3)
        self.assertEqual(result.total_volume,
                         sum(len("Shared/24x24/apps/%s" % name)
                             for name in "abc"))

    def test_same_file_for_repeated_targets_once(self):
        spec = self.builder.theme("Repeat")
        spec.fixed("24x24/apps", 24, ["a"]).build()

        result = self.select(
            [{"name": "a", "size": 24}, {"name": "a", "size": 24}], "Repeat")
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(result.selected[0]["targets"], [0, 1])
        self.assertEqual(result.total_volume, len("Repeat/24x24/apps/a"))

    def test_chooses_cheapest_legal_file(self):
        spec = self.builder.theme("Cheap")
        spec.fixed("24x24/apps", 24, ["a"])
        spec.threshold("apps", 24, 4, ["a"])
        spec.build()

        # Both directories match 24px.  The threshold copy is smaller, so the
        # minimum-volume answer must choose it even though Fixed came first.
        result = self.select(
            [{"name": "a", "size": 24}], "Cheap",
            file_sizes={
                os.path.join(self.root, "Cheap/24x24/apps/a.png"): 500,
                os.path.join(self.root, "Cheap/apps/a.png"): 100,
            })
        self.assertTrue(result.ok)
        self.assertEqual(result.total_volume, 100)
        self.assertIn("Cheap/apps/a.png", result.assignments[0]["path"])

    def test_one_file_can_cover_many_targets_across_sizes(self):
        spec = self.builder.theme("Cover")
        spec.fixed("16x16/apps", 16, ["a"])
        spec.fixed("24x24/apps", 24, ["a"])
        spec.scalable("scalable/apps", 128, 8, 256, ["a"])
        spec.build()

        result = self.select(
            [{"name": "a", "size": 16}, {"name": "a", "size": 24},
             {"name": "a", "size": 64}], "Cover",
            file_sizes={
                os.path.join(self.root, "Cover/16x16/apps/a.png"): 10,
                os.path.join(self.root, "Cover/24x24/apps/a.png"): 10,
                os.path.join(self.root, "Cover/scalable/apps/a.svg"): 15,
            })
        self.assertTrue(result.ok)
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(result.total_volume, 15)
        self.assertEqual(result.selected[0]["targets"], [0, 1, 2])

    def test_files_in_separate_themes_are_not_deduplicated(self):
        base = self.builder.theme("BaseDup")
        base.fixed("16x16/apps", 16, ["a"]).build()
        top = self.builder.theme("TopDup")
        top.inherit("BaseDup")
        top.fixed("16x16/apps", 16, ["a"]).build()

        result = self.select(
            [{"name": "a", "size": 16},
             {"name": "a", "size": 16, "scale": 1}], "TopDup")
        self.assertEqual(len(result.selected), 1)

    def test_volume_can_be_given_by_extension(self):
        spec = self.builder.theme("Extensions")
        spec.fixed("24x24/apps", 24, ["a"])
        spec.scalable("scalable/apps", 24, 16, 32, ["a"])
        spec.build()

        result = self.select([{"name": "a", "size": 24}], "Extensions",
                             file_sizes={"png": 800, "svg": 50})
        self.assertEqual(result.total_volume, 50)
        self.assertTrue(result.assignments[0]["path"].endswith(".svg"))


class ApiTest(IconPackTestCase):
    def test_dict_and_target_forms_are_equivalent(self):
        spec = self.builder.theme("Api")
        spec.fixed("24x24/apps", 24, ["a"]).build()

        first = self.select(
            [{"name": "a", "size": 24, "scale": 1, "tolerance": 0}], "Api")
        second = self.select(
            [IconTarget("a", size=24, scale=1, tolerance=0)], "Api")
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_pixel_size_is_converted_using_scale(self):
        spec = self.builder.theme("Pixels")
        spec.fixed("48x48@2/apps", 48, ["a"], scale=2).build()

        result = self.select(
            [{"name": "a", "pixel_size": 96, "scale": 2}], "Pixels")
        self.assertTrue(result.ok)
        self.assertEqual(result.assignments[0]["size"], 48)

    def test_alias_functions_and_wrapper(self):
        spec = self.builder.theme("Alias")
        spec.fixed("16x16/apps", 16, ["a"]).build()

        opts = dict(theme="Alias", search_paths=[self.root],
                    include_fallback=False)
        for function in (select_icon_pack, build_icon_pack, plan_icon_pack):
            result = function([{"name": "a", "size": 16}], **opts)
            self.assertTrue(result.ok)

        pack = IconPack(theme="Alias", search_paths=[self.root],
                        include_fallback=False)
        self.assertTrue(pack.select([{"name": "a", "size": 16}]).ok)

    def test_theme_path_is_accepted(self):
        spec = self.builder.theme("ByPath")
        spec.fixed("16x16/apps", 16, ["a"]).build()

        index = os.path.join(self.root, "ByPath", "index.theme")
        for theme in (index, os.path.dirname(index)):
            result = select_icon_pack([{"name": "a", "size": 16}],
                                      theme=theme,
                                      search_paths=[self.root],
                                      include_fallback=False)
            self.assertTrue(result.ok)
            self.assertIn("ByPath", result.assignments[0]["path"])

    def test_cli_json(self):
        spec = self.builder.theme("Cli")
        spec.fixed("16x16/apps", 16, ["a"]).build()

        request = os.path.join(self.tmp, "request.json")
        output = os.path.join(self.tmp, "output.json")
        with open(request, "w") as stream:
            json.dump({
                "theme": "Cli",
                "search_paths": [self.root],
                "targets": [{"name": "a", "size": 16}],
                "file_sizes": {"png": 10},
            }, stream)

        with open(output, "w") as stream:
            old = os.sys.stdout
            os.sys.stdout = stream
            try:
                status = iconpack_main([request])
            finally:
                os.sys.stdout = old
        self.assertEqual(status, 0)
        with open(output) as stream:
            data = json.load(stream)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total_volume"], 10)

    def test_search_roots_are_ordered(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        builder = ThemeBuilder(other)
        spec = builder.theme("Ordered")
        spec.fixed("16x16/apps", 16, ["a"]).build()
        mine = self.builder.theme("Ordered")
        mine.fixed("16x16/apps", 16, ["a"]).build()

        result = select_icon_pack(
            [{"name": "a", "size": 16}], theme="Ordered",
            search_paths=[other, self.root], include_fallback=False)
        self.assertIn(os.sep + "other" + os.sep,
                      result.assignments[0]["path"])

    def test_fallback_theme_can_be_disabled(self):
        spec = self.builder.theme("Requested")
        spec.fixed("16x16/apps", 16, ["only-here"]).build()
        fallback = self.builder.theme("Fallback")
        fallback.fixed("16x16/apps", 16, ["fallback-only"]).build()

        result = self.select(
            [{"name": "fallback-only", "size": 16}], "Requested")
        self.assertFalse(result.ok)

        result = select_icon_pack(
            [{"name": "fallback-only", "size": 16}], theme="Requested",
            search_paths=[self.root], fallback_theme="Fallback",
            include_fallback=True)
        self.assertTrue(result.ok)

    def test_one_theme_can_use_directories_from_several_roots(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        builder = ThemeBuilder(other)
        spec = builder.theme("Split")
        spec.fixed("16x16/apps", 16, ["first"]).build()
        mine = self.builder.theme("Split")
        mine.fixed("16x16/apps", 16, ["second"]).build()

        result = select_icon_pack(
            [{"name": "first", "size": 16}, {"name": "second", "size": 16}],
            theme="Split", search_paths=[other, self.root],
            include_fallback=False)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.selected), 2)

    def test_earlier_root_wins_for_the_same_file(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        builder = ThemeBuilder(other)
        spec = builder.theme("SameFile")
        spec.fixed("16x16/apps", 16, ["a"]).build()
        mine = self.builder.theme("SameFile")
        mine.fixed("16x16/apps", 16, ["a"]).build()

        result = select_icon_pack(
            [{"name": "a", "size": 16}], theme="SameFile",
            search_paths=[other, self.root], include_fallback=False)
        self.assertIn(os.sep + "other" + os.sep,
                      result.assignments[0]["path"])


class DirectoryHelperTest(unittest.TestCase):
    def test_scalable_helper_accepts_both_spellings_and_scale(self):
        from xdg.IconTheme import DirectoryMatchesSize, DirectorySizeDistance

        class FakeTheme(dict):
            def getType(self, directory):
                return self[directory]["Type"]

            def getSize(self, directory):
                return self[directory]["Size"]

            def getScale(self, directory):
                return self[directory].get("Scale", 1)

            def getThreshold(self, directory):
                return self[directory].get("Threshold", 2)

            def getMinSize(self, directory):
                return self[directory]["MinSize"]

            def getMaxSize(self, directory):
                return self[directory]["MaxSize"]

        theme = FakeTheme(scalable={
            "Type": "Scalable", "Size": 48, "MinSize": 16, "MaxSize": 256})
        self.assertTrue(DirectoryMatchesSize("scalable", 96, theme))
        self.assertFalse(DirectoryMatchesSize("scalable", 96, theme, scale=2))
        self.assertEqual(DirectorySizeDistance("scalable", 8, theme), 8)
        self.assertEqual(DirectorySizeDistance("scalable", 300, theme), 44)
        self.assertEqual(DirectorySizeDistance("scalable", 96, theme), 0)


if __name__ == "__main__":
    unittest.main()
