# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

import datetime

import pytest

from pyftpdlib.catalog import Ambiguity
from pyftpdlib.catalog import Catalog
from pyftpdlib.catalog import Coverage
from pyftpdlib.catalog import parse_list
from pyftpdlib.catalog import parse_mlsx
from pyftpdlib.catalog import Precision

from . import PyftpdlibTestCase


MLSD_FILE = (
    "type=file;size=156;perm=r;modify=20071029155301;unique=8012; music.mp3"
)
MLSD_DIR = (
    "type=dir;size=0;perm=el;modify=20071127230206;unique=801e33; ebooks"
)
LIST_FILE = (
    "-rw-rw-rw-   1 owner   group        156 Oct 29 15:53 music.mp3"
)
LIST_FILE_OLD = (
    "-rw-rw-rw-   1 owner   group        156 Oct 29  2001 music.mp3"
)


class TestParseMlsx(PyftpdlibTestCase):
    def test_known_facts(self):
        decl = parse_mlsx(MLSD_FILE)
        assert decl.source == "mlsd"
        assert decl.raw == MLSD_FILE
        assert decl.name == "music.mp3"
        assert decl.fields["size"].value == 156
        assert decl.fields["size"].precision == Precision.EXACT
        assert decl.fields["type"].value == "file"
        assert decl.fields["perm"].value == "r"
        assert decl.fields["unique"].value == "8012"
        modify = decl.fields["modify"]
        assert modify.value == datetime.datetime(2007, 10, 29, 15, 53, 1)
        assert modify.precision == Precision.SECOND

    def test_mlst_source(self):
        decl = parse_mlsx(MLSD_FILE, source="mlst")
        assert decl.source == "mlst"
        assert decl.fields["size"].source == "mlst"

    def test_fractional_seconds(self):
        decl = parse_mlsx("modify=20071029155301.25; a")
        modify = decl.fields["modify"]
        assert modify.value == datetime.datetime(
            2007, 10, 29, 15, 53, 1, 250000
        )
        assert modify.precision == Precision.MICROSECOND

    def test_unknown_facts_preserved_as_is(self):
        decl = parse_mlsx(
            "type=file;X-Custom=SomeValue;UNIX.owner=root; a.txt"
        )
        # unknown facts are preserved as declared
        assert decl.facts["X-Custom"] == "SomeValue"
        assert decl.facts["UNIX.owner"] == "root"
        assert decl.unknown_facts == {
            "X-Custom": "SomeValue",
            "UNIX.owner": "root",
        }
        # ...and are not turned into parsed fields
        assert "x-custom" not in decl.fields
        assert "unix.owner" not in decl.fields

    def test_missing_fields_not_zero_filled(self):
        decl = parse_mlsx("type=file; a.txt")
        assert "size" not in decl.fields
        assert "modify" not in decl.fields
        assert decl.fields.get("size") is None

    def test_unparseable_values_kept_as_is(self):
        decl = parse_mlsx("size=abc;modify=xyz; a.txt")
        assert decl.fields["size"].value == "abc"
        assert decl.fields["size"].precision == Precision.UNKNOWN
        assert decl.fields["modify"].value == "xyz"
        assert decl.fields["modify"].precision == Precision.UNKNOWN

    def test_name_with_spaces(self):
        decl = parse_mlsx("type=file; my file.txt")
        assert decl.name == "my file.txt"

    def test_no_facts(self):
        decl = parse_mlsx(" music.mp3")
        assert decl.name == "music.mp3"
        assert decl.facts == {}
        assert decl.fields == {}

    def test_crlf_and_bytes(self):
        decl = parse_mlsx(MLSD_FILE.encode() + b"\r\n")
        assert decl.name == "music.mp3"
        assert decl.raw == MLSD_FILE

    def test_malformed(self):
        with pytest.raises(ValueError):
            parse_mlsx("type=file")


class TestParseList(PyftpdlibTestCase):
    def test_fields(self):
        decl = parse_list(LIST_FILE)
        assert decl.source == "list"
        assert decl.raw == LIST_FILE
        assert decl.name == "music.mp3"
        assert decl.fields["type"].value == "file"
        assert decl.fields["perm"].value == "-rw-rw-rw-"
        assert decl.fields["nlinks"].value == 1
        assert decl.fields["owner"].value == "owner"
        assert decl.fields["group"].value == "group"
        assert decl.fields["size"].value == 156
        assert decl.fields["size"].precision == Precision.EXACT

    def test_minute_precision(self):
        decl = parse_list(LIST_FILE)
        modify = decl.fields["modify"]
        assert modify.precision == Precision.MINUTE
        # the year is not declared; the current one is assumed
        assert modify.value == datetime.datetime(
            datetime.datetime.now().year, 10, 29, 15, 53
        )

    def test_day_precision(self):
        decl = parse_list(LIST_FILE_OLD)
        modify = decl.fields["modify"]
        assert modify.precision == Precision.DAY
        assert modify.value == datetime.datetime(2001, 10, 29)

    def test_dir_type(self):
        decl = parse_list("drwxrwxrwx   1 owner   group 0 Aug 31 18:50 ebooks")
        assert decl.fields["type"].value == "dir"

    def test_symlink(self):
        decl = parse_list(
            "lrwxrwxrwx   1 owner   group  4 Aug 31 18:50 link -> real"
        )
        assert decl.name == "link"
        assert decl.fields["type"].value == "link"
        assert decl.fields["target"].value == "real"

    def test_name_with_spaces(self):
        decl = parse_list(
            "-rw-rw-rw-   1 owner   group  156 Oct 29 15:53 my file.txt"
        )
        assert decl.name == "my file.txt"

    def test_malformed(self):
        with pytest.raises(ValueError):
            parse_list("-rw-rw-rw- 1 owner")


class TestCatalog(PyftpdlibTestCase):
    def test_unify_mlsd_and_list(self):
        cat = Catalog()
        cat.observe_mlsx("/", [MLSD_FILE])
        cat.observe_list("/", [LIST_FILE])
        entries = cat.get("/", "music.mp3")
        assert len(entries) == 1
        entry = entries[0]
        # original declarations are recorded separately, per source
        assert entry.sources == ["mlsd", "list"]
        mlsd, lst = entry.declarations
        assert mlsd.source == "mlsd"
        assert mlsd.raw == MLSD_FILE
        assert lst.source == "list"
        assert lst.raw == LIST_FILE
        # the unified view trusts the most precise declaration
        assert entry.fields["modify"].precision == Precision.SECOND
        assert entry.fields["modify"].source == "mlsd"
        assert entry.fields["modify"].value == datetime.datetime(
            2007, 10, 29, 15, 53, 1
        )
        # fields declared by a single source are still unified
        assert entry.fields["owner"].value == "owner"
        assert entry.fields["unique"].value == "8012"

    def test_list_only_entry(self):
        cat = Catalog()
        cat.observe_list("/", [LIST_FILE_OLD])
        entry = cat.get("/", "music.mp3")[0]
        assert entry.sources == ["list"]
        assert entry.fields["modify"].precision == Precision.DAY

    def test_missing_fields_not_zero_filled(self):
        cat = Catalog()
        cat.observe_mlsx("/", ["type=file; a.txt"])
        entry = cat.get("/", "a.txt")[0]
        assert entry.field("size") is None
        assert entry.field("modify") is None
        assert "size" not in entry.fields

    def test_unknown_facts_preserved_as_is(self):
        cat = Catalog()
        cat.observe_mlsx("/", ["type=file;X-Custom=SomeValue; a.txt"])
        entry = cat.get("/", "a.txt")[0]
        assert entry.facts["X-Custom"] == "SomeValue"

    def test_same_name_unique_association(self):
        # a file is deleted and recreated with the same name: the two
        # items are kept as separate candidates
        cat = Catalog()
        cat.observe_mlsx("/", ["type=file;unique=aaa; report.txt"])
        cat.observe_mlsx("/", ["type=file;unique=bbb; report.txt"])
        candidates = cat.get("/", "report.txt")
        assert len(candidates) == 2
        # a new declaration with a matching unique fact is uniquely
        # associated with the right candidate
        entry = cat.observe(
            "/", parse_mlsx("type=file;unique=bbb;size=10; report.txt")
        )
        assert entry is candidates[1]
        assert entry.field("size") == 10
        assert cat.ambiguities == []

    def test_same_name_ambiguity_lists_candidates(self):
        cat = Catalog()
        cat.observe_mlsx("/", ["type=file;unique=aaa; report.txt"])
        cat.observe_mlsx("/", ["type=file;unique=bbb; report.txt"])
        candidates = cat.get("/", "report.txt")
        assert len(candidates) == 2
        # a declaration without unique facts cannot be uniquely
        # associated: candidates are listed, no order-based overwrite
        decl = parse_list("-rw-rw-rw- 1 owner group 9 Oct 29 15:53 report.txt")
        assert cat.observe("/", decl) is None
        assert len(cat.ambiguities) == 1
        ambiguity = cat.ambiguities[0]
        assert isinstance(ambiguity, Ambiguity)
        assert ambiguity.dirpath == "/"
        assert ambiguity.declaration is decl
        assert ambiguity.candidates == candidates
        # no candidate was modified
        for candidate in candidates:
            assert candidate.sources == ["mlsd"]
            assert "size" not in candidate.fields

    def test_reconcile_complete_coverage(self):
        cat = Catalog()
        cat.observe_mlsx("/", [MLSD_FILE, MLSD_DIR])
        deleted = cat.reconcile("/", ["music.mp3"], Coverage.COMPLETE)
        assert [d.name for d in deleted] == ["ebooks"]
        assert cat.deletions == deleted
        assert cat.get("/", "ebooks") == []
        assert len(cat.get("/", "music.mp3")) == 1

    def test_reconcile_failed_coverage_no_full_deletion(self):
        cat = Catalog()
        cat.observe_mlsx("/", [MLSD_FILE, MLSD_DIR])
        for coverage in (
            Coverage.PARTIAL,
            Coverage.FAILED,
            "partial",
            "failed",
        ):
            deleted = cat.reconcile("/", [], coverage)
            assert deleted == []
        assert cat.deletions == []
        # nothing was deleted
        assert len(cat.get("/", "music.mp3")) == 1
        assert len(cat.get("/", "ebooks")) == 1

    def test_directories_are_kept_separate(self):
        cat = Catalog()
        cat.observe_mlsx("/a", [MLSD_FILE])
        cat.observe_mlsx("/b", [MLSD_FILE])
        assert len(cat.get("/a", "music.mp3")) == 1
        assert len(cat.get("/b", "music.mp3")) == 1
        assert cat.get("/a", "music.mp3") != cat.get("/b", "music.mp3")
        deleted = cat.reconcile("/a", [], Coverage.COMPLETE)
        assert [d.name for d in deleted] == ["music.mp3"]
        assert len(cat.get("/b", "music.mp3")) == 1

    def test_entries(self):
        cat = Catalog()
        cat.observe_mlsx("/b", [MLSD_FILE])
        cat.observe_mlsx("/a", [MLSD_DIR])
        names = [(e.name) for e in cat.entries()]
        assert names == ["ebooks", "music.mp3"]
        names = [e.name for e in cat.entries("/b")]
        assert names == ["music.mp3"]

    def test_observe_list_skips_total_line(self):
        cat = Catalog()
        ret = cat.observe_list("/", ["total 1", LIST_FILE])
        assert len(ret) == 1  # "total" line is skipped
        assert len(cat.get("/", "music.mp3")) == 1
