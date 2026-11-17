"""Regression corpus for CIF text output.

Each case stores the input text, the input grammar, the value that the CIF
grammar assigns to that text, the output grammar and the exact token that the
writer must emit.  The expected value is derived from the CIF productions, not
from reading back the writer's own output.  `decode_token` below is a small
independent decoder for the folding and prefix protocols; it is used to check
that every expected token really denotes the expected value.
"""

import re
import unittest

from CifFile import CifBlock, CifFile
from CifFile.StarFile import StarDict, StarError, StarList


def _remove_prefix(text):
    match = re.match(r"(?P<prefix>[^;\\\n][^\n\\]+)(?P<folding>\\{1,2}[ \t\v\f]*\n)", text)
    if match is None:
        return text
    prefix = match.group("prefix")
    if match.group("folding")[:2] == "\\\\":
        return "\\" + text[match.end("folding") - 1:].replace("\n" + prefix, "\n")
    return text[match.end("folding") - 1:].replace("\n" + prefix, "\n")[1:]


def _remove_folding(text):
    if re.match(r"\\[ \t\v\f]*\n", text) is None:
        return text
    return re.sub(r"\\[ \t\v\f]*$\n?", "", text, flags=re.M)


def decode_token(token):
    if token.startswith("'''") or token.startswith('"""'):
        quote = token[:3]
        assert token.endswith(quote), token
        return token[3:-3]
    if token[:1] in ("'", '"'):
        quote = token[0]
        assert token.endswith(quote), token
        return token[1:-1]
    if not token.startswith("\n;"):
        assert "\n" not in token, token
        return token
    assert token.endswith("\n;"), token
    return _remove_folding(_remove_prefix(token[2:-2]))


SCALAR_CASES = [
    ("plain", "data_t\n_a alpha\n", "1.1", "alpha", "1.1", 2048, 80,
     "\n_a                                      alpha"),
    ("plain_cif10", "data_t\n_a alpha\n", "1.0", "alpha", "1.0", 2048, 80,
     "\n_a                                      alpha"),
    ("plain_cif2", "data_t\n_a alpha\n", "1.1", "alpha", "2.0", 2048, 80,
     "\n_a                                      alpha"),
    ("apostrophe", "data_t\n_a \"it's\"\n", "1.1", "it's", "1.1", 2048, 80,
     "\n_a                                      \"it's\""),
    ("double_quote", "data_t\n_a 'say \"hi\"'\n", "1.1", 'say "hi"', "1.1", 2048, 80,
     "\n_a                                      'say \"hi\"'"),
    ("both_quotes", "data_t\n_a\n;both ' \"\n;\n", "1.1", "both ' \"", "1.1", 2048, 80,
     "\n_a                                      \n;both ' \"\n;"),
    ("triple_quote_literal", "#\\#CIF_2.0\ndata_t\n_a \"'''\"\n", "2.0", "'''", "2.0", 2048, 80,
     "\n_a                                      \"'''\""),
    ("semicolon_first_line", "data_t\n_a\n;;alpha\nbeta\n;\n", "1.1",
     ";alpha\nbeta", "1.1", 2048, 80,
     "\n_a                                      \n;;alpha\nbeta\n;"),
    ("semicolon_second_line_cif2",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> alpha\nCIF:> ;beta\n;\n",
     "2.0", "alpha\n;beta", "2.0", 2048, 80,
     "\n_a                                      \n;CIF:> \\\nCIF:> alpha\nCIF:> ;beta\n;"),
    ("hash_second_line_cif2",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> alpha\nCIF:> #beta\n;\n",
     "2.0", "alpha\n#beta", "2.0", 2048, 80,
     "\n_a                                      \n;CIF:> \\\nCIF:> alpha\nCIF:> #beta\n;"),
    ("leading_newline", "data_t\n_a\n;\nalpha\n;\n", "1.1", "\nalpha", "1.1", 2048, 80,
     "\n_a                                      \n;\nalpha\n;"),
    ("trailing_newline", "data_t\n_a\n;alpha\n\n;\n", "1.1", "alpha\n", "1.1", 2048, 80,
     "\n_a                                      \n;alpha\n\n;"),
    ("both_newlines", "data_t\n_a\n;\nalpha\n\n;\n", "1.1", "\nalpha\n", "1.1", 2048, 80,
     "\n_a                                      \n;\nalpha\n\n;"),
    ("two_blank_edges", "data_t\n_a\n;\n\nalpha\n\n\n;\n", "1.1",
     "\n\nalpha\n\n", "1.1", 2048, 80,
     "\n_a                                      \n;\n\nalpha\n\n\n;"),
    ("only_newline", "data_t\n_a\n;\n\n;\n", "1.1", "\n", "1.1", 2048, 80,
     "\n_a                                      \n;\n\n;"),
    ("only_two_newlines", "data_t\n_a\n;\n\n\n;\n", "1.1", "\n\n", "1.1", 2048, 80,
     "\n_a                                      \n;\n\n\n;"),
    ("semicolon_only", "data_t\n_a ';'\n", "1.1", ";", "1.1", 2048, 80,
     "\n_a                                      ';'"),
    ("empty", "data_t\n_a ''\n", "1.1", "", "1.1", 2048, 80,
     "\n_a                                      ''"),
    ("padded", "data_t\n_a '  padded  '\n", "1.1", "  padded  ", "1.1", 2048, 80,
     "\n_a                                      '  padded  '"),
    ("hash_first_line", "data_t\n_a\n;#note\ntext\n;\n", "1.1", "#note\ntext", "1.1", 2048, 80,
     "\n_a                                      \n;#note\ntext\n;"),
    ("reserved_stop", "data_t\n_a 'stop_'\n", "1.1", "stop_", "1.1", 2048, 80,
     "\n_a                                      'stop_'"),
    ("reserved_loop", "data_t\n_a loop_\n", "1.1", "loop_", "1.1", 2048, 80,
     "\n_a                                      'loop_'"),
    ("reserved_data", "data_t\n_a 'data_x'\n", "1.1", "data_x", "1.1", 2048, 80,
     "\n_a                                      'data_x'"),
    ("reserved_global", "data_t\n_a 'global_x'\n", "1.1", "global_x", "1.1", 2048, 80,
     "\n_a                                      'global_x'"),
    ("reserved_save", "data_t\n_a 'save_x'\n", "1.1", "save_x", "1.1", 2048, 80,
     "\n_a                                      'save_x'"),
    ("first_line_backslash", "#\\#CIF_2.0\ndata_t\n_a '''\\\nalpha'''\n", "2.0",
     "\\\nalpha", "1.1", 2048, 80,
     "\n_a                                      \n;\\\n\\\\\n\nalpha\n;"),
    ("inner_backslash", "#\\#CIF_2.0\ndata_t\n_a '''alpha\\\nbeta'''\n", "2.0",
     "alpha\\\nbeta", "1.1", 2048, 80,
     "\n_a                                      \n;\\\nalpha\\\\\n\nbeta\n;"),
    ("inner_backslash_space", "#\\#CIF_2.0\ndata_t\n_a '''alpha\\ \nbeta'''\n", "2.0",
     "alpha\\ \nbeta", "1.1", 2048, 80,
     "\n_a                                      \n;\\\nalpha\\ \\\n\nbeta\n;"),
    ("fold_long_word", "#\\#CIF_2.0\ndata_t\n_a '''abcdefghijklmnopqrst'''\n", "2.0",
     "abcdefghijklmnopqrst", "1.1", 10, 10,
     "\n_a                                      \n;\\\nabcdefghi\\\njklmnopqr\\\nst\n;"),
    ("cr_cif2", "#\\#CIF_2.0\ndata_t\n_a '''a\r'''\n", "2.0", "a\r", "2.0", 2048, 80,
     "\n_a                                      '''a\r'''"),
    ("crlf_plain", "#\\#CIF_2.0\ndata_t\n_a '''alpha\r\nbeta'''\n", "2.0",
     "alpha\r\nbeta", "1.1", 2048, 80,
     "\n_a                                      \n;alpha\r\nbeta\n;"),
    ("crlf_semicolon_cif2", "#\\#CIF_2.0\ndata_t\n_a '''alpha\r\n;beta'''\n", "2.0",
     "alpha\r\n;beta", "2.0", 2048, 80,
     "\n_a                                      '''alpha\r\n;beta'''"),
    ("prefix_collision",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF::> \\\nCIF::> alpha\nCIF::> CIF:> beta\nCIF::> ;gamma\n;\n",
     "2.0", "alpha\nCIF:> beta\n;gamma", "2.0", 2048, 80,
     "\n_a                                      \n;CIF::> \\\nCIF::> alpha\nCIF::> CIF:> beta\nCIF::> ;gamma\n;"),
    ("prefix_leading_blank",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> \nCIF:> ;alpha\n;\n",
     "2.0", "\n;alpha", "2.0", 2048, 80,
     "\n_a                                      \n;CIF:> \\\nCIF:> \nCIF:> ;alpha\n;"),
    ("prefix_trailing_blank",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> alpha\nCIF:> ;beta\nCIF:> \n;\n",
     "2.0", "alpha\n;beta\n", "2.0", 2048, 80,
     "\n_a                                      \n;CIF:> \\\nCIF:> alpha\nCIF:> ;beta\nCIF:> \n;"),
    ("prefix_with_folding",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\\\nCIF:> \\\\\nCIF:> \nCIF:> ;alpha\n;\n",
     "2.0", "\\\n;alpha", "2.0", 2048, 80,
     "\n_a                                      \n;CIF:> \\\\\nCIF:> \\\\\nCIF:> \nCIF:> ;alpha\n;"),
]


LOOP_CASES = [
    ("loop_mixed_lines",
     "data_t\nloop_\n_a\n';alpha'\n;beta\n\n;\n;\ngamma\n;\n", "1.1",
     [";alpha", "beta\n", "\ngamma"], "1.1", 2048, 80,
     "\nloop_\n  _a\n         ';alpha'  \n         \n;beta\n\n;        \n         \n;\ngamma\n; "),
    ("loop_quotes",
     "data_t\nloop_\n_a\n\"it's\"\n'say \"hi\"'\n;both ' \"\n;\n", "1.1",
     ["it's", 'say "hi"', "both ' \""], "1.1", 2048, 80,
     "\nloop_\n  _a\n         \"it's\"    \n         'say \"hi\"'          \n         \n;both ' \"\n; "),
    ("loop_prefix_cif2",
     "#\\#CIF_2.0\ndata_t\nloop_\n_a\n;CIF:> \\\nCIF:> alpha\nCIF:> ;beta\n;\nplain\n",
     "2.0", ["alpha\n;beta", "plain"], "2.0", 2048, 80,
     "\nloop_\n  _a\n         \n;CIF:> \\\nCIF:> alpha\nCIF:> ;beta\n;        \n         plain "),
    ("loop_hash_cif2",
     "#\\#CIF_2.0\ndata_t\nloop_\n_a\n;CIF:> \\\nCIF:> alpha\nCIF:> #beta\n;\nplain\n",
     "2.0", ["alpha\n#beta", "plain"], "2.0", 2048, 80,
     "\nloop_\n  _a\n         \n;CIF:> \\\nCIF:> alpha\nCIF:> #beta\n;        \n         plain "),
    ("loop_cr_cif2", "#\\#CIF_2.0\ndata_t\nloop_\n_a\n'''a\r'''\n", "2.0",
     ["a\r"], "2.0", 2048, 80,
     "\nloop_\n  _a\n         '''a\r''' "),
    ("loop_fold_long", "#\\#CIF_2.0\ndata_t\nloop_\n_a\n'''abcdefghijklmnopqrst'''\n", "2.0",
     ["abcdefghijklmnopqrst"], "1.1", 10, 10,
     "\nloop_\n  _a\n         \n;\\\nabcdefghi\\\njklmnopqr\\\nst\n; "),
    ("loop_leading_blank_cif2",
     "#\\#CIF_2.0\ndata_t\nloop_\n_a\n;CIF:> \\\nCIF:> \nCIF:> ;alpha\n;\nplain\n",
     "2.0", ["\n;alpha", "plain"], "2.0", 2048, 80,
     "\nloop_\n  _a\n         \n;CIF:> \\\nCIF:> \nCIF:> ;alpha\n;        \n         plain "),
]


COMPOUND_CASES = [
    ("list_cif2", "#\\#CIF_2.0\ndata_t\n_a [a  b]\n", "2.0",
     StarList(["a", "b"]), "2.0", 2048, 80, "\n_a [a  b]"),
    ("table_cif2", "#\\#CIF_2.0\ndata_t\n_a {'k':v}\n", "2.0",
     StarDict([["k", "v"]]), "2.0", 2048, 80, "\n_a {'k':v}"),
]


REJECT_CASES = [
    ("cif1_second_line_semicolon",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> x\nCIF:> ;alpha\nCIF:> beta\n;\n", "2.0",
     "x\n;alpha\nbeta", "scalar", "1.1"),
    ("cif1_second_line_semicolon_loop",
     "#\\#CIF_2.0\ndata_t\nloop_\n_a\n;CIF:> \\\nCIF:> x\nCIF:> ;alpha\nCIF:> beta\n;\n", "2.0",
     ["x\n;alpha\nbeta"], "loop", "1.1"),
    ("cif1_bare_carriage_return", "#\\#CIF_2.0\ndata_t\n_a '''a\r'''\n", "2.0",
     "a\r", "scalar", "1.1"),
    ("cif1_crlf_semicolon_line",
     "#\\#CIF_2.0\ndata_t\n_a '''alpha\r\n;beta'''\n", "2.0",
     "alpha\r\n;beta", "scalar", "1.1"),
    ("cif2_both_triple_delimiters",
     "data_t\n_a\n;x'''\"\"\"\r\r\n;\n", "1.1",
     "x'''\"\"\"\r", "scalar", "2.0"),
    ("cif2_both_triple_delimiters_loop",
     "data_t\nloop_\n_a\n;x'''\"\"\"\r\r\n;\n", "1.1",
     ["x'''\"\"\"\r"], "loop", "2.0"),
    ("cif1_list", "#\\#CIF_2.0\ndata_t\n_a [a  b]\n", "2.0",
     ["a", "b"], "scalar", "1.0"),
    ("cif1_table", "#\\#CIF_2.0\ndata_t\n_a {'k':v}\n", "2.0",
     {"k": "v"}, "scalar", "1.1"),
]


REDUCED_FAILURES = [
    ("#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> x\nCIF:> ;alpha\nCIF:> beta\nCIF:> gamma\n;\n",
     "2.0", "x\n;alpha\nbeta\ngamma",
     "#\\#CIF_2.0\ndata_t\n_a\n;CIF:> \\\nCIF:> x\nCIF:> ;alpha\n;\n",
     "2.0", "x\n;alpha", "1.1"),
]


class TextWriterCorpusTestCase(unittest.TestCase):

    def _read_value(self, source, grammar):
        return CifFile(source, grammar=grammar, from_str=True)["t"]["_a"]

    def _write_block(self, value, kind, grammar, maxoutlength, wraplength):
        cif_block = CifBlock()
        if kind == "loop":
            cif_block.AddItem("_a", value)
            cif_block.CreateLoop(["_a"])
        else:
            cif_block.AddItem("_a", value)
        cif_block.set_grammar(grammar)
        cif_block.SetOutputLength(wraplength, maxoutlength)
        return str(cif_block)

    def test_scalar_corpus(self):
        for name, source, source_grammar, value, grammar, maxoutlength, wraplength, expected in SCALAR_CASES:
            with self.subTest(name=name):
                self.assertEqual(self._read_value(source, source_grammar), value)
                output = self._write_block(value, "scalar", grammar, maxoutlength, wraplength)
                self.assertEqual(output, expected)
                self.assertEqual(decode_token(output.split("_a", 1)[1].lstrip(" ")), value)

    def test_loop_corpus(self):
        for name, source, source_grammar, value, grammar, maxoutlength, wraplength, expected in LOOP_CASES:
            with self.subTest(name=name):
                self.assertEqual(list(self._read_value(source, source_grammar)), value)
                output = self._write_block(value, "loop", grammar, maxoutlength, wraplength)
                self.assertEqual(output, expected)
                tokens = _split_tokens_exact(expected)
                self.assertEqual([decode_token(token) for token in tokens], value)

    def test_compound_corpus(self):
        for name, source, source_grammar, value, grammar, maxoutlength, wraplength, expected in COMPOUND_CASES:
            with self.subTest(name=name):
                parsed = self._read_value(source, source_grammar)
                if isinstance(value, StarDict):
                    self.assertEqual(dict(parsed), dict(value))
                else:
                    self.assertEqual(list(parsed), list(value))
                output = self._write_block(value, "scalar", grammar, maxoutlength, wraplength)
                self.assertEqual(output, expected)

    def _assert_rejected(self, value, kind, grammar):
        with self.assertRaises(StarError) as caught:
            self._write_block(value, kind, grammar, 2048, 80)
        message = str(caught.exception)
        self.assertIn("cannot be represented", message)
        self.assertIn("CIF1" if grammar in ("1.0", "1.1") else "CIF2", message)
        return message

    def test_unrepresentable_values_are_rejected(self):
        for name, source, source_grammar, value, kind, grammar in REJECT_CASES:
            with self.subTest(name=name):
                self.assertEqual(self._read_value(source, source_grammar), value)
                self._assert_rejected(value, kind, grammar)

    def test_reduced_failures_keep_same_difference(self):
        for long_source, long_grammar, long_value, short_source, short_grammar, short_value, output_grammar in REDUCED_FAILURES:
            with self.subTest(short=short_value):
                self.assertEqual(self._read_value(long_source, long_grammar), long_value)
                self.assertEqual(self._read_value(short_source, short_grammar), short_value)
                for value in (long_value, short_value):
                    message = self._assert_rejected(value, "scalar", output_grammar)
                    self.assertIn("CIF1", message)

    def test_cif1_never_uses_cif2_prefix_protocol(self):
        for value in ("alpha\n;beta", "\n;alpha", "x\n;alpha"):
            with self.subTest(value=value):
                self._assert_rejected(value, "scalar", "1.1")
                cif2_output = self._write_block(value, "scalar", "2.0", 2048, 80)
                self.assertIn("CIF:> ", cif2_output)

    def test_rejection_does_not_mutate_value(self):
        value = "alpha\n;beta"
        for _ in range(2):
            self._assert_rejected(value, "scalar", "1.1")
        self.assertEqual(value, "alpha\n;beta")


def _split_tokens_exact(output):
    """Split a one-column loop into the exact value tokens.

    Inter-token whitespace is layout, not value.  A semicolon text token
    starts with the line feed that precedes its opening semicolon and ends
    with the closing semicolon."""
    body = output[output.index("loop_") + len("loop_"):]
    body = body.split("\n", 2)[2]
    tokens = []
    index = 0
    length = len(body)
    while index < length:
        while index < length and body[index] in " \t\n":
            index += 1
        if index >= length:
            break
        if body[index] == ";":
            close = body.find("\n;", index + 1)
            assert close >= 0, body[index:]
            tokens.append("\n" + body[index:close + 2])
            index = close + 2
        elif body[index] in ("'", '"'):
            if body[index:index + 3] in ("'''", '"""'):
                quote = body[index:index + 3]
            else:
                quote = body[index]
            close = body.index(quote, index + len(quote)) + len(quote)
            tokens.append(body[index:close])
            index = close
        else:
            start = index
            while index < length and body[index] not in " \t\n":
                index += 1
            tokens.append(body[start:index])
    return tokens


if __name__ == "__main__":
    unittest.main()
