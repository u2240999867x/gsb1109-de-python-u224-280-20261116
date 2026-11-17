#! /usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
##  DendroPy Phylogenetic Computing Library.
##
##  Copyright 2010-2026 Jeet Sukumaran, Mark T. Holder, and Matthew Andres Moreno.
##  All rights reserved.
##
##  See "LICENSE.rst" for terms and conditions of usage.
##
##  If you use this work or any portion thereof in published work,
##  please cite it as:
##
##     Moreno, M. A., Holder, M. T., & Sukumaran, J. (2024). DendroPy 5: a
##     mature Python library for phylogenetic computing. Journal of Open
##     Source Software, 9(101), 6943, https://doi.org/10.21105/joss.06943
##
##############################################################################

from dendropy.utility import error
import hashlib

def _stream_is_seekable(stream):
    try:
        return bool(stream.seekable())
    except AttributeError:
        return hasattr(stream, "seek") and hasattr(stream, "tell")

def _prefix_hasher(stream, end_offset):
    """
    Returns a fresh hasher primed with the first ``end_offset`` characters of
    ``stream`` (whose current position is preserved), or |None|.
    """
    if end_offset is None or end_offset <= 0:
        return None
    if not _stream_is_seekable(stream):
        return None
    position = stream.tell()
    try:
        stream.seek(0)
        text = stream.read(int(end_offset))
    finally:
        stream.seek(position)
    hasher = hashlib.sha256()
    hasher.update(text.encode("utf-8", "surrogatepass"))
    return hasher

def _prefix_digest(stream, end_offset):
    hasher = _prefix_hasher(stream, end_offset)
    if hasher is None:
        return None
    return hasher.hexdigest()

##############################################################################
## Tokenizer

class Tokenizer(object):
    """
    Stream tokenizer.
    """

    class TokenizerError(error.DataParseError):

        def __init__(self,
                message=None,
                line_num=None,
                col_num=None,
                stream=None):
            error.DataParseError.__init__(self,
                    message=message,
                    line_num=line_num,
                    col_num=col_num,
                    stream=stream)

    class UnterminatedQuoteError(TokenizerError):

        def __init__(self,
                quote_char=None,
                line_num=None,
                col_num=None,
                stream=None):
            Tokenizer.TokenizerError.__init__(self,
                    message="Unterminated quote: {}".format(quote_char),
                    line_num=line_num,
                    col_num=col_num,
                    stream=stream)

    class UnexpectedEndOfStreamError(TokenizerError):

        def __init__(self,
                message=None,
                line_num=None,
                col_num=None,
                stream=None):
            Tokenizer.TokenizerError.__init__(self,
                    message=message,
                    line_num=line_num,
                    col_num=col_num,
                    stream=stream)

    def __init__(self,
            src,                        # source stream
            uncaptured_delimiters,      # delimiters between tokens (not returned)
            captured_delimiters,        # delimiters between tokens (returned as tokens)
            quote_chars,                # characters enclosing literals
            escape_quote_by_doubling,   # should two consecutive quote characters indicate a literal character (rather than a quote)?
            escape_chars,               # characters indicating beginning of escaped character
            comment_begin,              # string indicating beginning of comment
            comment_end,                # string indicating end of comment
            capture_comments,           # are comments to be stored?
            preserve_unquoted_underscores,       # are unquoted underscores to be preserved
            ):
        # Tokenizer behavior customization
        self.uncaptured_delimiters = uncaptured_delimiters
        self.captured_delimiters = captured_delimiters
        self.quote_chars = quote_chars
        self.escape_quote_by_doubling = escape_quote_by_doubling
        self.escape_chars = escape_chars
        self.comment_begin = comment_begin
        self.comment_end = comment_end
        self.capture_comments = capture_comments
        self.preserve_unquoted_underscores = preserve_unquoted_underscores

        # State (internals)
        self.src = src
        self._cur_char = None
        self.current_token = None
        self.is_token_quoted = False

        # Meta-information
        self.captured_comments = []
        self.current_line_num = 1
        self.current_column_num = 0
        self.token_line_num = 0
        self.token_column_num = 0
        self.char_offset = 0
        self.token_char_offset = 0
        self.token_stream_position = None
        self.previous_token = None
        self.previous_token_position = None
        self._char_stream_position = None
        self.last_semicolon_offset = None
        self.last_semicolon_line_num = None
        self.last_semicolon_column_num = None
        self.last_semicolon_stream_position = None
        self.prefix_hasher = hashlib.sha256()
        self.last_semicolon_digest = None

    def reset(self):
        self.set_stream(src=None)

    def set_stream(self, src=None):
        self.src = src
        self._cur_char = None
        self.current_token = None
        self.is_token_quoted = False
        self.captured_comments = []
        self.current_line_num = 1
        self.current_column_num = 0
        self.token_line_num = 0
        self.token_column_num = 0
        self.char_offset = 0
        self.token_char_offset = 0
        self.token_stream_position = None
        self.previous_token = None
        self.previous_token_position = None
        self._char_stream_position = None
        self.last_semicolon_offset = None
        self.last_semicolon_line_num = None
        self.last_semicolon_column_num = None
        self.last_semicolon_stream_position = None
        self.prefix_hasher = hashlib.sha256()
        self.last_semicolon_digest = None

    def prefix_digest(self):
        """
        Digest of every character consumed so far (exact, not a window).

        Positions can be tied to this value so that resuming from a checkpoint
        fails loudly if the source prefix changed.
        """
        return self.prefix_hasher.hexdigest()

    def _tell(self):
        try:
            return self.src.tell()
        except (AttributeError, OSError, IOError):
            return None

    def set_position(self,
            src,
            char_offset,
            line_num=1,
            column_num=1,
            stream_position=None,
            captured_comments=None):
        """
        Positions the tokenizer at a previously recorded position.

        ``char_offset`` is a zero-based character index; ``line_num`` and
        ``column_num`` are the one-based location of the next character to be
        read.  If ``stream_position`` is given, it is used to seek the
        underlying stream (this is the form that remains valid for text-mode
        files, where the opaque value returned by ``tell()`` must be used).
        """
        if not _stream_is_seekable(src):
            raise ValueError("Cannot resume from a position on a non-seekable source")
        if stream_position is not None:
            src.seek(stream_position)
        else:
            src.seek(char_offset)
        self.set_stream(src)
        self.char_offset = int(char_offset)
        self.current_line_num = line_num
        self.current_column_num = max(0, int(column_num) - 1)
        # Prime the running digest with everything before the cursor so that
        # checkpoints created by the resumed run remain exact.
        primed = _prefix_hasher(src, self.char_offset)
        if primed is not None:
            self.prefix_hasher = primed
        if captured_comments:
            self.captured_comments.extend(captured_comments)

    @property
    def position(self):
        """
        Zero-based character offset of the next character to be read.
        """
        return self.char_offset

    def tell(self):
        return self.char_offset

    @property
    def next_position(self):
        """
        ``(char_offset, line_num, column_num, stream_position)`` of the next
        character to be read.

        The column value is chosen so that restoring the position with
        :meth:`set_position` reproduces the same internal state.
        """
        return (
                self.char_offset,
                self.current_line_num,
                self.current_column_num + 1,
                self._tell())

    @property
    def token_position(self):
        """
        ``(char_offset, line_num, column_num, stream_position)`` of the start
        of the current token.
        """
        return (
                self.token_char_offset,
                self.token_line_num,
                self.token_column_num,
                self.token_stream_position)

    @property
    def last_semicolon_position(self):
        """
        ``(char_offset, line_num, column_num, stream_position)`` immediately
        after the most recently read semi-colon, or |None|.
        """
        if self.last_semicolon_offset is None:
            return None
        return (
                self.last_semicolon_offset,
                self.last_semicolon_line_num,
                self.last_semicolon_column_num,
                self.last_semicolon_stream_position)

    def digest_at_offset(self, offset):
        """
        Exact digest of the source prefix of length ``offset``.

        This seeks in the underlying source (restoring the current position),
        so it is meant for establishing or validating a checkpoint rather than
        for per-character bookkeeping.
        """
        return _prefix_digest(self.src, offset)

    def is_eof(self):
        return self._cur_char == ""

    def has_captured_comments(self):
        return len(self.captured_comments) > 0

    def next_token(self):
        try:
            t = self.__next__()
            return t
        except StopIteration:
            self.current_token = None
            return None

    def require_next_token(self):
        try:
            t = self.__next__()
            return t
        except StopIteration:
            raise Tokenizer.UnexpectedEndOfStreamError(
                            message="Unexpected end of stream",
                            line_num=self.current_line_num,
                            col_num=self.current_column_num,
                            stream=self.src) from None

    def clear_captured_comments(self):
        del self.captured_comments[:]

    def pull_captured_comments(self):
        if not self.captured_comments:
            return None
        c = self.captured_comments[:]
        del self.captured_comments[:]
        return c

    def __iter__(self):
        return self

    def __next__(self):
        self.previous_token = self.current_token
        self.previous_token_position = self.token_position
        self.is_token_quoted = False
        if self._cur_char is None:
            self._get_next_char()
        self._skip_to_significant_char()
        if self._cur_char == "":
            raise StopIteration
        if self._cur_char in self.captured_delimiters:
            self.current_token = self._cur_char
            self.token_line_num = self.current_line_num
            self.token_column_num = self.current_column_num
            self.token_char_offset = max(0, self.char_offset - 1)
            self.token_stream_position = self._char_stream_position
            if self.current_token == ";":
                # Record the position immediately after the semi-colon (this
                # is where a resumed parse returns to the token stream).
                self.last_semicolon_offset = self.char_offset
                self.last_semicolon_line_num = self.current_line_num
                self.last_semicolon_column_num = self.current_column_num + 1
                self.last_semicolon_stream_position = self._tell()
                self.last_semicolon_digest = self.prefix_hasher.hexdigest()
            self._get_next_char()
            return self.current_token
        elif self._cur_char in self.quote_chars:
            self.token_line_num = self.current_line_num
            self.token_column_num = self.current_column_num
            self.token_char_offset = max(0, self.char_offset - 1)
            self.token_stream_position = self._char_stream_position
            dest = []
            self.is_token_quoted = True
            cur_quote_char = self._cur_char
            self._get_next_char()
            while True:
                if self._cur_char == "":
                    raise Tokenizer.UnterminatedQuoteError(
                            quote_char=cur_quote_char,
                            line_num=self.current_line_num,
                            col_num=self.current_column_num,
                            stream=self.src)
                if self._cur_char == cur_quote_char:
                    self._get_next_char()
                    if self.escape_quote_by_doubling:
                        if self._cur_char == cur_quote_char:
                            # dest.write(cur_quote_char)
                            dest.append(cur_quote_char)
                            self._get_next_char()
                        else:
                            break
                    else:
                        self._get_next_char()
                        break
                else:
                    # dest.write(self._cur_char)
                    dest.append(self._cur_char)
                    self._get_next_char()
            # self.current_token = dest.getvalue()
            self.current_token = "".join(dest)
            return self.current_token
        else:
            # unquoted
            self.token_line_num = self.current_line_num
            self.token_column_num = self.current_column_num
            self.token_char_offset = max(0, self.char_offset - 1)
            self.token_stream_position = self._char_stream_position
            dest = []
            self.is_token_quoted = False
            while self._cur_char != "":
                if self._cur_char in self.uncaptured_delimiters:
                    self._get_next_char()
                    break
                elif self._cur_char in self.captured_delimiters:
                    break
                elif self._cur_char in self.comment_begin:
                    self._handle_comment()
                    if self._cur_char == "":
                        break
                else:
                    if self._cur_char == "_" and not self.preserve_unquoted_underscores:
                        self._cur_char = " "
                    dest.append(self._cur_char)
                    self._get_next_char()
            # self.current_token = dest.getvalue()
            self.current_token = "".join(dest)
            if self.current_token == "":
                if self._cur_char != "":
                    self.__next__()
                else:
                    raise StopIteration
            return self.current_token

    def _skip_to_significant_char(self):
        if self._cur_char == "":
            return
        if self._cur_char is None:
            self._get_next_char()
        if self._cur_char not in self.uncaptured_delimiters:
            return
        while self._cur_char != "" and self._cur_char in self.uncaptured_delimiters:
            self._get_next_char()
        return

    def _get_next_char(self):
        self._char_stream_position = self._tell()
        self._cur_char = self.src.read(1)
        if self._cur_char != "":
            self.char_offset += 1
            self.prefix_hasher.update(self._cur_char.encode("utf-8", "surrogatepass"))
            if self._cur_char == "\n":
                self.current_line_num += 1
                self.current_column_num = 1
            else:
                # print("@@@ {}: {}".format(self.current_column_num, self._cur_char))
                self.current_column_num += 1
        return self._cur_char

    def _handle_comment(self):
        dest = []
        nesting = 0
        comment_complete = False
        while self._cur_char != "":
            if self._cur_char in self.comment_end:
                nesting -= 1
                if nesting <= 0:
                    comment_complete = True
                    self._get_next_char()
                    break
            elif self._cur_char in self.comment_begin:
                nesting += 1
            elif self.capture_comments:
                # dest.write(self._cur_char)
                dest.append(self._cur_char)
            self._get_next_char()
        if self.capture_comments:
            # self.captured_comments.append(dest.getvalue())
            self.captured_comments.append("".join(dest))

