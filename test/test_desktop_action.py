#!/usr/bin/env python3
# coding: utf-8
import io
import os
import shutil
import tempfile
import unittest

from xdg.DesktopAction import (
    DesktopAction,
    DesktopApplication,
    discover_desktop_applications,
)

SYSTEM_DESKTOP = """[Desktop Entry]
Type=Application
Name=Editor
Name[de]=Editor (System)
Exec=editor %%U
Actions=Window;Document;

[Desktop Action Window]
Name=New Window
Name[de]=Neues Fenster
Exec=editor --new-window

[Desktop Action Document]
Name=New Document
Exec=editor --new-document

[Desktop Action SysOnly]
Name=System Only
Exec=editor --system-only
"""

OVERRIDE_DESKTOP = """[Desktop Entry]
Type=Application
Name=Editor Override
Name[de]=Editor (Benutzer)
Exec=editor-override %%U
Actions=Window;Ghost;Window;Empty;SysOnly;

[Desktop Action Window]
Name=Override Window
Name[fr]=Fenetre de remplacement
Exec=editor-override --window

[Desktop Action Empty]
Name=Empty Command
Exec=

[Desktop Action Orphan]
Name=Not Referenced
Exec=editor --orphan
"""

SIMPLE_DESKTOP = """[Desktop Entry]
Type=Application
Name=Simple
Name[de]=Einfach
Exec=simple
Actions=Run;

[Desktop Action Run]
Name=Run now
Name[de]=Jetzt ausfuehren
Exec=simple --run
"""


def write(path, content):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(content)


class DesktopApplicationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "editor.desktop")
        write(self.test_file, SYSTEM_DESKTOP)
        self.app = DesktopApplication.from_file(self.test_file)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_application_metadata_and_source(self):
        self.assertEqual(self.app.getId(), "editor.desktop")
        self.assertEqual(self.app.getFileName(), self.test_file)
        self.assertEqual(self.app.getSource(), self.test_file)
        self.assertEqual(self.app.getShadowedFiles(), [])
        self.assertEqual(str(self.app), "Editor")

    def test_actions_follow_actions_key_order(self):
        actions = self.app.getActions()
        self.assertEqual([a.getId() for a in actions], ["Window", "Document"])
        self.assertTrue(all(isinstance(a, DesktopAction) for a in actions))

        window = self.app.getAction("Window")
        self.assertEqual(window.getName(), "New Window")
        self.assertEqual(window.getExec(), "editor --new-window")
        self.assertEqual(window.getGroup(), "Desktop Action Window")
        self.assertEqual(window.getFileName(), self.test_file)
        self.assertEqual(window.getSource(),
                         (self.test_file, "Desktop Action Window"))

    def test_action_ids_scoped_to_application(self):
        other_file = os.path.join(self.tmpdir, "other.desktop")
        write(other_file, SIMPLE_DESKTOP)
        other = DesktopApplication.from_file(other_file)

        self.assertIsNotNone(self.app.getAction("Window"))
        self.assertIsNone(self.app.getAction("Run"))
        self.assertIsNotNone(other.getAction("Run"))
        self.assertIsNone(other.getAction("Window"))
        self.assertEqual(other.getAction("Run").getName(), "Run now")

    def test_unreferenced_group_is_not_an_action(self):
        # SysOnly has a group but is not listed in Actions=.
        self.assertIsNone(self.app.getAction("SysOnly"))
        self.assertNotIn("SysOnly",
                         [a.getId() for a in self.app.getActions()])
        self.assertEqual(self.app.getUnreferencedActionGroups(),
                         ["Desktop Action SysOnly"])

    def test_locale_fallback(self):
        self.assertEqual(self.app.getName(["de"]), "Editor (System)")
        window = self.app.getAction("Window")
        self.assertEqual(window.getName(["de"]), "Neues Fenster")
        # Unknown language falls back to the unlocalized Name.
        self.assertEqual(self.app.getName(["zh_CN"]), "Editor")
        self.assertEqual(window.getName(["zh_CN"]), "New Window")
        # Language ids are normalized and expanded (de_DE -> de).
        self.assertEqual(window.getName(["de_DE.UTF-8"]), "Neues Fenster")

    def test_clean_file_has_no_errors(self):
        self.assertEqual(self.app.getErrors(), {
            "missing_groups": [],
            "duplicate_references": [],
            "empty_commands": [],
        })


class DesktopActionErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.override = os.path.join(self.tmpdir, "editor.desktop")
        write(self.override, OVERRIDE_DESKTOP)
        self.app = DesktopApplication.from_file(self.override)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_missing_group_duplicate_and_empty_command(self):
        errors = self.app.getErrors()
        self.assertEqual(errors["missing_groups"],
                         ["Ghost", "SysOnly"])
        self.assertEqual(errors["duplicate_references"], ["Window"])
        self.assertEqual(errors["empty_commands"], ["Empty"])

    def test_only_valid_actions_are_executable(self):
        self.assertEqual([a.getId() for a in self.app.getActions()],
                         ["Window"])
        self.assertIsNone(self.app.getAction("Ghost"))
        self.assertIsNone(self.app.getAction("Empty"))
        self.assertIsNone(self.app.getAction("SysOnly"))

    def test_unreferenced_group_stays_inert(self):
        # Orphan is a valid group but Actions= never references it.
        self.assertIsNone(self.app.getAction("Orphan"))
        self.assertEqual(self.app.getUnreferencedActionGroups(),
                         ["Desktop Action Orphan"])

    def test_action_locale_chain(self):
        window = self.app.getAction("Window")
        self.assertEqual(window.getName(["fr"]), "Fenetre de remplacement")
        self.assertEqual(window.getName(["de"]), "Override Window")
        self.assertEqual(window.getName(), "Override Window")


class OverrideLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_dir = os.path.join(self.tmpdir, "user", "share")
        self.system_dir = os.path.join(self.tmpdir, "system", "share")
        self.system_file = os.path.join(
            self.system_dir, "applications", "editor.desktop")
        self.user_file = os.path.join(
            self.user_dir, "applications", "editor.desktop")
        write(self.system_file, SYSTEM_DESKTOP)
        write(self.user_file, OVERRIDE_DESKTOP)
        self.data_dirs = [self.user_dir, self.system_dir]

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_user_override_replaces_system_actions(self):
        apps = discover_desktop_applications(self.data_dirs)
        self.assertEqual(sorted(apps), ["editor.desktop"])

        app = apps["editor.desktop"]
        self.assertEqual(app.getFileName(), self.user_file)
        self.assertEqual(app.getSource(), self.user_file)
        self.assertEqual(app.getShadowedFiles(), [self.system_file])
        self.assertEqual(app.getName(), "Editor Override")

        # Only actions declared by the effective file are listed.
        self.assertEqual([a.getId() for a in app.getActions()], ["Window"])
        self.assertEqual(app.getAction("Window").getExec(),
                         "editor-override --window")

        # Actions that only exist in the shadowed system file are gone,
        # and nothing is stitched together across the two files.
        self.assertIsNone(app.getAction("Document"))
        self.assertIsNone(app.getAction("SysOnly"))
        errors = app.getErrors()
        self.assertIn("SysOnly", errors["missing_groups"])

        # An id declared by neither file can never appear.
        self.assertIsNone(app.getAction("Phantom"))

    def test_removing_override_restores_system_file(self):
        apps = discover_desktop_applications(self.data_dirs)
        self.assertEqual(apps["editor.desktop"].getFileName(), self.user_file)

        os.remove(self.user_file)
        apps = discover_desktop_applications(self.data_dirs)
        app = apps["editor.desktop"]

        self.assertEqual(app.getFileName(), self.system_file)
        self.assertEqual(app.getShadowedFiles(), [])
        self.assertEqual([a.getId() for a in app.getActions()],
                         ["Window", "Document"])
        self.assertEqual(app.getName(["de"]), "Editor (System)")
        self.assertIsNone(app.getAction("Phantom"))

    def test_nested_files_share_id_across_dirs(self):
        nested_system = os.path.join(
            self.system_dir, "applications", "vendor", "tool.desktop")
        nested_user = os.path.join(
            self.user_dir, "applications", "vendor", "tool.desktop")
        write(nested_system, SYSTEM_DESKTOP.replace("editor", "tool"))
        write(nested_user, SIMPLE_DESKTOP.replace("simple", "tool"))

        apps = discover_desktop_applications(self.data_dirs)
        self.assertIn("vendor-tool.desktop", apps)
        app = apps["vendor-tool.desktop"]
        self.assertEqual(app.getFileName(), nested_user)
        self.assertEqual(app.getShadowedFiles(), [nested_system])
        self.assertEqual([a.getId() for a in app.getActions()], ["Run"])

    def test_non_desktop_and_broken_files_are_skipped(self):
        write(os.path.join(self.system_dir, "applications", "notes.txt"),
              "not a desktop file")
        write(os.path.join(self.system_dir, "applications", "broken.desktop"),
              "Type=Application\n")
        apps = discover_desktop_applications(self.data_dirs)
        self.assertEqual(sorted(apps), ["editor.desktop"])


if __name__ == "__main__":
    unittest.main()
