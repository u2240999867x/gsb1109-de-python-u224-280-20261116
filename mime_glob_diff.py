#!/usr/bin/env python3
"""Find shortest filename witnesses for globs2 classification changes.

The comparison uses the same filename-matching rules as xdg.Mime.GlobDB:
literals are checked as whole names, ``*.ext`` patterns as a final suffix,
and all other wildcard patterns with fnmatch's full-name matching.  A
classification is the set of MIME types at the highest matching weight, so
equal-weight types remain tied instead of being discarded by first_match().

Shortest names are found by exploring the product of the pattern automata,
not by generating every filename allowed by the scope.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


class UnsupportedPattern(ValueError):
    pass


def normalise_mime(name: str) -> str:
    if "/" not in name:
        raise ValueError("MIME type must contain '/': %r" % (name,))
    media, subtype = name.split("/", 1)
    return media.lower() + "/" + subtype.lower()


def parse_globs2(paths: Sequence[str]) -> Dict[str, Set[Tuple[int, str, Tuple[str, ...]]]]:
    allglobs: Dict[str, Set[Tuple[int, str, Tuple[str, ...]]]] = defaultdict(set)
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                fields = line[:-1].split(":")
                if len(fields) < 3:
                    raise ValueError("%s: malformed globs2 line: %r" % (path, line))
                weight = int(fields[0])
                mime = normalise_mime(fields[1])
                pattern = fields[2]
                flags = tuple(fields[3].split(",")) if len(fields) > 3 else ()
                if pattern == "__NOGLOBS__":
                    allglobs.pop(mime, None)
                    continue
                allglobs[mime].add((weight, pattern, flags))
    return allglobs


def parse_aliases(paths: Sequence[str]) -> Dict[str, str]:
    """Mirror xdg.Mime alias loading: the key is used verbatim, target lookup lowercases."""
    aliases: Dict[str, str] = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    raise ValueError("%s: malformed alias line: %r" % (path, line))
                aliases[parts[0]] = normalise_mime(parts[1])
    return aliases


def canonical_mime(mime: str, aliases: Dict[str, str]) -> str:
    return aliases.get(mime, mime)


def has_wildcard(text: str) -> bool:
    return "*" in text or "[" in text or "?" in text


@dataclass(frozen=True)
class Rule:
    weight: int
    mime: str
    pattern: str
    cased: bool
    kind: str
    source: str


def rules_from_globs2(
    paths: Sequence[str], aliases: Dict[str, str], label: str
) -> List[Rule]:
    """Extract wildcard rules exactly as GlobDB.finalise would keep them."""
    rules: List[Rule] = []
    for mime, entries in parse_globs2(paths).items():
        canonical = canonical_mime(mime, aliases)
        for weight, pattern, flags in entries:
            cased = "cs" in flags
            if not has_wildcard(pattern):
                continue
            if pattern.startswith("*.") and not has_wildcard(pattern[2:]):
                continue
            rules.append(Rule(weight, canonical, pattern, cased, "glob", label))
    rules.sort(key=lambda r: (r.pattern, r.mime, r.weight, r.cased, r.kind))
    return rules


def rules_from_database(
    db, paths: Sequence[str], aliases: Dict[str, str], label: str
) -> List[Rule]:
    """Read literal and extension rules from a finalized xdg GlobDB.

    Reading the finalized dicts avoids re-implementing GlobDB's last-wins
    handling for duplicate literal keys.  Wildcards are taken from the raw
    globs2 text because GlobDB keeps compiled regexes there.
    """
    rules: List[Rule] = []
    for key, (mime, weight) in db.cased_literals.items():
        rules.append(Rule(weight, str(mime), key, True, "literal", label))
    for key, (mime, weight) in db.literals.items():
        rules.append(Rule(weight, str(mime), key, False, "literal", label))
    for key, entries in db.cased_exts.items():
        for mime, weight in entries:
            rules.append(Rule(weight, str(mime), "*." + key, True, "extension", label))
    for key, entries in db.exts.items():
        for mime, weight in entries:
            rules.append(Rule(weight, str(mime), "*." + key, False, "extension", label))
    rules.extend(rules_from_globs2(paths, aliases, label))
    rules.sort(key=lambda r: (r.kind, r.pattern, r.mime, r.weight, r.cased))
    return rules


class Matcher:
    """Match one filename character.

    ``exact`` mirrors a case-sensitive dict lookup; ``lower`` mirrors
    ``name.lower() == pattern`` for literal and extension rules; ``regex``
    mirrors fnmatch's per-token regex, which pyxdg compiles with re.I.
    """

    def __init__(self, token: str, cased: bool, mode: str):
        self.token = token
        self.cased = cased
        self.mode = mode
        self.regex = None
        if mode == "exact":
            self.char = token
        elif mode == "lower":
            if len(token.lower()) != 1:
                raise UnsupportedPattern("case mapping of %r is not one character" % (token,))
            self.char = token.lower()
        elif mode == "regex":
            flags = 0 if cased else re.IGNORECASE
            self.regex = re.compile(fnmatch.translate(token), flags)
        else:
            raise AssertionError(mode)

    def matches(self, ch: str) -> bool:
        if self.mode == "exact":
            return ch == self.char
        if self.mode == "lower":
            folded = ch.lower()
            return len(folded) == 1 and folded == self.char
        return self.regex.match(ch) is not None


def literal_matcher(ch: str, cased: bool) -> Matcher:
    return Matcher(ch, cased, "exact" if cased else "lower")


def any_matcher() -> Matcher:
    return Matcher("?", True, "regex")


def _parse_glob_tokens(pattern: str, cased: bool) -> List[Tuple[str, Optional[Matcher]]]:
    tokens: List[Tuple[str, Optional[Matcher]]] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            while i < n and pattern[i] == "*":
                i += 1
            tokens.append(("star", None))
        elif ch == "?":
            tokens.append(("one", Matcher("?", cased, "regex")))
            i += 1
        elif ch == "[":
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                tokens.append(("literal", literal_matcher("[", cased)))
                i += 1
                continue
            token = pattern[i:j + 1]
            tokens.append(("one", Matcher(token, cased, "regex")))
            i = j + 1
        else:
            tokens.append(("literal", literal_matcher(ch, cased)))
            i += 1
    return tokens


class NFA:
    def __init__(self):
        self.epsilon: List[List[int]] = []
        self.transitions: List[List[Tuple[Matcher, int]]] = []
        self.start = self._new()
        self.accept = self._new()
        self._closure_cache: Dict[FrozenSet[int], FrozenSet[int]] = {}

    def _new(self) -> int:
        self.epsilon.append([])
        self.transitions.append([])
        return len(self.epsilon) - 1

    def add_epsilon(self, src: int, dst: int) -> None:
        self.epsilon[src].append(dst)

    def add_transition(self, src: int, matcher: Matcher, dst: int) -> None:
        self.transitions[src].append((matcher, dst))

    def closure(self, states: Iterable[int]) -> FrozenSet[int]:
        key = frozenset(states)
        cached = self._closure_cache.get(key)
        if cached is not None:
            return cached
        result = set(key)
        stack = list(key)
        while stack:
            state = stack.pop()
            for nxt in self.epsilon[state]:
                if nxt not in result:
                    result.add(nxt)
                    stack.append(nxt)
        frozen = frozenset(result)
        self._closure_cache[key] = frozen
        return frozen

    def initial(self) -> FrozenSet[int]:
        return self.closure((self.start,))

    def advance(self, states: FrozenSet[int], ch: str) -> FrozenSet[int]:
        dest: Set[int] = set()
        for state in states:
            for matcher, nxt in self.transitions[state]:
                if matcher.matches(ch):
                    dest.add(nxt)
        return self.closure(dest) if dest else frozenset()


def build_nfa(tokens: Sequence[Tuple[str, Optional[Matcher]]]) -> NFA:
    nfa = NFA()
    current = nfa.start
    for kind, matcher in tokens:
        if kind == "literal":
            nxt = nfa._new()
            assert matcher is not None
            nfa.add_transition(current, matcher, nxt)
            current = nxt
        elif kind == "one":
            nxt = nfa._new()
            assert matcher is not None
            nfa.add_transition(current, matcher, nxt)
            current = nxt
        elif kind == "star":
            nxt = nfa._new()
            nfa.add_epsilon(current, nxt)
            nfa.add_transition(current, any_matcher(), current)
            current = nxt
        else:
            raise AssertionError(kind)
    nfa.add_epsilon(current, nfa.accept)
    return nfa


def nfa_for_rule(rule: Rule) -> NFA:
    if rule.kind == "literal":
        tokens = [("literal", literal_matcher(ch, rule.cased)) for ch in rule.pattern]
        return build_nfa(tokens)
    if rule.kind == "extension":
        suffix = "." + rule.pattern[2:]
        tokens: List[Tuple[str, Optional[Matcher]]] = [("star", None)]
        tokens += [("literal", literal_matcher(ch, rule.cased)) for ch in suffix]
        return build_nfa(tokens)
    tokens = _parse_glob_tokens(rule.pattern, rule.cased)
    return build_nfa(tokens)


@dataclass
class Analysis:
    witnesses: Dict[Tuple[FrozenSet[str], FrozenSet[str]], Tuple[int, str]]
    covered: bool
    stopped: str
    states: int
    saturation_depth: int
    truncated: bool
    unsupported: List[Tuple[str, str, str]]


def _forbidden_start(forbidden: Sequence[str]) -> Tuple[Tuple[str, int], ...]:
    return tuple((name, 0) for name in forbidden)


def _forbidden_step(
    candidates: Tuple[Tuple[str, int], ...], ch: str
) -> Tuple[Tuple[str, int], ...]:
    out = []
    for name, pos in candidates:
        if pos < len(name) and name[pos] == ch:
            out.append((name, pos + 1))
        # A candidate already at len(name) has ceased to be a prefix; extra
        # characters mean the filename is no longer equal to the reserved name.
    return tuple(out)


def _is_forbidden(candidates: Tuple[Tuple[str, int], ...]) -> bool:
    return any(pos == len(name) for name, pos in candidates)


def analyse(
    old_rules: Sequence[Rule],
    new_rules: Sequence[Rule],
    alphabet: Sequence[str],
    min_length: int,
    max_length: Optional[int],
    max_states: int,
    forbidden: Sequence[str] = (),
) -> Analysis:
    fold_problem = [ch for ch in alphabet if len(ch.lower()) != 1]
    unsupported: List[Tuple[str, str, str]] = []
    if fold_problem:
        unsupported.append(
            (
                "scope",
                ",".join(repr(ch) for ch in fold_problem),
                "case mapping is not a single character; uncased rules are not modeled exactly",
            )
        )
    old_auto: List[Tuple[Rule, NFA]] = []
    new_auto: List[Tuple[Rule, NFA]] = []
    for target, rules in ((old_auto, old_rules), (new_auto, new_rules)):
        for rule in rules:
            try:
                nfa = nfa_for_rule(rule)
            except UnsupportedPattern as exc:
                unsupported.append((rule.source, rule.pattern, str(exc)))
                continue
            target.append((rule, nfa))

    all_auto = old_auto + new_auto
    initial = tuple(nfa.initial() for _, nfa in all_auto)
    initial_forbidden = _forbidden_start(tuple(sorted(set(forbidden))))

    # The product automaton is finite.  BFS over (state, reserved-name prefix)
    # gives each pair a shortest legal length.  If that length is below
    # min_length, a loop can revisit the same pair at a legal length; the key
    # therefore caps length at min_length, which keeps the space finite while
    # still allowing such witnesses.  Classification depends only on the pair.
    def key_for(state, fstate, length):
        return (state, fstate, min(length, min_length))

    queue = deque([(0, initial, initial_forbidden)])
    depth: Dict[Tuple[Tuple[FrozenSet[int], ...], Tuple[Tuple[str, int], ...], int], int] = {
        key_for(initial, initial_forbidden, 0): 0
    }
    examples = {key_for(initial, initial_forbidden, 0): ""}
    states_explored = 0
    stopped = "state space exhausted"
    covered = True
    saturation_depth = 0
    deepest_truncating = None

    while queue:
        length, state, fstate = queue.popleft()
        states_explored += 1
        if states_explored > max_states:
            covered = False
            stopped = "state limit %d exceeded" % max_states
            break
        if length > saturation_depth:
            saturation_depth = length
        for ch in alphabet:
            nxt = tuple(
                nfa.advance(rule_state, ch) for (_, nfa), rule_state in zip(all_auto, state)
            )
            if not any(nxt):
                continue
            nxt_forbidden = _forbidden_step(fstate, ch)
            nxt_length = length + 1
            key = key_for(nxt, nxt_forbidden, nxt_length)
            if key in depth:
                continue
            depth[key] = nxt_length
            examples[key] = examples[(state, fstate, min(length, min_length))] + ch
            queue.append((nxt_length, nxt, nxt_forbidden))

    witnesses: Dict[Tuple[FrozenSet[str], FrozenSet[str]], Tuple[int, str]] = {}
    for (state, fstate, _), first_length in depth.items():
        if first_length < min_length or _is_forbidden(fstate):
            continue
        if max_length is not None and first_length > max_length:
            old_class, new_class = _classify(old_auto, new_auto, state, len(old_auto))
            if old_class != new_class and (
                deepest_truncating is None or first_length < deepest_truncating
            ):
                deepest_truncating = first_length
            continue
        old_class, new_class = _classify(old_auto, new_auto, state, len(old_auto))
        if old_class != new_class:
            key = (old_class, new_class)
            text = examples[(state, fstate, min(first_length, min_length))]
            if key not in witnesses or first_length < witnesses[key][0]:
                witnesses[key] = (first_length, text)

    truncated = deepest_truncating is not None
    if truncated:
        covered = False
        stopped = "declared length bound %d hides classification changes" % max_length
    if unsupported:
        covered = False
        if stopped == "state space exhausted":
            stopped = "unsupported patterns present"
    return Analysis(witnesses, covered, stopped, states_explored, saturation_depth, truncated, unsupported)


def _classify(
    old_auto: Sequence[Tuple[Rule, NFA]],
    new_auto: Sequence[Tuple[Rule, NFA]],
    state: Tuple[FrozenSet[int], ...],
    split: int,
) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    classes = []
    for autos, part in ((old_auto, state[:split]), (new_auto, state[split:])):
        best: Optional[int] = None
        types: Set[str] = set()
        for (rule, nfa), rule_state in zip(autos, part):
            if nfa.accept in rule_state:
                if best is None or rule.weight > best:
                    best = rule.weight
                    types = {rule.mime}
                elif rule.weight == best:
                    types.add(rule.mime)
        classes.append(frozenset(types))
    return classes[0], classes[1]


def _load_actual_globdb(paths: Sequence[str], aliases: Dict[str, str]):
    from xdg import Mime

    Mime._cache_uptodate = True
    Mime.aliases = dict(aliases)
    db = Mime.GlobDB()
    for path in paths:
        db.merge_file(path)
    db.finalise()
    return db


def actual_class(db, name: str) -> FrozenSet[str]:
    matches = db.all_matches(name)
    if not matches:
        return frozenset()
    best = max(weight for _, weight in matches)
    return frozenset(str(mime) for mime, weight in matches if weight == best)


def verify_witnesses(
    old_paths: Sequence[str],
    new_paths: Sequence[str],
    old_aliases: Dict[str, str],
    new_aliases: Dict[str, str],
    witnesses: Dict[Tuple[FrozenSet[str], FrozenSet[str]], Tuple[int, str]],
) -> List[dict]:
    old_db = _load_actual_globdb(old_paths, old_aliases)
    new_db = _load_actual_globdb(new_paths, new_aliases)
    rows = []
    for (expected_old, expected_new), (length, name) in sorted(
        witnesses.items(), key=lambda item: (item[1][0], item[1][1], sorted(item[0][0]), sorted(item[0][1]))
    ):
        got_old = actual_class(old_db, name)
        got_new = actual_class(new_db, name)
        rows.append(
            {
                "name": name,
                "length": length,
                "expected_old": sorted(expected_old),
                "expected_new": sorted(expected_new),
                "actual_old": sorted(got_old),
                "actual_new": sorted(got_new),
                "first_match_old": str(old_db.first_match(name)) if old_db.first_match(name) else None,
                "first_match_new": str(new_db.first_match(name)) if new_db.first_match(name) else None,
                "ok": got_old == expected_old and got_new == expected_new,
            }
        )
    return rows


def parse_charset(spec: Optional[str]) -> List[str]:
    """Parse a charset description.

    ``a-z`` is a range; a leading or trailing hyphen is literal, and a
    backslash escapes the following character.
    """
    text = DEFAULT_CHARSET if spec is None else spec
    chars: List[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            chars.append(text[i + 1])
            i += 2
        elif i + 2 < len(text) and text[i + 1] == "-" and text[i] != "-":
            lo, hi = text[i], text[i + 2]
            if ord(lo) > ord(hi):
                raise ValueError("invalid charset range %r" % (text[i:i + 3],))
            chars.extend(chr(cp) for cp in range(ord(lo), ord(hi) + 1))
            i += 3
        else:
            chars.append(text[i])
            i += 1
    return list(dict.fromkeys(chars))


def fixture_paths(tmpdir: str, mode: str = "merge") -> Tuple[List[str], List[str]]:
    import importlib.util

    resources_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test", "resources.py")
    spec = importlib.util.spec_from_file_location("_pyxdg_test_resources", resources_path)
    resources = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(resources)

    old_path = os.path.join(tmpdir, "globs2-old")
    new_path = os.path.join(tmpdir, "globs2-new")
    with open(old_path, "w", encoding="utf-8") as f:
        f.write(resources.mime_globs2_a)
    with open(new_path, "w", encoding="utf-8") as f:
        if mode == "merge":
            f.write(resources.mime_globs2_a)
        f.write(resources.mime_globs2_b)
    return [old_path], [new_path]


def fmt_class(types: FrozenSet[str]) -> str:
    return "{" + ", ".join(sorted(types)) + "}" if types else "{}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", action="append", default=[], help="old globs2 file; repeat for merge order")
    parser.add_argument("--new", action="append", default=[], help="new globs2 file; repeat for merge order")
    parser.add_argument("--old-aliases", action="append", default=[], help="old alias file")
    parser.add_argument("--new-aliases", action="append", default=[], help="new alias file")
    parser.add_argument(
        "--fixture-pair",
        choices=("merge", "standalone"),
        help="analyse the test/resources.py versions: 'merge' is A -> A+B, 'standalone' is A -> B",
    )
    parser.add_argument("--charset", help="allowed filename characters, e.g. 'A-Za-z0-9._-'; default is portable POSIX")
    parser.add_argument("--min-length", type=int, default=1)
    parser.add_argument(
        "--max-length",
        type=int,
        default=255,
        help="declared maximum filename length in characters (default: 255)",
    )
    parser.add_argument(
        "--no-length-limit",
        action="store_true",
        help="ignore --max-length and explore the full reachable state space",
    )
    parser.add_argument("--max-states", type=int, default=200000)
    parser.add_argument(
        "--forbid-name",
        action="append",
        default=[".", ".."],
        help="filename that is not a legal directory entry (default: '.' and '..')",
    )
    parser.add_argument(
        "--allow-reserved-names",
        action="store_true",
        help="treat '.' and '..' as ordinary names",
    )
    parser.add_argument("--verify", action="store_true", help="verify witnesses through xdg.Mime.GlobDB.all_matches")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args(argv)

    if args.fixture_pair:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            old_paths, new_paths = fixture_paths(tmpdir, args.fixture_pair)
            return _run(args, old_paths, new_paths)
    if not args.old or not args.new:
        parser.error("provide --old and --new, or use --fixture-pair")
    return _run(args, args.old, args.new)


def _run(args: argparse.Namespace, old_paths: Sequence[str], new_paths: Sequence[str]) -> int:
    old_aliases = parse_aliases(args.old_aliases)
    new_aliases = parse_aliases(args.new_aliases)
    old_rules = rules_from_database(
        _load_actual_globdb(old_paths, old_aliases), old_paths, old_aliases, "old"
    )
    new_rules = rules_from_database(
        _load_actual_globdb(new_paths, new_aliases), new_paths, new_aliases, "new"
    )
    alphabet = parse_charset(args.charset)
    if len(alphabet) > 512:
        raise SystemExit("charset has more than 512 symbols; split the scope so shortest proofs stay finite")
    forbidden = [] if args.allow_reserved_names else args.forbid_name
    max_length = None if args.no_length_limit else args.max_length
    result = analyse(
        old_rules,
        new_rules,
        alphabet,
        args.min_length,
        max_length,
        args.max_states,
        forbidden,
    )
    rows = []
    if args.verify:
        rows = verify_witnesses(old_paths, new_paths, old_aliases, new_aliases, result.witnesses)
    payload = {
        "covered": result.covered,
        "stopped": result.stopped,
        "states_explored": result.states,
        "saturation_depth": result.saturation_depth,
        "truncated": result.truncated,
        "unsupported": [
            {"source": source, "pattern": pattern, "reason": reason}
            for source, pattern, reason in result.unsupported
        ],
        "witnesses": [
            {
                "name": name,
                "length": length,
                "old": sorted(old_class),
                "new": sorted(new_class),
            }
            for (old_class, new_class), (length, name) in sorted(
                result.witnesses.items(),
                key=lambda item: (item[1][0], item[1][1], sorted(item[0][0]), sorted(item[0][1])),
            )
        ],
    }
    if args.verify:
        payload["verification"] = rows
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print("covered:", "yes" if result.covered else "NO")
        print("stopped:", result.stopped)
        print("states explored:", result.states)
        print("saturation depth:", result.saturation_depth)
        print("truncated:", "yes" if result.truncated else "no")
        for source, pattern, reason in result.unsupported:
            print("unsupported: %s %r (%s)" % (source, pattern, reason))
        if not result.witnesses:
            print("classification changes: none in declared scope")
        for (old_class, new_class), (length, name) in sorted(
            result.witnesses.items(),
            key=lambda item: (item[1][0], item[1][1], sorted(item[0][0]), sorted(item[0][1])),
        ):
            print("%2d  %-16r  %s -> %s" % (length, name, fmt_class(old_class), fmt_class(new_class)))
        if args.verify:
            for row in rows:
                print(
                    "verify %-16r  old=%s new=%s first=%s/%s  %s"
                    % (
                        row["name"],
                        row["actual_old"],
                        row["actual_new"],
                        row["first_match_old"],
                        row["first_match_new"],
                        "OK" if row["ok"] else "MISMATCH",
                    )
                )
    return 0 if result.covered else 2


if __name__ == "__main__":
    sys.exit(main())
