"""Merge D-Bus records from multiple hosts into a single repository.

This module adds a *source domain* to the four kinds of identifiers that
collide when records from several machines are archived together:

- bus names (:class:`ScopedBusName`)
- connection generations, i.e. unique connection names
  (:class:`ScopedConnectionId`)
- message serials (:class:`ScopedSerial`)
- object paths (:class:`ScopedObjectPath`)

Every scoped identifier keeps the original name for later lookup and
carries its :class:`SourceDomain` in equality and hashing, so a reference
from one bus never resolves to a same-named entity on another bus.

:class:`MessageArchive` stores :class:`ScopedMessage` records from any
number of hosts. Records are framed with their domain and the message
payload is kept in the D-Bus wire format, so when the archive is split
back into per-host messages the parameter types and values are identical
to what was merged. Message bodies are never rewritten: a plain string in
the body that happens to look like a bus name or object path is archived
and restored exactly as-is.
"""

import io
import re
from struct import Struct
from typing import List, Optional, Tuple, Union

from ._private.unmarshaller import Unmarshaller
from .errors import InvalidBusNameError, InvalidObjectPathError
from .message import Message
from .validators import is_bus_name_valid, is_object_path_valid

_domain_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# record framing: u8 domain length, u8 sender length (0 = none),
# u32 payload length, then domain bytes, sender bytes, wire-format message
_RECORD_HEADER = Struct('>BBI')


class SourceDomain:
    """Identifies the host a record came from.

    The name is restricted to a single D-Bus name element so that scoped
    names and object paths remain valid D-Bus strings when encoded.

    :ivar name: The name of the domain.
    :vartype name: str

    :raises:
        - :class:`ValueError` - If the name is not a valid domain name.
    """
    def __init__(self, name: Union[str, 'SourceDomain']):
        if isinstance(name, SourceDomain):
            name = name.name
        if not isinstance(name, str) or len(name) > 255 or _domain_re.search(name) is None:
            raise ValueError(f'invalid source domain name: {name!r}')
        self.name = name

    def __eq__(self, other):
        if isinstance(other, SourceDomain):
            return self.name == other.name
        return NotImplemented

    def __hash__(self):
        return hash((SourceDomain, self.name))

    def __str__(self):
        return self.name

    def __repr__(self):
        return f'SourceDomain({self.name!r})'


def _encode_bus_name(domain: str, name: str) -> str:
    if name.startswith(':'):
        return ':' + domain + '.' + name[1:]
    return domain + '.' + name


def _decode_bus_name(domain: str, text: str) -> Optional[str]:
    prefix = domain + '.'
    if text.startswith(':' + prefix):
        return ':' + text[len(prefix) + 1:]
    if text.startswith(prefix):
        return text[len(prefix):]
    return None


def _encode_object_path(domain: str, path: str) -> str:
    if path == '/':
        return '/' + domain
    return '/' + domain + path


def _decode_object_path(domain: str, text: str) -> Optional[str]:
    if text == '/' + domain:
        return '/'
    prefix = '/' + domain + '/'
    if text.startswith(prefix):
        return text[len(domain) + 1:]
    return None


class ScopedName:
    """Base class for a name paired with the domain it came from.

    :ivar domain: The source domain of this name.
    :vartype domain: :class:`SourceDomain`
    :ivar original: The original name as used on the source host.
    :vartype original: str
    """
    def __init__(self, domain: Union[str, SourceDomain], original: str):
        self.domain = domain if isinstance(domain, SourceDomain) else SourceDomain(domain)
        self.original = original

    def __eq__(self, other):
        if type(other) is type(self):
            return self.domain == other.domain and self.original == other.original
        return NotImplemented

    def __hash__(self):
        return hash((type(self), self.domain, self.original))

    def matches(self, other: 'ScopedName') -> bool:
        """Whether this name refers to the same entity as another scoped name.

        Names from different domains never match, even when the original
        names are textually identical.
        """
        return self == other

    def encode(self) -> str:
        """The archive form of this name as a valid D-Bus string."""
        raise NotImplementedError

    def __str__(self):
        return self.encode()

    def __repr__(self):
        return f'{type(self).__name__}({self.domain.name!r}, {self.original!r})'


class ScopedBusName(ScopedName):
    """A well-known or unique bus name tagged with its source domain.

    :raises:
        - :class:`InvalidBusNameError` - If the original name is not a valid bus name.
    """
    def __init__(self, domain: Union[str, SourceDomain], original: str):
        if not is_bus_name_valid(original):
            raise InvalidBusNameError(original)
        super().__init__(domain, original)

    def encode(self) -> str:
        return _encode_bus_name(self.domain.name, self.original)

    @classmethod
    def decode(cls, text: str, domain: Union[str, SourceDomain]) -> Optional['ScopedBusName']:
        """Rebuild a scoped bus name from its archive form.

        :returns: The scoped name, or ``None`` if the text does not carry
            this domain's tag.
        """
        original = _decode_bus_name(SourceDomain(domain).name, text)
        if original is None:
            return None
        return cls(domain, original)


class ScopedConnectionId(ScopedName):
    """A connection generation: a unique connection name (such as
    ``:1.42``) tagged with the domain of the host the connection lived on.

    :raises:
        - :class:`InvalidBusNameError` - If the original name is not a valid unique name.
    """
    def __init__(self, domain: Union[str, SourceDomain], original: str):
        if not isinstance(original, str) or not original.startswith(':') \
                or not is_bus_name_valid(original):
            raise InvalidBusNameError(original)
        super().__init__(domain, original)

    def encode(self) -> str:
        return _encode_bus_name(self.domain.name, self.original)

    @classmethod
    def decode(cls, text: str, domain: Union[str, SourceDomain]) -> Optional['ScopedConnectionId']:
        """Rebuild a scoped connection id from its archive form.

        :returns: The scoped connection id, or ``None`` if the text does
            not carry this domain's tag.
        """
        if not text.startswith(':'):
            return None
        original = _decode_bus_name(SourceDomain(domain).name, text)
        if original is None:
            return None
        return cls(domain, original)


class ScopedObjectPath(ScopedName):
    """An object path tagged with its source domain.

    :raises:
        - :class:`InvalidObjectPathError` - If the original path is not a valid object path.
    """
    def __init__(self, domain: Union[str, SourceDomain], original: str):
        if not is_object_path_valid(original):
            raise InvalidObjectPathError(original)
        super().__init__(domain, original)

    def encode(self) -> str:
        return _encode_object_path(self.domain.name, self.original)

    @classmethod
    def decode(cls, text: str, domain: Union[str, SourceDomain]) -> Optional['ScopedObjectPath']:
        """Rebuild a scoped object path from its archive form.

        :returns: The scoped path, or ``None`` if the text does not carry
            this domain's tag.
        """
        original = _decode_object_path(SourceDomain(domain).name, text)
        if original is None:
            return None
        return cls(domain, original)


class ScopedSerial:
    """A message serial tagged with its source domain.

    Serials are only unique per connection, so the same serial from two
    hosts identifies different messages and never compares equal.

    :ivar domain: The source domain of this serial.
    :vartype domain: :class:`SourceDomain`
    :ivar serial: The original serial number.
    :vartype serial: int

    :raises:
        - :class:`ValueError` - If the serial is not a non-negative integer.
    """
    def __init__(self, domain: Union[str, SourceDomain], serial: int):
        self.domain = domain if isinstance(domain, SourceDomain) else SourceDomain(domain)
        if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
            raise ValueError(f'invalid message serial: {serial!r}')
        self.serial = serial

    @property
    def original(self) -> int:
        """The original serial number, kept for lookup."""
        return self.serial

    def __eq__(self, other):
        if isinstance(other, ScopedSerial):
            return self.domain == other.domain and self.serial == other.serial
        return NotImplemented

    def __hash__(self):
        return hash((ScopedSerial, self.domain, self.serial))

    def __int__(self):
        return self.serial

    def __repr__(self):
        return f'ScopedSerial({self.domain.name!r}, {self.serial!r})'


def tag_message(domain: Union[str, SourceDomain], message: Message) -> Message:
    """Return a copy of the message whose bus name, connection, and object
    path header fields carry the given source domain.

    Interface, member, and error names are shared vocabulary and are left
    unchanged. Body values are never rewritten, even strings that look
    like bus names or object paths.

    :param domain: The source domain to tag the message with.
    :type domain: :class:`SourceDomain` or str
    :param message: The message to tag.
    :type message: :class:`Message <dbus_next.message.Message>`

    :returns: The tagged message.
    :rtype: :class:`Message <dbus_next.message.Message>`
    """
    domain = domain if isinstance(domain, SourceDomain) else SourceDomain(domain)
    return Message(destination=_encode_bus_name(domain.name, message.destination)
                   if message.destination is not None else None,
                   path=_encode_object_path(domain.name, message.path)
                   if message.path is not None else None,
                   interface=message.interface,
                   member=message.member,
                   message_type=message.message_type,
                   flags=message.flags,
                   error_name=message.error_name,
                   reply_serial=message.reply_serial,
                   sender=_encode_bus_name(domain.name, message.sender)
                   if message.sender is not None else None,
                   unix_fds=list(message.unix_fds),
                   signature=message.signature,
                   body=list(message.body),
                   serial=message.serial)


def untag_message(domain: Union[str, SourceDomain], message: Message) -> Message:
    """Inverse of :func:`tag_message` for the given domain.

    Header fields that do not carry this domain's tag are passed through
    unchanged, so untagging with a foreign domain is a no-op. Body values
    are never rewritten.

    :param domain: The source domain to strip from the message.
    :type domain: :class:`SourceDomain` or str
    :param message: The tagged message.
    :type message: :class:`Message <dbus_next.message.Message>`

    :returns: The message with this domain's tags removed.
    :rtype: :class:`Message <dbus_next.message.Message>`
    """
    domain = domain if isinstance(domain, SourceDomain) else SourceDomain(domain)

    def decode(decode_fn, value):
        if value is None:
            return None
        decoded = decode_fn(domain.name, value)
        return decoded if decoded is not None else value

    return Message(destination=decode(_decode_bus_name, message.destination),
                   path=decode(_decode_object_path, message.path),
                   interface=message.interface,
                   member=message.member,
                   message_type=message.message_type,
                   flags=message.flags,
                   error_name=message.error_name,
                   reply_serial=message.reply_serial,
                   sender=decode(_decode_bus_name, message.sender),
                   unix_fds=list(message.unix_fds),
                   signature=message.signature,
                   body=list(message.body),
                   serial=message.serial)


class ScopedMessage:
    """A message tagged with the source domain of the host it came from.

    :ivar domain: The source domain of this message.
    :vartype domain: :class:`SourceDomain`
    :ivar message: The original message, unchanged.
    :vartype message: :class:`Message <dbus_next.message.Message>`
    """
    def __init__(self, domain: Union[str, SourceDomain], message: Message):
        self.domain = domain if isinstance(domain, SourceDomain) else SourceDomain(domain)
        if not isinstance(message, Message):
            raise TypeError(f'expected a Message, got {type(message).__name__}')
        self.message = message

    @property
    def serial(self) -> ScopedSerial:
        """The serial of this message scoped to its source domain."""
        return ScopedSerial(self.domain, self.message.serial)

    @property
    def reply_serial(self) -> Optional[ScopedSerial]:
        """The reply serial of this message scoped to its source domain."""
        if self.message.reply_serial is None:
            return None
        return ScopedSerial(self.domain, self.message.reply_serial)

    @property
    def path(self) -> Optional[ScopedObjectPath]:
        """The object path of this message scoped to its source domain."""
        if self.message.path is None:
            return None
        return ScopedObjectPath(self.domain, self.message.path)

    @property
    def sender(self) -> Optional[ScopedConnectionId]:
        """The sending connection scoped to its source domain."""
        if self.message.sender is None:
            return None
        return ScopedConnectionId(self.domain, self.message.sender)

    @property
    def destination(self) -> Optional[ScopedBusName]:
        """The destination bus name scoped to its source domain."""
        if self.message.destination is None:
            return None
        return ScopedBusName(self.domain, self.message.destination)

    def scoped_message(self) -> Message:
        """The message with its header fields encoded for the archive.

        :returns: The tagged message.
        :rtype: :class:`Message <dbus_next.message.Message>`
        """
        return tag_message(self.domain, self.message)

    def archive(self) -> bytes:
        """Serialize this record: the domain, the sender (which is not part
        of the wire format), and the message in D-Bus wire format.

        Unix file descriptors cannot be archived and are not preserved.

        :returns: The archived record.
        :rtype: bytes
        """
        payload = self.message._marshall()
        domain_bytes = self.domain.name.encode('ascii')
        sender_bytes = self.message.sender.encode('ascii') if self.message.sender else b''
        return _RECORD_HEADER.pack(len(domain_bytes), len(sender_bytes), len(payload)) \
            + domain_bytes + sender_bytes + payload

    @staticmethod
    def unarchive(buf: bytes, offset: int = 0) -> Tuple['ScopedMessage', int]:
        """Read one archived record from a buffer.

        :param buf: The buffer to read from.
        :param offset: The offset of the record within the buffer.

        :returns: The restored record and the offset of the next record.
        :rtype: tuple(:class:`ScopedMessage`, int)

        :raises:
            - :class:`ValueError` - If the record is truncated or malformed.
        """
        if len(buf) - offset < _RECORD_HEADER.size:
            raise ValueError('archived record is truncated')
        domain_len, sender_len, payload_len = _RECORD_HEADER.unpack_from(buf, offset)
        pos = offset + _RECORD_HEADER.size
        if len(buf) - pos < domain_len + sender_len + payload_len:
            raise ValueError('archived record is truncated')
        domain = SourceDomain(bytes(buf[pos:pos + domain_len]).decode('ascii'))
        pos += domain_len
        sender = bytes(buf[pos:pos + sender_len]).decode('ascii') if sender_len else None
        pos += sender_len
        payload = bytes(buf[pos:pos + payload_len])
        pos += payload_len
        message = Unmarshaller(io.BytesIO(payload)).unmarshall()
        if message is None:
            raise ValueError('archived message payload is incomplete')
        message.sender = sender
        return ScopedMessage(domain, message), pos

    def __eq__(self, other):
        if isinstance(other, ScopedMessage):
            return self.domain == other.domain and self.archive() == other.archive()
        return NotImplemented

    def __hash__(self):
        return hash((ScopedMessage, self.domain, self.archive()))

    def __repr__(self):
        return f'ScopedMessage({self.domain.name!r}, {self.message!r})'


class MessageArchive:
    """A repository that merges D-Bus message records from multiple hosts.

    Records are archived with their source domain and can be serialized
    for storage and split back into per-host messages. Because the payload
    is kept in the D-Bus wire format, a message that is merged, archived,
    and split back out has the same signature and body values as the
    original.
    """
    def __init__(self, records: List[ScopedMessage] = None):
        self._records = list(records) if records else []

    def add(self, domain: Union[str, SourceDomain], message: Message) -> ScopedMessage:
        """Merge a message from the given host into the archive.

        :returns: The archived record.
        :rtype: :class:`ScopedMessage`
        """
        record = ScopedMessage(domain, message)
        self._records.append(record)
        return record

    @property
    def records(self) -> Tuple[ScopedMessage, ...]:
        """The archived records in merge order."""
        return tuple(self._records)

    def __len__(self):
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def serialize(self) -> bytes:
        """Serialize the whole archive for storage."""
        return b''.join(record.archive() for record in self._records)

    @classmethod
    def deserialize(cls, data: bytes) -> 'MessageArchive':
        """Restore an archive previously written with :func:`serialize`."""
        archive = cls()
        offset = 0
        while offset < len(data):
            record, offset = ScopedMessage.unarchive(data, offset)
            archive._records.append(record)
        return archive

    def split(self, domain: Union[str, SourceDomain]) -> List[Message]:
        """Split the records of a single host back out of the archive.

        :returns: The original messages merged from this domain, in merge
            order, with parameter types and values identical to what was
            added.
        :rtype: list(:class:`Message <dbus_next.message.Message>`)
        """
        domain = domain if isinstance(domain, SourceDomain) else SourceDomain(domain)
        return [record.message for record in self._records if record.domain == domain]

    def lookup(self, scoped: Union[ScopedBusName, ScopedConnectionId, ScopedObjectPath,
                                   ScopedSerial]) -> List[ScopedMessage]:
        """Find the records a scoped identifier refers to.

        References only resolve within their own source domain: an
        identifier from one host never matches a record from another host
        whose original name or serial happens to be the same.

        :param scoped: The scoped bus name, connection id, object path, or
            serial to look up.

        :returns: The matching records.
        :rtype: list(:class:`ScopedMessage`)
        """
        if not isinstance(scoped, (ScopedBusName, ScopedConnectionId, ScopedObjectPath,
                                   ScopedSerial)):
            raise TypeError(f'cannot look up {type(scoped).__name__}')

        def matches(record):
            original = scoped.original
            if isinstance(scoped, ScopedConnectionId):
                return record.message.sender == original \
                    or record.message.destination == original
            if isinstance(scoped, ScopedBusName):
                return record.message.destination == original \
                    or record.message.sender == original
            if isinstance(scoped, ScopedObjectPath):
                return record.message.path == original
            return record.message.serial == scoped.serial

        return [r for r in self._records if r.domain == scoped.domain and matches(r)]
