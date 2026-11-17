import warnings
from struct import unpack

from LnkParse3.pidl import parse_id_list


"""
LINKTARGET_IDLIST:
An optional LinkTargetIDList structure (section 2.2), which specifies the
target of the link. The presence of this structure is specified by the
HasLinkTargetIDList bit (LinkFlags section 2.1.1) in the ShellLinkHeader.

------------------------------------------------------------------
|     0-7b     |     8-15b     |     16-23b     |     24-31b     |
------------------------------------------------------------------
|         IDListSize           |         IDList (variable)       |
|-------------------------------                                 |
|                             ...                                |
------------------------------------------------------------------
"""


class LnkTargets:
    SIZE_OF_ID_LIST_SIZE = 2

    def __init__(self, indata=None, cp=None, offset=None):
        self._targets = {}
        self.cp = cp
        self._raw = indata or b""
        self.offset = offset

        start = self.SIZE_OF_ID_LIST_SIZE
        end = self.size()
        self._raw_targets = self._raw[start:end]
        self.id_list_truncated = end > len(self._raw)
        self._parsed = parse_id_list(
            self._raw_targets,
            cp=self.cp,
            base_offset=(offset + start) if offset is not None else None,
        )

    def __iter__(self):
        return self._id_lists()

    def size(self):
        """
        Including IDListSize section itself
        """
        return self.id_list_size() + self.SIZE_OF_ID_LIST_SIZE

    def id_list_size(self):
        """IDListSize (2 bytes):
        The size, in bytes, of the IDList field.
        """
        start, end = 0, self.SIZE_OF_ID_LIST_SIZE
        if len(self._raw) < end:
            return 0
        size = unpack("<H", self._raw[start:end])[0]
        return size

    def _id_lists(self):
        """ItemIDList (variable):
        An array of zero or more ItemID structures (section 2.2.2), which
        contains the item ID list. An IDList structure conforms to the
        following ABNF [RFC5234]:

            IDLIST = *ITEMID TERMINALID
        """
        for record in self._parsed["records"]:
            target = record["target"]
            if target is not None:
                yield target

    def as_list(self):
        res = []
        for target in self:
            try:
                res.append(target.as_item())
            except KeyError as e:
                msg = f"Error while parsing TargetID `{target.name}` (KeyError {e})"
                warnings.warn(msg)
                continue
        return res

    # -- Auditable access --------------------------------------------------

    def records(self):
        """Raw item sequence with offsets, sizes and boundary state."""
        return [
            {
                "index": index,
                "offset": record["offset"],
                "size": record["declared_size"],
                "raw": record["raw"],
                "raw_hex": record["raw"].hex(),
                "available_size": record["available_size"],
                "item_type": (
                    f"0x{record['target'].item_type():02X}"
                    if record["target"] is not None and record["target"].item_type() is not None
                    else None
                ),
                "truncated": record["truncated"],
                "target": record["target"],
            }
            for index, record in enumerate(self._parsed["records"])
        ]

    def details(self):
        """Return every item with type, original range and extension data."""
        items = []
        for index, record in enumerate(self._parsed["records"]):
            target = record["target"]
            if target is None:
                items.append(
                    {
                        "index": index,
                        "offset": record["offset"],
                        "size": record["declared_size"],
                        "raw": record["raw"].hex(),
                        "parse_error": "no target class",
                    }
                )
                continue
            try:
                item = target.details()
            except Exception as exc:
                item = {
                    "class": getattr(target, "name", "Unknown"),
                    "parse_error": str(exc),
                    "offset": record["offset"],
                    "size": record["declared_size"],
                    "raw": record["raw"].hex(),
                }
            item["index"] = index
            items.append(item)
        return items

    def terminal(self):
        offset = self._parsed["terminal_offset"]
        if offset is None:
            return {"present": False, "offset": None, "size": 0, "raw": None}
        start = offset + self.SIZE_OF_ID_LIST_SIZE
        return {
            "present": True,
            "offset": offset,
            "size": 2,
            "raw": self._raw[start : start + 2].hex(),
        }

    def integrity(self):
        """Joint check of item lengths, extension boundaries and terminal."""
        trailing = self._parsed["trailing_data"]
        unexplained = []
        extension_checked = 0
        for index, record in enumerate(self._parsed["records"]):
            target = record["target"]
            if target is None:
                continue
            try:
                target.extensions()
            except Exception as exc:
                unexplained.append(
                    {
                        "index": index,
                        "item_type": (
                            f"0x{target.item_type():02X}"
                            if target.item_type() is not None
                            else None
                        ),
                        "error": str(exc),
                    }
                )
                continue
            if target.extension_area_start() is None:
                continue
            extension_checked += 1
            tail = target.unparsed_tail()
            if tail is not None:
                unexplained.append(
                    {
                        "index": index,
                        "item_type": (
                            f"0x{target.item_type():02X}"
                            if target.item_type() is not None
                            else None
                        ),
                        "extension_area_start": target.extension_area_start(),
                        "unparsed_tail": tail,
                    }
                )

        items_complete = not any(record["truncated"] for record in self._parsed["records"])
        return {
            "id_list_size": self.id_list_size(),
            "id_list_bytes_available": len(self._raw_targets),
            "id_list_truncated": self.id_list_truncated,
            "terminal_present": self._parsed["terminal_present"],
            "terminal_offset": self._parsed["terminal_offset"],
            "consumed": self._parsed["consumed"],
            "trailing_length": len(trailing),
            "trailing_hex": trailing.hex() if trailing else None,
            "errors": self._parsed["errors"],
            "items_complete": items_complete,
            "extensions_checked": extension_checked,
            "unexplained_extensions": unexplained,
            "extensions_complete": not unexplained,
            "complete": (
                self._parsed["terminal_present"]
                and not self._parsed["errors"]
                and items_complete
                and not unexplained
            ),
        }

    # -- Location view -----------------------------------------------------

    def location(self, link_info=None):
        from LnkParse3.target_location import build_location

        return build_location(self, link_info=link_info)
