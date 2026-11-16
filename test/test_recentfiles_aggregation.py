"""Tests for multi-source aggregation, visibility rules and deduplication."""

from xdg import RecentFiles as RF
from xdg.RecentFiles import (RecentFiles, AggregatedRecentFiles, RecentItem,
                             RecentMerge, uri_key)

import unittest
import os.path
import tempfile, shutil


def make_source(items):
    """Build a RecentFiles instance from (uri, mime, ts, private, groups)."""
    recent = RecentFiles()
    for uri, mime, ts, private, groups in items:
        rf = RF.RecentFile()
        rf.URI = uri
        rf.MimeType = mime
        rf.Timestamp = ts
        rf.Private = private
        rf.Groups = list(groups)
        recent.RecentFiles.append(rf)
    return recent


class UriEquivalenceTest(unittest.TestCase):
    def test_percent_encoding_and_spaces(self):
        self.assertEqual(uri_key("file:///home/a%20b"),
                         uri_key("file:///home/a b"))

    def test_localhost_and_empty_host(self):
        self.assertEqual(uri_key("file://localhost/tmp/x"),
                         uri_key("file:///tmp/x"))

    def test_dot_segments(self):
        self.assertEqual(uri_key("file:///a/b/../c/./d"),
                         uri_key("file:///a/c/d"))

    def test_scheme_host_case_and_default_port(self):
        self.assertEqual(uri_key("HTTP://Example.COM:80/p"),
                         uri_key("http://example.com/p"))
        self.assertEqual(uri_key("https://example.com:443/p"),
                         uri_key("https://example.com/p"))

    def test_non_default_port_distinct(self):
        self.assertNotEqual(uri_key("http://example.com:8080/p"),
                            uri_key("http://example.com/p"))

    def test_path_case_sensitive(self):
        self.assertNotEqual(uri_key("file:///A"), uri_key("file:///a"))


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.agg = AggregatedRecentFiles()
        self.agg.add_source("public", make_source([
            ("file:///open.txt", "text/plain", 100, False, ["desk"]),
        ]))
        self.agg.add_source("private", make_source([
            ("file:///secret.txt", "text/plain", 200, True, ["staff"]),
            ("file:///open.txt", "text/plain", 300, True, ["staff"]),
        ]))

    def test_public_list_hides_private_occurrences(self):
        uris = {item.URI for item in self.agg.getItems()}
        self.assertEqual(uris, {"file:///open.txt"})
        for item in self.agg.getItems():
            self.assertFalse(item.Private)

    def test_private_requires_group_authorization(self):
        # No group named: public evidence stays visible but no private one.
        visible = self.agg.getItems(include_private=True)
        self.assertTrue(visible)
        self.assertTrue(all(not item.Private for item in visible))
        # A group the private items do not belong to does not help.
        visible = self.agg.getItems(groups=["other"], include_private=True)
        self.assertTrue(all(not item.Private for item in visible))
        # The authorized group unlocks exactly the staff occurrences.
        visible = self.agg.getItems(groups=["staff"], include_private=True)
        self.assertEqual({item.URI for item in visible},
                         {"file:///secret.txt", "file:///open.txt"})
        self.assertTrue(all(item.Private for item in visible))

    def test_public_occurrence_never_authorizes_private_one(self):
        records = {r.key: r for r in self.agg.query()}
        merge = records[uri_key("file:///open.txt")]
        # Only the public occurrence backs the public merged record.
        self.assertEqual(len(merge.getItems()), 1)
        self.assertFalse(merge.getItems()[0].Private)
        self.assertEqual(merge.getItems()[0].source, "public")
        self.assertNotIn("staff", merge.Groups)
        # The newer private timestamp must not leak into the public record.
        self.assertEqual(merge.Timestamp, 100)
        self.assertFalse(merge.Private)

    def test_authorized_merge_uses_private_evidence(self):
        # The staff filter matches the private occurrence; the public one
        # only belongs to "desk" and is filtered out conjunctively.
        records = {r.key: r for r in
                   self.agg.query(groups=["staff"], include_private=True)}
        merge = records[uri_key("file:///open.txt")]
        sources = {item.source for item in merge.getItems()}
        self.assertEqual(sources, {"private"})
        # Newest visible occurrence now wins.
        self.assertEqual(merge.Timestamp, 300)
        self.assertEqual(merge.Groups, ["staff"])
        self.assertTrue(merge.Private)

    def test_authorized_query_merges_public_and_private_same_uri(self):
        agg = AggregatedRecentFiles()
        agg.add_source("public", make_source([
            ("file:///dual", "text/plain", 100, False, ["staff"]),
        ]))
        agg.add_source("private", make_source([
            ("file:///dual", "text/plain", 300, True, ["staff"]),
        ]))
        # Without authorization the private evidence stays out of the merge.
        public_view = {r.key: r for r in agg.query()}
        self.assertEqual(public_view[uri_key("file:///dual")].Timestamp, 100)
        self.assertFalse(public_view[uri_key("file:///dual")].Private)
        # With it, both occurrences back the same merged record and the
        # caller can still tell them apart via getItems().
        merge = agg.query(groups=["staff"], include_private=True)[0]
        self.assertEqual({(item.source, item.Private) for item in
                          merge.getItems()},
                         {("public", False), ("private", True)})
        self.assertEqual(merge.Timestamp, 300)
        self.assertFalse(merge.Private)


class FilterAndLimitTest(unittest.TestCase):
    def setUp(self):
        self.agg = AggregatedRecentFiles()
        self.agg.add_source("s1", make_source([
            ("file:///a.png", "image/png", 100, False, ["g1"]),
            ("file:///b.txt", "text/plain", 200, False, ["g2"]),
            ("file:///c.png", "image/png", 300, False, ["g2"]),
            ("file:///d.txt", "text/plain", 400, False, ["g1", "g2"]),
        ]))

    def test_mime_and_group_are_conjunctive(self):
        hits = self.agg.query(mimetypes=["image/png"], groups=["g2"])
        self.assertEqual([r.URI for r in hits], ["file:///c.png"])

    def test_mime_only_and_group_only(self):
        mime_hits = self.agg.query(mimetypes=["text/plain"])
        self.assertEqual({r.URI for r in mime_hits},
                         {"file:///b.txt", "file:///d.txt"})
        group_hits = self.agg.query(groups=["g1"])
        self.assertEqual({r.URI for r in group_hits},
                         {"file:///a.png", "file:///d.txt"})

    def test_limit_applies_after_dedup_and_sorting(self):
        # Equivalent spellings collapse before the top K cut.
        agg = AggregatedRecentFiles()
        agg.add_source("s1", make_source([
            ("file:///x", "text/plain", 100, False, []),
            ("file://localhost/x", "text/plain", 100, False, []),
            ("file:///y", "text/plain", 200, False, []),
            ("file:///z", "text/plain", 300, False, []),
        ]))
        hits = agg.query(limit=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual([r.URI for r in hits],
                         ["file:///z", "file:///y"])

    def test_sorted_newest_first(self):
        hits = self.agg.query()
        self.assertEqual([r.Timestamp for r in hits], [400, 300, 200, 100])


class ProvenanceAndRemovalTest(unittest.TestCase):
    def setUp(self):
        self.agg = AggregatedRecentFiles()
        self.agg.add_source("s1", make_source([
            ("file:///shared", "text/plain", 100, False, ["g1"]),
            ("file:///only-s1", "text/plain", 50, False, []),
        ]))
        self.agg.add_source("s2", make_source([
            ("file://localhost/shared", "text/plain", 200, False, ["g2"]),
        ]))

    def test_merged_record_traces_actual_items(self):
        merge = self.agg.query()[0]
        self.assertIsInstance(merge, RecentMerge)
        items = merge.getItems()
        self.assertEqual({item.source for item in items}, {"s1", "s2"})
        for item in items:
            self.assertIsInstance(item, RecentItem)
        self.assertEqual(merge.getSources(), ["s1", "s2"])
        self.assertEqual(merge.Timestamp, 200)
        self.assertEqual(merge.Groups, ["g1", "g2"])

    def test_get_items_returns_source_tagged_evidence(self):
        items = self.agg.getItems()
        self.assertEqual({item.source for item in items}, {"s1", "s2"})

    def test_duplicate_source_rejected(self):
        self.assertRaises(ValueError, self.agg.add_source, "s1",
                          make_source([]))

    def test_remove_source_withdraws_only_its_evidence(self):
        self.assertTrue(self.agg.remove_source("s2"))
        self.assertFalse(self.agg.remove_source("s2"))
        self.assertEqual(self.agg.sources(), ["s1"])

        records = {r.key: r for r in self.agg.query()}
        self.assertEqual(set(records),
                         {uri_key("file:///shared"),
                          uri_key("file:///only-s1")})
        merge = records[uri_key("file:///shared")]
        self.assertEqual(merge.getSources(), ["s1"])
        self.assertEqual(merge.Timestamp, 100)
        self.assertEqual(merge.Groups, ["g1"])

        self.agg.remove_source("s1")
        self.assertEqual(self.agg.query(), [])


class SingleSourceTest(unittest.TestCase):
    def test_query_deduplicates_within_one_source(self):
        recent = make_source([
            ("file:///a%20b", "text/plain", 100, False, ["g1"]),
            ("file:///a b", "text/plain", 100, False, ["g2"]),
        ])
        hits = recent.query()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].Groups, ["g1", "g2"])
        self.assertEqual({item.source for item in hits[0].getItems()},
                         {recent.filename or None})

    def test_legacy_get_files_filters_without_duplicates(self):
        recent = make_source([
            ("file:///a", "image/png", 300, False, ["g1", "g2"]),
            ("file:///b", "text/plain", 200, False, ["g1"]),
            ("file:///c", "text/plain", 100, True, ["g1"]),
        ])
        hits = recent.getFiles(mimetypes=["image/png"], groups=["g1"])
        self.assertEqual([item.URI for item in hits], ["file:///a"])
        self.assertEqual(len(recent.getFiles(groups=["g1", "g2"])), 2)
        self.assertEqual(len(recent.getFiles(limit=1)), 1)


class PrivateParsingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, ".recently-used")
        with open(self.test_file, "w") as f:
            f.write("""<?xml version="1.0"?>
<RecentFiles>
  <RecentItem>
    <URI>file:///secret</URI>
    <Mime-Type>text/plain</Mime-Type>
    <Timestamp>1272385187</Timestamp>
    <Private/>
    <Groups><Group>staff</Group></Groups>
  </RecentItem>
  <RecentItem>
    <URI>file:///open</URI>
    <Mime-Type>text/plain</Mime-Type>
    <Timestamp>1272378716</Timestamp>
  </RecentItem>
</RecentFiles>
""")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_private_flag_parsed_and_respected(self):
        recent = RecentFiles()
        recent.parse(self.test_file)
        self.assertTrue(recent.RecentFiles[0].Private)
        self.assertFalse(recent.RecentFiles[1].Private)

        items = recent.getItems()
        self.assertEqual([item.URI for item in items], ["file:///open"])
        authorized = recent.getItems(groups=["staff"], include_private=True)
        self.assertEqual([item.URI for item in authorized],
                         ["file:///secret"])

    def test_write_roundtrip_keeps_private(self):
        recent = RecentFiles()
        recent.parse(self.test_file)
        out = os.path.join(self.tmpdir, "copy")
        recent.write(out)

        again = RecentFiles()
        again.parse(out)
        self.assertTrue(again.RecentFiles[0].Private)
        self.assertEqual(again.getFiles(), [again.RecentFiles[1]])


if __name__ == "__main__":
    unittest.main()
