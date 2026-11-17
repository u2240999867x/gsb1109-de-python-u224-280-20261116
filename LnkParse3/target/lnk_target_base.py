import warnings
from struct import unpack

from LnkParse3.text_processor import TextProcessor


"""
An ItemID is an element in an IDList structure (section 2.2.1). The data stored
in a given ItemID is defined by the source that corresponds to the location in
the target namespace of the preceding ItemIDs. This data uniquely identifies
the items in that part of the namespace.

------------------------------------------------------------------
|     0-7b     |     8-15b     |     16-23b     |     24-31b     |
------------------------------------------------------------------
|         ItemIDSize           |          Data (variable)        |
------------------------------------------------------------------
|                             ...                                |
------------------------------------------------------------------
"""


class LnkTargetBase:
    SHELL_ITEM_SHEL_FS_FOLDER = {
        # FIXME: Temporary solution for not make a breaking change
        0x05: "Is Unicode directory",
        0x06: "Is Unicode file",
        0x07: "Is Unicode share",
        0x03: "Is share",
        0x01: "Is directory",
        0x02: "Is file",
        0x04: "Has Unicode strings",
        0x80: "Has CLSID",
    }

    SIZE_OF_TARGET_SIZE = 2

    # Extension block headers start with the 0xBEEF prefix. The high word is
    # the marker and the low word identifies the extension type.
    EXTENSION_SIGNATURE_MASK = 0xFFFF0000
    EXTENSION_SIGNATURE_PREFIX = 0xBEEF0000
    EXTENSION_NAMES = {
        0xBEEF0004: "file_entry_extension",
        0xBEEF0026: "shell_item_extension",
    }

    def __init__(self, indata=None, cp=None, offset=None, base_offset=None):
        self._target = {}
        self.cp = cp
        self._raw = indata or b""
        self.item_offset = offset
        self.base_offset = base_offset
        self.unparsed_tail_offset = None
        self._extension_area_start = None

        self.text_processor = TextProcessor(cp=self.cp)

        declared_size = self.size()
        available = max(len(self._raw) - self.SIZE_OF_TARGET_SIZE, 0)
        self.item_declared_size = declared_size
        self.item_truncated = declared_size < self.SIZE_OF_TARGET_SIZE or (
            declared_size - self.SIZE_OF_TARGET_SIZE > available
        )
        if self.item_truncated:
            data_size = min(max(declared_size - self.SIZE_OF_TARGET_SIZE, 0), available)
        else:
            data_size = declared_size - self.SIZE_OF_TARGET_SIZE

        self._raw_target = self._raw[
            self.SIZE_OF_TARGET_SIZE : self.SIZE_OF_TARGET_SIZE + data_size
        ]

    def get_item_shell_fs_folder(self, flags: int) -> str:
        # FIXME: Can have many flags at once
        for flag, name in self.SHELL_ITEM_SHEL_FS_FOLDER.items():
            if flags & flag == flag:
                return name
        msg = (
            f"Not implemented flags 0x{flags:02X} "
            "in LnkTargetBase.SHELL_ITEM_SHELL_FS_FOLDER. "
            'Use fallback value: "Unknown".'
        )
        warnings.warn(msg)
        return "Unknown"

    def as_item(self):
        return {
            "class": self.name,
        }

    def display_name(self):
        """Human readable name of the item, if the item type defines one."""

    def size(self):
        """ItemIDSize (2 bytes):
        A 16-bit, unsigned integer that specifies the size, in bytes, of the
        ItemID structure, including the ItemIDSize field.
        """
        start, end = 0, 2
        if len(self._raw) < end:
            return 0
        size = unpack("<H", self._raw[start:end])[0]
        return size

    def item_type(self):
        """ClassTypeIndicator (1 byte) of the item, if available."""
        if not self._raw_target:
            return None
        return self._raw_target[0]

    def class_type_indicator(self):
        start, end = 0, 1
        if len(self._raw_target) < end:
            return 0
        flags = unpack("<B", self._raw_target[start:end])[0]
        return flags

    def has_unicode_strings(self):
        inv = {v: k for k, v in self.SHELL_ITEM_SHEL_FS_FOLDER.items()}
        mask = inv["Has Unicode strings"]
        return bool(self.class_type_indicator() & mask)

    # -- Extension block support -------------------------------------------

    def extension_start(self):
        """Nominal offset (within item data) of the extension block area.

        ``None`` means the item layout is not understood well enough to locate
        extensions. Unknown items keep their whole payload as-is.
        """

    def extension_search_start(self):
        """Earliest offset where a misplaced extension chain may start."""
        return self.extension_start()

    def extensions(self):
        """Parse the extension block chain of this item with hard boundaries.

        Every size is checked against the item payload. An invalid or
        unparseable header terminates the chain; it never advances into the
        next ItemID and the remaining bytes are reported as an unparsed tail.

        A chain is only recognized when its blocks stay inside the ItemID and
        end exactly at the ItemID boundary. A plausible header in the middle
        of unparsed data is therefore never promoted to a real boundary.
        """
        self.unparsed_tail_offset = None
        self._extension_area_start = None

        nominal = self.extension_start()
        search = self.extension_search_start()

        if nominal is not None and self._has_extension_marker(nominal):
            self._extension_area_start = nominal
            extensions = self._parse_extensions(nominal)
            if self.unparsed_tail_offset is None:
                return extensions
        elif nominal is not None:
            # Bytes after the known item fields are not an extension chain if
            # no signature is present, but they still remain auditable.
            if nominal < len(self._raw_target) and any(self._raw_target[nominal:]):
                self.unparsed_tail_offset = nominal
                self._extension_area_start = nominal
            extensions = []
        else:
            extensions = []

        if search is not None:
            discovered = self._discover_extension_chain(search)
            if discovered is not None:
                self.unparsed_tail_offset = None
                self._extension_area_start = discovered
                return self._parse_extensions(discovered)

        return extensions

    def _has_extension_marker(self, offset):
        data = self._raw_target
        if offset < 0 or offset + 8 > len(data):
            return False
        marker = unpack("<I", data[offset + 4 : offset + 8])[0]
        return marker & self.EXTENSION_SIGNATURE_MASK == self.EXTENSION_SIGNATURE_PREFIX

    def extension_area_start(self):
        """Offset of the extension chain that was actually used, if any."""
        return self._extension_area_start

    def _discover_extension_chain(self, minimum):
        """Find a BEEF extension chain that ends exactly at the item end."""
        data = self._raw_target
        for candidate in range(max(minimum, 0), len(data) - 7):
            marker = unpack("<I", data[candidate + 4 : candidate + 8])[0]
            if marker & self.EXTENSION_SIGNATURE_MASK != self.EXTENSION_SIGNATURE_PREFIX:
                continue
            pos = candidate
            count = 0
            while pos + 8 <= len(data):
                size = unpack("<H", data[pos : pos + 2])[0]
                signature = unpack("<I", data[pos + 4 : pos + 8])[0]
                if size < 8 or pos + size > len(data):
                    break
                if signature & self.EXTENSION_SIGNATURE_MASK != self.EXTENSION_SIGNATURE_PREFIX:
                    break
                pos += size
                count += 1
            if count and pos == len(data):
                return candidate
        return None

    def _parse_extensions(self, start):
        extensions = []
        self.unparsed_tail_offset = None
        data = self._raw_target
        if start < 0 or start > len(data):
            self.unparsed_tail_offset = 0
            return extensions

        pos = start
        while pos + 8 <= len(data):
            size, version = unpack("<HH", data[pos : pos + 4])
            signature = unpack("<I", data[pos + 4 : pos + 8])[0]

            if size < 8 or pos + size > len(data):
                # A bad extension length must not consume bytes that belong to
                # the next ItemID; the ItemID itself was already bounded by its
                # own ItemIDSize field.
                self.unparsed_tail_offset = pos
                break
            if signature & self.EXTENSION_SIGNATURE_MASK != self.EXTENSION_SIGNATURE_PREFIX:
                self.unparsed_tail_offset = pos
                break

            extensions.append(
                {
                    "offset": pos,
                    "file_offset": (
                        self.base_offset + self.item_offset + self.SIZE_OF_TARGET_SIZE + pos
                        if self.base_offset is not None and self.item_offset is not None
                        else None
                    ),
                    "size": size,
                    "version": version,
                    "signature": f"0x{signature:08X}",
                    "class": self.EXTENSION_NAMES.get(signature, "Unknown extension"),
                    "raw": data[pos : pos + size].hex(),
                }
            )
            pos += size

        # Trailing zero bytes are padding inside the ItemID, not an unparsed
        # extension. Keep them out of the warning path.
        if pos < len(data) and any(data[pos:]):
            self.unparsed_tail_offset = pos
        return extensions

    def unparsed_tail(self):
        offset = self.unparsed_tail_offset
        if offset is None or offset >= len(self._raw_target):
            return None
        return self._raw_target[offset:].hex()

    # -- Auditable item metadata -------------------------------------------

    def details(self):
        """Return the parsed item plus type, original range and extensions.

        Item offsets and sizes are relative to the ItemIDList data, i.e. the
        byte after IDListSize. Extension offsets are relative to item data,
        i.e. the byte after ItemIDSize. ``file_offset`` is an absolute LNK
        offset when the caller knows the IDList start.
        """
        try:
            item = self.as_item()
        except Exception as exc:
            item = {"class": self.name, "parse_error": str(exc)}

        extensions = []
        try:
            extensions = self.extensions()
        except Exception as exc:
            item["extension_error"] = str(exc)

        item["item_type"] = f"0x{self.item_type():02X}" if self.item_type() is not None else None
        item["offset"] = self.item_offset
        item["file_offset"] = (
            self.base_offset + self.item_offset
            if self.base_offset is not None and self.item_offset is not None
            else None
        )
        item["size"] = self.item_declared_size
        item["data_size"] = len(self._raw_target)
        item["truncated"] = self.item_truncated
        item["raw"] = self._raw[: self.SIZE_OF_TARGET_SIZE + len(self._raw_target)].hex()
        item["data_hex"] = self._raw_target.hex()
        item["extension_area_start"] = self.extension_area_start()
        item["extensions"] = extensions
        item["extensions_complete"] = (
            None if self.extension_area_start() is None else self.unparsed_tail() is None
        )
        item["unparsed_tail"] = self.unparsed_tail()
        return item
