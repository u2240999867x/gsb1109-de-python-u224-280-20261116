import os
import shutil
import tempfile
import unittest

from xdg import Mime


class MimeAppsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_home = os.path.join(self.tmpdir, 'config')
        self.data_home = os.path.join(self.tmpdir, 'data')
        self.data_dir = os.path.join(self.tmpdir, 'system-data')
        self.config_dir = os.path.join(self.tmpdir, 'system-config')
        os.makedirs(self.config_home)
        os.makedirs(os.path.join(self.data_home, 'applications'))
        os.makedirs(os.path.join(self.data_dir, 'applications'))
        os.makedirs(self.config_dir)

        # Keep the shared MIME database untouched by using explicit private
        # aliases and inheritance for this test.
        Mime.update_cache()
        self._saved_aliases = dict(Mime.aliases)
        self._saved_inheritance = {k: list(v)
                                   for k, v in Mime.inheritance.items()}

    def tearDown(self):
        Mime.aliases.clear()
        Mime.aliases.update(self._saved_aliases)
        Mime.inheritance.clear()
        for key, value in self._saved_inheritance.items():
            Mime.inheritance[key] = value
        shutil.rmtree(self.tmpdir)

    def write(self, path, content):
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as f:
            f.write(content)

    def resolver(self):
        return Mime.MimeApps(
            desktop='TestDesktop',
            config_home=self.config_home,
            config_dirs=[self.config_dir],
            data_home=self.data_home,
            data_dirs=[self.data_dir],
            scan_desktop_files=False,
        )

    def test_default_before_optional_and_layer_order(self):
        self.write(os.path.join(self.config_home, 'TestDesktop-mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=high.desktop;\n')
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=user-default.desktop;\n'
                   '[Added Associations]\n'
                   'text/x-child=user.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=low.desktop;\n'
                   '[Added Associations]\n'
                   'text/x-child=system.desktop;\n')

        r = self.resolver()
        apps = r.get_applications('text/x-child')
        self.assertEqual(list(apps),
                         ['high.desktop', 'user.desktop', 'system.desktop'])
        self.assertEqual(apps.default, 'high.desktop')
        self.assertEqual(r.get_default_application('text/x-child'),
                         'high.desktop')
        self.assertEqual(r.get_optional_applications('text/x-child'),
                         ['user.desktop', 'system.desktop'])

    def test_aliases_are_canonicalised(self):
        Mime.aliases['text/x-alias'] = 'text/x-child'
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-alias=alias.desktop;\n'
                   '[Added Associations]\n'
                   'text/x-alias=more.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['alias.desktop', 'more.desktop'])
        self.assertEqual(list(r.get_applications('text/x-alias')),
                         ['alias.desktop', 'more.desktop'])

    def test_child_added_precedes_parent_default(self):
        Mime.inheritance['text/x-child'].add('text/x-parent')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-parent=parent.desktop;\n'
                   '[Added Associations]\n'
                   'text/x-child=child-added.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['child-added.desktop', 'parent.desktop'])

    def test_parent_types_are_inherited_after_child(self):
        Mime.inheritance['text/x-child'].add('text/x-parent')
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=child.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-parent=parent.desktop;\n'
                   '[Added Associations]\n'
                   'text/x-parent=parent-added.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['child.desktop', 'parent.desktop',
                          'parent-added.desktop'])

        candidates = r.get_associations('text/x-child')
        self.assertEqual(candidates[-1].mimetype, 'text/x-parent')

    def test_explicit_removal_blocks_lower_layers(self):
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Removed Associations]\n'
                   'text/x-child=removed.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=removed.desktop;\n'
                   '[Added Associations]\n'
                   'text/x-child=kept.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['kept.desktop'])
        self.assertEqual(r.get_default_application('text/x-child'), None)

    def test_removal_does_not_reappear_from_parent(self):
        Mime.inheritance['text/x-child'].add('text/x-parent')
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Removed Associations]\n'
                   'text/x-child=parent.desktop;\n'
                   '[Default Applications]\n'
                   'text/x-child=child.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-parent=parent.desktop;\n')

        r = self.resolver()
        apps = r.get_applications('text/x-child')
        self.assertEqual(list(apps), ['child.desktop'])
        self.assertNotIn('parent.desktop', apps)

    def test_removal_at_higher_precedence_than_parent_association(self):
        Mime.inheritance['text/x-child'].add('text/x-parent')
        self.write(os.path.join(self.config_home, 'TestDesktop-mimeapps.list'),
                   '[Removed Associations]\n'
                   'text/x-child=parent.desktop;\n')
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-parent=parent.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')), [])

    def test_removal_does_not_apply_to_same_file_added_entry(self):
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Added Associations]\n'
                   'text/x-child=app.desktop;\n'
                   '[Removed Associations]\n'
                   'text/x-child=app.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['app.desktop'])

    def test_empty_default_overrides_lower_default(self):
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=;\n'
                   '[Added Associations]\n'
                   'text/x-child=optional.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=lower.desktop;\n')

        r = self.resolver()
        self.assertEqual(r.get_default_application('text/x-child'), None)
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['optional.desktop'])

    def test_candidates_preserve_every_source(self):
        self.write(os.path.join(self.config_home, 'mimeapps.list'),
                   '[Default Applications]\n'
                   'text/x-child=shared.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Added Associations]\n'
                   'text/x-child=shared.desktop;\n')

        r = self.resolver()
        candidates = r.get_associations('text/x-child')
        self.assertEqual([c.desktop_id for c in candidates],
                         ['shared.desktop', 'shared.desktop'])
        self.assertEqual([c.section for c in candidates],
                         ['default', 'added'])
        self.assertNotEqual(candidates[0].source, candidates[1].source)

        apps = r.get_applications('text/x-child')
        self.assertEqual(list(apps), ['shared.desktop'])
        self.assertEqual(len(apps[0].sources), 2)

    def test_explicit_layer_files(self):
        high = os.path.join(self.tmpdir, 'high.list')
        low = os.path.join(self.tmpdir, 'low.list')
        self.write(high, '[Default Applications]\n'
                         'text/x-child=high.desktop;\n')
        self.write(low, '[Default Applications]\n'
                        'text/x-child=low.desktop;\n'
                        '[Added Associations]\n'
                        'text/x-child=low-extra.desktop;\n')

        r = Mime.MimeApps(paths=[high, low])
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['high.desktop', 'low-extra.desktop'])

    def test_explicit_mime_data_alias_and_parent(self):
        high = os.path.join(self.tmpdir, 'high.list')
        low = os.path.join(self.tmpdir, 'low.list')
        self.write(high, '[Default Applications]\n'
                         'text/x-alias=high.desktop;\n'
                         '[Removed Associations]\n'
                         'text/x-base=low.desktop;\n')
        self.write(low, '[Default Applications]\n'
                        'text/x-base=low.desktop;\n'
                        '[Added Associations]\n'
                        'text/x-base=base.desktop;\n')

        r = Mime.MimeApps(
            paths=[high, low], scan_desktop_files=False,
            mime_data={
                'aliases': 'text/x-alias text/x-child\n',
                'subclasses': 'text/x-child text/x-base\n',
            })
        self.assertEqual(list(r.get_applications('text/x-alias')),
                         ['high.desktop', 'base.desktop'])
        candidates = r.get_associations('text/x-alias')
        self.assertEqual(candidates[0].mimetype, 'text/x-child')
        self.assertEqual(candidates[1].mimetype, 'text/x-base')

    def test_file_argument_uses_detected_type(self):
        r = self.resolver()
        path = os.path.join(self.tmpdir, 'sample-unknown-extension')
        with open(path, 'wb') as f:
            f.write(b'\x00\x01\x02\x03')

        self.assertEqual(list(r.get_applications(path)), [])

    def test_desktop_ids_use_dash_for_subdirectories(self):
        app_dir = os.path.join(self.data_home, 'applications')
        self.write(os.path.join(app_dir, 'vendor-app.desktop'),
                   '[Desktop Entry]\n'
                   'Type=Application\n'
                   'Name=Vendor App\n'
                   'MimeType=text/x-child;\n')

        r = Mime.MimeApps(
            desktop='TestDesktop', config_home=self.config_home,
            config_dirs=[], data_home=self.data_home, data_dirs=[],
            scan_desktop_files=True)
        self.assertIn('vendor-app.desktop',
                      list(r.get_applications('text/x-child')))

    def test_unknown_mime_does_not_select_by_name(self):
        r = self.resolver()
        self.assertEqual(list(r.get_applications('application/x-unknown')), [])
        self.assertEqual(
            list(Mime.get_applications_for_file('mystery.unknown')), [])


    def test_mimeinfo_cache_is_lowest_precedence(self):
        self.write(os.path.join(self.data_dir, 'applications', 'mimeinfo.cache'),
                   '[MIME Cache]\n'
                   'text/x-child=cache.desktop;\n')
        self.write(os.path.join(self.data_dir, 'applications', 'mimeapps.list'),
                   '[Added Associations]\n'
                   'text/x-child=list.desktop;\n')

        r = self.resolver()
        self.assertEqual(list(r.get_applications('text/x-child')),
                         ['list.desktop', 'cache.desktop'])
