from struct import unpack

from LnkParse3.decorators import dostime
from LnkParse3.target.lnk_target_base import LnkTargetBase


"""
----------------------------------------------------------------------
|              0-7b              |               8-15b               |
----------------------------------------------------------------------
| ClassTypeIndicator == 0x30-0x3F|            UnknownValue           |
----------------------------------------------------------------------
|                        <u_int16> FileSize                          |
|                              4 B                                   |
----------------------------------------------------------------------
|           <dos_timestamp> LastModificationDateAndTime              |
|                              4 B                                   |
----------------------------------------------------------------------
|                       FileAttributeFlags                           |
----------------------------------------------------------------------
|                 <str/unicode_str> PrimaryName                      |
|                              ? B                                   |
----------------------------------------------------------------------
|                           UnknownData                              |
|                              ? B                                   |
----------------------------------------------------------------------
|                         ExtensionBlocks                            |
|                              ? B                                   |
----------------------------------------------------------------------
"""


# TODO: rename to file_entry
# https://github.com/libyal/libfwsi/blob/master/documentation/Windows%20Shell%20Item%20format.asciidoc#34-file-entry-shell-item
class ShellFSFolder(LnkTargetBase):
    FIXED_SIZE = 12

    def __init__(self, *args, **kwargs):
        self.name = "File entry"
        super().__init__(*args, **kwargs)

    def as_item(self):
        item = super().as_item()
        item["flags"] = self.flags()
        item["file_size"] = self.file_size()
        item["modification_time"] = self.modification_time()
        item["file_attribute_flags"] = self.file_attribute_flags()
        item["primary_name"] = self.primary_name()
        return item

    # dup: ./my_computer.py flags()
    # dup: ../target_factory.py item_type()
    def flags(self):
        flags = self.class_type_indicator()

        # FIXME: delete masking
        return self.get_item_shell_fs_folder(flags & 0x0F)

    def file_size(self):
        start, end = 2, 6
        if len(self._raw_target) < end:
            return None
        size = unpack("<I", self._raw_target[start:end])[0]
        return size

    @dostime
    def modification_time(self):
        start, end = 6, 10
        if len(self._raw_target) < end:
            return b"\x00\x00\x00\x00"
        return self._raw_target[start:end]

    def file_attribute_flags(self):
        start, end = 10, 12
        if len(self._raw_target) < end:
            return None
        flags = unpack("<H", self._raw_target[start:end])[0]
        return flags

    def _name_end(self):
        """End offset (within item data) of the primary name, or None."""
        if len(self._raw_target) <= self.FIXED_SIZE:
            return None
        binary = self._raw_target[self.FIXED_SIZE :]

        if self.has_unicode_strings():
            pos = 0
            while pos + 1 < len(binary):
                if binary[pos : pos + 2] == b"\x00\x00":
                    return self.FIXED_SIZE + pos + 2
                pos += 2
            return None

        pos = binary.find(b"\x00")
        if pos < 0:
            return None
        return self.FIXED_SIZE + pos + 1

    def primary_name(self):
        binary = self._raw_target[self.FIXED_SIZE :]
        end = self._name_end()
        if end is not None:
            binary = self._raw_target[self.FIXED_SIZE : end]

        if self.has_unicode_strings():
            text = self.text_processor.read_unicode_string(binary)
        else:
            text = self.text_processor.read_string(binary)

        return text

    def display_name(self):
        try:
            # Parse extensions first; a legacy fixed name area may end before
            # the null terminator scanned by primary_name().
            self.extensions()
            extension_start = self.extension_area_start()
            name_end = self._name_end()
            if (
                extension_start is not None
                and extension_start >= self.FIXED_SIZE
                and (name_end is None or name_end > extension_start)
            ):
                binary = self._raw_target[self.FIXED_SIZE : extension_start]
                while binary.endswith(b"\x00"):
                    binary = binary[:-1]
                if self.has_unicode_strings():
                    return self.text_processor.read_unicode_string(binary)
                return self.text_processor.read_string(binary)
            return self.primary_name()
        except Exception:
            return None

    def is_directory(self):
        flags = self.class_type_indicator()
        if flags & 0x05:
            return True
        if flags & 0x06:
            return False
        return bool(flags & 0x01) and not bool(flags & 0x02)

    def extension_start(self):
        end = self._name_end()
        if end is None:
            return None
        # Extension blocks are 2-byte aligned; one padding byte may follow a
        # null terminated ASCII name.
        return end + (end & 1)

    def extension_search_start(self):
        # Some legacy file entry variants keep a fixed name area that is
        # shorter than a null terminated string, so the computed name end can
        # run into the extension header. Search from the fixed fields as well.
        return self.FIXED_SIZE

    # https://github.com/libyal/libfwsi/blob/master/documentation/Windows%20Shell%20Item%20format.asciidoc#341-file-entry-shell-item--pre-windows-xp
    def secondary_name(self):
        # TODO:
        pass

    def shell_folder_identifier(self):
        # TODO:
        pass
