import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from mpd.filters import And, Not, Or, TagFilter, select_songs
from mpd.replay import ReplayClient, load_songs, replay


SONGS: List[Dict[str, Any]] = [
    {"file": "01.mp3", "artist": "AC/DC", "genre": ["Rock"]},
    {"file": "02.mp3", "artist": "Other", "genre": ["Pop"]},
    {"file": "03.mp3", "title": "untagged"},
]


def confirmed(result: Any) -> List[str]:
    return [song["file"] for song in result.confirmed]


class TestReplayClient(unittest.TestCase):
    def test_exact_and_loose_commands(self) -> None:
        exact = ReplayClient(SONGS, "0.19.0").find("artist", "AC/DC")
        self.assertEqual([song["file"] for song in exact], ["01.mp3"])
        loose = ReplayClient(SONGS, "0.19.0").search("artist", "ac")
        self.assertEqual([song["file"] for song in loose], ["01.mp3"])

    def test_expression_commands(self) -> None:
        server = ReplayClient(SONGS, "0.21.0")
        result = server.find('(genre == "Rock")')
        self.assertEqual([song["file"] for song in result], ["01.mp3"])
        result = server.search('(artist contains "OTHER")')
        self.assertEqual([song["file"] for song in result], ["02.mp3"])

    def test_full_scan(self) -> None:
        self.assertEqual(len(ReplayClient(SONGS, "0.19.0").listallinfo()), 3)


class TestReplayHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_json(self, records: List[Dict[str, Any]]) -> None:
        (self.directory / "songs.json").write_text(
            json.dumps(records), encoding="utf-8"
        )

    def test_old_and_new_confirmed_sets_match(self) -> None:
        self.write_json(SONGS)
        report = replay(
            Not(TagFilter("genre", "Rock")),
            self.directory,
            versions=("0.19.0", "0.21.0"),
        )
        self.assertTrue(report.equivalent)
        self.assertEqual(report.confirmed("0.19.0"), ["02.mp3"])
        self.assertEqual(report.confirmed("0.21.0"), ["02.mp3"])
        self.assertEqual(report.differences, {})

    def test_unsupported_is_reported(self) -> None:
        self.write_json(SONGS)
        report = replay(Not(TagFilter("genre", "Rock")), self.directory)
        self.assertIn("0.20.0", report.unsupported)

    def test_undetermined_is_not_excluded(self) -> None:
        self.write_json(SONGS)
        report = replay(
            Not(TagFilter("genre", "Rock")),
            self.directory,
            versions=("0.19.0",),
        )
        self.assertEqual(report.undetermined["0.19.0"], ["03.mp3"])

    def test_per_version_directories(self) -> None:
        old = self.directory / "old"
        new = self.directory / "new"
        old.mkdir()
        new.mkdir()
        (old / "songs.json").write_text(json.dumps(SONGS), encoding="utf-8")
        (new / "songs.json").write_text(json.dumps(SONGS), encoding="utf-8")
        report = replay(
            Or(TagFilter("artist", "AC/DC"), TagFilter("genre", "Pop")),
            {"0.19.0": old, "0.21.0": new},
        )
        self.assertTrue(report.equivalent)
        self.assertEqual(report.confirmed("0.19.0"), ["01.mp3", "02.mp3"])

    def test_protocol_fixture_accumulates_duplicate_tags(self) -> None:
        (self.directory / "songs.mpd").write_text(
            "file: d.mp3\nGenre: Rock\nGenre: Pop\nOK\n", encoding="utf-8"
        )
        songs = load_songs(self.directory)
        self.assertEqual(songs, [{"file": "d.mp3", "genre": ["Rock", "Pop"]}])

    def test_multi_value_not_scope(self) -> None:
        self.write_json(
            [
                {"file": "both.mp3", "genre": ["Rock", "Pop"]},
                {"file": "rock.mp3", "genre": ["Rock"]},
            ]
        )
        condition = And(TagFilter("genre", "Rock"), Not(TagFilter("genre", "Pop")))
        report = replay(condition, self.directory, versions=("0.19.0", "0.22.0"))
        self.assertTrue(report.equivalent)
        self.assertEqual(report.confirmed("0.19.0"), ["rock.mp3"])
        self.assertEqual(report.confirmed("0.22.0"), ["rock.mp3"])

    def test_boolean_coercion_paths(self) -> None:
        self.write_json(SONGS)
        for condition in (
            "(artist == \"Other\")",
            {"artist": "Other"},
            ("artist", "==", "Other"),
        ):
            result = select_songs(ReplayClient(SONGS, "0.21.0"), condition)
            self.assertEqual(confirmed(result), ["02.mp3"])


if __name__ == "__main__":
    unittest.main()
