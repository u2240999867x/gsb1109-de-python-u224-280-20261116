import unittest

import pyx12.errors
import pyx12.rawx12stream
from pyx12.errors import X12Error
from pyx12.rawx12stream import RawX12Stream

ISA_401 = "ISA*00*          *00*          *ZZ*ZZ000          *ZZ*ZZ001          *030828*1128*U*00401*000010121*0*T*:~"
ISA_501_ALT = "ISA&00&          &00&          &ZZ&ZZ000          &ZZ&ZZ001          &030828&1128&^&00501&000010122&0&T&!+"

BODY_401 = (
    "GS*HC*ZZ000*ZZ001*20030828*1128*17*X*004010X098~\n"
    "ST*837*11280001~\n"
    "TST*AA:1:1*BB:5~\n"
    "SE*3*11280001~\n"
    "GE*1*17~\n"
    "IEA*1*000010121~\n"
)
BODY_501_ALT = (
    "GS&HC&ZZ000&ZZ001&20030828&1128&17&X&005010X218+\n"
    "ST&837&11280001+\n"
    "TST&AA!1!1&BB!5+\n"
    "SE&3&11280001+\n"
    "GE&1&17+\n"
    "IEA&1&000010122+\n"
)

STREAM_401 = (ISA_401 + "\n" + BODY_401).encode("latin-1")
STREAM_TWO = STREAM_401 + (ISA_501_ALT + "\n" + BODY_501_ALT).encode("latin-1")


def _seg_ids(segs):
    return [seg.segment.split(seg.ele_term, 1)[0] for seg in segs]


class SingleInterchange(unittest.TestCase):
    def test_one_chunk(self):
        reader = RawX12Stream()
        segs = reader.feed(STREAM_401)
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "GS", "ST", "TST", "SE", "GE", "IEA"])
        for seg in segs:
            self.assertEqual(seg.seg_term, "~")
            self.assertEqual(seg.ele_term, "*")
            self.assertEqual(seg.subele_term, ":")
            self.assertEqual(seg.repetition_term, None)
            raw = STREAM_401[seg.start : seg.end]
            self.assertEqual(raw, seg.segment.encode("latin-1") + b"~")
        self.assertEqual(segs[0].start, 0)
        self.assertEqual(reader.icvn, None)

    def test_byte_by_byte_delivers_each_segment_once(self):
        reader = RawX12Stream()
        segs = []
        deliveries = 0
        for idx in range(len(STREAM_401)):
            got = reader.feed(STREAM_401[idx : idx + 1])
            deliveries += len(got)
            segs.extend(got)
        reader.close()
        self.assertEqual(deliveries, 7)
        self.assertEqual(segs, RawX12Stream().feed(STREAM_401))
        self.assertEqual(_seg_ids(segs), ["ISA", "GS", "ST", "TST", "SE", "GE", "IEA"])

    def test_awkward_chunk_boundaries(self):
        reader = RawX12Stream()
        segs = []
        pos = 0
        for size in (1, 105, 2, 7, 4096, 3):
            segs.extend(reader.feed(STREAM_401[pos : pos + size]))
            pos += size
        segs.extend(reader.feed(STREAM_401[pos:]))
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "GS", "ST", "TST", "SE", "GE", "IEA"])

    def test_feed_str_matches_feed_bytes(self):
        from_str = RawX12Stream().feed(STREAM_401.decode("latin-1"))
        from_bytes = RawX12Stream().feed(STREAM_401)
        self.assertEqual(from_str, from_bytes)

    def test_blank_segments_and_eol_are_skipped(self):
        stream = ISA_401 + "~~\r\n" + "GS*HC*ZZ000*ZZ001*20030828*1128*17*X*004010X098~"
        stream += "\n~\nIEA*1*000010121~\n"
        segs = RawX12Stream().feed(stream.encode("latin-1"))
        self.assertEqual(_seg_ids(segs), ["ISA", "GS", "IEA"])

    def test_repetition_term_5010(self):
        segs = RawX12Stream().feed((ISA_501_ALT + "\n" + BODY_501_ALT).encode("latin-1"))
        self.assertEqual(segs[0].repetition_term, "^")
        self.assertTrue(all(seg.repetition_term == "^" for seg in segs))


class ConcatenatedInterchanges(unittest.TestCase):
    def test_each_interchange_uses_its_own_delimiters(self):
        reader = RawX12Stream()
        segs = reader.feed(STREAM_TWO)
        reader.close()
        self.assertEqual(len(segs), 14)
        first, second = segs[:7], segs[7:]
        self.assertTrue(all(seg.ele_term == "*" and seg.seg_term == "~" for seg in first))
        self.assertTrue(all(seg.ele_term == "&" and seg.seg_term == "+" for seg in second))
        self.assertTrue(all(seg.subele_term == "!" for seg in second))
        self.assertEqual(_seg_ids(first), _seg_ids(second))
        for seg in segs:
            raw = STREAM_TWO[seg.start : seg.end]
            self.assertEqual(raw, seg.segment.encode("latin-1") + seg.seg_term.encode("latin-1"))

    def test_delimiters_not_inherited(self):
        reader = RawX12Stream()
        reader.feed(STREAM_401)
        # Between interchanges no delimiter settings survive
        self.assertEqual(reader.seg_term, None)
        self.assertEqual(reader.ele_term, None)
        self.assertEqual(reader.subele_term, None)
        self.assertEqual(reader.get_term(), (None, None, None, "\n", None))
        segs = reader.feed((ISA_501_ALT + "\n" + BODY_501_ALT).encode("latin-1"))
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "GS", "ST", "TST", "SE", "GE", "IEA"])
        self.assertTrue(all(seg.seg_term == "+" for seg in segs))

    def test_second_interchange_would_break_with_first_delimiters(self):
        # The second ISA declares '&'/'+'/'!'; scanning it with the first
        # interchange's '~' terminator could never find its segments.
        reader = RawX12Stream()
        segs = []
        for idx in range(len(STREAM_TWO)):
            segs.extend(reader.feed(STREAM_TWO[idx : idx + 1]))
        reader.close()
        self.assertEqual(len(segs), 14)
        self.assertEqual(segs[7].segment, ISA_501_ALT[:-1])


class BinSegments(unittest.TestCase):
    def _stream_with_payload(self, payload):
        bin_seg = b"BIN*" + str(len(payload)).encode("ascii") + b"*" + payload + b"~"
        return (ISA_401 + "\n").encode("latin-1") + bin_seg + b"IEA*0*000010121~\n"

    def test_payload_may_contain_delimiters_newlines_and_ISA(self):
        payload = b"ISA*00*~\r\nGS*forged~IEA*1*9~\nBIN*2*ab~"
        stream = self._stream_with_payload(payload)
        reader = RawX12Stream()
        segs = reader.feed(stream)
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "BIN", "IEA"])
        bin_seg = segs[1]
        self.assertEqual(
            bin_seg.segment,
            ("BIN*%i*" % len(payload)) + payload.decode("latin-1"),
        )
        self.assertEqual(
            stream[bin_seg.start : bin_seg.end],
            bin_seg.segment.encode("latin-1") + b"~",
        )

    def test_length_counts_bytes_not_characters(self):
        # 10 characters, 20 bytes in UTF-8; BIN01 counts the bytes
        payload = "é" * 10
        data = payload.encode("utf-8")
        stream = self._stream_with_payload(data)
        reader = RawX12Stream()
        segs = reader.feed(stream)
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "BIN", "IEA"])
        self.assertEqual(segs[1].segment, "BIN*20*" + data.decode("latin-1"))
        self.assertEqual(segs[2].segment, "IEA*0*000010121")

    def test_empty_payload(self):
        segs = RawX12Stream().feed(self._stream_with_payload(b""))
        self.assertEqual(_seg_ids(segs), ["ISA", "BIN", "IEA"])
        self.assertEqual(segs[1].segment, "BIN*0*")

    def test_partial_length_field_is_held(self):
        stream = self._stream_with_payload(b"payload")
        cut = 107 + len("BIN*1")  # stops inside the BIN01 length field
        reader = RawX12Stream()
        self.assertEqual(_seg_ids(reader.feed(stream[:cut])), ["ISA"])
        segs = reader.feed(stream[cut:])
        reader.close()
        self.assertEqual(_seg_ids(segs), ["BIN", "IEA"])

    def test_partial_payload_is_held(self):
        payload = b"x" * 50
        stream = self._stream_with_payload(payload)
        cut = 107 + len("BIN*50*") + 10  # 10 payload bytes still missing
        reader = RawX12Stream()
        self.assertEqual(_seg_ids(reader.feed(stream[:cut])), ["ISA"])
        segs = reader.feed(stream[cut:])
        reader.close()
        self.assertEqual(_seg_ids(segs), ["BIN", "IEA"])
        self.assertEqual(segs[0].segment, "BIN*50*" + "x" * 50)

    def test_partial_isa_is_held(self):
        reader = RawX12Stream()
        self.assertEqual(reader.feed(STREAM_401[:50]), [])
        self.assertEqual(reader.feed(STREAM_401[50:105]), [])
        segs = reader.feed(STREAM_401[105:])
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "GS", "ST", "TST", "SE", "GE", "IEA"])

    def test_non_numeric_length_raises(self):
        stream = (ISA_401 + "\n").encode("latin-1") + b"BIN*AB*123~IEA*0*000010121~"
        reader = RawX12Stream()
        self.assertRaises(X12Error, reader.feed, stream)

    def test_oversized_length_field_raises(self):
        stream = (ISA_401 + "\n").encode("latin-1") + b"BIN*" + b"1" * 40
        reader = RawX12Stream()
        self.assertRaises(X12Error, reader.feed, stream)

    def test_length_not_landing_on_terminator_raises(self):
        # Declared length ends inside the payload; the reader must not
        # resynchronize by scanning for a segment name after the '~'.
        stream = (ISA_401 + "\n").encode("latin-1") + b"BIN*5*ab~SE*2*1~IEA*0*000010121~"
        reader = RawX12Stream()
        self.assertRaises(X12Error, reader.feed, stream)

    def test_forged_isa_in_payload_does_not_start_interchange(self):
        payload = b"ISA" + b"\x00" * 103  # looks like an ISA header, inside BIN02
        stream = self._stream_with_payload(payload)
        reader = RawX12Stream()
        segs = reader.feed(stream)
        reader.close()
        self.assertEqual(_seg_ids(segs), ["ISA", "BIN", "IEA"])


class StreamEnd(unittest.TestCase):
    def test_close_clean(self):
        reader = RawX12Stream()
        reader.feed(STREAM_TWO)
        reader.close()
        reader.close()

    def test_close_mid_interchange_raises(self):
        reader = RawX12Stream()
        reader.feed((ISA_401 + "GS*HC*ZZ000~").encode("latin-1"))
        self.assertRaises(X12Error, reader.close)

    def test_close_mid_isa_raises(self):
        reader = RawX12Stream()
        reader.feed(STREAM_401[:60])
        self.assertRaises(X12Error, reader.close)

    def test_close_mid_payload_raises(self):
        reader = RawX12Stream()
        reader.feed((ISA_401 + "\n").encode("latin-1") + b"BIN*10*abc")
        self.assertRaises(X12Error, reader.close)

    def test_feed_after_close_raises(self):
        reader = RawX12Stream()
        reader.feed(STREAM_401)
        reader.close()
        self.assertRaises(X12Error, reader.feed, b"")

    def test_garbage_between_interchanges_raises(self):
        reader = RawX12Stream()
        reader.feed(STREAM_401)
        self.assertRaises(X12Error, reader.feed, b"~~")

    def test_not_starting_with_isa_raises(self):
        reader = RawX12Stream()
        self.assertRaises(X12Error, reader.feed, b"GS*HC~")

    def test_unknown_icvn_raises(self):
        bad = ISA_401.replace("00401", "00101")
        reader = RawX12Stream()
        self.assertRaises(X12Error, reader.feed, bad.encode("latin-1"))


if __name__ == "__main__":
    unittest.main()
