import unittest
from typing import Any, Dict, List, Optional

from mpd.filters import (
    And,
    Filter,
    FilterError,
    FilterNotSupported,
    Not,
    Or,
    TagFilter,
    compile_filter,
    evaluate_filter,
    evaluate_records,
    normalize_filter,
    parse_filter,
    select_songs,
)


SONGS: List[Dict[str, Any]] = [
    {"file": "01.mp3", "artist": "AC/DC", "album": "Powerage", "genre": ["Rock"]},
    {"file": "02.mp3", "artist": "ac/dc", "album": "Powerage", "genre": ["Rock"]},
    {
        "file": "03.mp3",
        "artist": "Various",
        "album": "Mixed",
        "genre": ["Rock", "Pop"],
        "title": 'Say "Hello"',
    },
    {"file": "04.mp3", "artist": "Other", "album": "Mixed", "genre": ["Jazz"]},
    {"file": "05.mp3", "title": 'back\\slash "quote"'},
]


class ReplayServer:
    """Small deterministic MPD stand-in implementing find/search semantics."""

    def __init__(self, songs: List[Dict[str, Any]], version: str) -> None:
        self.songs = songs
        self.mpd_version = version
        self.calls: List[str] = []

    def find(self, *args: Any) -> List[Dict[str, Any]]:
        self.calls.append("find " + repr(args))
        if self._modern(args):
            return self._expression(args[0], ignore_case=False)
        return self._legacy(args, exact=True)

    def search(self, *args: Any) -> List[Dict[str, Any]]:
        self.calls.append("search " + repr(args))
        if self._modern(args):
            return self._expression(args[0], ignore_case=True)
        return self._legacy(args, exact=False)

    def _modern(self, args: Any) -> bool:
        return len(args) == 1 and str(args[0]).lstrip().startswith("(")

    def _expression(self, expression: str, ignore_case: bool) -> List[Dict[str, Any]]:
        condition = parse_filter(expression, ignore_case=ignore_case)
        return [song for song in self.songs if condition.evaluate(song) is True]

    def listallinfo(self, *args: Any) -> List[Dict[str, Any]]:
        self.calls.append("listallinfo")
        return list(self.songs)

    def _legacy(self, args: Any, exact: bool) -> List[Dict[str, Any]]:
        pairs = list(zip(args[0::2], args[1::2]))
        return [song for song in self.songs if self._pairs_match(song, pairs, exact)]

    @staticmethod
    def _pairs_match(
        song: Dict[str, Any], pairs: List[Any], exact: bool
    ) -> bool:
        for tag, needle in pairs:
            values = ReplayServer._values(song, tag)
            if values is None:
                return False
            if exact:
                if not any(value == needle for value in values):
                    return False
            else:
                if not any(needle.lower() in value.lower() for value in values):
                    return False
        return True

    @staticmethod
    def _values(song: Dict[str, Any], tag: str) -> Optional[List[str]]:
        key = tag.lower()
        if key == "any":
            values: List[str] = []
            for name, value in song.items():
                if name in ("file", "directory", "playlist"):
                    continue
                values.extend(value if isinstance(value, list) else [str(value)])
            return values
        if key not in song:
            return None
        value = song[key]
        return value if isinstance(value, list) else [str(value)]


def files(result: Any) -> List[str]:
    return [song["file"] for song in result.confirmed]


class TestFilterSemantics(unittest.TestCase):
    def test_exact_is_case_sensitive(self) -> None:
        condition = TagFilter("artist", "AC/DC")
        self.assertEqual(files(evaluate_records(condition, SONGS)), ["01.mp3"])

    def test_contains_is_case_insensitive(self) -> None:
        condition = TagFilter("artist", "ac/", "contains")
        self.assertEqual(
            files(evaluate_records(condition, SONGS)), ["01.mp3", "02.mp3"]
        )

    def test_multi_value_positive_matches_any_value(self) -> None:
        condition = TagFilter("genre", "Pop")
        self.assertEqual(files(evaluate_records(condition, SONGS)), ["03.mp3"])

    def test_negation_applies_to_all_values(self) -> None:
        condition = Not(TagFilter("genre", "Rock"))
        # 05 has no genre at all: undetermined, not excluded.
        result = evaluate_records(condition, SONGS)
        self.assertEqual(files(result), ["04.mp3"])
        self.assertEqual([song["file"] for song in result.undetermined], ["05.mp3"])

    def test_inequality_is_negated_positive(self) -> None:
        condition = normalize_filter(TagFilter("genre", "Rock", "!="))
        self.assertEqual(
            [song["file"] for song in evaluate_records(condition, SONGS).excluded],
            ["01.mp3", "02.mp3", "03.mp3"],
        )

    def test_missing_field_is_undetermined(self) -> None:
        result = evaluate_records(TagFilter("genre", "Rock"), SONGS)
        self.assertIn("05.mp3", [song["file"] for song in result.undetermined])
        self.assertFalse(result.complete)

    def test_three_valued_logic_keeps_unknown(self) -> None:
        condition = And(TagFilter("artist", "Other"), TagFilter("genre", "Rock"))
        self.assertIs(evaluate_filter(condition, SONGS[4]), None)
        self.assertIs(evaluate_filter(condition, SONGS[3]), False)

    def test_or_is_true_when_any_branch_true(self) -> None:
        condition = Or(TagFilter("artist", "Other"), TagFilter("genre", "Rock"))
        self.assertTrue(evaluate_filter(condition, SONGS[0]))
        self.assertTrue(evaluate_filter(condition, SONGS[3]))
        self.assertIsNone(evaluate_filter(condition, SONGS[4]))


class TestFilterParsing(unittest.TestCase):
    def test_escaped_quote_stays_in_value(self) -> None:
        condition = parse_filter(r'(title == "Say \"Hello\"")')
        self.assertEqual(condition.value, 'Say "Hello"')
        self.assertEqual(condition.evaluate(SONGS[2]), True)

    def test_escaped_backslash_round_trips(self) -> None:
        condition = TagFilter("title", 'back\\slash "quote"')
        rendered = condition.to_mpd()
        self.assertEqual(parse_filter(rendered), condition)

    def test_escaped_quote_cannot_create_operator(self) -> None:
        hostile = 'x" OR artist == "y'
        condition = TagFilter("title", hostile)
        parsed = parse_filter(condition.to_mpd())
        self.assertEqual(parsed, condition)
        self.assertFalse(condition.evaluate({"title": "x", "artist": "y"}))

    def test_backslash_cannot_escape_closing_quote(self) -> None:
        with self.assertRaises(FilterError):
            parse_filter(r'(title == "x\")')

    def test_malformed_expression_raises(self) -> None:
        for text in ["", "(artist ==", 'artist == "x', "artist ~~ x"]:
            with self.assertRaises(FilterError):
                parse_filter(text)

    def test_operator_precedence(self) -> None:
        condition = parse_filter('artist=="a" OR artist=="b" AND genre=="Rock"')
        self.assertIsInstance(condition, Or)
        self.assertIsInstance(condition.children[1], And)


class TestFilterPlanning(unittest.TestCase):
    def command_texts(self, plan: Any) -> List[str]:
        return [query.as_command() for query in plan.queries]

    def test_old_server_uses_exact_find_for_exact_condition(self) -> None:
        plan = compile_filter(TagFilter("artist", "AC/DC"), "0.19.0")
        self.assertEqual(self.command_texts(plan), ["find artist AC/DC"])
        self.assertTrue(plan.server_supported)

    def test_old_server_never_uses_search_for_exact_find(self) -> None:
        plan = compile_filter(TagFilter("artist", "AC/DC"), "0.19.0")
        self.assertNotIn("search", self.command_texts(plan))

    def test_old_server_uses_search_for_substring(self) -> None:
        plan = compile_filter(TagFilter("artist", "ac", "contains"), "0.19.0")
        self.assertEqual(self.command_texts(plan), ["search artist ac"])
        self.assertTrue(plan.server_supported)

    def test_old_server_negation_needs_full_scan(self) -> None:
        plan = compile_filter(Not(TagFilter("artist", "AC/DC")), "0.19.0")
        self.assertEqual(self.command_texts(plan), ["listallinfo"])
        self.assertIsNotNone(plan.residual)

    def test_old_server_conjunction_uses_all_pairs(self) -> None:
        condition = And(
            TagFilter("artist", "AC/DC"),
            TagFilter("genre", "Rock"),
        )
        plan = compile_filter(condition, "0.19.0")
        self.assertEqual(self.command_texts(plan), ["find artist AC/DC genre Rock"])
        self.assertTrue(plan.server_supported)
        self.assertIsNone(plan.residual)

    def test_old_server_or_queries_both_branches(self) -> None:
        condition = Or(TagFilter("artist", "AC/DC"), TagFilter("genre", "Jazz"))
        plan = compile_filter(condition, "0.19.0")
        self.assertEqual(
            self.command_texts(plan), ["find artist AC/DC", "find genre Jazz"]
        )
        self.assertIsNotNone(plan.residual)

    def test_old_server_mixed_families_use_safe_candidate(self) -> None:
        condition = And(
            TagFilter("artist", "AC/DC"),
            TagFilter("title", "hello", "contains"),
        )
        plan = compile_filter(condition, "0.19.0")
        self.assertEqual(self.command_texts(plan), ["find artist AC/DC"])
        self.assertIsNotNone(plan.residual)

    def test_new_server_uses_native_expression(self) -> None:
        condition = And(
            TagFilter("artist", "AC/DC"), TagFilter("genre", "Rock")
        )
        plan = compile_filter(condition, "0.21.0")
        self.assertEqual(
            self.command_texts(plan),
            ['find ((artist == "AC/DC") AND (genre == "Rock"))'],
        )
        self.assertIsNone(plan.residual)

    def test_new_server_or_and_not(self) -> None:
        condition = And(
            Or(TagFilter("artist", "AC/DC"), TagFilter("genre", "Jazz")),
            Not(TagFilter("album", "Mixed")),
        )
        plan = compile_filter(condition, "0.22.0")
        self.assertEqual(
            self.command_texts(plan),
            [
                'find (((artist == "AC/DC") OR (genre == "Jazz")) AND '
                '(!(album == "Mixed")))'
            ],
        )

    def test_unknown_version_returns_explicit_guarantee(self) -> None:
        plan = compile_filter(TagFilter("artist", "AC/DC"), None)
        self.assertFalse(plan.server_supported)
        self.assertTrue(plan.unsupported)
        self.assertIsNotNone(plan.residual)

    def test_server_only_raises_with_fallback_plan(self) -> None:
        with self.assertRaises(FilterNotSupported) as caught:
            compile_filter(
                Not(TagFilter("artist", "AC/DC")),
                "0.19.0",
                require_server_only=True,
            )
        self.assertIsNotNone(caught.exception.plan)
        self.assertEqual(
            self.command_texts(caught.exception.plan), ["listallinfo"]
        )

    def test_server_only_passes_for_exact_legacy_query(self) -> None:
        plan = compile_filter(
            TagFilter("artist", "AC/DC"), "0.19.0", require_server_only=True
        )
        self.assertTrue(plan.server_supported)

    def test_escape_survives_planning(self) -> None:
        condition = TagFilter("title", 'x" OR artist == "y')
        for version in ("0.19.0", "0.21.0"):
            plan = compile_filter(condition, version)
            self.assertIsNotNone(plan.queries)
        text = compile_filter(condition, "0.21.0").queries[0].args[0]
        self.assertEqual(parse_filter(text).value, 'x" OR artist == "y')


class TestClientIntegration(unittest.TestCase):
    def test_sync_selection_returns_structured_result(self) -> None:
        server = ReplayServer(SONGS, "0.19.0")
        result = select_songs(server, Not(TagFilter("genre", "Rock")))
        self.assertEqual(files(result), ["04.mp3"])
        self.assertEqual(result.guarantee, "candidate superset with local verification")
        self.assertEqual(result.executed[0].command, "listallinfo")

    def test_async_selection(self) -> None:
        import asyncio

        from mpd.filters import select_songs_async

        class AsyncServer(ReplayServer):
            async def find(self, *args: Any) -> List[Dict[str, Any]]:
                return super().find(*args)

            async def search(self, *args: Any) -> List[Dict[str, Any]]:
                return super().search(*args)

            async def listallinfo(self, *args: Any) -> List[Dict[str, Any]]:
                return super().listallinfo(*args)

        server = AsyncServer(SONGS, "0.19.0")
        condition = Or(TagFilter("artist", "Other"), TagFilter("genre", "Jazz"))
        result = asyncio.run(select_songs_async(server, condition))
        self.assertEqual(files(result), ["04.mp3"])

    def test_unsupported_result_is_explicit(self) -> None:
        server = ReplayServer(SONGS, "0.19.0")
        result = select_songs(server, Not(TagFilter("genre", "Rock")))
        self.assertFalse(result.supported)
        self.assertTrue(result.unsupported)
        self.assertNotIn("search", [query.command for query in result.executed])

    def test_require_server_only_rejects_legacy_negation(self) -> None:
        server = ReplayServer(SONGS, "0.19.0")
        with self.assertRaises(FilterNotSupported):
            select_songs(
                server,
                Not(TagFilter("genre", "Rock")),
                require_server_only=True,
            )


class TestReplayEquivalence(unittest.TestCase):
    """The same condition must confirm the same records on every version."""

    CONDITIONS = [
        TagFilter("artist", "AC/DC"),
        TagFilter("artist", "ac", "contains"),
        TagFilter("genre", "Rock"),
        TagFilter("genre", "Pop"),
        Not(TagFilter("genre", "Rock")),
        And(TagFilter("genre", "Rock"), Not(TagFilter("genre", "Pop"))),
        Or(TagFilter("artist", "Other"), TagFilter("genre", "Jazz")),
        And(
            TagFilter("artist", "AC/DC"),
            TagFilter("album", "Powerage"),
        ),
    ]

    def run_condition(self, condition: Filter, version: str) -> List[str]:
        server = ReplayServer(SONGS, version)
        from mpd.filters import _classify, _run_queries

        plan = compile_filter(condition, version)
        candidates = _run_queries(server, plan)
        return files(_classify(condition, plan, candidates))

    def test_old_and_new_paths_confirm_same_records(self) -> None:
        for condition in self.CONDITIONS:
            old = self.run_condition(condition, "0.19.0")
            new = self.run_condition(condition, "0.21.0")
            self.assertEqual(old, new, msg=str(condition))

    def test_candidates_cover_every_match(self) -> None:
        from mpd.filters import _run_queries

        for condition in self.CONDITIONS:
            expected = {song["file"] for song in SONGS if condition.evaluate(song)}
            for version in ("0.19.0", "0.21.0", "0.22.0"):
                server = ReplayServer(SONGS, version)
                plan = compile_filter(condition, version)
                candidates = {song["file"] for song in _run_queries(server, plan)}
                self.assertTrue(
                    expected <= candidates,
                    msg="{} on {}: {} not covered".format(
                        condition, version, expected - candidates
                    ),
                )

    def test_starts_with_uses_contains_on_older_filter_servers(self) -> None:
        condition = TagFilter("title", "back", "starts_with")
        plan = compile_filter(condition, "0.21.0")
        self.assertEqual(
            plan.as_commands(), ['search (title contains "back")']
        )
        self.assertFalse(plan.server_supported)
        self.assertEqual(
            compile_filter(condition, "0.20.0").as_commands(),
            ["search title back"],
        )
        self.assertEqual(
            compile_filter(condition, "0.22.0").as_commands(),
            ['search (title starts_with "back")'],
        )

    def test_any_matches_tag_values_not_file(self) -> None:
        song = {"file": "needle.mp3", "artist": "other"}
        self.assertFalse(TagFilter("any", "needle").evaluate(song))
        self.assertTrue(TagFilter("any", "other").evaluate(song))

    def test_old_search_is_not_used_as_exact_find(self) -> None:
        # "ac/dc" would be found case-insensitively by search but must not be
        # confirmed by an exact find.
        condition = TagFilter("artist", "ac/dc")
        result = self.run_condition(condition, "0.19.0")
        self.assertEqual(result, ["02.mp3"])
    def test_escaped_values_keep_same_confirmed_set(self) -> None:
        songs = [
            {"file": "q.mp3", "title": 'Say "Hi"'},
            {"file": "b.mp3", "title": "back\\slash"},
            {"file": "n.mp3", "title": "Say Hi"},
        ]
        cases = [
            (TagFilter("title", 'Say "Hi"'), ["q.mp3"]),
            (TagFilter("title", "back\\slash"), ["b.mp3"]),
        ]
        for condition, expected in cases:
            old = select_songs(ReplayServer(songs, "0.19.0"), condition)
            new = select_songs(ReplayServer(songs, "0.21.0"), condition)
            self.assertEqual(files(old), files(new), msg=str(condition))
            self.assertEqual(files(old), expected)

    def test_duplicate_tags_replay_same_set(self) -> None:
        songs = [
            {"file": "both.mp3", "genre": ["Rock", "Pop"]},
            {"file": "rock.mp3", "genre": ["Rock"]},
            {"file": "pop.mp3", "genre": ["Pop"]},
        ]
        condition = And(TagFilter("genre", "Rock"), Not(TagFilter("genre", "Pop")))
        for version in ("0.19.0", "0.21.0"):
            result = select_songs(ReplayServer(songs, version), condition)
            self.assertEqual(files(result), ["rock.mp3"])

    def test_missing_field_replay_keeps_undetermined(self) -> None:
        songs = [
            {"file": "known.mp3", "artist": "Other"},
            {"file": "missing.mp3"},
        ]
        condition = Not(TagFilter("artist", "AC/DC"))
        old = select_songs(ReplayServer(songs, "0.19.0"), condition)
        new = select_songs(ReplayServer(songs, "0.21.0"), condition)
        # Confirmed records agree.  The legacy full scan sees the record with
        # no artist and must report it as undetermined rather than excluding
        # it; the native query never returns that record.
        self.assertEqual(files(old), ["known.mp3"])
        self.assertEqual(files(new), ["known.mp3"])
        self.assertEqual(old.undetermined_files, ["missing.mp3"])
        self.assertFalse(old.complete)



if __name__ == "__main__":
    unittest.main()
