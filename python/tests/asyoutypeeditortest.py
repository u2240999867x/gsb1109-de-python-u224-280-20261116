"""Unit tests for asyoutypeeditor.py"""

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import unittest

import phonenumbers
from phonenumbers import AsYouTypeFormatter, AsYouTypeEditor
from phonenumbers import DeletedChar, DeletedCharKind, EditResult, InputState
from phonenumbers import replay_edit_log
from phonenumbers.util import u


class AsYouTypeEditorTest(unittest.TestCase):
    """Unit tests for AsYouTypeEditor.

    Note that these tests use the real metadata, not the test metadata.
    """

    def _enter(self, editor, text):
        """Appends text one character at a time, returning the last result."""
        result = None
        for char in text:
            result = editor.insert(len(editor.display), char)
        return result

    def testAppendMatchesAsYouTypeFormatter(self):
        editor = AsYouTypeEditor("US")
        formatter = AsYouTypeFormatter("US")
        for char in "6502530000":
            expected = formatter.input_digit(char)
            result = editor.insert(len(editor.display), char)
            self.assertEqual(expected, result.display)
            self.assertEqual(expected, editor.display)
        self.assertEqual("(650) 253-0000", editor.display)
        self.assertEqual("6502530000", editor.raw_input)

    def testInsertAtArbitraryPosition(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, "6502530000")
        result = editor.insert(0, "1")
        self.assertEqual("1 (650) 253-0000", result.display)
        self.assertEqual("16502530000", result.raw_input)
        # Insert in the middle of the displayed text.
        editor = AsYouTypeEditor("US")
        self._enter(editor, "6502530000")
        # Display is "(650) 253-0000"; position 6 is just before "253".
        result = editor.insert(6, "9")
        self.assertEqual("65092530000", result.raw_input)

    def testAutoFormattingNotCountedAsUserDigits(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, "16502530000")
        self.assertEqual("1 (650) 253-0000", editor.display)
        # The spaces and parentheses in the display are not user input.
        self.assertEqual("16502530000", editor.raw_input)
        self.assertNotIn(" ", editor.raw_input)
        self.assertEqual("16502530000", editor.user_digits)

    def testDeleteAutoFormattingVersusDigit(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, "16502530000")
        self.assertEqual("1 (650) 253-0000", editor.display)
        # Position 1 is the automatically-inserted space.
        result = editor.delete(1)
        self.assertEqual(DeletedCharKind.AUTO_FORMATTING, result.deleted.kind)
        self.assertFalse(result.deleted.is_digit)
        self.assertTrue(result.deleted.is_formatting)
        self.assertFalse(result.deleted.is_user_entered)
        self.assertEqual(" ", result.deleted.char)
        # The raw input is untouched by deleting an auto-inserted separator.
        self.assertEqual("16502530000", result.raw_input)
        self.assertEqual("1 (650) 253-0000", result.display)
        # Position 3 holds the user-entered digit "6".
        result = editor.delete(3)
        self.assertEqual(DeletedCharKind.USER_DIGIT, result.deleted.kind)
        self.assertTrue(result.deleted.is_digit)
        self.assertFalse(result.deleted.is_formatting)
        self.assertTrue(result.deleted.is_user_entered)
        self.assertEqual("6", result.deleted.char)
        self.assertEqual("1502530000", result.raw_input)

    def testDeleteUserTypedFormatting(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, "650-2530000")
        # The user typed the dash themselves, so it is part of the raw input.
        self.assertEqual("650-2530000", editor.raw_input)
        result = editor.delete(3)
        self.assertEqual(DeletedCharKind.USER_FORMATTING, result.deleted.kind)
        self.assertFalse(result.deleted.is_digit)
        self.assertTrue(result.deleted.is_formatting)
        self.assertTrue(result.deleted.is_user_entered)
        self.assertEqual("-", result.deleted.char)
        self.assertEqual("6502530000", result.raw_input)

    def testDeletedCharEquality(self):
        deleted = DeletedChar("6", 3, DeletedCharKind.USER_DIGIT)
        self.assertEqual(DeletedChar("6", 3, DeletedCharKind.USER_DIGIT), deleted)
        self.assertNotEqual(DeletedChar("6", 3, DeletedCharKind.AUTO_FORMATTING), deleted)
        self.assertNotEqual(DeletedChar("5", 3, DeletedCharKind.USER_DIGIT), deleted)

    def testIncompleteInternationalPrefixPlus(self):
        editor = AsYouTypeEditor("US")
        result = self._enter(editor, "+")
        self.assertEqual(InputState.INCOMPLETE, result.state)
        self.assertTrue(editor.has_incomplete_international_prefix)
        self.assertFalse(editor.is_complete)
        self.assertIsNone(editor.parse())
        self.assertEqual("+", editor.raw_input)
        result = self._enter(editor, "4")
        self.assertEqual(InputState.INCOMPLETE, result.state)
        self.assertTrue(editor.has_incomplete_international_prefix)
        result = self._enter(editor, "9")
        # The country calling code is complete now, but the number is not.
        self.assertFalse(editor.has_incomplete_international_prefix)
        self.assertEqual(InputState.INCOMPLETE, result.state)
        result = self._enter(editor, "30123456")
        self.assertEqual(InputState.COMPLETE, result.state)
        self.assertTrue(editor.is_complete)
        self.assertEqual(phonenumbers.parse("+4930123456"), editor.parse())

    def testIncompleteInternationalPrefixIdd(self):
        editor = AsYouTypeEditor("US")
        result = self._enter(editor, "0")
        self.assertTrue(editor.has_incomplete_international_prefix)
        self.assertEqual(InputState.INCOMPLETE, result.state)
        self._enter(editor, "1")
        self.assertTrue(editor.has_incomplete_international_prefix)
        result = self._enter(editor, "1")
        # "011" is exactly the US IDD, with nothing after it yet.
        self.assertTrue(editor.has_incomplete_international_prefix)
        self.assertEqual(InputState.INCOMPLETE, result.state)
        self.assertEqual("011", editor.raw_input)
        self._enter(editor, "4")
        self.assertFalse(editor.has_incomplete_international_prefix)
        self.assertEqual(InputState.INCOMPLETE, editor.state)

    def testLeadingZeroPreserved(self):
        editor = AsYouTypeEditor("IT")
        result = self._enter(editor, "0236618300")
        self.assertEqual("02 3661 8300", editor.display)
        self.assertEqual("0236618300", editor.raw_input)
        self.assertTrue(editor.has_leading_zero)
        self.assertEqual(InputState.COMPLETE, result.state)
        numobj = editor.parse()
        self.assertTrue(numobj.italian_leading_zero)
        self.assertEqual(phonenumbers.parse("0236618300", "IT"), numobj)
        # A trunk prefix zero is not a significant leading zero.
        editor = AsYouTypeEditor("GB")
        self._enter(editor, "02070313000")
        self.assertFalse(editor.has_leading_zero)

    def testExtensionFragmentRetained(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, "6502530000")
        self.assertEqual(InputState.COMPLETE, editor.state)
        result = self._enter(editor, " x")
        # The extension fragment keeps the input in an incomplete state;
        # it must not be stored as a complete number yet.
        self.assertEqual(InputState.INCOMPLETE, result.state)
        self.assertFalse(editor.is_complete)
        self.assertTrue(editor.has_extension)
        self.assertEqual("6502530000 x", editor.raw_input)
        result = self._enter(editor, "123")
        self.assertEqual(InputState.COMPLETE, result.state)
        self.assertTrue(editor.has_extension)
        numobj = editor.parse()
        self.assertEqual("123", numobj.extension)
        self.assertEqual(phonenumbers.parse("6502530000 x123", "US"), numobj)

    def testRegionSwitch(self):
        editor = AsYouTypeEditor("GB")
        self._enter(editor, "02070313000")
        self.assertEqual("020 7031 3000", editor.display)
        result = editor.set_region("US")
        self.assertEqual("US", result.region_code)
        # The raw input survives the region switch.
        self.assertEqual("02070313000", result.raw_input)
        self.assertEqual(phonenumbers.parse("02070313000", "US"), editor.parse())
        result = editor.set_region("GB")
        self.assertEqual("020 7031 3000", result.display)
        self.assertEqual(phonenumbers.parse("02070313000", "GB"), editor.parse())

    def testReplayMatchesOneShotParse(self):
        events = [("insert", 0, "6502530000"),
                  ("insert", 0, "1"),
                  ("delete", 1),   # auto-inserted space
                  ("delete", 3),   # the digit "6"
                  ("insert", 3, "6"),
                  ("set_region", "US"),
                  ("insert", 0, "+")]
        editor, results = replay_edit_log("US", events)
        self.assertEqual(7, len(results))
        for result in results:
            self.assertIsInstance(result, EditResult)
        # Deleting the auto-inserted space left the raw input untouched,
        # deleting the digit removed it from the raw input.
        self.assertEqual(DeletedCharKind.AUTO_FORMATTING, results[2].deleted.kind)
        self.assertEqual(DeletedCharKind.USER_DIGIT, results[3].deleted.kind)
        # The replayed result matches a one-shot parse of the raw input.
        self.assertEqual(phonenumbers.parse(editor.raw_input, editor.region_code),
                         editor.parse())
        self.assertEqual("+16502530000", editor.raw_input)
        self.assertEqual("+1 650-253-0000", editor.display)
        self.assertEqual(InputState.COMPLETE, editor.state)

    def testReplayEditLogWithDictEvents(self):
        events = [{"op": "insert", "position": 0, "text": "6502530000"},
                  {"type": "insert", "position": 0, "text": "1"},
                  {"kind": "delete", "position": 1},
                  {"op": "set_region", "region": "US"}]
        editor, results = replay_edit_log("US", events)
        self.assertEqual("16502530000", editor.raw_input)
        self.assertEqual("1 (650) 253-0000", editor.display)
        self.assertEqual(DeletedCharKind.AUTO_FORMATTING, results[2].deleted.kind)
        self.assertEqual(phonenumbers.parse(editor.raw_input, "US"), editor.parse())

    def testReplayRejectsUnknownEvent(self):
        editor = AsYouTypeEditor("US")
        self.assertRaises(ValueError, editor.replay, [("frobnicate", 0)])
        self.assertRaises(ValueError, editor.replay, [{"op": "frobnicate"}])
        self.assertRaises(ValueError, editor.replay, [42])

    def testInvalidPositions(self):
        editor = AsYouTypeEditor("US")
        self.assertRaises(IndexError, editor.delete, 0)
        self.assertRaises(IndexError, editor.insert, 1, "5")
        self._enter(editor, "650")
        self.assertRaises(IndexError, editor.delete, 3)
        self.assertRaises(IndexError, editor.delete, -1)
        self.assertRaises(IndexError, editor.insert, 4, "5")

    def testClear(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, "6502530000")
        editor.clear()
        self.assertEqual("", editor.display)
        self.assertEqual("", editor.raw_input)
        self.assertEqual(InputState.EMPTY, editor.state)
        self.assertIsNone(editor.parse())
        self.assertEqual("US", editor.region_code)

    def testEmptyState(self):
        editor = AsYouTypeEditor("US")
        self.assertEqual(InputState.EMPTY, editor.state)
        self.assertFalse(editor.is_complete)
        self.assertFalse(editor.has_incomplete_international_prefix)
        self.assertFalse(editor.has_leading_zero)
        self.assertFalse(editor.has_extension)

    def testFullWidthDigits(self):
        editor = AsYouTypeEditor("US")
        self._enter(editor, u("６５０２５３００００"))
        self.assertEqual("(650) 253-0000", editor.display)
        # The raw input keeps the original full-width digits.
        self.assertEqual(u("６５０２５３００００"),
                         editor.raw_input)
        self.assertEqual("6502530000", editor.user_digits)
        # Deleting a displayed digit removes the corresponding raw character.
        result = editor.delete(1)
        self.assertEqual(DeletedCharKind.USER_DIGIT, result.deleted.kind)
        self.assertEqual(u("６"), result.deleted.char)
        self.assertEqual(9, len(editor.raw_input))

    def testInvalidRegion(self):
        editor = AsYouTypeEditor("ZZ")
        result = self._enter(editor, "6502530000")
        self.assertEqual("6502530000", result.display)
        self.assertEqual("6502530000", editor.raw_input)
        self.assertIsNone(editor.parse())
        self.assertEqual(InputState.INCOMPLETE, editor.state)


if __name__ == '__main__':
    unittest.main()
