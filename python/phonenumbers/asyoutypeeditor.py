"""An edit-aware phone number field built on top of AsYouTypeFormatter.

An AsYouTypeEditor can be created by invoking
AsYouTypeEditor(region_code). Unlike AsYouTypeFormatter, which only
supports appending one character at a time, the editor accepts
arbitrary-position edits (insert/delete anywhere in the displayed text)
and region switches, making it suitable for replaying the edit logs
(insertions, deletions, region changes) captured from a phone number
input field.

The editor keeps the raw user input (exactly the characters the user
entered) separate from the formatted display: separators inserted
automatically by the formatter (e.g. spaces) never become part of the
raw input. Deleting an automatically-inserted formatting character is
reported distinctly from deleting a user-entered digit, so the two can
be told apart when auditing a replayed log.

Incomplete input states are preserved as-is: a partially entered
international prefix (e.g. "+" or "011"), a significant leading zero
(e.g. Italian numbers) and a partially entered extension fragment are
kept in the raw input and are not reported as complete numbers. Once
the input is complete, parse() returns the same PhoneNumber that a
one-shot phonenumbers.parse() of the raw input would produce.
"""

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
import re

from enum import Enum

from .util import unicod, U_EMPTY_STRING
from .unicode_util import digit as unicode_digit
from .re_util import fullmatch, re_compile
from .asyoutypeformatter import AsYouTypeFormatter
from .phonemetadata import PhoneMetadata
from .phonenumberutil import _PLUS_SIGN, _PLUS_CHARS_PATTERN
from .phonenumberutil import _extract_country_code
from .phonenumberutil import normalize_digits_only, normalize_diallable_chars_only
from .phonenumberutil import is_possible_number, parse, NumberParseException

# Matches an extension label at the very end of the input with no
# extension digits following it yet, e.g. "6502530000 ext." or
# "6502530000 x". Such input is an extension fragment: the user is
# still typing and the input must not be treated as a complete number.
_EXTN_FRAGMENT_PATTERN = re.compile(unicod("(?:;ext=|ext\\.?|extn\\.?|extension|ex|x)\\s*$"),
                                    re.IGNORECASE)

# Bounds for expanding international-prefix patterns (e.g. "00(?:1[245]|99)")
# into literal strings when detecting partially entered IDD prefixes.
_MAX_IDD_EXPANSIONS = 256
_MAX_IDD_LENGTH = 12


class InputState(Enum):
    """How far the input accrued by an AsYouTypeEditor has progressed."""
    # Nothing has been entered yet.
    EMPTY = unicod("empty")
    # Some input has been entered but it is not a complete, possible
    # phone number yet (e.g. an incomplete international prefix, a
    # partial national number or an unfinished extension fragment).
    INCOMPLETE = unicod("incomplete")
    # The raw input parses to a possible phone number.
    COMPLETE = unicod("complete")


class DeletedCharKind(Enum):
    """What kind of character a delete() operation removed."""
    # A digit (or the leading plus sign) entered by the user.
    USER_DIGIT = unicod("user_digit")
    # A formatting character (separator) entered by the user.
    USER_FORMATTING = unicod("user_formatting")
    # A formatting character (separator) inserted automatically by the
    # formatter; it was never part of the raw user input.
    AUTO_FORMATTING = unicod("auto_formatting")


class DeletedChar(object):
    """Describes the single character removed by a delete() operation."""
    def __init__(self, char, position, kind):
        self.char = char
        self.position = position
        self.kind = kind

    @property
    def is_digit(self):
        """True if the deleted character was a user-entered digit (or
        the leading plus sign)."""
        return self.kind == DeletedCharKind.USER_DIGIT

    @property
    def is_formatting(self):
        """True if the deleted character was a formatting character
        (separator), whether entered by the user or auto-inserted."""
        return self.kind != DeletedCharKind.USER_DIGIT

    @property
    def is_user_entered(self):
        """True if the deleted character was part of the raw user input
        (as opposed to an automatically-inserted separator)."""
        return self.kind != DeletedCharKind.AUTO_FORMATTING

    def __eq__(self, other):
        if not isinstance(other, DeletedChar):
            return NotImplemented
        return (self.char == other.char and
                self.position == other.position and
                self.kind == other.kind)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):
        return unicod("DeletedChar(char=%r, position=%r, kind=%r)") % (
            self.char, self.position, self.kind)


class EditResult(object):
    """The state of an AsYouTypeEditor after a single edit operation."""
    def __init__(self, display, raw_input, state, region_code, deleted=None):
        # The formatted display text after the edit.
        self.display = display
        # The raw user input after the edit; never contains separators
        # that were automatically inserted by the formatter.
        self.raw_input = raw_input
        # The InputState of the editor after the edit.
        self.state = state
        # The region the editor is currently using.
        self.region_code = region_code
        # For delete() operations, a DeletedChar describing what was
        # removed; None for other operations.
        self.deleted = deleted

    def __eq__(self, other):
        if not isinstance(other, EditResult):
            return NotImplemented
        return (self.display == other.display and
                self.raw_input == other.raw_input and
                self.state == other.state and
                self.region_code == other.region_code and
                self.deleted == other.deleted)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):
        return unicod("EditResult(display=%r, raw_input=%r, state=%r, region_code=%r, deleted=%r)") % (
            self.display, self.raw_input, self.state, self.region_code, self.deleted)


class _IddPatternTooComplexError(Exception):
    """Raised when an international-prefix pattern is too complex to
    expand into literal strings."""
    pass


def _parse_idd_atom(pattern, ii):
    """Parses one atom of an IDD pattern at position ii, returning a
    (strings, next_ii) pair where strings is the list of literal strings
    the atom can match."""
    char = pattern[ii]
    if char == unicod("("):
        ii += 1
        if pattern[ii:ii + 2] == unicod("?:"):
            ii += 2
        alternatives, ii = _parse_idd_alternatives(pattern, ii)
        if ii >= len(pattern) or pattern[ii] != unicod(")"):
            raise _IddPatternTooComplexError(unicod("unbalanced group"))
        ii += 1
        strings = []
        for atoms in alternatives:
            parts = [U_EMPTY_STRING]
            for atom in atoms:
                parts = [prefix + piece for prefix in parts for piece in atom]
                if len(parts) > _MAX_IDD_EXPANSIONS:
                    raise _IddPatternTooComplexError(unicod("too many expansions"))
            strings.extend(parts)
        return strings, ii
    if char == unicod("\\"):
        nxt = pattern[ii + 1] if ii + 1 < len(pattern) else U_EMPTY_STRING
        if nxt == unicod("d"):
            return list(unicod("0123456789")), ii + 2
        if nxt.isdigit() or nxt in unicod("~+ "):
            return [nxt], ii + 2
        raise _IddPatternTooComplexError(unicod("unsupported escape"))
    if char == unicod("["):
        end = pattern.find(unicod("]"), ii)
        if end == -1:
            raise _IddPatternTooComplexError(unicod("unbalanced character class"))
        contents = pattern[ii + 1:end]
        chars = []
        kk = 0
        while kk < len(contents):
            if kk + 2 < len(contents) and contents[kk + 1] == unicod("-"):
                for code in range(ord(contents[kk]), ord(contents[kk + 2]) + 1):
                    chars.append(unicod(chr(code)))
                kk += 3
            else:
                chars.append(contents[kk])
                kk += 1
        return chars, end + 1
    if char.isdigit() or char in unicod("~+"):
        return [char], ii + 1
    raise _IddPatternTooComplexError(unicod("unsupported character %r") % char)


def _parse_idd_alternatives(pattern, ii):
    """Parses alternations of an IDD pattern, returning a (alternatives,
    next_ii) pair where each alternative is a list of atoms."""
    alternatives = [[]]
    while ii < len(pattern) and pattern[ii] != unicod(")"):
        if pattern[ii] == unicod("|"):
            alternatives.append([])
            ii += 1
            continue
        atom, ii = _parse_idd_atom(pattern, ii)
        if ii < len(pattern) and pattern[ii] == unicod("?"):
            atom = atom + [U_EMPTY_STRING]
            ii += 1
        alternatives[-1].append(atom)
    return alternatives, ii


def _expand_idd_pattern(pattern):
    """Expands an international-prefix pattern into the literal IDD
    strings it can match. Raises _IddPatternTooComplexError if the
    pattern uses unsupported constructs or expands beyond bounds."""
    alternatives, end = _parse_idd_alternatives(pattern, 0)
    if end != len(pattern):
        raise _IddPatternTooComplexError(unicod("unbalanced pattern"))
    results = []
    for atoms in alternatives:
        strings = [U_EMPTY_STRING]
        for atom in atoms:
            strings = [prefix + piece for prefix in strings for piece in atom]
            if (len(strings) > _MAX_IDD_EXPANSIONS or
                (len(strings) > 0 and len(strings[-1]) > _MAX_IDD_LENGTH)):
                raise _IddPatternTooComplexError(unicod("too many expansions"))
        results.extend(strings)
    return results


def _is_plus_sign(char):
    return fullmatch(_PLUS_CHARS_PATTERN, char) is not None


def _same_entered_char(display_char, raw_char):
    """True if display_char is the rendering of the user-entered
    raw_char. Digits may be normalized (e.g. full-width digits are
    displayed as ASCII digits), so they are compared by value."""
    if display_char == raw_char:
        return True
    display_digit = unicode_digit(display_char, -1)
    if display_digit != -1 and display_digit == unicode_digit(raw_char, -1):
        return True
    return _is_plus_sign(display_char) and _is_plus_sign(raw_char)


def _position_map(display, raw_input):
    """Maps each position in the formatted display to the index of the
    raw input character it renders, or None for separators that were
    automatically inserted by the formatter."""
    mapping = []
    raw_index = 0
    for char in display:
        if raw_index < len(raw_input) and _same_entered_char(char, raw_input[raw_index]):
            mapping.append(raw_index)
            raw_index += 1
        else:
            mapping.append(None)
    return mapping


class AsYouTypeEditor(object):
    """Formats phone numbers as they are edited, supporting
    arbitrary-position insertions and deletions as well as region
    switches, on top of AsYouTypeFormatter.

    The editor tracks the raw user input separately from the formatted
    display, so separators inserted by the formatter are never mistaken
    for user-entered digits, and deleting a formatting character can be
    told apart from deleting a digit.
    """
    def __init__(self, region_code):
        """Gets an AsYouTypeEditor for the specified region.

        Arguments:
        region_code -- The region where the phone number is being entered
        """
        self._region_code = unicod(region_code).upper()
        self._raw_input = U_EMPTY_STRING
        self._reformat()

    def _reformat(self):
        """Recomputes the formatted display from the raw input by
        feeding each entered character through a fresh
        AsYouTypeFormatter."""
        formatter = AsYouTypeFormatter(self._region_code)
        output = U_EMPTY_STRING
        for char in self._raw_input:
            output = formatter.input_digit(char)
        self._display = output
        self._position_map = _position_map(self._display, self._raw_input)

    def _raw_index_for_display_position(self, position):
        """The index in the raw input at which text inserted at the
        given display position would end up. Automatically-inserted
        separators do not count towards the position."""
        raw_index = 0
        for mapped in self._position_map[:position]:
            if mapped is not None:
                raw_index += 1
        return raw_index

    def _make_result(self, deleted=None):
        return EditResult(display=self._display,
                          raw_input=self._raw_input,
                          state=self.state,
                          region_code=self._region_code,
                          deleted=deleted)

    def insert(self, position, text):
        """Inserts text at the given position in the displayed text and
        returns an EditResult describing the new state.

        Positions refer to the formatted display; separators that were
        automatically inserted by the formatter are skipped when mapping
        the position back to the raw input, so they never count as user
        digits.

        Arguments:
        position -- Position in the current display at which to insert,
              between 0 and len(display) inclusive.
        text -- The text to insert; typically digits, but any characters
              are accepted and preserved in the raw input.
        """
        if not 0 <= position <= len(self._display):
            raise IndexError(unicod("insert position %r out of range") % position)
        text = unicod(text)
        if len(text) > 0:
            raw_index = self._raw_index_for_display_position(position)
            self._raw_input = self._raw_input[:raw_index] + text + self._raw_input[raw_index:]
            self._reformat()
        return self._make_result()

    def delete(self, position):
        """Deletes the character at the given position in the displayed
        text and returns an EditResult describing the new state.

        The returned EditResult carries a DeletedChar which tells
        deleting a user-entered digit (DeletedCharKind.USER_DIGIT) apart
        from deleting a formatting character, whether the formatting
        character was typed by the user (USER_FORMATTING) or inserted
        automatically by the formatter (AUTO_FORMATTING). Deleting an
        automatically-inserted separator leaves the raw input unchanged.

        Arguments:
        position -- Position in the current display of the character to
              delete, between 0 and len(display) - 1 inclusive.
        """
        if not 0 <= position < len(self._display):
            raise IndexError(unicod("delete position %r out of range") % position)
        mapped = self._position_map[position]
        if mapped is None:
            deleted = DeletedChar(char=self._display[position],
                                  position=position,
                                  kind=DeletedCharKind.AUTO_FORMATTING)
        else:
            raw_char = self._raw_input[mapped]
            if raw_char.isdigit() or _is_plus_sign(raw_char):
                kind = DeletedCharKind.USER_DIGIT
            else:
                kind = DeletedCharKind.USER_FORMATTING
            deleted = DeletedChar(char=raw_char, position=position, kind=kind)
            self._raw_input = self._raw_input[:mapped] + self._raw_input[mapped + 1:]
        self._reformat()
        return self._make_result(deleted)

    def set_region(self, region_code):
        """Switches the region used for formatting and parsing, keeping
        the accrued raw input, and returns an EditResult describing the
        new state."""
        self._region_code = unicod(region_code).upper()
        self._reformat()
        return self._make_result()

    def clear(self):
        """Clears the accrued input; the region is kept."""
        self._raw_input = U_EMPTY_STRING
        self._reformat()

    def replay(self, events):
        """Applies a log of edit events in order, returning the
        EditResult of each step.

        Each event is either a tuple ("insert", position, text),
        ("delete", position) or ("set_region", region_code), or a dict
        with an "op" (or "type"/"kind") key of "insert", "delete" or
        "set_region" plus the corresponding "position", "text" or
        "region" keys.
        """
        results = []
        for event in events:
            results.append(self._apply_event(event))
        return results

    def _apply_event(self, event):
        if isinstance(event, dict):
            op = event.get(unicod("op")) or event.get(unicod("type")) or event.get(unicod("kind"))
            if op == unicod("insert"):
                return self.insert(event[unicod("position")], event[unicod("text")])
            elif op == unicod("delete"):
                return self.delete(event[unicod("position")])
            elif op in (unicod("set_region"), unicod("region")):
                return self.set_region(event[unicod("region")])
        elif isinstance(event, (tuple, list)) and len(event) >= 1:
            op = event[0]
            if op == unicod("insert"):
                return self.insert(event[1], event[2])
            elif op == unicod("delete"):
                return self.delete(event[1])
            elif op in (unicod("set_region"), unicod("region")):
                return self.set_region(event[1])
        raise ValueError(unicod("unsupported edit event: %r") % (event,))

    @property
    def region_code(self):
        """The region currently used for formatting and parsing."""
        return self._region_code

    @property
    def raw_input(self):
        """The characters entered by the user so far. Separators that
        were automatically inserted by the formatter are not included."""
        return self._raw_input

    @property
    def display(self):
        """The current formatted display text."""
        return self._display

    @property
    def user_digits(self):
        """The digits the user has entered, normalized to ASCII. The
        leading plus sign and any formatting characters (whether typed
        by the user or auto-inserted) are not included."""
        return normalize_digits_only(self._raw_input)

    def parse(self):
        """Parses the accrued raw input, returning a PhoneNumber, or
        None if the raw input cannot (yet) be parsed as a phone number.

        Whenever this returns a PhoneNumber, it is identical to what a
        one-shot phonenumbers.parse() of the raw input with the current
        region would produce."""
        if len(self._raw_input) == 0:
            return None
        try:
            return parse(self._raw_input, self._region_code)
        except NumberParseException:
            return None

    @property
    def state(self):
        """The InputState of the editor. Incomplete international
        prefixes, partial numbers and unfinished extension fragments all
        report InputState.INCOMPLETE and are never reported as complete
        numbers prematurely."""
        if len(self._raw_input) == 0:
            return InputState.EMPTY
        if self._has_extension_fragment():
            return InputState.INCOMPLETE
        numobj = self.parse()
        if numobj is not None and is_possible_number(numobj):
            return InputState.COMPLETE
        return InputState.INCOMPLETE

    @property
    def is_complete(self):
        """True if the accrued input is a complete, possible phone
        number that could be stored."""
        return self.state == InputState.COMPLETE

    def _diallable_input(self):
        return normalize_diallable_chars_only(self._raw_input)

    def _idd_strings(self):
        """The literal international dialing prefix strings for the
        current region, or an empty tuple if they cannot be determined."""
        metadata = PhoneMetadata.metadata_for_region(self._region_code, None)
        if metadata is None or metadata.international_prefix is None:
            return ()
        try:
            return tuple(_expand_idd_pattern(metadata.international_prefix))
        except _IddPatternTooComplexError:
            return ()

    @property
    def has_incomplete_international_prefix(self):
        """True if the input so far is a partially entered international
        prefix: a plus sign with no (complete) country calling code yet,
        or a prefix of the region's international dialing prefix (e.g.
        "0" or "011" for the US, "00" for most of Europe)."""
        diallable = self._diallable_input()
        if len(diallable) == 0:
            return False
        if diallable.startswith(_PLUS_SIGN):
            rest = diallable[1:]
            if len(rest) == 0:
                return True
            country_code, _remaining = _extract_country_code(rest)
            return country_code == 0
        metadata = PhoneMetadata.metadata_for_region(self._region_code, None)
        if metadata is not None and metadata.international_prefix is not None:
            idd_pattern = re_compile(metadata.international_prefix)
            if fullmatch(idd_pattern, diallable):
                return True
        for idd in self._idd_strings():
            if idd.startswith(diallable):
                return True
        return False

    def _national_significant_input(self):
        """The diallable input with any plus sign, international prefix
        and country calling code removed."""
        diallable = self._diallable_input()
        if diallable.startswith(_PLUS_SIGN):
            rest = diallable[1:]
            country_code, remaining = _extract_country_code(rest)
            return remaining if country_code != 0 else rest
        for idd in self._idd_strings():
            if len(diallable) > len(idd) and diallable.startswith(idd):
                rest = diallable[len(idd):]
                country_code, remaining = _extract_country_code(rest)
                return remaining if country_code != 0 else rest
        return diallable

    @property
    def has_leading_zero(self):
        """True if the input carries a significant leading zero (as used
        e.g. by Italian numbers). The zero is preserved in the raw input
        and in the parsed PhoneNumber's italian_leading_zero."""
        numobj = self.parse()
        if numobj is not None:
            return bool(numobj.italian_leading_zero)
        national = self._national_significant_input()
        if not national.startswith(unicod("0")):
            return False
        # A leading zero that is just the region's national (trunk)
        # prefix is not a significant leading zero.
        metadata = PhoneMetadata.metadata_for_region(self._region_code, None)
        if metadata is not None and metadata.national_prefix_for_parsing is not None:
            npp_match = re_compile(metadata.national_prefix_for_parsing).match(national)
            if npp_match and npp_match.end() > 0:
                return False
        return True

    def _has_extension_fragment(self):
        """True if the input ends with an extension label that has no
        extension digits after it yet."""
        return _EXTN_FRAGMENT_PATTERN.search(self._raw_input) is not None

    @property
    def has_extension(self):
        """True if the input contains an extension, either a complete
        one captured by parsing or a fragment that is still being
        entered."""
        numobj = self.parse()
        if numobj is not None and numobj.extension is not None:
            return True
        return self._has_extension_fragment()

    def __repr__(self):
        return unicod("AsYouTypeEditor(region_code=%r, raw_input=%r)") % (
            self._region_code, self._raw_input)


def replay_edit_log(region_code, events):
    """Replays a captured edit log on a fresh AsYouTypeEditor.

    Arguments:
    region_code -- The region the number field started in.
    events -- The edit events, as accepted by AsYouTypeEditor.replay().

    Returns an (editor, results) pair with the editor in its final state
    and the EditResult of every replayed step.
    """
    editor = AsYouTypeEditor(region_code)
    results = editor.replay(events)
    return editor, results
