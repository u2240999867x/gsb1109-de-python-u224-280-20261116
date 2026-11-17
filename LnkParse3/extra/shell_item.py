import warnings

from LnkParse3.extra.lnk_extra_base import LnkExtraBase
from LnkParse3.pidl import parse_id_list


"""
------------------------------------------------------------------
|     0-7b     |     8-15b     |     16-23b     |     24-31b     |
------------------------------------------------------------------
|              <u_int32> BlockSize >= 0x0000000A                 |
------------------------------------------------------------------
|            <u_int32> BlockSignature == 0xA000000C              |
------------------------------------------------------------------
|                       <IDList> IDList                          |
------------------------------------------------------------------
"""


class ShellItem(LnkExtraBase):
    def name(self):
        return "SHELL_ITEM_IDENTIFIER_BLOCK"

    def _id_list(self):
        """ItemIDList (variable):
        An array of zero or more ItemID structures (section 2.2.2), which
        contains the item ID list. An IDList structure conforms to the
        following ABNF [RFC5234]:

            IDLIST = *ITEMID TERMINALID

        ------------------------------------------------------------------
        |     0-7b     |     8-15b     |     16-23b     |     24-31b     |
        ------------------------------------------------------------------
        |                       ItemIDList (variable)                    |
        ------------------------------------------------------------------
        |                             ...                                |
        |-----------------------------------------------------------------
        |         TerminalID           |
        --------------------------------
        """
        data = self._raw[8 : self.size()] if self.size() >= 8 else b""
        parsed = parse_id_list(data, cp=self.cp)
        for record in parsed["records"]:
            target = record["target"]
            if target is not None:
                yield target

    def id_list(self):
        res = []
        for target in self._id_list():
            try:
                res.append(target.as_item())
            except KeyError as e:
                msg = f"Error while parsing extra TargetID `{target.name}` (KeyError {e})"
                warnings.warn(msg)
                continue
        return res

    def as_dict(self):
        tmp = super().as_dict()
        tmp["id_list"] = self.id_list()
        return tmp
