from LnkParse3.decorators import uuid
from LnkParse3.target.lnk_target_base import LnkTargetBase


"""
With name (flag 0x01 set):
----------------------------------------------------------------------
|              0-7b              |               8-15b               |
----------------------------------------------------------------------
| ClassTypeIndicator == 0x20-0x2F|                                   |
----------------------------------------------------------------------
|                  VolumeName (null-terminated)                      |
|                             20 B                                   |
----------------------------------------------------------------------

Without name (flag 0x01 not set):
----------------------------------------------------------------------
|              0-7b              |               8-15b               |
----------------------------------------------------------------------
| ClassTypeIndicator == 0x20-0x2F|           Unknown                 |
----------------------------------------------------------------------
|                <GUID> VolumeIdentifier                             |
|                             16 B                                   |
----------------------------------------------------------------------

With extension block (flag 0x02 or 0x09 set):
----------------------------------------------------------------------
| ClassTypeIndicator == 0x20-0x2F|           Flags                   |
----------------------------------------------------------------------
|                  VolumeName (null-terminated)                      |
----------------------------------------------------------------------
|                      ExtensionBlocks                               |
----------------------------------------------------------------------

https://github.com/libyal/libfwsi/blob/master/documentation/Windows%20Shell%20Item%20format.asciidoc#33-volume-shell-item
"""


# TODO: rename to volume_shell_item
class MyComputer(LnkTargetBase):
    FLAGS = {
        0x01: "Has name",
        0x02: "Unknown",
        0x04: "Unknown",
        0x08: "Removable media",
    }

    # Known virtual/shell volume identifiers
    KNOWN_VOLUME_GUIDS = {
        "21EC2020-3AEA-1069-A2DD-08002B30309D": "Control Panel",
        "20D04FE0-3AEA-1069-A2D8-08002B30309D": "My Computer",
        "374DE290-123F-4565-9164-39C4925E467B": "Downloads",
        "B4BFCC3A-DB2C-424C-B029-7FE99A87C641": "Desktop",
    }

    def __init__(self, *args, **kwargs):
        self.name = "Volume Item"
        super().__init__(*args, **kwargs)

    def flags(self):
        return self.class_type_indicator() & 0x0F

    def _has_name(self):
        return bool(self.flags() & 0x01)

    def _has_guid(self):
        # 0x02 (and 0x09) variants carry an extension block rather than a
        # GUID directly; 0x0E items carry a 16 byte volume identifier.
        return self.flags() == 0x0E

    def volume_name(self):
        if not self._has_name():
            return None
        if len(self._raw_target) <= 1:
            return ""
        return self.text_processor.read_string(self._raw_target[1:21])

    @uuid
    def volume_identifier(self):
        if self._has_name():
            return None
        if len(self._raw_target) < 18:
            return b"\x00" * 16
        return self._raw_target[2:18]

    def display_name(self):
        if self._has_name():
            return self.volume_name()
        try:
            guid = self.volume_identifier()
        except Exception:
            return None
        return self.KNOWN_VOLUME_GUIDS.get(guid, guid)

    def is_drive_letter(self):
        name = self.volume_name() if self._has_name() else None
        return bool(name and len(name) >= 2 and name[1] == ":" and name[0].isalpha())

    def extension_start(self):
        # The volume name field occupies a fixed 20 bytes after the type
        # indicator, so extensions start at offset 21. The no-name variant
        # carries a 16 byte GUID and may append extensions from offset 18.
        if self._has_name():
            return 21
        if len(self._raw_target) >= 18:
            return 18
        return super().extension_start()

    def extension_search_start(self):
        return 2

    def as_item(self):
        item = super().as_item()
        item["flags"] = hex(self.flags())
        if self._has_name():
            item["volume_name"] = self.volume_name()
        else:
            item["volume_identifier"] = self.volume_identifier()
        return item
