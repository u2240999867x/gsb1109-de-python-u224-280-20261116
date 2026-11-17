from struct import unpack

from LnkParse3.target_factory import TargetFactory


SIZE_OF_ITEM_ID_SIZE = 2


def _make_target(chunk, cp, offset, base_offset):
    factory = TargetFactory(indata=chunk)
    target_class = factory.target_class()
    if target_class is None:
        return None
    return target_class(indata=chunk, cp=cp, offset=offset, base_offset=base_offset)


def parse_id_list(data, cp=None, base_offset=None):
    """Parse an ItemIDList with strict item boundaries.

    ``data`` is the IDList payload (without IDListSize or TerminalID size
    fields). Each ItemIDSize is verified against the payload before the next
    item is read. A malformed item is reported and parsing stops; bytes that
    belong to subsequent items are never consumed by an item's extension
    parser.

    Returns a dict with:
        records: list of records (target, offset, size, raw, truncated, ...)
        terminal_present: whether a two byte zero TerminalID was found
        terminal_offset: offset of the TerminalID within ``data``
        errors: list of boundary problems
        consumed: number of bytes trusted as item data
        trailing_data: bytes after the terminal (or after the last valid item)
    """
    records = []
    errors = []
    pos = 0
    terminal_present = False
    terminal_offset = None

    while True:
        if pos + SIZE_OF_ITEM_ID_SIZE > len(data):
            if pos < len(data):
                errors.append(
                    {
                        "code": "truncated_item_size",
                        "offset": pos,
                        "remaining": len(data) - pos,
                        "message": "ItemIDSize field is incomplete",
                    }
                )
            break

        size = unpack("<H", data[pos : pos + SIZE_OF_ITEM_ID_SIZE])[0]

        if size == 0:
            terminal_present = True
            terminal_offset = pos
            pos += SIZE_OF_ITEM_ID_SIZE
            break

        if size < SIZE_OF_ITEM_ID_SIZE:
            errors.append(
                {
                    "code": "invalid_item_size",
                    "offset": pos,
                    "size": size,
                    "message": "ItemIDSize must be at least 2",
                }
            )
            break

        if pos + size > len(data):
            chunk = data[pos:]
            target = _make_target(chunk, cp, pos, base_offset)
            records.append(
                {
                    "target": target,
                    "offset": pos,
                    "declared_size": size,
                    "available_size": len(chunk),
                    "raw": chunk,
                    "truncated": True,
                }
            )
            errors.append(
                {
                    "code": "item_overruns_id_list",
                    "offset": pos,
                    "declared_size": size,
                    "available": len(chunk),
                    "message": "ItemIDSize exceeds the remaining IDList",
                }
            )
            pos = len(data)
            break

        chunk = data[pos : pos + size]
        target = _make_target(chunk, cp, pos, base_offset)
        if target is None:
            errors.append(
                {
                    "code": "missing_class_type_indicator",
                    "offset": pos,
                    "size": size,
                    "message": "ItemIDSize is non-zero but the item has no type byte",
                }
            )
        records.append(
            {
                "target": target,
                "offset": pos,
                "declared_size": size,
                "available_size": len(chunk),
                "raw": chunk,
                "truncated": False,
            }
        )
        pos += size

    if not terminal_present and not errors:
        errors.append(
            {
                "code": "missing_terminal",
                "offset": pos,
                "message": "IDList ended without a TerminalID",
            }
        )

    trailing = data[pos:]
    if terminal_present and trailing:
        errors.append(
            {
                "code": "trailing_data_after_terminal",
                "offset": pos,
                "length": len(trailing),
                "message": "IDList has bytes after the TerminalID",
            }
        )

    return {
        "records": records,
        "terminal_present": terminal_present,
        "terminal_offset": terminal_offset,
        "errors": errors,
        "consumed": pos,
        "trailing_data": trailing,
    }
