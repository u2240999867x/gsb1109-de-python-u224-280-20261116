"""Build synthetic FTP captures for tests and replay demonstrations.

The builder emits Ethernet/IPv4/TCP frames for a small FTP world:
several clients, several servers, passive and active data channels, a
reused passive port and dropped channels.  It is deliberately dumb: it
serialises exactly the packets the scenario describes so the correlator
can be tested against ground truth.
"""

import struct
from dataclasses import dataclass
from dataclasses import field

MSS = 1400


def _ip_checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _eth(src_mac, dst_mac, ethertype, payload):
    return dst_mac + src_mac + struct.pack("!H", ethertype) + payload


def _ipv4(src, dst, proto, payload, ident):
    total = 20 + len(payload)
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total,
        ident,
        0x4000,
        64,
        proto,
        0,
        bytes(map(int, src.split("."))),
        bytes(map(int, dst.split("."))),
    )
    checksum = _ip_checksum(header)
    header = header[:10] + struct.pack("!H", checksum) + header[12:]
    return header + payload


def _tcp(sport, dport, seq, ack, flags, payload, window=65535):
    header = struct.pack(
        "!HHIIHHHH",
        sport,
        dport,
        seq % (1 << 32),
        ack % (1 << 32),
        (20 >> 2) << 12 | flags,
        window,
        0,
        0,
    )
    return header + payload


@dataclass
class TcpEnd:
    ip: str
    port: int
    mac: bytes
    seq: int = 0
    ack: int = 0


@dataclass
class World:
    ts: float = 1_700_000_000.0
    ident: int = 1
    frames: list = field(default_factory=list)
    step: float = 0.0005

    def tick(self):
        self.ts += self.step
        return self.ts

    def packet(self, src, dst, seq, ack, flags, payload=b""):
        tcp = _tcp(src.port, dst.port, seq, ack, flags, payload)
        ip = _ipv4(src.ip, dst.ip, 6, tcp, self.ident)
        self.ident = (self.ident + 1) % 65535
        frame = _eth(src.mac, dst.mac, 0x0800, ip)
        self.frames.append((self.tick(), frame))

    def handshake(self, client, server):
        client.seq = 1000
        server.seq = 5000
        self.packet(client, server, client.seq, 0, 0x02)
        client.seq += 1
        self.packet(server, client, server.seq, client.seq, 0x12)
        server.seq += 1
        self.packet(client, server, client.seq, server.seq, 0x10)

    def fin(self, client, server):
        self.packet(client, server, client.seq, server.seq, 0x11)
        client.seq += 1
        self.packet(server, client, server.seq, client.seq, 0x11)
        server.seq += 1
        self.packet(client, server, client.seq, server.seq, 0x10)

    def send(self, src, dst, payload, flags=0x18):
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + MSS]
            self.packet(src, dst, src.seq, dst.seq, flags, chunk)
            src.seq += len(chunk)
            dst.ack = src.seq
            offset += len(chunk)

    def control(self, client, server, command_bytes):
        self.send(client, server, command_bytes)

    def reply(self, server, client, text):
        self.send(server, client, text.encode())


def build_world():
    """Two servers, three clients, mixed active/passive, plus traps."""

    w = World()
    mac_a = b"\x02\x00\x00\x00\x00\x0a"
    mac_s = b"\x02\x00\x00\x00\x00\x05"

    # Session 1: client 10.0.0.10 to server 10.0.0.5, passive, two files.
    c1 = TcpEnd("10.0.0.10", 40001, mac_a)
    s1 = TcpEnd("10.0.0.5", 21, mac_s)
    w.handshake(c1, s1)
    w.reply(s1, c1, "220 test server ready\r\n")
    w.control(c1, s1, b"USER anonymous\r\n")
    w.reply(s1, c1, "331 password please\r\n")
    w.control(c1, s1, b"PASS x@y\r\n")
    w.reply(s1, c1, "230 logged in\r\n")
    w.control(c1, s1, b"PASV\r\n")
    w.reply(s1, c1, "227 Entering passive mode (10,0,0,5,195,80).\r\n")

    # Data connection 1: passive, client opens to server 10.0.0.5:50000
    d1c = TcpEnd("10.0.0.10", 40011, mac_a)
    d1s = TcpEnd("10.0.0.5", 50000, mac_s)
    w.handshake(d1c, d1s)
    w.control(c1, s1, b"RETR a.bin\r\n")
    w.reply(s1, c1, "150 opening\r\n")
    w.send(d1s, d1c, b"A" * 3000)
    w.reply(s1, c1, "226 transfer complete\r\n")
    w.fin(d1c, d1s)

    # Second transfer on a fresh passive port.
    w.control(c1, s1, b"PASV\r\n")
    w.reply(s1, c1, "227 Entering passive mode (10,0,0,5,195,81).\r\n")
    d2c = TcpEnd("10.0.0.10", 40012, mac_a)
    d2s = TcpEnd("10.0.0.5", 50001, mac_s)
    w.handshake(d2c, d2s)
    w.control(c1, s1, b"RETR b.bin\r\n")
    w.reply(s1, c1, "150 opening\r\n")
    w.send(d2s, d2c, b"B" * 2000)
    w.reply(s1, c1, "226 transfer complete\r\n")
    w.fin(d2c, d2s)
    w.control(c1, s1, b"QUIT\r\n")
    w.reply(s1, c1, "221 bye\r\n")
    w.fin(c1, s1)

    # Session 2: another client, active mode, upload; the server
    # initiates the data connection back to the client.
    c2 = TcpEnd("10.0.0.20", 41001, mac_a)
    s2 = TcpEnd("10.0.0.5", 21, mac_s)
    w.handshake(c2, s2)
    w.reply(s2, c2, "220 test server ready\r\n")
    w.control(c2, s2, b"USER bob\r\n")
    w.reply(s2, c2, "331 password please\r\n")
    w.control(c2, s2, b"PASS pw\r\n")
    w.reply(s2, c2, "230 logged in\r\n")
    w.control(c2, s2, b"PORT 10,0,0,20,160,42\r\n")  # port 41002
    w.reply(s2, c2, "200 active ok\r\n")

    d3s = TcpEnd("10.0.0.5", 50002, mac_s)  # server side ephemeral
    d3c = TcpEnd("10.0.0.20", 41002, mac_a)  # client listening port
    d3s.seq = 7000
    d3c.seq = 9000
    w.packet(d3s, d3c, d3s.seq, 0, 0x02)
    d3s.seq += 1
    w.packet(d3c, d3s, d3c.seq, d3s.seq, 0x12)
    d3c.seq += 1
    w.packet(d3s, d3c, d3s.seq, d3c.seq, 0x10)

    w.control(c2, s2, b"STOR up.bin\r\n")
    w.reply(s2, c2, "150 ready\r\n")
    w.send(d3c, d3s, b"U" * 2500)
    w.reply(s2, c2, "226 done\r\n")
    w.fin(d3s, d3c)

    # Failed active negotiation: server refuses the privileged port.
    w.control(c2, s2, b"PORT 10,0,0,20,0,22\r\n")
    w.reply(s2, c2, "501 PORT against the privileged port \"22\" refused.\r\n")
    w.control(c2, s2, b"RETR forbidden.bin\r\n")
    w.reply(s2, c2, "425 Can't open data connection.\r\n")

    # Transfer whose data channel was never captured.
    w.control(c2, s2, b"PASV\r\n")
    w.reply(s2, c2, "227 Entering passive mode (10,0,0,5,195,90).\r\n")
    w.control(c2, s2, b"RETR lost.bin\r\n")
    w.reply(s2, c2, "150 opening\r\n")
    w.reply(s2, c2, "226 transfer complete\r\n")
    w.control(c2, s2, b"QUIT\r\n")
    w.reply(s2, c2, "221 bye\r\n")
    w.fin(c2, s2)

    # Session 3: passive port reuse across sessions.  Session 1 already
    # used server port 50000 and closed; session 3 gets the same port.
    c3 = TcpEnd("10.0.0.30", 42001, mac_a)
    s3 = TcpEnd("10.0.0.5", 21, mac_s)
    w.handshake(c3, s3)
    w.reply(s3, c3, "220 test server ready\r\n")
    w.control(c3, s3, b"USER carol\r\n")
    w.reply(s3, c3, "331 password please\r\n")
    w.control(c3, s3, b"PASS pw\r\n")
    w.reply(s3, c3, "230 logged in\r\n")
    w.control(c3, s3, b"PASV\r\n")
    w.reply(s3, c3, "227 Entering passive mode (10,0,0,5,195,80).\r\n")
    d4c = TcpEnd("10.0.0.30", 42002, mac_a)
    d4s = TcpEnd("10.0.0.5", 50000, mac_s)
    w.handshake(d4c, d4s)
    w.control(c3, s3, b"RETR c.bin\r\n")
    w.reply(s3, c3, "150 opening\r\n")
    w.send(d4s, d4c, b"C" * 1500)
    w.reply(s3, c3, "226 transfer complete\r\n")
    w.fin(d4c, d4s)

    # Data connection on the reused client port: after session 1 closed,
    # client 10.0.0.10 reuses source port 40011 for an unrelated session.
    d5c = TcpEnd("10.0.0.10", 40011, mac_a)
    d5s = TcpEnd("10.0.0.5", 50000, mac_s)
    w.handshake(d5c, d5s)
    w.send(d5s, d5c, b"D" * 700)
    w.fin(d5c, d5s)

    # Control session with an EPSV negotiation and a matching channel.
    w.control(c3, s3, b"EPSV\r\n")
    w.reply(s3, c3, "229 Entering extended passive mode (|||50010|).\r\n")
    d6c = TcpEnd("10.0.0.30", 42003, mac_a)
    d6s = TcpEnd("10.0.0.5", 50010, mac_s)
    w.handshake(d6c, d6s)
    w.control(c3, s3, b"LIST\r\n")
    w.reply(s3, c3, "150 here it comes\r\n")
    w.send(d6s, d6c, b"total 2\r\n-rw-r--r-- 1 x x 3 a\r\n")
    w.reply(s3, c3, "226 done\r\n")
    w.fin(d6c, d6s)
    w.control(c3, s3, b"QUIT\r\n")
    w.reply(s3, c3, "221 bye\r\n")
    w.fin(c3, s3)

    return w


def build():
    return build_world().frames


def build_ambiguous():
    """Two sessions from one client IP negotiate the same passive port.

    Neither session's evidence can claim the single data connection, so
    the correlator must keep it as a candidate and leave its bytes out
    of both sessions.
    """

    w = World()
    mac_c = b"\x02\x00\x00\x00\x00\x0c"
    mac_s = b"\x02\x00\x00\x00\x00\x05"

    sessions = []
    for index, port in enumerate((43001, 43002)):
        client = TcpEnd("10.0.0.50", port, mac_c)
        server = TcpEnd("10.0.0.5", 21, mac_s)
        w.handshake(client, server)
        w.reply(server, client, "220 ready\r\n")
        w.control(client, server, b"USER same\r\n")
        w.reply(server, client, "331 password please\r\n")
        w.control(client, server, b"PASS pw\r\n")
        w.reply(server, client, "230 logged in\r\n")
        sessions.append((client, server))

    # Both sessions open a listener on the same announced port while the
    # other one is still pending.
    first_client, first_server = sessions[0]
    second_client, second_server = sessions[1]
    w.control(first_client, first_server, b"PASV\r\n")
    w.reply(
        first_server,
        first_client,
        "227 Entering passive mode (10,0,0,5,195,200).\r\n",
    )
    w.control(second_client, second_server, b"PASV\r\n")
    w.reply(
        second_server,
        second_client,
        "227 Entering passive mode (10,0,0,5,195,200).\r\n",
    )

    data_client = TcpEnd("10.0.0.50", 43003, mac_c)
    data_server = TcpEnd("10.0.0.5", 50120, mac_s)
    w.handshake(data_client, data_server)
    w.control(first_client, first_server, b"RETR shared.bin\r\n")
    w.reply(first_server, first_client, "150 opening\r\n")
    w.send(data_server, data_client, b"X" * 1234)
    w.reply(first_server, first_client, "226 done\r\n")
    w.fin(data_client, data_server)

    for client, server in sessions:
        w.control(client, server, b"QUIT\r\n")
        w.reply(server, client, "221 bye\r\n")
        w.fin(client, server)

    return w.frames


def build_nat():
    """Passive reply announces a NAT address that never appears on the wire.

    Only the server port connects the data channel to the session, so the
    result must be a candidate, not an exact link.
    """

    w = World()
    mac_c = b"\x02\x00\x00\x00\x00\x0c"
    mac_s = b"\x02\x00\x00\x00\x00\x05"
    client = TcpEnd("10.0.0.60", 44001, mac_c)
    server = TcpEnd("10.0.0.5", 21, mac_s)
    w.handshake(client, server)
    w.reply(server, client, "220 ready\r\n")
    w.control(client, server, b"USER nat\r\n")
    w.reply(server, client, "331 password please\r\n")
    w.control(client, server, b"PASS pw\r\n")
    w.reply(server, client, "230 logged in\r\n")
    w.control(client, server, b"PASV\r\n")
    w.reply(
        server,
        client,
        "227 Entering passive mode (203,0,113,9,196,0).\r\n",
    )

    data_client = TcpEnd("10.0.0.60", 44002, mac_c)
    data_server = TcpEnd("10.0.0.5", 50176, mac_s)
    w.handshake(data_client, data_server)
    w.control(client, server, b"RETR nat.bin\r\n")
    w.reply(server, client, "150 opening\r\n")
    w.send(data_server, data_client, b"N" * 900)
    w.reply(server, client, "226 done\r\n")
    w.fin(data_client, data_server)
    w.control(client, server, b"QUIT\r\n")
    w.reply(server, client, "221 bye\r\n")
    w.fin(client, server)
    return w.frames


def build_reuse_without_renegotiation():
    """A second RETR reuses the previous channel without a new PASV."""

    w = World()
    mac_c = b"\x02\x00\x00\x00\x00\x0c"
    mac_s = b"\x02\x00\x00\x00\x00\x05"
    client = TcpEnd("10.0.0.80", 46001, mac_c)
    server = TcpEnd("10.0.0.5", 21, mac_s)
    w.handshake(client, server)
    w.reply(server, client, "220 ready\r\n")
    w.control(client, server, b"USER reuse\r\n")
    w.reply(server, client, "331 password please\r\n")
    w.control(client, server, b"PASS pw\r\n")
    w.reply(server, client, "230 logged in\r\n")
    w.control(client, server, b"PASV\r\n")
    w.reply(server, client, "227 Entering passive mode (10,0,0,5,196,10).\r\n")

    data_client = TcpEnd("10.0.0.80", 46002, mac_c)
    data_server = TcpEnd("10.0.0.5", 50186, mac_s)
    w.handshake(data_client, data_server)
    w.control(client, server, b"RETR one.bin\r\n")
    w.reply(server, client, "150 opening\r\n")
    w.send(data_server, data_client, b"1" * 400)
    w.reply(server, client, "226 done\r\n")
    w.fin(data_client, data_server)

    # No PASV here: the client just issues another RETR.
    w.control(client, server, b"RETR two.bin\r\n")
    w.reply(server, client, "425 Can't open data connection.\r\n")
    w.control(client, server, b"QUIT\r\n")
    w.reply(server, client, "221 bye\r\n")
    w.fin(client, server)
    return w.frames


def build_gap_in_data_channel():
    """One data segment is missing from the capture."""

    w = World()
    mac_c = b"\x02\x00\x00\x00\x00\x0c"
    mac_s = b"\x02\x00\x00\x00\x00\x05"
    client = TcpEnd("10.0.0.81", 46003, mac_c)
    server = TcpEnd("10.0.0.5", 21, mac_s)
    w.handshake(client, server)
    w.reply(server, client, "220 ready\r\n")
    w.control(client, server, b"USER gap\r\n")
    w.reply(server, client, "331 password please\r\n")
    w.control(client, server, b"PASS pw\r\n")
    w.reply(server, client, "230 logged in\r\n")
    w.control(client, server, b"PASV\r\n")
    w.reply(server, client, "227 Entering passive mode (10,0,0,5,196,11).\r\n")

    data_client = TcpEnd("10.0.0.81", 46004, mac_c)
    data_server = TcpEnd("10.0.0.5", 50187, mac_s)
    w.handshake(data_client, data_server)
    w.control(client, server, b"RETR gap.bin\r\n")
    w.reply(server, client, "150 opening\r\n")
    w.send(data_server, data_client, b"A" * 200)
    # Skip 300 bytes, then continue: the bytes are missing from the file.
    data_server.seq += 300
    w.send(data_server, data_client, b"B" * 200)
    w.reply(server, client, "226 done\r\n")
    w.fin(data_client, data_server)
    w.control(client, server, b"QUIT\r\n")
    w.reply(server, client, "221 bye\r\n")
    w.fin(client, server)
    return w.frames



