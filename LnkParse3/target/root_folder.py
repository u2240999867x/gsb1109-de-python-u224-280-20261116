import warnings
from struct import unpack

from LnkParse3.decorators import uuid
from LnkParse3.target.lnk_target_base import LnkTargetBase


"""
----------------------------------------------------------------------
|             0-7b                 |              8-15b              |
----------------------------------------------------------------------
|   ClassTypeIndicator == 0x1F     | SortIndex == (0x00,0x42,...0x80)|
----------------------------------------------------------------------
|                     <GUID> ShellFolderID                           |
|                             16 B                                   |
----------------------------------------------------------------------
|                         ExtensionBlock                             |
|                              ? B                                   |
----------------------------------------------------------------------
"""


class RootFolder(LnkTargetBase):
    # https://github.com/libyal/libfwsi/blob/master/documentation/Windows%20Shell%20Item%20format.asciidoc#321-sort-index
    SORT_INDEX = {
        0x00: "Internet Explorer",
        0x42: "Libraries",
        0x44: "Users",
        0x4C: "Public Folder",  # https://strontic.github.io/xcyclopedia/library/clsid_4336a54d-038b-4685-ab02-99bb52d3fb8b.html
        0x48: "My Documents",
        0x50: "My Computer",
        0x54: "UsersLibraries",  # https://strontic.github.io/xcyclopedia/library/clsid_031E4825-7B94-4dc3-B131-E946B44C8DD5.html
        0x58: "My Networs Places/Network",
        0x60: "Recycle Bin",
        0x68: "Internet Explorer",
        # 0x70: "Unknown",  # Seems to be a Control Panel
        0x78: "Recycle Bin",  # https://strontic.github.io/xcyclopedia/library/clsid_645FF040-5081-101B-9F08-00AA002F954E.html
        0x80: "My Games",
    }

    # GUIDs that are commonly used as root folder identifiers. The GUID is
    # more reliable than the legacy SortIndex value, which is sometimes not
    # understood (for example SortIndex 0x5B in the unknown_target sample).
    KNOWN_GUIDS = {
        "20D04FE0-3AEA-1069-A2D8-08002B30309D": "My Computer",
        "59031A47-3F72-44A7-89C5-5595FE6B30EE": "Users",
        "450D8FBA-AD25-11D0-98A8-0800361B1103": "My Documents",
        "645FF040-5081-101B-9F08-00AA002F954E": "Recycle Bin",
        "208D2C60-3AEA-1069-A2D7-08002B30309D": "My Network Places",
        "031E4825-7B94-4DC3-B131-E946B44C8DD5": "Libraries",
        "4336A54D-038B-4685-AB02-99BB52D3FB8B": "Public Folder",
        "871C5380-42A0-1069-A2EA-08002B30309D": "Internet Explorer",
        "21EC2020-3AEA-1069-A2DD-08002B30309D": "Control Panel",
        "1D2680C9-0E2A-469D-B787-065558BC7D43": "Favorites",
    }

    FILESYSTEM_CONTAINER_GUIDS = {
        "20D04FE0-3AEA-1069-A2D8-08002B30309D",  # My Computer
        "59031A47-3F72-44A7-89C5-5595FE6B30EE",  # Users
        "450D8FBA-AD25-11D0-98A8-0800361B1103",  # My Documents
    }

    @classmethod
    def get_sort_index(cls, sort_index_value):
        if sort_index_value not in cls.SORT_INDEX:
            msg = (
                f"Not implemented sort_index_value {sort_index_value:02X} "
                "in RootFolder.SORT_INDEX. "
                'Use fallback value: "Unknown".'
            )
            warnings.warn(msg)
            return "Unknown"
        return cls.SORT_INDEX[sort_index_value]

    def __init__(self, *args, **kwargs):
        self.name = "Root Folder"
        super().__init__(*args, **kwargs)

    def as_item(self):
        item = super().as_item()
        item["sort_index"] = self.sort_index()
        item["sort_index_value"] = self.sort_index_value()
        item["guid"] = self.guid()
        return item

    def sort_index_value(self):
        if len(self._raw_target) < 2:
            return None
        start, end = 1, 2
        return unpack("<B", self._raw_target[start:end])[0]

    def sort_index(self):
        value = self.sort_index_value()
        if value is None:
            return "Unknown"
        return self.get_sort_index(value)

    @uuid
    def guid(self):
        start, end = 2, 18
        if len(self._raw_target) < end:
            return b"\x00" * 16
        guid = self._raw_target[start:end]
        return guid

    def known_guid_name(self):
        try:
            return self.KNOWN_GUIDS.get(self.guid())
        except Exception:
            return None

    def display_name(self):
        return self.known_guid_name() or self.sort_index()

    def namespace_kind(self):
        try:
            guid = self.guid()
        except Exception:
            return "unknown"
        if guid in self.FILESYSTEM_CONTAINER_GUIDS:
            return "filesystem"
        if guid in self.KNOWN_GUIDS:
            return "virtual"
        return "unknown"

    def extension_start(self):
        # RootFolder: ClassTypeIndicator (1) + SortIndex (1) + GUID (16).
        if len(self._raw_target) <= 18:
            return None
        return 18

    def extension_search_start(self):
        return 18

    def extension_block(self):
        return self.extensions()
