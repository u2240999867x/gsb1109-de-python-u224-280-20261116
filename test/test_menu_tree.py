import io
import os
import shutil
import tempfile
import unittest

import xdg.Menu


def _menu_xml(body, apps):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Menu>\n<Name>Root</Name>\n<AppDir>%s</AppDir>\n%s\n</Menu>\n'
        % (apps, body)
    )


class MenuTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.apps = os.path.join(self.tmpdir, "applications")
        os.mkdir(self.apps)

        self.write_desktop("a.desktop", "Alpha", ["A"])
        self.write_desktop("b.desktop", "Beta", ["B"])
        self.write_desktop("both.desktop", "Both", ["A", "B"])
        self.write_desktop("none.desktop", "None", [])
        self.write_desktop("nodisplay.desktop", "No Display", ["A"],
                           "NoDisplay=true\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def write_desktop(self, filename, name, categories, extra=""):
        path = os.path.join(self.apps, filename)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(
                u"[Desktop Entry]\nType=Application\nName=%s\nExec=true\n"
                u"Categories=%s\n%s" %
                (name, ";".join(categories), extra)
            )

    def write_menu(self, filename, body):
        path = os.path.join(self.tmpdir, filename)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(_menu_xml(body, self.apps))
        return path

    def write_raw(self, filename, body):
        path = os.path.join(self.tmpdir, filename)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def menu_entry_ids(self, menu):
        return [entry.DesktopFileID for entry in menu.MenuEntries]

    def test_merged_tree_keeps_both_menus(self):
        self.write_raw(
            "child.menu",
            "<Menu><Name>Child</Name>"
            "<Menu><Name>A</Name>"
            "<Include><Category>A</Category></Include></Menu></Menu>"
        )
        test_file = self.write_menu(
            "root.menu",
            "<MergeFile>child.menu</MergeFile>"
            "<Menu><Name>B</Name>"
            "<Include><Category>B</Category></Include></Menu>"
        )
        menu = xdg.Menu.parse(test_file)

        self.assertEqual(menu.Name, "Root")
        self.assertIsNotNone(menu.getMenu("A"))
        self.assertIsNotNone(menu.getMenu("B"))
        self.assertEqual(set(self.menu_entry_ids(menu.getMenu("A"))),
                         {"a.desktop", "both.desktop", "nodisplay.desktop"})
        self.assertEqual(set(self.menu_entry_ids(menu.getMenu("B"))),
                         {"b.desktop", "both.desktop"})

    def test_application_can_appear_in_multiple_menus(self):
        test_file = self.write_menu(
            "multiple.menu",
            "<Menu><Name>A</Name>"
            "<Include><Category>A</Category></Include></Menu>"
            "<Menu><Name>B</Name>"
            "<Include><Category>B</Category></Include></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        both = menu.getMenu("A").getMenuEntry("both.desktop")

        self.assertIn(menu.getMenu("B").getMenuEntry("both.desktop"),
                      menu.getMenu("B").MenuEntries)
        self.assertEqual([m.Name for m in both.Parents], ["A", "B"])
        self.assertEqual([m.Name for m in both.Appearances], ["A", "B"])
        self.assertEqual(
            set(m.Name for m in menu.getEntryAppearances("both.desktop")),
            {"A", "B"})

    def test_appearance_distinguishes_hidden_entries(self):
        test_file = self.write_menu(
            "appearances.menu",
            "<Menu><Name>A</Name><Include><All/></Include></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        submenu = menu.getMenu("A")
        entry = submenu.getMenuEntry("nodisplay.desktop")

        self.assertIn(submenu, entry.Parents)
        self.assertNotIn(submenu, entry.Appearances)
        self.assertNotIn("nodisplay.desktop", menu.getAppearances(deep=True))
        self.assertIn("nodisplay.desktop",
                      menu.getAppearances(deep=True, visible=False))
        self.assertEqual(
            menu.getEntryAppearances("nodisplay.desktop"), [])
        self.assertEqual(
            menu.getEntryAppearances("nodisplay.desktop", visible=False),
            [submenu])

    def test_exclude_rule_reason_is_queryable(self):
        test_file = self.write_menu(
            "exclude.menu",
            "<Menu><Name>A</Name>"
            "<Include><Category>A</Category></Include>"
            "<Exclude><Filename>a.desktop</Filename></Exclude></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        submenu = menu.getMenu("A")

        self.assertNotIn("a.desktop", self.menu_entry_ids(submenu))
        reasons = submenu.getExclusionReason("a.desktop")
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0].Reason, xdg.Menu.EXCLUDED)
        self.assertIsNotNone(reasons[0].Rule)
        self.assertIn("<Filename>a.desktop</Filename>", reasons[0].Detail)
        self.assertEqual(reasons[0].DesktopFileID, "a.desktop")

    def test_not_included_reason_is_queryable(self):
        test_file = self.write_menu(
            "include.menu",
            "<Menu><Name>A</Name>"
            "<Include><Category>A</Category></Include></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        reasons = menu.getMenu("A").getExclusionReason("b.desktop")

        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0].Reason, xdg.Menu.NOT_INCLUDED)
        self.assertEqual(reasons[0].Menu.Name, "A")

    def test_visibility_reason_is_queryable(self):
        test_file = self.write_menu(
            "visibility.menu",
            "<Menu><Name>A</Name><Include><All/></Include></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        reasons = menu.getMenu("A").getExclusionReason("nodisplay.desktop")

        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0].Reason, xdg.Menu.NO_DISPLAY)

    def test_merge_cycle_terminates(self):
        self.write_raw(
            "first.menu",
            _menu_xml(
                "<MergeFile>second.menu</MergeFile>"
                "<Menu><Name>A</Name>"
                "<Include><Category>A</Category></Include></Menu>",
                self.apps)
        )
        self.write_raw(
            "second.menu",
            "<Menu><Name>Second</Name><MergeFile>first.menu</MergeFile>"
            "<Menu><Name>B</Name>"
            "<Include><Category>B</Category></Include></Menu></Menu>"
        )
        test_file = self.write_raw(
            "first.menu",
            _menu_xml(
                "<MergeFile>second.menu</MergeFile>"
                "<Menu><Name>A</Name>"
                "<Include><Category>A</Category></Include></Menu>",
                self.apps)
        )
        menu = xdg.Menu.parse(test_file)

        self.assertIsNotNone(menu.getMenu("A"))
        self.assertIsNotNone(menu.getMenu("B"))

    def test_missing_merge_file_does_not_empty_tree(self):
        test_file = self.write_menu(
            "missing.menu",
            "<MergeFile>does-not-exist.menu</MergeFile>"
            "<Menu><Name>A</Name>"
            "<Include><Category>A</Category></Include></Menu>"
        )
        builder = xdg.Menu.XMLMenuBuilder()
        menu = builder.parse(test_file)

        self.assertEqual(set(self.menu_entry_ids(menu.getMenu("A"))),
                         {"a.desktop", "both.desktop", "nodisplay.desktop"})
        self.assertEqual(menu.MissingMergeFiles,
                         [os.path.join(self.tmpdir, "does-not-exist.menu")])
        self.assertEqual(
            [record["Filename"] for record in builder.get_missing_merge_files()],
            [os.path.join(self.tmpdir, "does-not-exist.menu")])

    def test_layout_with_one_element_is_honoured(self):
        test_file = self.write_menu(
            "layout.menu",
            "<Menu><Name>A</Name><Include><All/></Include>"
            "<Layout><Filename>a.desktop</Filename></Layout></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        submenu = menu.getMenu("A")

        self.assertEqual([entry.DesktopFileID for entry in submenu.Entries],
                         ["a.desktop"])
        reasons = submenu.getExclusionReason("b.desktop")
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0].Reason, xdg.Menu.NOT_IN_LAYOUT)

    def test_explicit_empty_layout_shows_nothing(self):
        test_file = self.write_menu(
            "empty-layout.menu",
            "<Menu><Name>A</Name><Include><All/></Include>"
            "<Layout/></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        submenu = menu.getMenu("A")

        self.assertEqual(submenu.Entries, [])
        reasons = submenu.getExclusionReason("a.desktop")
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0].Reason, xdg.Menu.NOT_IN_LAYOUT)

    def test_default_layout_attributes_without_children(self):
        test_file = self.write_menu(
            "empty-default-layout.menu",
            "<Menu><Name>A</Name><Include><All/></Include>"
            "<DefaultLayout show_empty=\"true\"/></Menu>"
        )
        menu = xdg.Menu.parse(test_file)
        submenu = menu.getMenu("A")

        # The attributes are retained, and an explicit layout without <Merge>
        # does not add entries automatically.
        self.assertTrue(submenu.Layout.show_empty)
        self.assertEqual(submenu.Entries, [])

    def test_same_merge_file_in_two_branches(self):
        self.write_raw(
            "leaf.menu",
            "<Menu><Name>Leaf</Name>"
            "<Menu><Name>A</Name>"
            "<Include><Category>A</Category></Include></Menu></Menu>"
        )
        test_file = self.write_menu(
            "branches.menu",
            "<Menu><Name>P</Name><MergeFile>leaf.menu</MergeFile></Menu>"
            "<Menu><Name>Q</Name><MergeFile>leaf.menu</MergeFile></Menu>"
        )
        menu = xdg.Menu.parse(test_file)

        self.assertIsNotNone(menu.getMenu("P").getMenu("A"))
        self.assertIsNotNone(menu.getMenu("Q").getMenu("A"))
        self.assertEqual(
            self.menu_entry_ids(menu.getMenu("Q").getMenu("A")),
            self.menu_entry_ids(menu.getMenu("P").getMenu("A")))


if __name__ == '__main__':
    unittest.main()
