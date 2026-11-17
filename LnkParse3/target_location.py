"""Resolve the location described by a LinkTargetIDList (PIDL).

A PIDL is a namespace chain, not a plain list of path components. Each item's
data is only meaningful inside the namespace established by the preceding
items. A file system path is therefore composed only while that relationship
is supported by the item types; a familiar display name next to another name
is not evidence of a parent/child disk path.
"""

from LnkParse3.target.control_panel import ControlPanel
from LnkParse3.target.control_panel_category import ControlPanelCategory
from LnkParse3.target.control_panel_cpl import ControlPanelCPL
from LnkParse3.target.my_computer import MyComputer
from LnkParse3.target.network_location import NetworkLocation
from LnkParse3.target.root_folder import RootFolder
from LnkParse3.target.shell_fs_folder import ShellFSFolder
from LnkParse3.target.users_files_folder import UsersFilesFolder


def _join_display(names):
    out = ""
    for name in names:
        if not name:
            continue
        if not out:
            out = name
        elif out.endswith("\\"):
            out += name
        else:
            out += "\\" + name
    return out


def _normalize_path(value):
    if not value:
        return None
    value = value.replace("/", "\\")
    prefix = ""
    if value.startswith("\\\\"):
        prefix = "\\\\"
        value = value[2:]
    while "\\\\" in value:
        value = value.replace("\\\\", "\\")
    value = prefix + value
    if len(value) > 3:
        value = value.rstrip("\\")
    return value


def _path_parts(path):
    if not path:
        return []
    path = _normalize_path(path)
    parts = path[2:].split("\\") if path.startswith("\\\\") else path.split("\\")
    return [part for part in parts if part]


def _same_component(left, right):
    """Compare a PIDL component with a LinkInfo component.

    Equality is by name, with an allowance for an 8.3 short name matching its
    long form. Matching is only used to cross-check; the PIDL value is never
    replaced by the LinkInfo value.
    """
    left = left.rstrip("\\").lower()
    right = right.rstrip("\\").lower()
    if left == right:
        return True
    for short, long in ((left, right), (right, left)):
        if "~" not in short:
            continue
        base, _, extension = short.partition(".")
        short_base = base.split("~", 1)[0]
        long_base, _, long_extension = long.partition(".")
        if not short_base:
            continue
        if long_base.startswith(short_base) and (not extension or extension == long_extension):
            return True
    return False


def _ordered_match(haystack_parts, needle_parts):
    if not needle_parts:
        return False
    index = 0
    for part in haystack_parts:
        if _same_component(part, needle_parts[index]):
            index += 1
            if index == len(needle_parts):
                return True
    return False


class LocationBuilder:
    def __init__(self, targets, link_info_path=None):
        self.targets = targets
        self.records = targets.records()
        self.link_info_path = _normalize_path(link_info_path) if link_info_path else None

        self.segments = []
        self.parts = []
        self.part_trace = []
        self.gaps = []
        self.namespace = "unknown"
        self.drive = None
        self.filesystem_ok = False
        self.chain_broken = False
        self._drive_gap_pending = False

    # -- internals ---------------------------------------------------------

    def _add_part(self, value, index, certain=True):
        self.parts.append(value)
        self.part_trace.append({"component": value, "item_index": index, "certain": certain})

    def _add_gap(self, after_index, reason, missing, position=None, breaks_chain=True):
        if position is None:
            position = len(self.parts)
        self.gaps.append(
            {
                "after_index": after_index,
                "position": position,
                "reason": reason,
                "missing": missing,
            }
        )
        if breaks_chain:
            self.chain_broken = True

    def _item_segment(self, index, target, role, namespace, display_name, certain):
        record = self.records[index]
        try:
            extensions = target.extensions()
        except Exception as exc:
            extensions = []
            extension_error = str(exc)
        else:
            extension_error = None

        segment = {
            "index": index,
            "class": getattr(target, "name", "Unknown"),
            "item_type": (
                f"0x{target.item_type():02X}" if target.item_type() is not None else None
            ),
            "offset": record["offset"],
            "size": record["size"],
            "data_size": max(record["available_size"] - 2, 0),
            "raw": record["raw_hex"],
            "data_hex": record["raw_hex"][4:],
            "display_name": display_name,
            "role": role,
            "namespace": namespace,
            "certain": certain,
            "extensions": extensions,
            "extensions_complete": (
                None if target.extension_area_start() is None else target.unparsed_tail() is None
            ),
            "extension_area_start": target.extension_area_start(),
            "unparsed_tail": target.unparsed_tail(),
        }
        if extension_error:
            segment["extension_error"] = extension_error
        self.segments.append(segment)
        return segment

    # -- item handlers -----------------------------------------------------

    def add_item(self, index, target):
        item_type = target.item_type()

        if isinstance(target, RootFolder):
            self._add_root(index, target)
        elif isinstance(target, MyComputer):
            self._add_volume(index, target)
        elif isinstance(target, UsersFilesFolder):
            self._add_delegate(index, target)
        elif isinstance(target, ShellFSFolder):
            self._add_file_entry(index, target, item_type)
        elif isinstance(target, ControlPanel):
            self._add_virtual(index, target, "Control Panel", "virtual")
        elif isinstance(target, ControlPanelCategory):
            name = target.category()
            self._add_virtual(index, target, name, "virtual")
        elif isinstance(target, ControlPanelCPL):
            try:
                name = target.name_string() or target.cpl_file_path() or None
            except Exception:
                name = None
            self._add_virtual(index, target, name, "virtual")
        elif isinstance(target, NetworkLocation):
            self._add_virtual(index, target, target.location(), "network")
        else:
            display_name = target.display_name()
            self.namespace = "unknown"
            self.filesystem_ok = False
            self._add_gap(index, "item namespace is unknown", "parent namespace")
            self._item_segment(index, target, "unknown", "unknown", display_name, False)
            if display_name:
                self._add_part(display_name, index, certain=False)

    def _add_root(self, index, target):
        display_name = target.display_name()
        namespace = target.namespace_kind()
        if namespace == "filesystem":
            self.namespace = "filesystem"
            self.filesystem_ok = True
            name = target.known_guid_name()
            if name in ("Users", "My Documents"):
                # These roots are relative to a drive. The drive is expected
                # before the root; if it is absent, the connection to a
                # concrete drive remains unknown.
                self._add_part(name, index)
                has_drive = bool(
                    self.parts
                    and len(self.parts[0]) == 3
                    and self.parts[0][1] == ":"
                    and self.parts[0].endswith("\\")
                )
                if not has_drive:
                    self._add_gap(
                        index,
                        "drive letter is not encoded in the PIDL",
                        "drive letter",
                        position=0,
                        breaks_chain=False,
                    )
                    self._drive_gap_pending = True
                else:
                    self._drive_gap_pending = False
            elif name == "My Computer":
                self._drive_gap_pending = True
            elif name:
                self._add_gap(
                    index, "root folder has no supported file system anchor", "file system anchor"
                )
            self._item_segment(index, target, "root", namespace, display_name, True)
            return

        self.namespace = "virtual" if namespace == "virtual" else "unknown"
        self.filesystem_ok = False
        self._add_gap(index, "root folder is not a file system root", "file system root")
        if display_name:
            self._add_part(display_name, index, certain=False)
        self._item_segment(index, target, "root", self.namespace, display_name, False)

    def _add_volume(self, index, target):
        display_name = target.display_name()
        if target.is_drive_letter():
            self.namespace = "filesystem"
            self.filesystem_ok = True
            self.drive = target.volume_name().rstrip("\\") + "\\"

            root_part = self.parts[0] if self.parts else None
            if root_part in ("Users", "My Documents") and self._drive_gap_pending:
                self.parts.insert(0, self.drive)
                self.part_trace.insert(
                    0, {"component": self.drive, "item_index": index, "certain": True}
                )
                for gap in self.gaps:
                    gap["position"] += 1
                self.gaps = [
                    gap
                    for gap in self.gaps
                    if not (gap["missing"] == "drive letter" and gap["position"] == 1)
                ]
            else:
                self._add_part(self.drive, index)
            self._drive_gap_pending = False
            self._item_segment(index, target, "volume", "filesystem", display_name, True)
            return

        self.namespace = "virtual"
        self.filesystem_ok = False
        self._add_gap(
            index,
            "volume item does not identify a file system volume",
            "file system volume",
            breaks_chain=False,
        )
        if display_name:
            self._add_part(display_name, index, certain=False)
        self._item_segment(index, target, "volume", "virtual", display_name, False)
        # The "file system volume" gap already describes the missing
        # connection; a virtual volume cannot supply the drive anchor either.
        self._drive_gap_pending = False

    def _add_delegate(self, index, target):
        try:
            embedded = target.file_entry().primary_name()
        except Exception:
            embedded = None

        # The delegate folder is a virtual bridge. Its parent profile
        # directory is not present in the PIDL, so no later name may fill it.
        self._drive_gap_pending = False
        self._add_gap(
            index,
            "user profile directory name is not encoded in the PIDL",
            "user profile directory name",
        )
        if embedded:
            # The embedded file entry is relative to the user profile, which
            # is exactly the connection that is missing. The name is known,
            # but it is not a confirmed child of the preceding root.
            self._add_part(embedded, index, certain=False)

        self.namespace = "delegate"
        self.filesystem_ok = True
        self.chain_broken = True
        self._item_segment(index, target, "virtual_delegate", "virtual", embedded, True)

    def _add_file_entry(self, index, target, item_type):
        display_name = target.display_name()
        if item_type is not None and (item_type & 0x70) == 0x30:
            if self.filesystem_ok and display_name:
                if self.chain_broken:
                    # The disk namespace is known, but an earlier connection
                    # is not. Keep the name in the display/hierarchy; it is
                    # not a confirmed component of a usable path.
                    self._add_part(display_name, index, certain=False)
                    self._item_segment(
                        index, target, "file_entry", "filesystem", display_name, False
                    )
                    return
                self._add_part(display_name, index)
                self._item_segment(index, target, "file_entry", "filesystem", display_name, True)
            else:
                self.filesystem_ok = False
                if not self.chain_broken:
                    self._add_gap(
                        index,
                        "file entry has no supported parent namespace",
                        "parent namespace",
                    )
                if display_name:
                    self._add_part(display_name, index, certain=False)
                self._item_segment(index, target, "file_entry", "unknown", display_name, False)

            if not 0x30 <= item_type <= 0x3F:
                self._add_gap(
                    index,
                    f"item type 0x{item_type:02X} uses an undocumented file entry variant",
                    "documented file entry type",
                )
            return

        self.namespace = "unknown"
        self.filesystem_ok = False
        if not self.chain_broken:
            self._add_gap(index, "item namespace is unknown", "parent namespace")
        self._item_segment(index, target, "unknown", "unknown", display_name, False)

    def _add_virtual(self, index, target, display_name, namespace):
        self.namespace = namespace
        self.filesystem_ok = False
        self._add_gap(
            index, f"{namespace} namespace is not a file system parent", "file system parent"
        )
        if display_name:
            self._add_part(display_name, index, certain=False)
        self._item_segment(index, target, "virtual", namespace, display_name, False)

    # -- result ------------------------------------------------------------

    def _confirmed_parts(self):
        return [entry["component"] for entry in self.part_trace if entry["certain"]]

    def _link_info_check(self):
        if not self.link_info_path:
            return {
                "provided": False,
                "corroborates": False,
                "ordered_match": False,
                "suffix_match": False,
                "matched_components": 0,
            }
        confirmed = self._confirmed_parts()
        haystack = _path_parts(self.link_info_path)
        if not confirmed:
            return {
                "provided": True,
                "corroborates": False,
                "ordered_match": False,
                "suffix_match": False,
                "matched_components": 0,
            }
        ordered = _ordered_match(haystack, confirmed)
        suffix = haystack[-len(confirmed) :]
        suffix_ok = len(suffix) == len(confirmed) and all(
            _same_component(left, right) for left, right in zip(suffix, confirmed, strict=False)
        )
        return {
            "provided": True,
            "corroborates": ordered,
            "ordered_match": ordered,
            "suffix_match": suffix_ok,
            "matched_components": len(confirmed) if ordered else 0,
        }

    def _pidl_path(self):
        if not self.filesystem_ok or self.gaps:
            return None
        if not self.parts:
            return None
        first = self.parts[0]
        if not (len(first) == 3 and first[1] == ":" and first.endswith("\\")):
            return None
        return _normalize_path(
            "".join(part if part.endswith("\\") else part + "\\" for part in self.parts)
        )

    def _hierarchy(self):
        """Confirmed components with explicit placeholders for missing links.

        A placeholder marks a namespace connection that the item chain does
        not identify. It is never replaced by the next display name. Gaps at
        the end of the chain are kept in ``uncertain_connections`` only, since
        there is no following component to attach them to.
        """
        pieces = list(self.parts)
        positions = {}
        for gap in self.gaps:
            positions.setdefault(gap["position"], []).append(gap["missing"])
        for position in sorted(positions, reverse=True):
            missing = positions[position]
            if not missing or position < 0 or position >= len(pieces):
                continue
            marker = "<missing:" + " + ".join(dict.fromkeys(missing)) + ">"
            pieces.insert(position, marker)
        return pieces

    def _partial_path(self):
        pieces = self._hierarchy()
        if not pieces:
            return None
        if not any(piece.startswith("<missing:") for piece in pieces):
            return None
        text = "\\".join(pieces)
        first_component = next((piece for piece in pieces if not piece.startswith("<missing:")), "")
        has_drive = len(first_component) == 3 and first_component[1] == ":"
        has_drive_marker = pieces[0] == "<missing:drive letter>"
        if has_drive_marker:
            return "<missing:drive letter>:\\" + "\\".join(pieces[1:])
        if not has_drive:
            text = "<missing:drive letter>:\\" + text
        return text

    def build(self):
        for index, record in enumerate(self.records):
            if record["target"] is not None:
                self.add_item(index, record["target"])

        if (
            self._drive_gap_pending
            and self.parts
            and not any(gap["missing"] == "drive letter" for gap in self.gaps)
        ):
            self._add_gap(
                None,
                "drive letter is not encoded in the PIDL",
                "drive letter",
                position=0,
                breaks_chain=False,
            )
            self._drive_gap_pending = False

        display_names = [
            segment["display_name"] for segment in self.segments if segment["display_name"]
        ]
        pidl_path = self._pidl_path()
        link_info = self._link_info_check()

        if pidl_path:
            status = "resolved"
        elif self.gaps:
            status = "partial"
        else:
            status = "unknown"

        return {
            "status": status,
            "display_name": _join_display(display_names),
            "path": pidl_path,
            "path_source": "pidl" if pidl_path else None,
            "partial_path": self._partial_path() if status == "partial" else None,
            "hierarchy": self._hierarchy(),
            "link_info_path": self.link_info_path,
            "link_info_check": link_info,
            "namespace": self.namespace,
            "drive": self.drive,
            "segments": self.segments,
            "item_trace": [
                {
                    "index": segment["index"],
                    "item_type": segment["item_type"],
                    "class": segment["class"],
                    "offset": segment["offset"],
                    "size": segment["size"],
                }
                for segment in self.segments
            ],
            "confirmed_components": self._confirmed_parts(),
            "component_trace": self.part_trace,
            "uncertain_connections": self.gaps,
        }


def build_location(targets, link_info=None, link_info_path=None):
    if link_info_path is None:
        link_info_path = link_info
    return LocationBuilder(targets, link_info_path=link_info_path).build()
