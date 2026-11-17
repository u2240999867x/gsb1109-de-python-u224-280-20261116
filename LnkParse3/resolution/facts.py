"""Extract resolution facts from a parsed LNK without losing variants.

The parser intentionally exposes some fields through convenience accessors that
choose one encoding or one LinkInfo sub-structure.  Resolution needs every
variant at once: the ANSI and Unicode spellings, the local and network halves of
a LinkInfo, and the environment ANSI/Unicode pair.  This module reads those
values independently and attaches a reference to the exact source field to each
one.
"""

from dataclasses import dataclass
from dataclasses import field

from LnkParse3.info.network import Network


@dataclass
class SourceField:
    """A value together with the original field it was read from."""

    source: str
    field: str
    value: object
    json_path: str = None
    encoding: str = None
    item_index: int = None
    item_class: str = None

    def as_dict(self):
        result = {
            "source": self.source,
            "field": self.field,
            "value": self.value,
        }
        if self.json_path:
            result["path"] = self.json_path
        if self.encoding:
            result["encoding"] = self.encoding
        if self.item_index is not None:
            result["item_index"] = self.item_index
        if self.item_class:
            result["item_class"] = self.item_class
        return result

    def key(self):
        return (
            self.source,
            self.field,
            self.item_index,
            self.encoding,
            str(self.value),
        )


@dataclass
class ShellItem:
    index: int
    item_class: str
    values: dict = field(default_factory=dict)
    raw: object = None

    def get(self, key, default=None):
        return self.values.get(key, default)

    def source(self, source_name="idlist", field_name=None):
        field_name = field_name or "item"
        return SourceField(
            source=source_name,
            field=field_name,
            value=self.values.get(field_name),
            json_path=f"target.items[{self.index}].{field_name}",
            item_index=self.index,
            item_class=self.item_class,
        )


@dataclass
class LocalVariant:
    encoding: str
    base: SourceField
    suffix: SourceField
    volume: dict = field(default_factory=dict)


@dataclass
class NetworkVariant:
    encoding: str
    net_name: SourceField
    device_name: SourceField = None
    suffix: SourceField = None
    flags: int = None


@dataclass
class LnkFacts:
    link_flags: list = field(default_factory=list)
    file_flags: list = field(default_factory=list)
    id_items: list = field(default_factory=list)
    id_list_present: bool = False
    local_variants: list = field(default_factory=list)
    network_variants: list = field(default_factory=list)
    has_link_info: bool = False
    force_no_link_info: bool = False
    relative_path: SourceField = None
    working_directory: SourceField = None
    environment_variants: list = field(default_factory=list)
    environment_present: bool = False
    volume_identity: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)
    file_size: int = None
    source: str = "lnk"

    def has_flag(self, name):
        return name in self.link_flags


def _field(source, field_name, value, json_path, encoding=None, **kwargs):
    if value is None:
        return None
    return SourceField(
        source=source,
        field=field_name,
        value=value,
        json_path=json_path,
        encoding=encoding,
        **kwargs,
    )


def _safe(callable_, default=None):
    try:
        return callable_()
    except Exception:
        return default


class _FactsFromLnk:
    def __init__(self, lnk):
        self.lnk = lnk

    def build(self):
        lnk = self.lnk
        header = lnk.header
        facts = LnkFacts(
            link_flags=list(_safe(header.link_flags, []) or []),
            file_flags=list(_safe(header.file_flags, []) or []),
            file_size=_safe(header.file_size),
            has_link_info=bool(_safe(lnk.has_link_info, False)),
            force_no_link_info=bool(_safe(lnk.force_no_link_info, False)),
            id_list_present=bool(_safe(lnk.has_target_id_list, False)),
            source="LnkFile",
        )

        self._extract_string_data(facts)
        self._extract_targets(facts)
        self._extract_link_info(facts)
        self._extract_extras(facts)
        return facts

    def _extract_string_data(self, facts):
        data = _safe(self.lnk.string_data.as_dict, {}) or {}
        if data.get("relative_path") is not None:
            facts.relative_path = _field(
                "relative_path",
                "relative_path",
                data["relative_path"],
                "data.relative_path",
            )
        if data.get("working_directory") is not None:
            facts.working_directory = _field(
                "relative_path",
                "working_directory",
                data["working_directory"],
                "data.working_directory",
            )

    def _extract_targets(self, facts):
        targets = getattr(self.lnk, "targets", None)
        if not targets:
            return
        for index, target in enumerate(targets):
            item = _safe(target.as_item, {}) or {}
            item_class = item.get("class") or getattr(target, "name", "Unknown")
            facts.id_items.append(
                ShellItem(index=index, item_class=item_class, values=item, raw=target)
            )

    def _extract_link_info(self, facts):
        info = getattr(self.lnk, "info", None)
        if info is None:
            return

        raw = getattr(info, "raw_data", lambda: getattr(info, "_raw", b""))()
        cp = getattr(self.lnk, "cp", None) or "cp1252"

        flags = _safe(info.flags, 0) or 0
        has_local = bool(flags & 0x0001)
        has_network = bool(flags & 0x0002)

        # LinkInfoFlags can carry both bits.  The parser picks one subclass,
        # but the raw bytes still contain both structures and resolution must
        # not lose one of them because of that choice.
        if not has_local and not has_network:
            if isinstance(info, Network):
                has_network = True
            else:
                has_local = True

        if has_local:
            facts.volume_identity = self._volume_identity(info)
            self._extract_local_variants(facts, info)
        if has_network:
            self._extract_network_variants(facts, info, raw, cp)

    def _volume_identity(self, info):
        identity = {}
        for name, json_name in (
            ("drive_type", "link_info.location_info.drive_type"),
            ("volume_label", "link_info.location_info.volume_label"),
            ("drive_serial_number", "link_info.location_info.drive_serial_number"),
        ):
            value = _safe(getattr(info, name, lambda: None))
            if value is not None:
                identity[name] = _field("link_info_local", name, value, json_name)
        unicode_label = _safe(getattr(info, "volume_label_unicode", lambda: None))
        if unicode_label is not None:
            identity["volume_label_unicode"] = _field(
                "link_info_local",
                "volume_label_unicode",
                unicode_label,
                "link_info.location_info.volume_label_unicode",
                encoding="unicode",
            )
        return identity

    def _extract_local_variants(self, facts, info):
        base_ansi = _safe(info.local_base_path)
        suffix_ansi = _safe(info.common_path_suffix)
        if base_ansi is not None:
            facts.local_variants.append(
                LocalVariant(
                    encoding="ansi",
                    base=_field(
                        "link_info_local",
                        "local_base_path",
                        base_ansi,
                        "link_info.local_base_path",
                        encoding="ansi",
                    ),
                    suffix=_field(
                        "link_info_local",
                        "common_path_suffix",
                        suffix_ansi or "",
                        "link_info.common_path_suffix",
                        encoding="ansi",
                    ),
                    volume=dict(facts.volume_identity),
                )
            )

        base_unicode = _safe(info.local_base_path_unicode)
        if base_unicode is not None:
            suffix_unicode = _safe(info.common_path_suffix_unicode)
            facts.local_variants.append(
                LocalVariant(
                    encoding="unicode",
                    base=_field(
                        "link_info_local",
                        "local_base_path_unicode",
                        base_unicode,
                        "link_info.local_base_path_unicode",
                        encoding="unicode",
                    ),
                    suffix=_field(
                        "link_info_local",
                        "common_path_suffix_unicode",
                        suffix_unicode or "",
                        "link_info.common_path_suffix_unicode",
                        encoding="unicode",
                    ),
                    volume=dict(facts.volume_identity),
                )
            )

    def _extract_network_variants(self, facts, info, raw, cp):
        network = info if isinstance(info, Network) else _safe(lambda: Network(indata=raw, cp=cp))
        if network is None:
            return

        suffix_ansi = _field(
            "link_info_network",
            "common_path_suffix",
            _safe(network.common_path_suffix) or "",
            "link_info.common_path_suffix",
            encoding="ansi",
        )

        net_ansi = _safe(network.net_name)
        if net_ansi is not None:
            facts.network_variants.append(
                NetworkVariant(
                    encoding="ansi",
                    net_name=_field(
                        "link_info_network",
                        "net_name",
                        net_ansi,
                        "link_info.location_info.net_name",
                        encoding="ansi",
                    ),
                    device_name=_field(
                        "link_info_network",
                        "device_name",
                        _safe(network.device_name),
                        "link_info.location_info.device_name",
                        encoding="ansi",
                    ),
                    suffix=suffix_ansi,
                    flags=_safe(network.common_network_relative_link_flags),
                )
            )

        net_unicode = _safe(network.net_name_unicode)
        if net_unicode is not None:
            facts.network_variants.append(
                NetworkVariant(
                    encoding="unicode",
                    net_name=_field(
                        "link_info_network",
                        "net_name_unicode",
                        net_unicode,
                        "link_info.location_info.net_name_unicode",
                        encoding="unicode",
                    ),
                    device_name=_field(
                        "link_info_network",
                        "device_name_unicode",
                        _safe(network.device_name_unicode),
                        "link_info.location_info.device_name_unicode",
                        encoding="unicode",
                    ),
                    suffix=suffix_ansi,
                    flags=_safe(network.common_network_relative_link_flags),
                )
            )

    def _extract_extras(self, facts):
        extras = {}
        for extra in getattr(self.lnk, "extras", []) or []:
            name = _safe(extra.name, type(extra).__name__)
            value = _safe(extra.as_dict, {}) or {}
            extras[name] = value

            if name != "ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK":
                continue

            facts.environment_present = True
            ansi = value.get("target_ansi")
            unicode = value.get("target_unicode")
            if ansi is not None:
                facts.environment_variants.append(
                    _field(
                        "environment",
                        "target_ansi",
                        ansi,
                        "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_ansi",
                        encoding="ansi",
                    )
                )
            if unicode is not None:
                facts.environment_variants.append(
                    _field(
                        "environment",
                        "target_unicode",
                        unicode,
                        "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
                        encoding="unicode",
                    )
                )
        facts.extras = extras


def facts_from_lnk(lnk):
    """Build :class:`LnkFacts` from a parsed ``LnkFile`` instance."""
    return _FactsFromLnk(lnk).build()


class _FactsFromDict:
    def __init__(self, data):
        self.data = data or {}

    def build(self):
        header = self.data.get("header") or {}
        facts = LnkFacts(
            link_flags=list(header.get("link_flags") or []),
            file_flags=list(header.get("file_flags") or []),
            file_size=header.get("file_size"),
            has_link_info="HasLinkInfo" in (header.get("link_flags") or []),
            force_no_link_info="ForceNoLinkInfo" in (header.get("link_flags") or []),
            id_list_present="HasTargetIDList" in (header.get("link_flags") or []),
            source="dict",
        )
        data = self.data.get("data") or {}
        if data.get("relative_path") is not None:
            facts.relative_path = _field(
                "relative_path",
                "relative_path",
                data["relative_path"],
                "data.relative_path",
            )
        if data.get("working_directory") is not None:
            facts.working_directory = _field(
                "relative_path",
                "working_directory",
                data["working_directory"],
                "data.working_directory",
            )

        target = self.data.get("target") or {}
        for index, item in enumerate(target.get("items") or []):
            facts.id_items.append(
                ShellItem(
                    index=index,
                    item_class=item.get("class") or "Unknown",
                    values=item,
                )
            )

        self._link_info(facts)
        self._extras(facts)
        return facts

    def _link_info(self, facts):
        info = self.data.get("link_info") or {}
        if not info:
            return
        location_info = info.get("location_info") or {}
        flags = info.get("link_info_flags")
        has_local = bool((flags or 0) & 1) or info.get("location") == "Local"
        has_network = bool((flags or 0) & 2) or info.get("location") == "Network"

        if has_local:
            volume = {}
            for name, json_name in (
                ("drive_type", "link_info.location_info.drive_type"),
                ("volume_label", "link_info.location_info.volume_label"),
                ("drive_serial_number", "link_info.location_info.drive_serial_number"),
            ):
                if location_info.get(name) is not None:
                    volume[name] = _field("link_info_local", name, location_info[name], json_name)
            facts.volume_identity = volume

            for encoding, base_key, suffix_key in (
                ("ansi", "local_base_path", "common_path_suffix"),
                (
                    "unicode",
                    "local_base_path_unicode",
                    "common_path_suffix_unicode",
                ),
            ):
                if info.get(base_key) is None:
                    continue
                facts.local_variants.append(
                    LocalVariant(
                        encoding=encoding,
                        base=_field(
                            "link_info_local",
                            base_key,
                            info.get(base_key),
                            f"link_info.{base_key}",
                            encoding=encoding,
                        ),
                        suffix=_field(
                            "link_info_local",
                            suffix_key,
                            info.get(suffix_key) or "",
                            f"link_info.{suffix_key}",
                            encoding=encoding,
                        ),
                        volume=dict(volume),
                    )
                )

        if has_network:
            for encoding, net_key, dev_key in (
                ("ansi", "net_name", "device_name"),
                ("unicode", "net_name_unicode", "device_name_unicode"),
            ):
                if location_info.get(net_key) is None:
                    continue
                facts.network_variants.append(
                    NetworkVariant(
                        encoding=encoding,
                        net_name=_field(
                            "link_info_network",
                            net_key,
                            location_info[net_key],
                            f"link_info.location_info.{net_key}",
                            encoding=encoding,
                        ),
                        device_name=_field(
                            "link_info_network",
                            dev_key,
                            location_info.get(dev_key),
                            f"link_info.location_info.{dev_key}",
                            encoding=encoding,
                        ),
                        suffix=_field(
                            "link_info_network",
                            "common_path_suffix",
                            info.get("common_path_suffix") or "",
                            "link_info.common_path_suffix",
                            encoding="ansi",
                        ),
                        flags=location_info.get("common_network_relative_link_flags"),
                    )
                )

    def _extras(self, facts):
        extra = self.data.get("extra") or {}
        facts.extras = extra
        env = extra.get("ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK")
        if not env:
            return
        facts.environment_present = True
        for key, encoding in (("target_ansi", "ansi"), ("target_unicode", "unicode")):
            if env.get(key) is not None:
                facts.environment_variants.append(
                    _field(
                        "environment",
                        key,
                        env[key],
                        f"extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.{key}",
                        encoding=encoding,
                    )
                )


def facts_from_dict(data):
    """Build :class:`LnkFacts` from a ``LnkFile.get_json``-style mapping."""
    return _FactsFromDict(data).build()


def extract_facts(lnk):
    if isinstance(lnk, dict):
        return facts_from_dict(lnk)
    return facts_from_lnk(lnk)
