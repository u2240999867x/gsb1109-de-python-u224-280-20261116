"""Build minimal offline icon packs from XDG icon themes.

The module is intentionally self contained: it reads the theme metadata with
:class:`xdg.IconTheme.IconTheme`, applies the icon-theme size rules, and returns
a deterministic selection of files whose total volume is as small as possible.

The public entry point is :func:`select_icon_pack` (also exported as
``build_icon_pack`` and ``plan_icon_pack``).  A target describes an icon name,
its nominal size, the requested scale factor and the amount of size error the
caller is willing to accept::

    >>> from xdg.IconPack import select_icon_pack
    >>> result = select_icon_pack(
    ...     [{"name": "folder", "size": 24, "scale": 1, "tolerance": 2}],
    ...     theme="Adwaita",
    ... )
    >>> result.selected[0]["path"]

Theme inheritance is followed in declaration order.  Cycles are detected by
the resolved ``index.theme`` path, so an inheritance loop cannot recurse
forever.  Candidate files are never identified by icon name alone: the theme,
its directory and the actual file path all remain part of the decision, and a
file is included in the resulting pack only once even when it serves several
targets.
"""

from __future__ import print_function

import json
import os
import sys

from xdg.Exceptions import ParsingError
from xdg.IconTheme import IconTheme
import xdg.IconTheme as _icon_theme_module


DEFAULT_EXTENSIONS = ("png", "svg", "xpm")
DEFAULT_FALLBACK_THEME = "hicolor"

try:
    string_types = (basestring,)  # Python 2
except NameError:
    string_types = (str,)


class _Record(dict):
    """A JSON-friendly mapping whose keys are also available as attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _StringComparableRecord(_Record):
    """A record which compares equal to its natural string identifier."""

    def _string_value(self):
        return self.get("path") or self.get("target")

    def __eq__(self, other):
        if isinstance(other, string_types):
            return self._string_value() == other
        return dict.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __lt__(self, other):
        if isinstance(other, string_types):
            return (self._string_value() or "") < other
        return NotImplemented

    def __str__(self):
        return str(self._string_value() or "")


def _as_number(value, what, minimum=None):
    if isinstance(value, bool):
        raise ValueError("%s must be a number, not a boolean" % what)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number: %r" % (what, value))
    if minimum is not None and number < minimum:
        raise ValueError("%s must be >= %s: %r" % (what, minimum, value))
    if number == int(number):
        return int(number)
    return number


class IconTarget(object):
    """One requested icon.

    ``size`` is the nominal, unscaled icon size (the ``Size`` value in an icon
    theme).  ``scale`` is the device scale factor (``Scale`` in the theme), and
    ``tolerance`` is the permitted difference between the target size and a
    directory's nominal size, in the same nominal units.  If ``pixel_size`` is
    supplied instead of ``size``, the nominal size is derived as
    ``pixel_size / scale``.
    """

    __slots__ = ("name", "size", "scale", "tolerance", "pixel_size", "data")

    def __init__(self, name, size=None, scale=1, tolerance=0, pixel_size=None,
                 tolerance_ratio=None):
        if not name or not isinstance(name, string_types):
            raise ValueError("icon target name must be a non-empty string")
        if scale is None:
            scale = 1
        scale = _as_number(scale, "scale", 0)
        if scale == 0:
            raise ValueError("scale must be greater than zero")

        if size is None and pixel_size is not None:
            size = _as_number(pixel_size, "pixel_size", 0) / scale
        if size is None:
            raise ValueError("icon target %r has no size" % name)

        self.name = name
        self.size = _as_number(size, "size", 0)
        self.scale = scale
        self.tolerance = _as_number(tolerance, "tolerance", 0)
        if tolerance_ratio is not None:
            self.tolerance = self._ratio_tolerance(tolerance_ratio)
        self.pixel_size = pixel_size
        self.data = {}

    def _ratio_tolerance(self, ratio):
        ratio = _as_number(ratio, "scaling error", 0)
        if ratio > 1:
            # Values above one are read as percentages, e.g. 10 -> 10%.
            ratio = ratio / 100.0
        return self.size * ratio

    def as_dict(self):
        result = {
            "name": self.name,
            "size": self.size,
            "scale": self.scale,
            "tolerance": self.tolerance,
        }
        if self.pixel_size is not None:
            result["pixel_size"] = self.pixel_size
        result.update(self.data)
        return result

    def __repr__(self):
        return "IconTarget(%r, size=%r, scale=%r, tolerance=%r)" % (
            self.name, self.size, self.scale, self.tolerance)


def _target_iterable(targets):
    """Normalise the different convenient target container forms."""

    if isinstance(targets, IconTarget):
        return [targets]
    if isinstance(targets, (tuple, list)) and 2 <= len(targets) <= 4 \
            and isinstance(targets[0], string_types) \
            and isinstance(targets[1], (int, float)):
        # A lone ``(name, size)`` tuple is one target rather than a list of
        # one-character icon names.
        return [targets]
    if isinstance(targets, dict):
        # A mapping with a name is one target; a mapping of names to specs is
        # a convenient shorthand for several targets.
        if "name" in targets or "icon" in targets or "icon_name" in targets:
            return [targets]
        result = []
        for name, value in targets.items():
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("name", name)
            elif isinstance(value, (tuple, list)):
                entry = [name] + list(value)
            else:
                entry = {"name": name, "size": value}
            result.append(entry)
        return result
    return targets


def _coerce_target(value, default_tolerance=0, default_scale=1):
    if isinstance(value, IconTarget):
        return value

    if isinstance(value, string_types):
        raise ValueError("icon target %r needs a size" % value)

    if isinstance(value, dict):
        return _target_from_dict(value, default_tolerance, default_scale)

    if isinstance(value, (tuple, list)):
        if len(value) < 2:
            raise ValueError("icon target tuple needs a name and a size")
        scale = value[2] if len(value) > 2 else default_scale
        tolerance = value[3] if len(value) > 3 else default_tolerance
        return IconTarget(value[0], size=value[1], scale=scale,
                          tolerance=tolerance)

    return _target_from_object(value, default_tolerance, default_scale)


def _target_from_dict(data, default_tolerance=0, default_scale=1):
    data = dict(data)
    name = data.get("name", data.get("icon", data.get("icon_name",
                       data.get("iconName"))))
    size = data.get("size", data.get("nominal_size", data.get("nominalSize")))
    pixel_size = data.get("pixel_size", data.get("pixelSize",
                        data.get("pixels")))
    scale = data.get("scale", data.get("scale_factor",
                     data.get("scaleFactor", default_scale)))
    tolerance = data.get(
        "tolerance",
        data.get("allowed_error", data.get("allowedError",
                data.get("scaling_error", data.get("scalingError",
                        default_tolerance)))),
    )
    tolerance_ratio = data.get(
        "tolerance_ratio",
        data.get("scaling_error_ratio", data.get(
            "scalingErrorRatio", data.get("allowed_scaling_error",
                                          data.get("scale_error")))))
    target = IconTarget(name, size=size, scale=scale, tolerance=tolerance,
                        pixel_size=pixel_size,
                        tolerance_ratio=tolerance_ratio)
    known = set((
        "name", "icon", "icon_name", "iconName", "size", "nominal_size",
        "nominalSize", "pixel_size", "pixelSize", "pixels", "scale",
        "scale_factor", "scaleFactor", "tolerance", "allowed_error",
        "allowedError", "scaling_error", "scalingError", "tolerance_ratio",
        "scaling_error_ratio", "scalingErrorRatio", "allowed_scaling_error",
        "scale_error",
    ))
    target.data.update(dict((key, value) for (key, value) in data.items()
                            if key not in known))
    return target


def _target_from_object(value, default_tolerance=0, default_scale=1):
    """Accept duck-typed targets, including objects parsed from JSON."""

    name = getattr(value, "name", None)
    size = getattr(value, "size", None)
    if name is None or size is None:
        # Some integrations use ``get``/``__getitem__`` rather than
        # attributes; fall back to a mapping-like view when possible.
        try:
            mapping = dict(value)
        except (TypeError, ValueError):
            mapping = None
        if mapping is not None:
            return _target_from_dict(mapping, default_tolerance, default_scale)
        raise ValueError("cannot interpret icon target %r" % (value,))
    return IconTarget(
        name,
        size=size,
        scale=getattr(value, "scale", getattr(value, "scale_factor",
                                               default_scale)),
        tolerance=getattr(value, "tolerance", getattr(
            value, "allowed_error", default_tolerance)),
        pixel_size=getattr(value, "pixel_size", None),
        tolerance_ratio=getattr(value, "tolerance_ratio", getattr(
            value, "scaling_error_ratio", None)),
    )


class _ThemeNode(object):
    __slots__ = ("theme", "name", "index_path", "theme_dir", "roots", "order")

    def __init__(self, theme, index_path, roots, order):
        self.theme = theme
        self.name = theme.name
        self.index_path = index_path
        self.theme_dir = theme.dir
        self.roots = roots
        self.order = order


class _Directory(object):
    __slots__ = ("node", "subdir", "path", "index", "root_index", "type",
                 "size", "scale", "threshold", "min_size", "max_size")

    def __init__(self, node, subdir, path, index, root_index):
        theme = node.theme
        self.node = node
        self.subdir = subdir
        self.path = path
        self.index = index
        self.root_index = root_index
        self.type = theme.getType(subdir)
        self.size = theme.getSize(subdir)
        self.scale = theme.getScale(subdir)
        self.threshold = theme.getThreshold(subdir)
        self.min_size = theme.getMinSize(subdir)
        self.max_size = theme.getMaxSize(subdir)

    def info(self):
        return {
            "theme": self.node.name,
            "directory": self.subdir,
            "directory_path": self.path,
            "directory_type": self.type,
            "directory_size": self.size,
            "directory_scale": self.scale,
            "directory_threshold": self.threshold,
            "directory_min_size": self.min_size,
            "directory_max_size": self.max_size,
        }


class _Candidate(object):
    __slots__ = ("path", "key", "volume", "directory", "extension",
                 "extension_index", "distance")

    def __init__(self, path, key, volume, directory, extension,
                 extension_index, distance):
        self.path = path
        self.key = key
        self.volume = volume
        self.directory = directory
        self.extension = extension
        self.extension_index = extension_index
        self.distance = distance


class _Inventory(object):
    """Caches directory listings and readability checks for one plan."""

    def __init__(self):
        self._entries = {}
        self._readable = {}

    def entries(self, path):
        if path in self._entries:
            return self._entries[path]
        try:
            result = (os.listdir(path), None)
        except OSError as exc:
            result = (None, exc)
        self._entries[path] = result
        return result

    def readable(self, path):
        if path in self._readable:
            return self._readable[path]
        if not os.path.lexists(path):
            result = (False, "missing")
        elif not os.path.exists(path):
            result = (False, "broken symbolic link")
        elif not os.path.isfile(path):
            result = (False, "not a regular file")
        else:
            try:
                fd = open(path, "rb")
                try:
                    fd.read(1)
                finally:
                    fd.close()
            except (IOError, OSError) as exc:
                result = (False, getattr(exc, "strerror", None) or str(exc))
            else:
                result = (True, None)
        self._readable[path] = result
        return result


def _normalise_path(path):
    return os.path.normpath(os.path.abspath(path))


def _file_key(path):
    """Identify the actual file, not merely its icon name.

    Resolving the real path and, where possible, the inode makes a symlink and
    its target count as one packaged file while keeping identically named
    icons from separate themes distinct.
    """

    try:
        real = os.path.realpath(path)
        stat = os.stat(real)
    except (IOError, OSError):
        return _normalise_path(path)
    return (stat.st_dev, stat.st_ino)


class _VolumeTable(object):
    def __init__(self, file_sizes=None, extensions=DEFAULT_EXTENSIONS):
        self.exact = {}
        self.basenames = {}
        self.extensions = {}
        self.callable = None

        if file_sizes is None:
            return
        if callable(file_sizes):
            self.callable = file_sizes
            return

        if isinstance(file_sizes, dict):
            items = list(file_sizes.items())
        elif isinstance(file_sizes, (list, tuple)):
            items = []
            for entry in file_sizes:
                if isinstance(entry, dict):
                    path = entry.get("path", entry.get("file"))
                    volume = entry.get(
                        "volume", entry.get("size", entry.get("bytes")))
                    if path is None or volume is None:
                        raise ValueError("file size entry needs 'path' and 'volume'")
                    items.append((path, volume))
                else:
                    path, volume = entry
                    items.append((path, volume))
        else:
            raise ValueError("file_sizes must be a mapping, list or callable")

        ext_names = set(extensions)
        for key, value in items:
            if isinstance(value, dict):
                value = value.get(
                    "volume", value.get("size", value.get("bytes", value)))
            value = _as_number(value, "volume for %r" % (key,), 0)
            key = str(key)
            plain = key[1:] if key.startswith(".") else key
            if plain in ext_names and not os.path.exists(key):
                self.extensions[plain] = value
                continue
            self.exact[_normalise_path(key)] = value
            try:
                self.exact[os.path.realpath(key)] = value
            except OSError:
                pass
            if os.path.basename(key) == key:
                self.basenames[key] = value

    def volume(self, path):
        if self.callable is not None:
            value = self.callable(path)
            return _as_number(value, "volume for %r" % path, 0)

        for key in (_normalise_path(path), os.path.realpath(path), path):
            if key in self.exact:
                return self.exact[key]
        basename = os.path.basename(path)
        if basename in self.basenames:
            return self.basenames[basename]
        extension = os.path.splitext(path)[1].lstrip(".")
        if extension in self.extensions:
            return self.extensions[extension]
        try:
            return os.path.getsize(path)
        except OSError:
            return 0


def _theme_from_reference(reference):
    """Load a theme object from a path or return an existing IconTheme."""

    if isinstance(reference, IconTheme):
        return reference, reference.filename

    if not isinstance(reference, string_types):
        raise ValueError("theme must be a theme name, path or IconTheme object")

    if os.path.isdir(reference):
        for filename in ("index.theme", "index.desktop"):
            index = os.path.join(reference, filename)
            if os.path.isfile(index):
                return _load_theme(index)
        raise ValueError("no index.theme in theme directory %r" % reference)

    if os.path.isfile(reference):
        return _load_theme(reference)

    return None


def _load_theme(index_path):
    theme = IconTheme()
    try:
        theme.parse(index_path)
    except (IOError, OSError, ParsingError, UnicodeError) as exc:
        return None, index_path, (
            getattr(exc, "strerror", None) or str(exc))
    return theme, index_path, None


def _find_index(theme_name, search_paths):
    """Find a theme index, returning its path, first root and unreadable error."""
    first_error = None
    for root in search_paths:
        theme_dir = os.path.join(root, theme_name)
        for filename in ("index.theme", "index.desktop"):
            index = os.path.join(theme_dir, filename)
            if os.path.lexists(index):
                if not os.access(index, os.R_OK):
                    first_error = first_error or index
                    continue
                return index, root, None
    return None, None, first_error


def _theme_roots(theme_name, search_paths):
    """Data roots which contain a directory named after the theme.

    The XDG icon theme spec lets one theme be assembled from several data
    roots.  Root order is preserved so the caller's precedence is explicit.
    """
    roots = []
    for root in search_paths:
        if os.path.isdir(os.path.join(root, theme_name)):
            roots.append(root)
    return roots


def _resolve_theme_chain(theme, search_paths, fallback_theme=DEFAULT_FALLBACK_THEME,
                         include_fallback=True):
    """Resolve a theme and its inheritance chain without following loops."""

    nodes = []
    errors = []
    visited = set()

    def add_reference(reference, parent=None):
        if isinstance(reference, IconTheme):
            node_theme = reference
            index_path = reference.filename
            load_error = None
            name = reference.name
        else:
            name = reference
            index_path, first_root, unreadable = _find_index(name, search_paths)
            if index_path is None:
                if unreadable is not None:
                    errors.append(_Record({
                        "type": "theme_unreadable",
                        "theme": name,
                        "parent": parent,
                        "path": unreadable,
                        "reason": "theme index is not readable",
                    }))
                else:
                    errors.append(_Record({
                        "type": "theme_missing",
                        "theme": name,
                        "parent": parent,
                        "path": None,
                        "reason": "theme index not found",
                    }))
                return
            node_theme, index_path, load_error = _load_theme(index_path)
            if node_theme is None:
                errors.append(_Record({
                    "type": "theme_unreadable",
                    "theme": name,
                    "parent": parent,
                    "path": index_path,
                    "reason": load_error,
                }))
                return

        key = os.path.realpath(index_path) if index_path else node_theme.dir
        if key in visited:
            return
        visited.add(key)

        roots = _theme_roots(node_theme.name, search_paths)
        explicit_root = os.path.dirname(node_theme.dir)
        if explicit_root not in roots:
            roots.insert(0, explicit_root)
        node = _ThemeNode(node_theme, index_path, roots, len(nodes))
        nodes.append(node)

        for subtheme in node_theme.getInherits():
            add_reference(subtheme, parent=node.name)

    if isinstance(theme, IconTheme):
        add_reference(theme)
    else:
        name = theme
        if name is None:
            name = _icon_theme_module.xdg.Config.icon_theme
        add_reference(name)

    if include_fallback and fallback_theme:
        fallback_key = None
        fallback_index, _root, _unreadable = _find_index(
            fallback_theme, search_paths)
        if fallback_index is not None:
            fallback_key = os.path.realpath(fallback_index)
        if fallback_key is not None and fallback_key not in visited:
            add_reference(fallback_theme)

    return nodes, errors


def _iter_directories(node):
    theme = node.theme
    names = []
    seen = set()
    for key in ("Directories", "ScaledDirectories"):
        try:
            entries = theme.get(key, list=True)
        except Exception:
            entries = []
        for entry in entries:
            if not entry or entry in seen:
                continue
            if os.path.isabs(entry):
                continue
            parts = entry.replace("\\", "/").split("/")
            if ".." in parts:
                continue
            seen.add(entry)
            names.append(entry)

    for index, subdir in enumerate(names):
        # A theme may be spread over several data roots.  Every existing copy
        # of the directory is considered; root order is a tie-breaker, just as
        # it is in the icon-theme lookup rules.
        for root_index, root in enumerate(node.roots):
            path = os.path.join(root, node.name, subdir)
            if os.path.isdir(path):
                yield _Directory(node, subdir, path, index, root_index)


def _directory_distance(directory, target):
    """Distance in nominal units, or ``None`` when the scale differs."""

    if target.scale is not None and directory.scale != target.scale:
        return None

    kind = directory.type.lower()
    size = directory.size
    if kind == "fixed":
        return abs(size - target.size)
    if kind in ("scalable", "scaleable"):
        if target.size < directory.min_size:
            return directory.min_size - target.size
        if target.size > directory.max_size:
            return target.size - directory.max_size
        return 0
    if kind == "threshold":
        lower = size - directory.threshold
        upper = size + directory.threshold
        if target.size < lower:
            return lower - target.size
        if target.size > upper:
            return target.size - upper
        return 0
    return None


def _directory_matches(directory, target):
    """Apply the theme's Fixed/Threshold/Scalable rules plus caller tolerance."""

    distance = _directory_distance(directory, target)
    if distance is None:
        return False
    return distance <= target.tolerance


def _normalise_extensions(extensions):
    if extensions is None:
        return list(DEFAULT_EXTENSIONS)
    result = []
    for extension in extensions:
        extension = str(extension).lstrip(".")
        if extension and extension not in result:
            result.append(extension)
    return result


def _candidate_name(name, extensions):
    """Return the icon stem and, if explicitly requested, its extension.

    A target written as ``app.png`` asks for that specific format.  Falling
    back to ``app.svg`` would conceal a missing requested file by silently
    changing the icon name, so only an unqualified name searches all formats.
    """

    suffix = os.path.splitext(name)[1].lstrip(".")
    if suffix in extensions:
        return os.path.splitext(name)[0], suffix
    return name, None


class _TargetState(object):
    __slots__ = ("target", "index", "candidates", "available", "unreadable",
                 "declared", "unreadable_dirs")

    def __init__(self, target, index):
        self.target = target
        self.index = index
        self.candidates = []
        self.available = []
        self.unreadable = []
        self.declared = False
        self.unreadable_dirs = []


def _enumerate_target(state, nodes, extensions, inventory, volumes):
    target = state.target
    iconname, requested_extension = _candidate_name(target.name, extensions)
    search_extensions = [requested_extension] if requested_extension else extensions

    for node in nodes:
        node_candidates = []
        node_available = []
        node_unreadable = []
        node_unreadable_dirs = []
        node_declared = False
        seen_locations = set()
        seen_paths = set()

        for directory in _iter_directories(node):
            entries, list_error = inventory.entries(directory.path)
            if entries is None:
                if os.path.lexists(directory.path):
                    node_unreadable_dirs.append(_with_target(_StringComparableRecord({
                        "type": "unreadable",
                        "path": directory.path,
                        "path_kind": "directory",
                        "reason": getattr(list_error, "strerror", None) or str(list_error),
                    }), state, directory))
                continue

            names = []
            for extension_index, extension in enumerate(search_extensions):
                filename = iconname + "." + extension
                if filename in entries:
                    names.append((extension_index, extension, filename))
            if not names:
                continue

            node_declared = True
            matches = _directory_matches(directory, target)

            for extension_index, extension, filename in names:
                location = (directory.subdir, filename)
                if location in seen_locations:
                    # An earlier data root already supplied this icon file for
                    # the theme directory.  Root order decides; a later root is
                    # not a second source for the same logical icon.
                    continue
                seen_locations.add(location)

                path = os.path.join(directory.path, filename)
                if not matches:
                    node_available.append(_with_target(_StringComparableRecord({
                        "type": "no_size_match",
                        "path": path,
                        "path_kind": "file",
                        "reason": "directory does not satisfy the requested size/scale",
                    }), state, directory))
                    continue

                readable, reason = inventory.readable(path)
                if not readable:
                    node_unreadable.append(_with_target(_StringComparableRecord({
                        "type": "unreadable",
                        "path": path,
                        "path_kind": "file",
                        "reason": reason,
                    }), state, directory))
                    continue

                key = _file_key(path)
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                node_candidates.append(_Candidate(
                    path=path,
                    key=key,
                    volume=volumes.volume(path),
                    directory=directory,
                    extension=extension,
                    extension_index=extension_index,
                    distance=_directory_distance(directory, target),
                ))

        state.declared = state.declared or node_declared
        state.available.extend(node_available)
        state.unreadable.extend(node_unreadable)
        state.unreadable_dirs.extend(node_unreadable_dirs)

        if node_candidates:
            # The first theme in the inheritance chain which offers a legal
            # file supplies the icon.  Cheaper files from later themes cannot
            # override that theme rule.
            state.candidates = node_candidates
            break

        if node_unreadable or node_unreadable_dirs:
            # The icon is declared in this theme but cannot be read.  Falling
            # through to another theme would mask that fact.
            break


def _with_target(record, state, directory):
    record.update({
        "target": state.target.name,
        "target_index": state.index,
        "theme": directory.node.name,
        "directory": directory.subdir,
    })
    record.update(directory.info())
    return record


def _candidate_sort_key(candidate):
    directory = candidate.directory
    return (
        directory.node.order,
        directory.root_index,
        directory.index,
        candidate.extension_index,
        candidate.distance,
        candidate.volume,
        candidate.path,
    )


def _optimise(candidate_masks, volumes, group_count):
    """Return the exact minimum-volume file set for the covered groups.

    ``candidate_masks`` contains ``(file_key, group_mask, path)`` triples.
    Files whose coverage is identical are reduced to the cheapest (and, on a
    tie, the earliest in theme order).  Independent families of groups are
    solved separately, then each family uses a memoised branch-and-bound
    weighted set-cover search.  Both reductions preserve the exact optimum.
    """

    if group_count == 0 or not candidate_masks:
        return []

    # Keep the best file for each distinct coverage.  Candidate order encodes
    # theme precedence, so equal-volume ties retain the earlier theme.
    by_mask = {}
    mask_order = []
    for key, mask, path in candidate_masks:
        if mask == 0:
            continue
        current = by_mask.get(mask)
        if current is None:
            by_mask[mask] = (key, path)
            mask_order.append(mask)
        elif volumes[key] < volumes[current[0]]:
            by_mask[mask] = (key, path)

    # Union groups that can be served by a common file.  Files only ever
    # couple groups within one component, so components can be optimised
    # independently and still produce a global optimum.
    parent = list(range(group_count))

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    for mask in mask_order:
        groups = [group for group in range(group_count) if mask & (1 << group)]
        first = groups[0]
        for group in groups[1:]:
            union(first, group)

    components = {}
    for mask in mask_order:
        groups = [group for group in range(group_count) if mask & (1 << group)]
        components.setdefault(find(groups[0]), []).append(mask)

    chosen = []
    for component_masks in components.values():
        component_groups = sorted(set(
            group
            for mask in component_masks
            for group in range(group_count)
            if mask & (1 << group)
        ))
        local_index = dict((group, index) for index, group in enumerate(component_groups))
        full = (1 << len(component_groups)) - 1

        local_masks = []
        for mask in component_masks:
            local = 0
            for group in component_groups:
                if mask & (1 << group):
                    local |= 1 << local_index[group]
            local_masks.append((local, mask))

        by_target = {}
        for local, original in local_masks:
            for target in range(len(component_groups)):
                if local & (1 << target):
                    by_target.setdefault(target, []).append((local, original))

        memo = {}

        def solve(covered):
            if covered == full:
                return (0, 0, ())
            if covered in memo:
                return memo[covered]

            remaining = [target for target in range(len(component_groups))
                         if not (covered & (1 << target))]
            target = min(remaining, key=lambda item: len(by_target[item]))

            best = None
            tried = set()
            for local, original in by_target[target]:
                if local in tried:
                    continue
                tried.add(local)
                key, path = by_mask[original]
                rest = solve(covered | local)
                if rest is None:
                    continue
                candidate = (
                    rest[0] + volumes[key],
                    rest[1] + 1,
                    rest[2] + (key,),
                )
                if best is None or candidate[:2] < best[:2] or (
                        candidate[:2] == best[:2]
                        and tuple(sorted(candidate[2], key=str))
                        < tuple(sorted(best[2], key=str))):
                    best = candidate
            memo[covered] = best
            return best

        result = solve(0)
        if result is not None:
            chosen.extend(result[2])

    # A file appears in exactly one component, so no dedupe is needed here,
    # but keep the output deterministic and duplicate-free.
    return sorted(set(chosen), key=lambda key: str(key))


class PackResult(object):
    """The result of :func:`select_icon_pack`.

    All attributes are simple JSON-friendly values.  ``selected`` is the
    deduplicated set of files to ship, ``assignments`` describes the file used
    for every target, and the three diagnostics lists distinguish missing
    files, unreadable declarations and entries with no qualifying size.
    """

    def __init__(self, theme, search_paths, targets, selected, assignments,
                 missing, unreadable, no_size_match, theme_errors,
                 total_volume):
        self.theme = theme
        self.search_paths = search_paths
        self.targets = targets
        self.selected = selected
        self.assignments = assignments
        self.missing = missing
        self.unreadable = unreadable
        self.no_size_match = no_size_match
        self.theme_errors = theme_errors
        self.total_volume = total_volume

    @property
    def files(self):
        """The selected file records (``selected`` under another name)."""
        return self.selected

    @property
    def packed(self):
        return self.selected

    @property
    def size(self):
        """The total volume of the selected files."""
        return self.total_volume

    @property
    def volume(self):
        return self.total_volume

    @property
    def unmet(self):
        return [entry for entry in self.assignments
                if entry.get("status") != "selected"]

    @property
    def gaps(self):
        return self.unmet

    @property
    def missing_files(self):
        return self.missing

    @property
    def unreadable_files(self):
        return self.unreadable

    @property
    def size_mismatches(self):
        return self.no_size_match

    @property
    def satisfied(self):
        return [entry for entry in self.assignments
                if entry.get("status") == "selected"]

    @property
    def unresolved(self):
        return self.unmet

    @property
    def ok(self):
        return not self.unmet

    @property
    def feasible(self):
        return self.ok

    @property
    def diagnostics(self):
        return self.missing + self.unreadable + self.no_size_match + self.theme_errors

    def as_dict(self):
        return {
            "theme": self.theme,
            "search_paths": list(self.search_paths),
            "targets": list(self.targets),
            "selected": self.selected,
            "assignments": self.assignments,
            "satisfied": self.satisfied,
            "unmet": self.unmet,
            "gaps": self.gaps,
            "total_volume": self.total_volume,
            "ok": self.ok,
            "missing": self.missing,
            "unreadable": self.unreadable,
            "no_size_match": self.no_size_match,
            "theme_errors": self.theme_errors,
            "diagnostics": self.diagnostics,
        }

    to_dict = as_dict

    def __getitem__(self, key):
        return self.as_dict()[key]

    def __repr__(self):
        return "<PackResult theme=%r selected=%d total_volume=%r ok=%r>" % (
            self.theme, len(self.selected), self.total_volume, self.ok)


def select_icon_pack(targets, theme=None, file_sizes=None, volumes=None,
                     search_paths=None, extensions=None, fallback_theme=DEFAULT_FALLBACK_THEME,
                     include_fallback=True, default_tolerance=0, default_scale=1,
                     **kwargs):
    """Select a minimum-volume set of icon files satisfying ``targets``.

    :param targets:
        An iterable of :class:`IconTarget` objects, dictionaries or
        ``(name, size, scale, tolerance)`` tuples.
    :param theme:
        Theme name, theme directory, ``index.theme`` path, or an already
        parsed :class:`xdg.IconTheme.IconTheme`.
    :param file_sizes:
        Mapping from a file's full path, basename or extension to volume.
        Missing entries fall back to the file's real size on disk.  A callable
        is also accepted.
    :param search_paths:
        Icon roots to search, in order.  Defaults to the standard XDG icon
        roots used by ``xdg.IconTheme``.
    :param extensions:
        Candidate extensions in preference order; defaults to ``png``, ``svg``
        and ``xpm``.
    :param fallback_theme:
        Theme consulted after the requested inheritance chain (``hicolor`` by
        default).  Set to ``None`` or pass ``include_fallback=False`` to
        disable the fallback.
    :param default_tolerance:
        Tolerance used by targets which do not state one.
    :param default_scale:
        Scale used by targets which do not state one.

    The returned :class:`PackResult` contains the selected files, the actual
    selection for each target, and separate ``missing``, ``unreadable`` and
    ``no_size_match`` diagnostics.  When some targets cannot be satisfied the
    result still selects the best pack for all satisfiable targets and reports
    the remaining gaps.
    """

    # Accept the common alternate call order build_icon_pack(theme, targets).
    if isinstance(targets, (str, IconTheme)) and theme is not None \
            and not isinstance(theme, (str, IconTheme)):
        targets, theme = theme, targets

    # Friendly aliases, useful for JSON callers as well as Python callers.
    values = {
        "file_sizes": file_sizes if file_sizes is not None else volumes,
        "search_paths": search_paths,
        "fallback_theme": fallback_theme,
        "include_fallback": include_fallback,
        "default_tolerance": default_tolerance,
        "default_scale": default_scale,
    }

    def take_alias(name, canonical):
        if name in kwargs:
            values[canonical] = kwargs.pop(name)

    take_alias("fileSizes", "file_sizes")
    take_alias("fileVolumes", "file_sizes")
    take_alias("searchPaths", "search_paths")
    take_alias("fallbackTheme", "fallback_theme")
    take_alias("includeFallback", "include_fallback")
    take_alias("defaultTolerance", "default_tolerance")
    take_alias("defaultScale", "default_scale")
    kwargs.pop("camelCase", None)
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError("unexpected keyword argument(s): %s" % unknown)

    file_sizes = values["file_sizes"]
    search_paths = values["search_paths"]
    fallback_theme = values["fallback_theme"]
    include_fallback = values["include_fallback"]
    default_tolerance = values["default_tolerance"]
    default_scale = values["default_scale"]
    extensions = _normalise_extensions(extensions)

    search_paths = list(search_paths) if search_paths is not None \
        else list(_icon_theme_module.icondirs)
    search_paths = [_normalise_path(path) for path in search_paths]

    target_objects = [
        _coerce_target(value, default_tolerance=default_tolerance,
                       default_scale=default_scale)
        for value in _target_iterable(targets)
    ]

    if theme is None:
        theme = _icon_theme_module.xdg.Config.icon_theme

    if isinstance(theme, string_types) and not (
            os.path.isabs(theme) or os.sep in theme
            or (os.altsep and os.altsep in theme)):
        theme_name = theme
    elif isinstance(theme, IconTheme):
        theme_name = theme.name
    else:
        loaded = _theme_from_reference(theme)
        if isinstance(loaded, tuple) and loaded[0] is not None:
            theme_name = loaded[0].name
            theme = loaded[0]
        else:
            theme_name = os.path.basename(os.path.normpath(theme))
            # A path to a missing/invalid index should still be diagnosed as
            # the requested theme rather than silently falling back.
            theme = theme_name

    nodes, theme_errors = _resolve_theme_chain(
        theme, search_paths, fallback_theme=fallback_theme,
        include_fallback=include_fallback)

    if theme_name is None:
        theme_name = nodes[0].name if nodes else None

    inventory = _Inventory()
    volume_table = _VolumeTable(file_sizes, extensions)

    states = [_TargetState(target, index)
              for index, target in enumerate(target_objects)]
    for state in states:
        _enumerate_target(state, nodes, extensions, inventory, volume_table)

    missing = []
    unreadable = []
    no_size_match = []
    assignments = []

    satisfiable_states = []
    for state in states:
        if state.candidates:
            satisfiable_states.append(state)
            continue

        target_record = state.target.as_dict()
        if state.unreadable or state.unreadable_dirs:
            reason = "unreadable"
        elif state.available:
            reason = "no_size_match"
        else:
            reason = "missing"

        assignment = _Record({
            "index": state.index,
            "target": state.target.name,
            "name": state.target.name,
            "size": state.target.size,
            "scale": state.target.scale,
            "tolerance": state.target.tolerance,
            "status": reason,
            "path": None,
            "volume": None,
            "theme": None,
            "directory": None,
        })
        assignment.update(target_record)
        assignments.append(assignment)

        if reason == "missing":
            missing.append(_StringComparableRecord({
                "type": "missing",
                "target": state.target.name,
                "target_index": state.index,
                "reason": "no icon file with a requested extension was found in the theme chain",
                "searched_themes": [node.name for node in nodes],
                "searched_extensions": list(extensions),
                "search_paths": list(search_paths),
            }))

        unreadable.extend(state.unreadable)
        unreadable.extend(state.unreadable_dirs)
        no_size_match.extend(state.available)

    for state in states:
        # Unreadable entries are diagnostics even if another theme supplied a
        # usable file; they are never silently turned into a different icon.
        if state.candidates:
            unreadable.extend(state.unreadable)
            unreadable.extend(state.unreadable_dirs)

    # Optimise over groups of targets which share exactly the same candidate
    # files.  Identical groups are redundant; collapsing them substantially
    # reduces the exact solver's state space.
    by_path = {}
    groups = {}
    group_order = []
    for state in satisfiable_states:
        path_keys = tuple(sorted(candidate.key for candidate in state.candidates))
        if path_keys not in groups:
            groups[path_keys] = []
            group_order.append(path_keys)
        groups[path_keys].append(state.index)
        for candidate in state.candidates:
            by_path.setdefault(candidate.key, candidate)

    candidate_masks = []
    for key, candidate in by_path.items():
        mask = 0
        for group, path_keys in enumerate(group_order):
            if key in path_keys:
                mask |= 1 << group
        candidate_masks.append((key, mask, candidate.path))

    chosen_keys = _optimise(candidate_masks, dict(
        (key, candidate.volume) for key, candidate in by_path.items()),
        len(group_order))

    chosen_set = set(chosen_keys)
    selected = []
    selected_targets = {}
    for key in chosen_keys:
        candidate = by_path[key]
        selected.append(_StringComparableRecord({
            "path": candidate.path,
            "volume": candidate.volume,
            "theme": candidate.directory.node.name,
            "directory": candidate.directory.subdir,
            "targets": [],
            "target_names": [],
        }))
        selected_targets[key] = selected[-1]

    total_volume = sum(entry["volume"] for entry in selected)

    # Fill in the actual file for every satisfiable target.  The file must be
    # in the optimum set and must be a legal member of this target's candidate
    # list; theme and directory order decide between several legal files.
    for state in satisfiable_states:
        options = [candidate for candidate in state.candidates
                   if candidate.key in chosen_set]
        options.sort(key=_candidate_sort_key)
        candidate = options[0]
        assignment = _Record({
            "index": state.index,
            "target": state.target.name,
            "name": state.target.name,
            "size": state.target.size,
            "scale": state.target.scale,
            "tolerance": state.target.tolerance,
            "status": "selected",
            "path": candidate.path,
            "volume": candidate.volume,
            "theme": candidate.directory.node.name,
            "directory": candidate.directory.subdir,
            "directory_path": candidate.directory.path,
            "directory_type": candidate.directory.type,
            "directory_size": candidate.directory.size,
            "directory_scale": candidate.directory.scale,
            "deviation": state.target.size - candidate.directory.size
                           if candidate.directory.type.lower() in ("fixed", "threshold")
                           else 0,
        })
        assignment.update(state.target.as_dict())
        assignments.append(assignment)

        selected_entry = selected_targets[candidate.key]
        selected_entry["targets"].append(state.index)
        selected_entry["target_names"].append(state.target.name)

    assignments.sort(key=lambda entry: entry["index"])
    selected.sort(key=lambda entry: (entry["theme"], entry["path"]))
    # Keep the target order stable in the public selected records.
    for entry in selected:
        order = sorted(entry["targets"])
        entry["targets"] = order
        entry["target_names"] = [
            next(entry_target["name"] for entry_target in assignments
                 if entry_target["index"] == index)
            for index in order
        ]

    missing.sort(key=lambda entry: entry["target_index"])
    unreadable.sort(key=lambda entry: (entry["target_index"], entry["path"]))
    no_size_match.sort(key=lambda entry: (entry["target_index"], entry["path"]))

    return PackResult(
        theme=theme_name,
        search_paths=search_paths,
        targets=[target.as_dict() for target in target_objects],
        selected=selected,
        assignments=assignments,
        missing=missing,
        unreadable=unreadable,
        no_size_match=no_size_match,
        theme_errors=theme_errors,
        total_volume=total_volume,
    )


# The longer, verb-oriented spellings are convenient and keep the API usable
# from both Python and shell-oriented code.
build_icon_pack = select_icon_pack
plan_icon_pack = select_icon_pack
selectIconPack = select_icon_pack
buildIconPack = select_icon_pack
getIconPack = select_icon_pack


class IconPack(object):
    """Reusable planner object.

    ``IconPack(theme, search_paths=...).select(targets, file_sizes=...)`` is a
    small convenience wrapper around :func:`select_icon_pack`.
    """

    def __init__(self, theme=None, search_paths=None, extensions=None,
                 fallback_theme=DEFAULT_FALLBACK_THEME, include_fallback=True,
                 default_tolerance=0, default_scale=1):
        self.theme = theme
        self.search_paths = search_paths
        self.extensions = extensions
        self.fallback_theme = fallback_theme
        self.include_fallback = include_fallback
        self.default_tolerance = default_tolerance
        self.default_scale = default_scale

    def select(self, targets, file_sizes=None, volumes=None, **kwargs):
        options = dict(
            theme=self.theme,
            search_paths=self.search_paths,
            extensions=self.extensions,
            fallback_theme=self.fallback_theme,
            include_fallback=self.include_fallback,
            default_tolerance=self.default_tolerance,
            default_scale=self.default_scale,
        )
        options.update(kwargs)
        return select_icon_pack(targets, file_sizes=file_sizes,
                                volumes=volumes, **options)

    build = select
    pack = select


def _load_request(path):
    if path in (None, "-"):
        return json.load(sys.stdin)
    stream = open(path, "r")
    try:
        return json.load(stream)
    finally:
        stream.close()


def main(argv=None):
    """Command line interface for ``python -m xdg.IconPack``.

    Reads a JSON request from a file or standard input and writes the pack as
    JSON.  A request contains ``theme``, ``targets``, ``file_sizes`` and
    optionally ``search_paths`` and ``extensions``.
    """

    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m xdg.IconPack",
        description="Select a minimum-volume offline XDG icon pack.")
    parser.add_argument("request", nargs="?", default="-",
                        help="JSON request file (default: standard input)")
    parser.add_argument("--theme", help="override the theme in the request")
    parser.add_argument("--search-path", action="append", dest="search_paths",
                        help="icon root (may be repeated)")
    parser.add_argument("--fallback-theme", default=None,
                        help="fallback theme name; use '' to disable")
    parser.add_argument("--no-fallback", action="store_true",
                        help="do not consult a fallback theme")
    parser.add_argument("--pretty", action="store_true",
                        help="pretty-print JSON")
    parser.add_argument("--fail-on-gaps", action="store_true",
                        help="exit with status 1 if any target cannot be met")
    args = parser.parse_args(argv)

    request = _load_request(args.request)
    if not isinstance(request, dict):
        parser.error("request must be a JSON object")
    targets = request.get("targets", [])
    theme = args.theme if args.theme is not None else request.get("theme")
    search_paths = args.search_paths or request.get("search_paths") \
        or request.get("searchPaths")
    file_sizes = request.get("file_sizes", request.get("volumes"))
    extensions = request.get("extensions")

    fallback = DEFAULT_FALLBACK_THEME
    include_fallback = True
    if args.no_fallback:
        include_fallback = False
    elif args.fallback_theme is not None:
        fallback = args.fallback_theme or None

    result = select_icon_pack(
        targets,
        theme=theme,
        file_sizes=file_sizes,
        search_paths=search_paths,
        extensions=extensions,
        fallback_theme=fallback,
        include_fallback=include_fallback,
    )

    if args.pretty:
        json.dump(result.as_dict(), sys.stdout, indent=2, sort_keys=True)
    else:
        json.dump(result.as_dict(), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")

    if args.fail_on_gaps and result.unmet:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
