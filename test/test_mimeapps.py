import os
import shutil
import tempfile
import unittest

from xdg import MimeApps


def app(desktop_id, mimes, executable=True, **kwargs):
    data = {
        'id': desktop_id,
        'name': desktop_id,
        'type': 'Application',
        'exec': '/usr/bin/' + desktop_id,
        'executable': executable,
        'mime_types': mimes,
    }
    data.update(kwargs)
    return data


class MimeAppsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)
        self.user_file = os.path.join(self.tmpdir, 'config', 'mimeapps.list')

    def scenario(self, inventory, targets, inheritance=None, aliases=None,
                 associations=None, desktop_env=None):
        return {
            'config_home': os.path.join(self.tmpdir, 'config'),
            'user_file': self.user_file,
            'inventory': inventory,
            'targets': targets,
            'inheritance': inheritance or {},
            'aliases': aliases or {},
            'associations': associations,
            'desktop_env': desktop_env,
        }

    def plan(self, scenario):
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        return MimeApps.plan_associations(
            scenario['targets'],
            associations=associations,
            inventory=inventory,
            graph=graph)

    def write(self, text):
        directory = os.path.dirname(self.user_file)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(self.user_file, 'w') as fp:
            fp.write(text)

    def read(self):
        with open(self.user_file) as fp:
            return fp.read()


class InventoryTest(MimeAppsTestCase):
    def test_ineligible_applications_are_rejected(self):
        inventory = [
            app('ok.desktop', ['text/plain']),
            app('hidden.desktop', ['text/plain'], hidden=True),
            app('deleted.desktop', ['text/plain'], deleted=True),
            app('nodisplay.desktop', ['text/plain'], no_display=True),
            app('noexec.desktop', ['text/plain'], executable=False),
            app('wrongtype.desktop', ['text/plain'], type='Link'),
            app('unsupported.desktop', ['image/png']),
        ]
        scenario = self.scenario(inventory, [('text/plain', 'ok.desktop')])
        graph, inventory, associations = MimeApps.load_scenario(scenario)

        expected = {
            'ok.desktop': True,
            'hidden.desktop': False,
            'deleted.desktop': False,
            'nodisplay.desktop': False,
            'noexec.desktop': False,
            'wrongtype.desktop': False,
            'unsupported.desktop': False,
        }
        for desktop_id, allowed in expected.items():
            ok, reason = inventory.selectable(desktop_id, 'text/plain')
            self.assertEqual(ok, allowed, (desktop_id, reason))

        self.assertEqual(
            inventory.selectable('unsupported.desktop', 'text/plain')[1],
            'unsupported_mime_type')

    def test_supports_inherited_type(self):
        inventory = [app('editor.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/x-python', 'editor.desktop')],
            inheritance={'text/x-python': ['text/plain']})
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        self.assertEqual(
            inventory.selectable('editor.desktop', 'text/x-python'), (True, None))
        self.assertEqual(graph.ancestors('text/x-python'), ['text/plain'])

    def test_only_show_in_filters_inventory(self):
        inventory = [app('kde.desktop', ['text/plain'],
                         only_show_in=['KDE'])]
        scenario = self.scenario(inventory, [], desktop_env='GNOME')
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        self.assertEqual(
            inventory.selectable('kde.desktop', 'text/plain')[1],
            'not_shown_in_current_desktop')


class ExistingDefaultTest(MimeAppsTestCase):
    def test_existing_default_is_reported(self):
        self.write('[Default Applications]\ntext/plain=gedit.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain'])]
        scenario = self.scenario(inventory, [])
        scenario['associations'] = [
            {'path': self.user_file},
        ]
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        resolver = MimeApps.Resolver(associations, inventory, graph)
        resolved = resolver.resolve('text/plain')
        self.assertEqual(resolved.app, 'gedit.desktop')
        self.assertEqual(resolved.source, 'default')

    def test_inherited_default_from_parent(self):
        self.write('[Default Applications]\ntext/plain=gedit.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/x-python', 'gedit.desktop')],
            inheritance={'text/x-python': ['text/plain']})
        scenario['associations'] = [{'path': self.user_file}]
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        resolved = MimeApps.Resolver(
            associations, inventory, graph).resolve('text/x-python')
        self.assertEqual(resolved.app, 'gedit.desktop')
        self.assertEqual(resolved.inherited_from, 'text/plain')


class MinimalChangeTest(MimeAppsTestCase):
    def test_no_write_when_already_default(self):
        text = ('[Default Applications]\ntext/plain=kate.desktop;\n'
                '\n[Removed Associations]\ntext/plain=gedit.desktop;\n')
        self.write(text)
        inventory = [app('kate.desktop', ['text/plain']),
                     app('gedit.desktop', ['text/plain'])]
        scenario = self.scenario(inventory, [('text/plain', 'kate.desktop')])
        scenario['associations'] = [{'path': self.user_file}]
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        self.assertFalse(applied['written'])
        self.assertEqual(applied['reason'], 'unchanged')
        self.assertEqual(self.read(), text)

    def test_writes_only_the_changed_type(self):
        self.write('[Default Applications]\ntext/plain=gedit.desktop;\n'
                   'image/png=eog.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain']),
                     app('eog.desktop', ['image/png'])]
        scenario = self.scenario(inventory, [('text/plain', 'kate.desktop')])
        scenario['associations'] = [{'path': self.user_file}]
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        self.assertTrue(applied['written'])
        self.assertEqual(
            self.read(),
            '[Default Applications]\ntext/plain=kate.desktop;\n'
            'image/png=eog.desktop;\n')
        self.assertEqual(applied['verification']['selections'],
                         [{'mime': 'text/plain', 'expected': 'kate.desktop',
                           'actual': 'kate.desktop', 'source': 'default',
                           'origin': self.user_file, 'inherited_from': None}])

    def test_removes_redundant_entry_to_inherit(self):
        self.write('[Default Applications]\n'
                   'text/plain=gedit.desktop;\n'
                   'text/x-python=kate.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain', 'text/x-python'])]
        scenario = self.scenario(
            inventory, [('text/x-python', 'gedit.desktop')],
            inheritance={'text/x-python': ['text/plain']})
        scenario['associations'] = [{'path': self.user_file}]
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        self.assertEqual(
            self.read(),
            '[Default Applications]\ntext/plain=gedit.desktop;\n')
        self.assertEqual(applied['verification']['selections'][0]
                         ['inherited_from'], 'text/plain')

    def test_adds_new_type_keeping_other_defaults(self):
        self.write('[Default Applications]\nimage/png=eog.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('eog.desktop', ['image/png'])]
        scenario = self.scenario(inventory, [('text/plain', 'gedit.desktop')])
        scenario['associations'] = [{'path': self.user_file}]
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        self.assertEqual(
            self.read(),
            '[Default Applications]\nimage/png=eog.desktop;\n'
            'text/plain=gedit.desktop;\n')


class ConflictTest(MimeAppsTestCase):
    def test_same_mime_different_apps_is_a_conflict(self):
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/plain', 'gedit.desktop'),
                        ('text/plain', 'kate.desktop')])
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertFalse(applied['ok'])
        self.assertFalse(applied['written'])
        self.assertEqual(
            sorted(conflict['app'] for conflict in plan.as_dict()['conflicts']),
            ['gedit.desktop', 'kate.desktop'])
        self.assertFalse(os.path.exists(self.user_file))

    def test_conflict_keeps_original_file(self):
        original = '[Default Applications]\ntext/plain=gedit.desktop;\n'
        self.write(original)
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/plain', 'kate.desktop'),
                        ('text/plain', 'gedit.desktop')])
        scenario['associations'] = [{'path': self.user_file}]
        result = MimeApps.main_result = None
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertFalse(applied['ok'])
        self.assertEqual(self.read(), original)

    def test_inherited_change_is_reported(self):
        # Setting text/plain would also change text/x-python, which inherits
        # from text/plain and has no default of its own.
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/plain', 'kate.desktop')],
            inheritance={'text/x-python': ['text/plain']},
            associations=[
                {'path': self.user_file,
                 'defaults': {'text/plain': ['gedit.desktop']}},
            ])
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        text = self.read()
        self.assertIn('text/plain=kate.desktop;', text)
        self.assertIn('text/x-python=gedit.desktop;', text)

    def test_inherited_default_is_pinned(self):
        # text/x-python gets its default from text/plain.  Changing the
        # parent would change the child, which was not requested, so the
        # child's default is written explicitly and the shared config stays
        # valid.
        self.write('[Default Applications]\ntext/plain=gedit.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/plain', 'kate.desktop')],
            inheritance={'text/x-python': ['text/plain']},
            associations=[{'path': self.user_file}])
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        self.assertEqual(
            self.read(),
            '[Default Applications]\ntext/plain=kate.desktop;\n'
            'text/x-python=gedit.desktop;\n')
        child = [item for item in applied['verification']['selections']
                 if item['mime'] == 'text/x-python']
        self.assertEqual(child, [])
        actual = applied['verification']['actual']
        self.assertEqual(actual['text/x-python']['app'], 'gedit.desktop')

    def test_unrelated_default_is_preserved(self):
        self.write('[Default Applications]\nimage/png=eog.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('eog.desktop', ['image/png'])]
        scenario = self.scenario(
            inventory, [('text/plain', 'gedit.desktop')],
            associations=[{'path': self.user_file}])
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        self.assertIn('image/png=eog.desktop;', self.read())


class InheritanceTest(MimeAppsTestCase):
    def test_child_entry_does_not_shadow_parent_target(self):
        # An explicit child entry must not stop a requested parent default
        # from taking effect for the parent itself.
        self.write('[Default Applications]\ntext/x-python=kate.desktop;\n'
                   'text/plain=gedit.desktop;\n')
        inventory = [app('gedit.desktop', ['text/plain']),
                     app('kate.desktop', ['text/plain', 'text/x-python'])]
        scenario = self.scenario(
            inventory, [('text/plain', 'kate.desktop')],
            inheritance={'text/x-python': ['text/plain']},
            associations=[{'path': self.user_file}])
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        plan = MimeApps.plan_associations(
            scenario['targets'], associations=associations,
            inventory=inventory, graph=graph)
        applied = MimeApps.apply_plan(plan)
        self.assertTrue(applied['ok'], applied)
        # text/x-python keeps its own explicit entry; text/plain changes.
        self.assertEqual(
            self.read(),
            '[Default Applications]\ntext/x-python=kate.desktop;\n'
            'text/plain=kate.desktop;\n')

    def test_inheritance_chain_is_transitive(self):
        inventory = [app('editor.desktop', ['text/plain'])]
        scenario = self.scenario(
            inventory, [('text/x-python', 'editor.desktop')],
            inheritance={'text/x-python': ['text/x-script'],
                         'text/x-script': ['text/plain']})
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        self.assertEqual(graph.ancestors('text/x-python'),
                         ['text/x-script', 'text/plain'])
        self.assertEqual(
            inventory.selectable('editor.desktop', 'text/x-python')[0], True)

    def test_alias_is_canonicalised(self):
        inventory = [app('viewer.desktop', ['application/xml'])]
        scenario = self.scenario(
            inventory, [('text/xml', 'viewer.desktop')],
            aliases={'text/xml': 'application/xml'})
        graph, inventory, associations = MimeApps.load_scenario(scenario)
        self.assertEqual(graph.canonical('text/xml'), 'application/xml')
        self.assertEqual(
            inventory.selectable('viewer.desktop', 'text/xml')[0], True)


if __name__ == '__main__':
    unittest.main()
