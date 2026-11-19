"""Deterministic automata derived from libphonenumber metadata regular expressions.

The metadata describes valid national significant numbers using a restricted
regular expression dialect: non-capturing groups, alternation, (non-negated)
character classes, ``\\d`` and bounded quantifiers.  This module compiles such
an expression into a Thompson NFA and then a DFA over the characters that
appear in the expression, so that a whole *range* of fixed-length digit
strings can be classified without enumerating the individual numbers.

WARNING: this is internal machinery for the range-based directory support in
this package; it is not part of the stable public API.
"""
from .util import u


class RegexSyntaxError(ValueError):
    """The regular expression uses syntax that the range automaton does not support."""

    def __init__(self, reason, expression, pos):
        msg = u("unsupported regex syntax at position %d of %r: %s") % (pos, expression, reason)
        super(RegexSyntaxError, self).__init__(msg)
        self.expression = expression
        self.pos = pos


# Parsed expression node types.  Each node is a tuple whose first element is a
# short tag, keeping the construction independent of class overhead.
_NODE_CHAR = "char"      # (_NODE_CHAR, frozenset of characters)
_NODE_CONCAT = "concat"  # (_NODE_CONCAT, [nodes])
_NODE_ALT = "alt"        # (_NODE_ALT, [nodes])
_NODE_REPEAT = "repeat"  # (_NODE_REPEAT, node, min_count, max_count or None)

_DIGITS = frozenset("0123456789")


class _Parser(object):
    """Recursive descent parser for the restricted metadata regex dialect."""

    def __init__(self, expression):
        self.expression = expression
        self.pos = 0

    def error(self, reason):
        raise RegexSyntaxError(reason, self.expression, self.pos)

    def peek(self):
        if self.pos >= len(self.expression):
            return None
        return self.expression[self.pos]

    def take(self):
        ch = self.expression[self.pos]
        self.pos += 1
        return ch

    def expect(self, ch):
        if self.peek() != ch:
            self.error(u("expected %r") % ch)
        self.pos += 1

    def parse(self):
        node = self.parse_alternation()
        if self.pos != len(self.expression):
            self.error(u("unexpected %r") % self.peek())
        return node

    def parse_alternation(self):
        options = [self.parse_concatenation()]
        while self.peek() == "|":
            self.take()
            options.append(self.parse_concatenation())
        if len(options) == 1:
            return options[0]
        return (_NODE_ALT, options)

    def parse_concatenation(self):
        parts = []
        while True:
            ch = self.peek()
            if ch is None or ch == "|" or ch == ")":
                break
            parts.append(self.parse_repeat())
        if len(parts) == 1:
            return parts[0]
        return (_NODE_CONCAT, parts)

    def parse_repeat(self):
        node = self.parse_atom()
        ch = self.peek()
        if ch == "?":
            self.take()
            return (_NODE_REPEAT, node, 0, 1)
        if ch == "*":
            self.take()
            return (_NODE_REPEAT, node, 0, None)
        if ch == "+":
            self.take()
            return (_NODE_REPEAT, node, 1, None)
        if ch == "{":
            min_count, max_count = self.parse_brace()
            return (_NODE_REPEAT, node, min_count, max_count)
        return node

    def parse_brace(self):
        self.expect("{")
        start = self.pos
        digits = self.read_digits()
        if not digits:
            self.error("quantifier is missing a lower bound")
        min_count = int(digits)
        max_count = min_count
        if self.peek() == ",":
            self.take()
            tail = self.read_digits()
            if tail:
                max_count = int(tail)
            else:
                max_count = None
        self.expect("}")
        if max_count is not None and max_count < min_count:
            self.error(u("quantifier upper bound %d is below lower bound %d") % (max_count, min_count))
        return min_count, max_count

    def read_digits(self):
        start = self.pos
        while self.peek() is not None and self.peek().isdigit():
            self.take()
        return self.expression[start:self.pos]

    def parse_atom(self):
        ch = self.peek()
        if ch is None:
            self.error("expression ended unexpectedly")
        if ch == "(":
            return self.parse_group()
        if ch == "[":
            return self.parse_char_class()
        if ch == "\\":
            return self.parse_escape()
        if ch in ")|?*+{":
            self.error(u("unexpected %r") % ch)
        if ch in "^$":
            self.error("anchors are not supported")
        self.take()
        return (_NODE_CHAR, frozenset((ch,)))

    def parse_group(self):
        self.expect("(")
        if self.peek() != "?":
            self.error("only non-capturing groups (?:...) are supported")
        self.take()
        if self.peek() != ":":
            self.error("only non-capturing groups (?:...) are supported")
        self.take()
        node = self.parse_alternation()
        self.expect(")")
        return node

    def parse_escape(self):
        self.expect("\\")
        ch = self.peek()
        if ch is None:
            self.error("dangling escape")
        self.take()
        if ch == "d":
            return (_NODE_CHAR, frozenset(_DIGITS))
        if ch in "DwWsSbB":
            self.error(u("escape \\%s is not used by phone metadata and is not supported") % ch)
        # An escaped punctuation character or digit is a literal.
        return (_NODE_CHAR, frozenset((ch,)))

    def parse_char_class(self):
        self.expect("[")
        if self.peek() == "^":
            self.error("negated character classes are not supported")
        chars = set()
        while True:
            ch = self.peek()
            if ch is None:
                self.error("unterminated character class")
            if ch == "]":
                self.take()
                break
            if ch == "\\":
                node = self.parse_escape()
                chars.update(node[1])
                continue
            self.take()
            nxt = self.peek()
            if (nxt == "-" and ch != "-" and
                    self.pos + 1 < len(self.expression) and
                    self.expression[self.pos + 1] != "]"):
                self.take()  # the '-'
                end = self.peek()
                if end is None or end == "]":
                    self.error("unterminated range in character class")
                if end == "\\":
                    end_node = self.parse_escape()
                    if len(end_node[1]) != 1:
                        self.error("range end must be a single character")
                    end = next(iter(end_node[1]))
                else:
                    self.take()
                if ord(end) < ord(ch):
                    self.error("character class range is reversed")
                for code in range(ord(ch), ord(end) + 1):
                    chars.add(chr(code))
            else:
                chars.add(ch)
        if not chars:
            self.error("empty character class")
        return (_NODE_CHAR, frozenset(chars))


class _NFA(object):
    """Thompson NFA: states carry epsilon or character-set edges."""

    def __init__(self):
        self.edges = []  # state -> list of (predicate frozenset or None, target state)

    def new_state(self):
        self.edges.append([])
        return len(self.edges) - 1

    def add_edge(self, source, target, predicate=None):
        self.edges[source].append((predicate, target))

    def epsilon_closure(self, states):
        closure = set(states)
        stack = list(states)
        while stack:
            state = stack.pop()
            for predicate, target in self.edges[state]:
                if predicate is None and target not in closure:
                    closure.add(target)
                    stack.append(target)
        return frozenset(closure)

    def build(self, node):
        start = self.new_state()
        accept = self.new_state()
        self._build(node, start, accept)
        return start, accept

    def _build(self, node, start, accept):
        tag = node[0]
        if tag == _NODE_CHAR:
            self.add_edge(start, accept, node[1])
            return
        if tag == _NODE_CONCAT:
            parts = node[1]
            current = start
            for index, part in enumerate(parts):
                if index == len(parts) - 1:
                    part_accept = accept
                else:
                    part_accept = self.new_state()
                part_start = self.new_state()
                self.add_edge(current, part_start)
                self._build(part, part_start, part_accept)
                current = part_accept
            return
        if tag == _NODE_ALT:
            for option in node[1]:
                option_start = self.new_state()
                option_accept = self.new_state()
                self.add_edge(start, option_start)
                self._build(option, option_start, option_accept)
                self.add_edge(option_accept, accept)
            return
        if tag == _NODE_REPEAT:
            _, inner, min_count, max_count = node
            current = start
            for _ in range(min_count):
                inner_start = self.new_state()
                inner_accept = self.new_state()
                self.add_edge(current, inner_start)
                self._build(inner, inner_start, inner_accept)
                current = inner_accept
            if max_count is None:
                loop_start = self.new_state()
                loop_accept = self.new_state()
                self.add_edge(current, loop_start)
                self.add_edge(loop_start, accept)  # zero extra copies
                inner_start = self.new_state()
                inner_accept = self.new_state()
                self.add_edge(loop_start, inner_start)
                self._build(inner, inner_start, inner_accept)
                self.add_edge(inner_accept, loop_accept)
                self.add_edge(loop_accept, loop_start)
                self.add_edge(loop_accept, accept)
            else:
                for _ in range(max_count - min_count):
                    optional_start = self.new_state()
                    optional_accept = self.new_state()
                    self.add_edge(current, optional_start)
                    self.add_edge(optional_start, accept)  # skip the optional copy
                    inner_start = self.new_state()
                    inner_accept = self.new_state()
                    self.add_edge(optional_start, inner_start)
                    self._build(inner, inner_start, inner_accept)
                    self.add_edge(inner_accept, optional_accept)
                    self.add_edge(optional_accept, accept)
                    current = optional_accept
                # The chain of mandatory copies (and any optional copies) ends here.
                self.add_edge(current, accept)
            return
        raise RegexSyntaxError(u("unknown parse node %r") % (tag,), u(""), 0)


class DigitRangeDFA(object):
    """DFA that classifies fixed-length digit strings from a metadata pattern.

    The automaton is built lazily over the decimal alphabet; every state has
    an implicit transition to a dead state for characters without an edge.
    """

    def __init__(self, pattern):
        parser = _Parser(pattern)
        nfa = _NFA()
        nfa_start, nfa_accept = nfa.build(parser.parse())
        self._nfa_accept = nfa_accept
        self._nfa_edges = nfa.edges
        start_closure = nfa.epsilon_closure((nfa_start,))
        self._alphabet = tuple(sorted(self._collect_alphabet()))
        self._state_ids = {}
        self._transitions = []
        self._accepting = []
        self._suffix_cache = {}
        self._start = self._intern(start_closure)

    def _collect_alphabet(self):
        alphabet = set()
        for edges in self._nfa_edges:
            for predicate, _target in edges:
                if predicate is not None:
                    alphabet.update(predicate)
        return alphabet

    def _intern(self, subset):
        state_id = self._state_ids.get(subset)
        if state_id is not None:
            return state_id
        state_id = len(self._state_ids)
        self._state_ids[subset] = state_id
        self._transitions.append({})
        self._accepting.append(self._nfa_accept in subset)
        for ch in self._alphabet:
            targets = set()
            for nfa_state in subset:
                for predicate, target in self._nfa_edges[nfa_state]:
                    if predicate is not None and ch in predicate:
                        targets.add(target)
            if targets:
                next_subset = self._epsilon_closure(targets)
                self._transitions[state_id][ch] = self._intern(next_subset)
        return state_id

    def _epsilon_closure(self, states):
        closure = set(states)
        stack = list(states)
        while stack:
            state = stack.pop()
            for predicate, target in self._nfa_edges[state]:
                if predicate is None and target not in closure:
                    closure.add(target)
                    stack.append(target)
        return frozenset(closure)

    @property
    def start_state(self):
        return self._start

    def is_accepting(self, state_id):
        return self._accepting[state_id]

    def transition(self, state_id, digit):
        return self._transitions[state_id].get(digit)

    def accepts(self, digits):
        """Return whether the whole digit string is accepted (full match)."""
        state = self._start
        for digit in digits:
            state = self.transition(state, digit)
            if state is None:
                return False
        return self.is_accepting(state)

    def state_for_prefix(self, digits):
        """Return the DFA state after consuming the prefix, or None if dead."""
        state = self._start
        for digit in digits:
            state = self.transition(state, digit)
            if state is None:
                return None
        return state

    def _suffix_tables(self, max_free_length):
        """Precompute all/any acceptance for suffixes of length 0..max."""
        cached = self._suffix_cache.get(max_free_length)
        if cached is not None:
            return cached
        state_count = len(self._state_ids)
        all_accept = [[False] * state_count for _ in range(max_free_length + 1)]
        any_accept = [[False] * state_count for _ in range(max_free_length + 1)]
        for state in range(state_count):
            all_accept[0][state] = self._accepting[state]
            any_accept[0][state] = self._accepting[state]
        for length in range(1, max_free_length + 1):
            for state in range(state_count):
                destinations = [self._transitions[state].get(digit) for digit in _DIGITS]
                alive = [dest for dest in destinations if dest is not None]
                all_accept[length][state] = (len(alive) == len(destinations) and
                                              all(all_accept[length - 1][dest] for dest in alive))
                any_accept[length][state] = any(any_accept[length - 1][dest] for dest in alive)
        self._suffix_cache[max_free_length] = (all_accept, any_accept)
        return all_accept, any_accept

    def classify_interval(self, length, lower, upper):
        """Split a fixed-length integer interval into accepted and rejected pieces.

        Arguments:
        length -- the number of digits in every represented string
        lower -- smallest integer in the interval (inclusive)
        upper -- largest integer in the interval (inclusive)

        Returns a tuple (accepted, rejected) of disjoint, sorted, maximal
        ``(start, end)`` integer intervals whose union is ``[lower, upper]``.
        """
        if not (0 <= lower <= upper < 10 ** length):
            raise ValueError(u("interval [%d, %d] does not hold %d-digit numbers") % (lower, upper, length))
        all_accept, any_accept = self._suffix_tables(length)
        accepted = []
        rejected = []
        for block_start, free_length in _interval_blocks(lower, upper, length):
            prefix = str(block_start).zfill(length)[:length - free_length]
            state = self.state_for_prefix(prefix)
            if state is None:
                rejected.append((block_start, block_start + 10 ** free_length - 1))
                continue
            self._classify_block(state, free_length, block_start,
                                 all_accept, any_accept, accepted, rejected)
        return _merge_pieces(accepted), _merge_pieces(rejected)

    def _classify_block(self, state, free_length, base,
                        all_accept, any_accept, accepted, rejected):
        if all_accept[free_length][state]:
            accepted.append((base, base + 10 ** free_length - 1))
            return
        if not any_accept[free_length][state]:
            rejected.append((base, base + 10 ** free_length - 1))
            return
        if free_length == 0:
            # all/any coincide for the empty suffix; defence in depth.
            rejected.append((base, base))
            return
        step = 10 ** (free_length - 1)
        for digit in "0123456789":
            next_state = self._transitions[state].get(digit)
            child_base = base + (ord(digit) - ord("0")) * step
            if next_state is None:
                rejected.append((child_base, child_base + step - 1))
            else:
                self._classify_block(next_state, free_length - 1, child_base,
                                     all_accept, any_accept, accepted, rejected)


def _interval_blocks(lower, upper, length):
    """Decompose [lower, upper] into aligned decimal subtrees.

    Yields ``(first_integer, free_digit_count)`` pairs: the block starting at
    ``first_integer`` contains every number sharing the same fixed prefix,
    i.e. the interval ``[first_integer, first_integer + 10**free - 1]``.
    """
    blocks = []
    cursor = lower
    limit = 10 ** length
    while cursor <= upper:
        free = 0
        size = 1
        while (free < length and
               cursor % (size * 10) == 0 and
               cursor + size * 10 <= limit and
               cursor + size * 10 - 1 <= upper):
            size *= 10
            free += 1
        blocks.append((cursor, free))
        cursor += size
    return blocks


def _merge_pieces(pieces):
    if not pieces:
        return []
    pieces = sorted(pieces)
    merged = []
    current_start, current_end = pieces[0]
    for start, end in pieces[1:]:
        if start <= current_end + 1:
            if end > current_end:
                current_end = end
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged
