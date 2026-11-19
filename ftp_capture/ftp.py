"""FTP control-channel parsing.

Reassembled streams are split into commands and replies; replies are
then paired to commands by capture time (FTP control channels are
lockstep), which keeps a delayed ``226`` or a multiline ``227`` from
being attributed to the wrong command.
"""

import re
from dataclasses import dataclass
from dataclasses import field

_PASV_RE = re.compile(
    rb"(?P<h1>\d{1,3}),(?P<h2>\d{1,3}),(?P<h3>\d{1,3}),(?P<h4>\d{1,3}),"
    rb"(?P<p1>\d{1,3}),(?P<p2>\d{1,3})"
)
_EPSV_RE = re.compile(rb"\(\|\|\|(?P<port>\d+)\|\)")
_PORT_RE = re.compile(
    rb"(?P<h1>\d{1,3}),(?P<h2>\d{1,3}),(?P<h3>\d{1,3}),(?P<h4>\d{1,3}),"
    rb"(?P<p1>\d{1,3}),(?P<p2>\d{1,3})"
)
_EPRT_RE = re.compile(rb"\|(?P<proto>[12])\|(?P<addr>[^|]+)\|(?P<port>\d+)\|")
_COMMAND_RE = re.compile(r"^([A-Za-z]{3,4})(?:[ \t]+(.*))?$", re.S)
_REPLY_RE = re.compile(rb"^(\d{3})([ -])(.*)$")
_MAX_COMMAND_LINE = 8192

COMMANDS = {
    "ABOR",
    "ACCT",
    "ADAT",
    "ALLO",
    "APPE",
    "AUTH",
    "CCC",
    "CDUP",
    "CONF",
    "CWD",
    "DELE",
    "ENC",
    "EPRT",
    "EPSV",
    "FEAT",
    "HELP",
    "HOST",
    "LANG",
    "LIST",
    "LPRT",
    "LPSV",
    "MDTM",
    "MFF",
    "MFMT",
    "MIC",
    "MKD",
    "MLSD",
    "MLST",
    "MODE",
    "NLST",
    "NOOP",
    "OPTS",
    "PASS",
    "PASV",
    "PBSZ",
    "PORT",
    "PROT",
    "PWD",
    "QUIT",
    "REIN",
    "REST",
    "RETR",
    "RMD",
    "RNFR",
    "RNTO",
    "SITE",
    "SIZE",
    "SMNT",
    "STAT",
    "STOR",
    "STOU",
    "STRU",
    "SYST",
    "TYPE",
    "USER",
    "XCUP",
    "XCWD",
    "XMKD",
    "XPWD",
    "XRMD",
}

NEGOTIATION_COMMANDS = {"PASV", "EPSV", "PORT", "EPRT"}
TRANSFER_COMMANDS = {"RETR", "STOR", "STOU", "APPE", "LIST", "NLST", "MLSD"}


@dataclass
class Command:
    index: int
    offset: int
    ts: float
    frame: int
    name: str
    argument: str
    raw: bytes

    @property
    def verb(self):
        return self.name.upper()


@dataclass
class Reply:
    offset: int
    ts: float
    frame: int
    code: int
    text: str
    final: bool
    raw: bytes

    @property
    def is_positive_preliminary(self):
        return self.code in (125, 150)


@dataclass
class Exchange:
    command: Command = None
    replies: list = field(default_factory=list)

    @property
    def final(self):
        for reply in reversed(self.replies):
            if reply.final:
                return reply
        return None

    @property
    def preliminary(self):
        for reply in self.replies:
            if reply.is_positive_preliminary:
                return reply
        return None

    @property
    def code(self):
        final = self.final
        return final.code if final else None

    @property
    def last_ts(self):
        if self.replies:
            return self.replies[-1].ts
        return self.command.ts if self.command else None


def looks_like_control(text_bytes, side):
    """Heuristic: does this reassembled direction carry FTP control lines?

    Lines touching a capture gap are filled with NUL bytes by reassembly;
    they are skipped rather than used to disqualify the stream.
    """

    if not text_bytes:
        return False
    lines = text_bytes.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines or len(lines) > 100000:
        return False
    if side == "commands":
        seen = 0
        known = 0
        for raw in lines:
            if b"\x00" in raw:
                continue
            if len(raw) > _MAX_COMMAND_LINE:
                return False
            stripped = raw.rstrip(b"\r")
            if not stripped.strip():
                continue
            seen += 1
            match = _COMMAND_RE.match(stripped.decode("utf-8", "replace"))
            if not match:
                return False
            if match.group(1).upper() in COMMANDS:
                known += 1
        return seen > 0 and known / seen >= 0.75
    return False


def looks_like_replies(text_bytes):
    if not text_bytes:
        return False
    lines = text_bytes.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        return False
    codes = 0
    valid = 0
    for raw in lines:
        if b"\x00" in raw:
            continue
        stripped = raw.rstrip(b"\r")
        if not stripped:
            continue
        valid += 1
        if _REPLY_RE.match(stripped):
            codes += 1
        elif codes:
            # Continuation text of a multiline reply.
            continue
        else:
            return False
    return codes > 0 and codes / valid >= 0.6


def _iter_lines(data):
    offset = 0
    while offset < len(data):
        end = data.find(b"\n", offset)
        if end == -1:
            end = len(data)
        raw = data[offset:end]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        yield raw, offset
        offset = end + 1


def parse_commands(direction):
    """Turn a client stream into :class:`Command` records."""

    commands = []
    for raw, offset in _iter_lines(direction.data):
        if not raw.strip():
            continue
        text = raw.decode("utf-8", "replace")
        match = _COMMAND_RE.match(text)
        if match:
            name, argument = match.group(1), match.group(2) or ""
        else:
            name, argument = text, ""
        commands.append(
            Command(
                index=len(commands),
                offset=offset,
                ts=direction.ts_at(offset),
                frame=direction.frame_at(offset),
                name=name,
                argument=argument,
                raw=raw,
            )
        )
    return commands


def parse_replies(direction):
    """Turn a server stream into :class:`Reply` records.

    ``ddd-`` continuation lines stay inside one multiline reply and the
    reply closes only on a matching ``ddd `` line.
    """

    replies = []
    pending = None
    for raw, offset in _iter_lines(direction.data):
        match = _REPLY_RE.match(raw)
        if pending is not None:
            if (
                match
                and match.group(2) == b" "
                and int(match.group(1)) == pending["code"]
            ):
                pending["parts"].append(
                    match.group(3).decode("utf-8", "replace")
                )
                pending["raw"].append(raw)
                replies.append(
                    Reply(
                        offset=pending["offset"],
                        ts=direction.ts_at(pending["offset"]),
                        frame=direction.frame_at(pending["offset"]),
                        code=pending["code"],
                        text="\n".join(pending["parts"]),
                        final=True,
                        raw=b"\n".join(pending["raw"]),
                    )
                )
                pending = None
            elif match and match.group(2) == b"-":
                pending["parts"].append(raw.decode("utf-8", "replace"))
                pending["raw"].append(raw)
            else:
                pending["parts"].append(raw.decode("utf-8", "replace"))
                pending["raw"].append(raw)
            continue
        if not match:
            if replies:
                replies[-1].text += "\n" + raw.decode("utf-8", "replace")
            continue
        code = int(match.group(1))
        if match.group(2) == b"-":
            pending = {
                "code": code,
                "offset": offset,
                "parts": [match.group(3).decode("utf-8", "replace")],
                "raw": [raw],
            }
        else:
            replies.append(
                Reply(
                    offset=offset,
                    ts=direction.ts_at(offset),
                    frame=direction.frame_at(offset),
                    code=code,
                    text=match.group(3).decode("utf-8", "replace"),
                    final=True,
                    raw=raw,
                )
            )
    if pending is not None:
        replies.append(
            Reply(
                offset=pending["offset"],
                ts=direction.ts_at(pending["offset"]),
                frame=direction.frame_at(pending["offset"]),
                code=pending["code"],
                text="\n".join(pending["parts"]),
                final=False,
                raw=b"\n".join(pending["raw"]),
            )
        )
    return replies


def pair_exchanges(commands, replies):
    """Pair each reply to the last command issued at or before its time."""

    exchanges = [Exchange(command=command) for command in commands]
    greeting = []
    for reply in replies:
        target = None
        for exchange in exchanges:
            if exchange.command.ts <= reply.ts + 1e-9:
                target = exchange
            else:
                break
        if target is None:
            greeting.append(reply)
        else:
            target.replies.append(reply)
    return exchanges, greeting


def parse_pasv(text):
    if isinstance(text, str):
        text = text.encode("utf-8", "replace")
    match = _PASV_RE.search(text)
    if not match:
        return None
    host = ".".join(
        match.group(name).decode() for name in ("h1", "h2", "h3", "h4")
    )
    port = int(match.group("p1")) * 256 + int(match.group("p2"))
    if port == 0 or port > 65535:
        return None
    return host, port


def parse_epsv(text):
    if isinstance(text, str):
        text = text.encode("utf-8", "replace")
    match = _EPSV_RE.search(text)
    if not match:
        return None
    port = int(match.group("port"))
    if port == 0 or port > 65535:
        return None
    return port


def parse_port(text):
    if isinstance(text, str):
        text = text.encode("utf-8", "replace")
    match = _PORT_RE.search(text)
    if not match:
        return None
    host = ".".join(
        match.group(name).decode() for name in ("h1", "h2", "h3", "h4")
    )
    port = int(match.group("p1")) * 256 + int(match.group("p2"))
    if port == 0 or port > 65535:
        return None
    return host, port


def parse_eprt(text):
    if isinstance(text, str):
        text = text.encode("utf-8", "replace")
    match = _EPRT_RE.search(text)
    if not match:
        return None
    port = int(match.group("port"))
    if port == 0 or port > 65535:
        return None
    return match.group("addr").decode(), port
