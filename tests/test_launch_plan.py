import base64
import json
import os
import unittest
from contextlib import contextmanager

import LnkParse3
from LnkParse3.launch_plan import LaunchPlan
from LnkParse3.launch_plan import PLAN_FORMAT
from LnkParse3.launch_plan import RULE_COMMAND_LINE_TO_ARGV_W
from LnkParse3.launch_plan import RULE_MSVCRT
from LnkParse3.launch_plan import RULE_RAW
from LnkParse3.launch_plan import split_windows_command_line


TARGET_DIR = os.path.join(os.path.dirname(__file__), 'samples')


@contextmanager
def open_sample(path):
    with open(path, 'rb') as indata:
        yield base64.b64decode(indata.read())


class TestSplitWindowsCommandLine(unittest.TestCase):
    def test_simple_arguments(self):
        self.assertEqual(split_windows_command_line('a b c'), ['a', 'b', 'c'])

    def test_empty_string(self):
        self.assertEqual(split_windows_command_line(''), [])

    def test_quoted_argument_with_spaces(self):
        self.assertEqual(split_windows_command_line('"a b" c'), ['a b', 'c'])

    def test_empty_quoted_argument(self):
        self.assertEqual(split_windows_command_line('a "" b'), ['a', '', 'b'])

    def test_only_empty_quoted_argument(self):
        self.assertEqual(split_windows_command_line('""'), [''])

    def test_tab_separates_arguments(self):
        self.assertEqual(split_windows_command_line('a\tb'), ['a', 'b'])

    def test_tab_inside_quotes_is_kept(self):
        self.assertEqual(split_windows_command_line('"a\tb"'), ['a\tb'])

    def test_backslashes_not_before_quote_are_literal(self):
        self.assertEqual(
            split_windows_command_line(r'C:\dir\file.exe \\server\share'),
            ['C:\\dir\\file.exe', '\\\\server\\share'],
        )

    def test_even_backslashes_before_quote(self):
        # 2n backslashes + quote -> n backslashes, quote toggles quoted mode
        self.assertEqual(split_windows_command_line(r'\\"a b"'), ['\\a b'])

    def test_odd_backslashes_before_quote(self):
        # 2n+1 backslashes + quote -> n backslashes + literal quote
        self.assertEqual(split_windows_command_line(r'\\\"'), ['\\"'])

    def test_escaped_quote_inside_argument(self):
        self.assertEqual(split_windows_command_line(r'a\"b'), ['a"b'])

    def test_consecutive_backslashes_at_end(self):
        self.assertEqual(split_windows_command_line(r'a\\'), ['a\\\\'])

    def test_unterminated_quote(self):
        self.assertEqual(split_windows_command_line('"a b'), ['a b'])

    def test_msvcrt_double_quote_inside_quotes(self):
        self.assertEqual(
            split_windows_command_line('"a""b"', rule=RULE_MSVCRT),
            ['a"b'],
        )

    def test_command_line_to_argv_w_double_quote_inside_quotes(self):
        self.assertEqual(
            split_windows_command_line('"a""b"', rule=RULE_COMMAND_LINE_TO_ARGV_W),
            ['ab'],
        )

    def test_raw_rule_cannot_split(self):
        with self.assertRaises(ValueError):
            split_windows_command_line('a b', rule=RULE_RAW)


class TestLaunchPlanFromLnk(unittest.TestCase):
    def test_target_identity_from_link_info(self):
        with open_sample(os.path.join(TARGET_DIR, 'microsoft_example')) as indata:
            lnk = LnkParse3.lnk_file(indata=indata)

        plan = lnk.launch_plan()

        self.assertEqual(plan.target_identity, 'C:\\test\\a.txt')
        self.assertEqual(plan.target_source, 'link_info')
        self.assertEqual(plan.working_directory, 'C:\\test')
        self.assertEqual(plan.working_directory_kind, 'absolute')
        self.assertEqual(plan.resolved_working_directory, 'C:\\test')

    def test_no_rule_keeps_raw_arguments(self):
        with open_sample(os.path.join(TARGET_DIR, 'padded_cli_arguments')) as indata:
            lnk = LnkParse3.lnk_file(indata=indata)

        plan = lnk.launch_plan()

        self.assertEqual(plan.argument_rule, RULE_RAW)
        self.assertIsNone(plan.arguments)
        self.assertEqual(plan.raw_arguments, lnk.string_data.command_line_arguments())

    def test_declared_rule_splits_arguments(self):
        with open_sample(os.path.join(TARGET_DIR, 'padded_cli_arguments')) as indata:
            lnk = LnkParse3.lnk_file(indata=indata)

        plan = lnk.launch_plan(argument_rule=RULE_COMMAND_LINE_TO_ARGV_W)

        self.assertEqual(plan.argument_rule, RULE_COMMAND_LINE_TO_ARGV_W)
        self.assertIsNotNone(plan.arguments)
        self.assertEqual(plan.arguments[0], '/c')
        # raw string is still preserved alongside the parsed view
        self.assertEqual(plan.raw_arguments, lnk.string_data.command_line_arguments())

    def test_unknown_rule_is_rejected(self):
        with open_sample(os.path.join(TARGET_DIR, 'microsoft_example')) as indata:
            lnk = LnkParse3.lnk_file(indata=indata)

        with self.assertRaises(ValueError):
            lnk.launch_plan(argument_rule='posix-shell')

    def test_target_and_path_shaped_arguments_stay_separate(self):
        plan = LaunchPlan(
            target_identity='C:\\app\\tool.exe',
            target_source='link_info',
            argument_rule=RULE_COMMAND_LINE_TO_ARGV_W,
            raw_arguments='"C:\\app\\tool.exe" --verbose',
            arguments=['C:\\app\\tool.exe', '--verbose'],
        )

        self.assertEqual(plan.target_identity, 'C:\\app\\tool.exe')
        self.assertEqual(plan.arguments[0], plan.target_identity)
        self.assertNotEqual(plan.to_dict()['target'], plan.to_dict()['arguments'])


class TestDriveRelativeWorkingDirectory(unittest.TestCase):
    def test_drive_relative_unresolved_without_machine_config(self):
        kind, resolved = LaunchPlan._resolve_working_directory('C:work', None)

        self.assertEqual(kind, 'drive_relative_unresolved')
        self.assertIsNone(resolved)

    def test_drive_relative_unresolved_with_unknown_drive(self):
        config = {'drive_current_directories': {'D': 'D:\\data'}}
        kind, resolved = LaunchPlan._resolve_working_directory('C:work', config)

        self.assertEqual(kind, 'drive_relative_unresolved')
        self.assertIsNone(resolved)

    def test_drive_relative_resolved_with_machine_config(self):
        config = {'drive_current_directories': {'C': 'C:\\Windows\\System32'}}
        kind, resolved = LaunchPlan._resolve_working_directory('C:work\\sub', config)

        self.assertEqual(kind, 'drive_relative_resolved')
        self.assertEqual(resolved, 'C:\\Windows\\System32\\work\\sub')

    def test_drive_relative_bare_drive_resolves_to_current_directory(self):
        config = {'drive_current_directories': {'C:': 'C:\\Windows\\System32'}}
        kind, resolved = LaunchPlan._resolve_working_directory('C:', config)

        self.assertEqual(kind, 'drive_relative_resolved')
        self.assertEqual(resolved, 'C:\\Windows\\System32')

    def test_absolute_working_directory_is_kept_as_is(self):
        kind, resolved = LaunchPlan._resolve_working_directory('C:\\test', None)

        self.assertEqual(kind, 'absolute')
        self.assertEqual(resolved, 'C:\\test')

    def test_unc_working_directory_is_kept_as_is(self):
        kind, resolved = LaunchPlan._resolve_working_directory('\\\\server\\share', None)

        self.assertEqual(kind, 'unc')
        self.assertEqual(resolved, '\\\\server\\share')


class TestLaunchPlanRoundTrip(unittest.TestCase):
    def test_round_trip_keeps_arguments_and_rule(self):
        plan = LaunchPlan(
            target_identity='C:\\app\\tool.exe',
            target_source='link_info',
            working_directory='C:work',
            resolved_working_directory=None,
            working_directory_kind='drive_relative_unresolved',
            argument_rule=RULE_COMMAND_LINE_TO_ARGV_W,
            raw_arguments='a "" "b c"',
            arguments=['a', '', 'b c'],
        )

        reread = LaunchPlan.from_dict(plan.to_dict())

        self.assertEqual(reread, plan)
        self.assertEqual(reread.arguments, ['a', '', 'b c'])
        self.assertEqual(reread.argument_rule, RULE_COMMAND_LINE_TO_ARGV_W)
        self.assertEqual(reread.to_dict(), plan.to_dict())

    def test_round_trip_through_json(self):
        with open_sample(os.path.join(TARGET_DIR, 'padded_cli_arguments')) as indata:
            lnk = LnkParse3.lnk_file(indata=indata)
        plan = lnk.launch_plan(argument_rule=RULE_COMMAND_LINE_TO_ARGV_W)

        reread = LaunchPlan.from_json(plan.to_json())

        self.assertEqual(reread, plan)
        self.assertEqual(reread.arguments, plan.arguments)
        self.assertEqual(reread.argument_rule, plan.argument_rule)

    def test_unknown_extension_fields_are_preserved(self):
        data = LaunchPlan(target_identity='C:\\app\\tool.exe').to_dict()
        data['x-rpa-metadata'] = {'runbook': 42, 'tags': ['preview']}
        data['x-note'] = 'keep me'

        reread = LaunchPlan.from_dict(data)

        self.assertEqual(reread.extras['x-rpa-metadata'], {'runbook': 42, 'tags': ['preview']})
        self.assertEqual(reread.extras['x-note'], 'keep me')
        self.assertEqual(reread.to_dict(), data)

    def test_serialized_plan_declares_format(self):
        plan = LaunchPlan()

        self.assertEqual(plan.to_dict()['format'], PLAN_FORMAT)

    def test_unsupported_format_is_rejected(self):
        data = LaunchPlan().to_dict()
        data['format'] = 'lnk-launch-plan/999'

        with self.assertRaises(ValueError):
            LaunchPlan.from_dict(data)

    def test_json_round_trip_of_full_dict(self):
        with open_sample(os.path.join(TARGET_DIR, 'microsoft_example')) as indata:
            lnk = LnkParse3.lnk_file(indata=indata)
        plan = lnk.launch_plan(argument_rule=RULE_COMMAND_LINE_TO_ARGV_W)

        serialized = json.dumps(plan.to_dict())
        reread = LaunchPlan.from_dict(json.loads(serialized))

        self.assertEqual(reread.to_dict(), plan.to_dict())


if __name__ == '__main__':
    unittest.main()
