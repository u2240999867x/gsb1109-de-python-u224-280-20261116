"""Range-based number directory (catalogue) assembly and persistence.

Suppliers deliver *ranges* (and exclusion ranges), never individual contacts.
A catalogue therefore stores a compressed description of the covered E.164
number space together with the total count of valid numbers, instead of
materialising millions of contacts.

Exclusions are scoped: an exclusion only removes numbers from the supply
declared by the same source.  Each supplied range is split against the
region metadata into provably valid, pending (possible but not provably
valid) and invalid pieces; only the valid pieces enter the merged coverage.
"""
import json

from .phonemetadata import PhoneMetadata, REGION_CODE_FOR_NON_GEO_ENTITY
from .numberrange import (NumberRange, NumberRangeError, intersect_ranges,
                          merge_ranges, parse_range, subtract_ranges, total_count)
from .numberrules import (RANGE_PENDING, RANGE_VALID,
                          ClassifiedRange, RangeRuleClassifier)
from .util import u

CATALOG_SCHEMA_VERSION = 1


def _metadata_version():
    # Imported lazily because __version__ is defined by the package __init__,
    # which is still running while this submodule is first imported.
    from . import __version__
    return __version__


class NumberCatalog(object):
    """Builds and holds the compressed coverage for one explicit region."""

    def __init__(self, region_code):
        self.region_code = region_code.upper()
        self._classifier = RangeRuleClassifier(self.region_code)
        self._non_geo = self.region_code == REGION_CODE_FOR_NON_GEO_ENTITY
        self._supplies = {}       # source_id -> [NumberRange]
        self._exclusions = {}     # source_id -> [NumberRange]
        self._coverage = []       # merged, maximal valid NumberRanges
        self._review = []         # ClassifiedRange pieces needing attention
        self._source_stats = {}   # source_id -> stats dict
        self._built = False

    def add_source(self, source_id, supply_endpoints, exclusion_endpoints=None):
        """Register one supplier's supply and exclusion endpoint pairs.

        Arguments:
        source_id -- identifier used to scope the exclusions
        supply_endpoints -- iterable of ``(start_text, end_text)`` pairs
        exclusion_endpoints -- iterable of ``(start_text, end_text)`` pairs;
            numbers outside this source's own supply are recorded as
            out-of-scope and have no effect.
        """
        self._built = False
        parse_region = None if self._non_geo else self.region_code
        supplies = [parse_range(start, end, parse_region)
                    for start, end in supply_endpoints]
        exclusions = [parse_range(start, end, parse_region)
                      for start, end in (exclusion_endpoints or ())]
        for rng in supplies + exclusions:
            self._check_calling_code(rng.country_code)
        self._supplies[source_id] = merge_ranges(
            self._supplies.get(source_id, []) + supplies)
        self._exclusions[source_id] = merge_ranges(
            self._exclusions.get(source_id, []) + exclusions)

    def _check_calling_code(self, country_code):
        if self._non_geo:
            if PhoneMetadata.metadata_for_nongeo_region(country_code, None) is None:
                raise NumberRangeError(
                    u("calling code %d has no non-geographical metadata") % country_code)
        elif country_code != self._classifier.metadata_for_code(country_code).country_code:
            raise NumberRangeError(
                u("calling code %d does not belong to region %r") %
                (country_code, self.region_code))

    def build(self):
        """Apply scoped exclusions, classify and merge the catalogue."""
        valid_pieces = []
        review_pieces = []
        source_stats = {}
        for source_id in sorted(self._supplies):
            supplies = self._supplies[source_id]
            exclusions = self._exclusions.get(source_id, [])
            effective_exclusions = intersect_ranges(exclusions, supplies)
            out_of_scope = subtract_ranges(exclusions, supplies)
            net_supply = subtract_ranges(supplies, exclusions)
            stats = {
                "source_id": source_id,
                "supplied_count": total_count(supplies),
                "excluded_count": total_count(effective_exclusions),
                "exclusion_out_of_scope_count": total_count(out_of_scope),
                "net_count": total_count(net_supply),
                "valid_count": 0,
                "pending_count": 0,
                "invalid_count": 0,
            }
            for rng in net_supply:
                for classified in self._classifier.classify(rng):
                    if classified.status == RANGE_VALID:
                        valid_pieces.append(classified.number_range)
                        stats["valid_count"] += classified.number_count
                    else:
                        review_pieces.append(_AttributedRange(classified, source_id))
                        if classified.status == RANGE_PENDING:
                            stats["pending_count"] += classified.number_count
                        else:
                            stats["invalid_count"] += classified.number_count
            source_stats[source_id] = stats
        self._coverage = merge_ranges(valid_pieces)
        self._review = review_pieces
        self._source_stats = source_stats
        self._built = True
        return self

    def _require_built(self):
        if not self._built:
            self.build()

    @property
    def coverage(self):
        """Merged, maximal ranges that are provably valid."""
        self._require_built()
        return list(self._coverage)

    @property
    def valid_count(self):
        self._require_built()
        return total_count(self._coverage)

    @property
    def review_ranges(self):
        """Pending and invalid pieces with their originating source."""
        self._require_built()
        return list(self._review)

    @property
    def source_stats(self):
        self._require_built()
        return dict(self._source_stats)

    def totals(self):
        """Return aggregate counts across every source."""
        self._require_built()
        supplied = sum(stats["supplied_count"] for stats in self._source_stats.values())
        excluded = sum(stats["excluded_count"] for stats in self._source_stats.values())
        net = sum(stats["net_count"] for stats in self._source_stats.values())
        pending = sum(stats["pending_count"] for stats in self._source_stats.values())
        invalid = sum(stats["invalid_count"] for stats in self._source_stats.values())
        return {
            "supplied_count": supplied,
            "excluded_count": excluded,
            "net_count": net,
            "valid_count": self.valid_count,
            "pending_count": pending,
            "invalid_count": invalid,
            "coverage_range_count": len(self._coverage),
            "review_range_count": len(self._review),
        }

    def to_dict(self):
        """Serialise the catalogue to plain JSON-compatible data."""
        self._require_built()
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "region_code": self.region_code,
            "metadata_version": _metadata_version(),
            "totals": self.totals(),
            "sources": [self._source_stats[key] for key in sorted(self._source_stats)],
            "coverage": [rng.to_dict() for rng in self._coverage],
            "review": [item.to_dict() for item in self._review],
        }

    def save(self, path):
        """Write the catalogue as pretty-printed JSON, overwriting the file."""
        with open(path, "w") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write(u("\n"))

    @classmethod
    def from_dict(cls, data):
        """Reload a persisted catalogue (coverage and review pieces)."""
        if data.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise NumberRangeError(
                u("unsupported catalogue schema version %r") % data.get("schema_version"))
        catalog = cls(data["region_code"])
        coverage = [NumberRange.from_dict(item) for item in data.get("coverage", ())]
        catalog._coverage = merge_ranges(coverage)
        catalog._review = [_AttributedRange.from_dict(item)
                           for item in data.get("review", ())]
        catalog._source_stats = {stats["source_id"]: dict(stats)
                                 for stats in data.get("sources", ())}
        catalog._built = True
        return catalog

    @classmethod
    def load(cls, path):
        with open(path, "r") as handle:
            return cls.from_dict(json.load(handle))

    @classmethod
    def from_supply_doc(cls, region_code, document):
        """Build a catalogue from a parsed supplier document.

        Document shape::

            {"sources": [
                {"source_id": "alpha",
                 "supply": [{"start": "+493000000000", "end": "+493000009999"}],
                 "exclusions": [{"start": "...", "end": "..."}]}]}
        """
        catalog = cls(region_code)
        for source in document.get("sources", ()):
            source_id = source["source_id"]
            supplies = [(item["start"], item["end"]) for item in source.get("supply", ())]
            exclusions = [(item["start"], item["end"])
                          for item in source.get("exclusions", ())]
            catalog.add_source(source_id, supplies, exclusions)
        return catalog.build()


class _AttributedRange(object):
    """A review piece (pending or invalid) tied back to its source."""

    __slots__ = ("classified_range", "source_id")

    def __init__(self, classified_range, source_id):
        self.classified_range = classified_range
        self.source_id = source_id

    @property
    def number_range(self):
        return self.classified_range.number_range

    @property
    def status(self):
        return self.classified_range.status

    @property
    def number_type(self):
        return self.classified_range.number_type

    @property
    def number_count(self):
        return self.classified_range.number_count

    def to_dict(self):
        data = self.classified_range.to_dict()
        data["source_id"] = self.source_id
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        source_id = data.pop("source_id")
        return cls(ClassifiedRange.from_dict(data), source_id)


def _main(argv=None):
    """Command line entry point: build or inspect a range catalogue."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Build a compressed number-range catalogue from supplier ranges.")
    subparsers = parser.add_subparsers(dest="command")

    build_parser = subparsers.add_parser("build", help="build a catalogue JSON file")
    build_parser.add_argument("--region", required=True,
                              help="ISO 3166-1 alpha-2 region code, or 001 for non-geographical codes")
    build_parser.add_argument("--input", required=True,
                              help="supplier document JSON with sources/supply/exclusions")
    build_parser.add_argument("--output", required=True, help="catalogue JSON output path")

    show_parser = subparsers.add_parser("show", help="print catalogue totals and ranges")
    show_parser.add_argument("--input", required=True, help="catalogue JSON path")

    args = parser.parse_args(argv)
    if args.command == "build":
        with open(args.input, "r") as handle:
            document = json.load(handle)
        catalogue = NumberCatalog.from_supply_doc(args.region, document)
        catalogue.save(args.output)
        print(u("wrote %s") % args.output)
        for key in sorted(catalogue.totals()):
            print(u("  %s: %d") % (key, catalogue.totals()[key]))
        return 0
    if args.command == "show":
        catalogue = NumberCatalog.load(args.input)
        print(u("region %s, metadata version %s") %
              (catalogue.region_code, catalogue.to_dict()["metadata_version"]))
        for key in sorted(catalogue.totals()):
            print(u("  %s: %d") % (key, catalogue.totals()[key]))
        for rng in catalogue.coverage:
            print(u("  coverage %s .. %s (%d)") %
                  (rng.e164_start(), rng.e164_end(), rng.number_count))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma no cover
    import sys
    sys.exit(_main())
