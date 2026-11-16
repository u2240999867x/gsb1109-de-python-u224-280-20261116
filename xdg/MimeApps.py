"""
Default application associations for MIME types.

This module implements the freedesktop.org `MIME Applications Associations
specification <http://specifications.freedesktop.org/mime-apps-spec/>`_.

It answers three related questions:

* which applications are really installed, visible and able to open a MIME
  type (taking MIME inheritance into account);
* which application is currently the effective default for a MIME type, and
  where that choice comes from;
* which entries a user-level ``mimeapps.list`` needs in order to reach a set
  of requested defaults with the smallest possible change, without altering
  the effective default of any MIME type that was not requested.

Conflicting requests are reported instead of being applied: a later request
never silently replaces an earlier one, and when the requested defaults cannot
all be satisfied the original file is left untouched.

The command line interface reads a scenario description, writes the user file,
and reads it back from disk to report the real selection::

    python -m xdg.MimeApps scenario.json [--dry-run]

See :func:`load_scenario` for the accepted input format.
"""

from __future__ import unicode_literals

import io
import json
import os
import re
import tempfile

from collections import OrderedDict, defaultdict

from xdg import BaseDirectory
from xdg.DesktopEntry import DesktopEntry
from xdg.Exceptions import ParsingError
from xdg.util import which

DEFAULT_GROUP = 'Default Applications'
ADDED_GROUP = 'Added Associations'
REMOVED_GROUP = 'Removed Associations'

MAIN_FILE = 'mimeapps.list'

try:
    _string_types = (str, unicode)
except NameError:       # Python 3
    _string_types = (str,)

if hasattr(os, 'replace'):
    def _replace(source, destination):
        os.replace(source, destination)
else:                   # Python 2
    def _replace(source, destination):
        if os.path.exists(destination):
            os.unlink(destination)
        os.rename(source, destination)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def canonical_mime(value):
    """Normalise the spelling of a MIME type."""
    return value.strip().lower()


def split_association_list(value):
    """Split a semicolon separated desktop file list."""
    if value is None:
        return []
    result = []
    for item in value.split(';'):
        item = item.strip()
        if item:
            result.append(item)
    return result


def dedupe(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _parse_association_text(text):
    """Parse association text into ``(defaults, added, removed)``."""
    defaults = OrderedDict()
    added = OrderedDict()
    removed = OrderedDict()
    group = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            group = line[1:-1].strip()
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = canonical_mime(key)
        values = split_association_list(value)
        if group == DEFAULT_GROUP:
            defaults[key] = values
        elif group == ADDED_GROUP:
            added[key] = dedupe(added.get(key, []) + values)
        elif group == REMOVED_GROUP:
            removed[key] = dedupe(removed.get(key, []) + values)
    return defaults, added, removed


# ---------------------------------------------------------------------------
# MIME graph
# ---------------------------------------------------------------------------

class MimeGraph(object):
    """Aliases and the ``mime/subclasses`` inheritance graph.

    ``parents`` maps a type to its immediate parents; ``ancestors`` returns
    the transitive closure nearest-first.
    """

    def __init__(self, aliases=None, parents=None):
        self.aliases = dict(aliases or {})
        self.parents = defaultdict(list)
        self.children = defaultdict(list)
        self._ancestor_cache = {}
        self._descendant_cache = {}
        for child, values in (parents or {}).items():
            self.set_parents(child, values)

    @classmethod
    def from_data_dirs(cls, data_dirs=None):
        if data_dirs is None:
            data_dirs = BaseDirectory.xdg_data_dirs
        graph = cls()
        for directory in data_dirs:
            graph._load_aliases(os.path.join(directory, 'mime', 'aliases'))
        for directory in data_dirs:
            graph._load_subclasses(os.path.join(directory,
                                                'mime', 'subclasses'))
        return graph

    def _load_aliases(self, path):
        for alias, target in self._read_pairs(path):
            self.aliases.setdefault(canonical_mime(alias),
                                    canonical_mime(target))

    def _load_subclasses(self, path):
        for child, parent in self._read_pairs(path):
            self.set_parents(child, [parent])

    @staticmethod
    def _read_pairs(path):
        try:
            fp = io.open(path, 'r', encoding='utf-8', errors='replace')
        except IOError:
            return
        with fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                fields = line.split()
                if len(fields) >= 2:
                    yield fields[0], fields[1]

    def set_parents(self, child, parents):
        child = self.canonical(canonical_mime(child))
        for parent in parents:
            parent = self.canonical(canonical_mime(parent))
            if parent != child and parent not in self.parents[child]:
                self.parents[child].append(parent)
                self.children[parent].append(child)
        self._ancestor_cache.clear()
        self._descendant_cache.clear()

    def canonical(self, mime):
        mime = canonical_mime(mime)
        seen = set()
        while mime in self.aliases and mime not in seen:
            seen.add(mime)
            mime = self.aliases[mime]
        return mime

    def _walk(self, mime, edges, cache):
        mime = self.canonical(mime)
        if mime in cache:
            return list(cache[mime])
        result = []
        seen = set([mime])
        frontier = [mime]
        while frontier:
            following = []
            for current in frontier:
                for next_type in edges.get(current, ()):
                    if next_type in seen:
                        continue
                    seen.add(next_type)
                    result.append(next_type)
                    following.append(next_type)
            frontier = following
        cache[mime] = list(result)
        return list(result)

    def ancestors(self, mime):
        return self._walk(mime, self.parents, self._ancestor_cache)

    def descendants(self, mime):
        return self._walk(mime, self.children, self._descendant_cache)

    def as_dict(self, types):
        return dict((mime, self.ancestors(mime))
                    for mime in sorted(set(types)))


# ---------------------------------------------------------------------------
# Desktop entry inventory
# ---------------------------------------------------------------------------

_PROGRAM_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def split_exec(line):
    """Split an ``Exec`` value using the desktop entry quoting rules."""
    words = []
    current = []
    quoted = False
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\' and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if current:
                words.append(''.join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append(''.join(current))
    return words


def exec_program(exec_line):
    """Return the program started by an ``Exec`` line, ignoring wrappers."""
    if not exec_line:
        return None
    words = split_exec(exec_line)
    index = 0
    if index < len(words) and os.path.basename(words[index]) == 'env':
        index += 1
        while index < len(words) and _PROGRAM_ASSIGNMENT.match(words[index]):
            index += 1
    if index >= len(words):
        return None
    program = words[index]
    if program.startswith('%'):
        return None
    return program


def program_is_executable(program):
    if not program:
        return False
    if os.sep in program or (os.altsep and os.altsep in program):
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return which(program) is not None


class AppInfo(object):
    """One desktop entry, either parsed from a file or supplied directly."""

    FIELDS = ('id', 'path', 'name', 'type', 'hidden', 'no_display',
              'only_show_in', 'not_show_in', 'exec_line', 'try_exec',
              'mime_types', 'executable', 'deleted', 'dbus_activatable')

    def __init__(self, desktop_id, path=None, **kwargs):
        self.id = desktop_id
        self.path = path
        self.name = None
        self.type = 'Application'
        self.hidden = False
        self.no_display = False
        self.only_show_in = []
        self.not_show_in = []
        self.exec_line = None
        self.try_exec = None
        self.mime_types = []
        self.deleted = False
        self.dbus_activatable = False
        self.parse_error = None
        self.executable = None
        for key, value in kwargs.items():
            if key not in self.FIELDS:
                raise TypeError('Unknown desktop entry field: %s' % key)
            setattr(self, key, value)
        self._given_executable = 'executable' in kwargs
        self._declared = set()

    @classmethod
    def from_file(cls, desktop_id, path):
        app = cls(desktop_id, path)
        try:
            entry = DesktopEntry(path)
            app.name = entry.getName()
            app.type = entry.getType() or 'Application'
            app.hidden = bool(entry.getHidden())
            app.no_display = bool(entry.getNoDisplay())
            app.only_show_in = list(entry.getOnlyShowIn())
            app.not_show_in = list(entry.getNotShowIn())
            app.exec_line = entry.getExec()
            app.try_exec = entry.getTryExec()
            app.mime_types = [m for m in entry.getMimeTypes() if m]
            app.dbus_activatable = bool(
                entry.get('DBusActivatable', type='boolean'))
        except (ParsingError, IOError, OSError) as exc:
            app.parse_error = str(exc)
        except Exception as exc:        # defensive: malformed entry
            app.parse_error = str(exc)
        return app

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        desktop_id = data.pop('id', None) or data.pop('desktop_id', None)
        if not desktop_id:
            raise ValueError('Desktop entry is missing an id')
        path = data.pop('path', None)
        if path and not any(key in data for key in
                            ('mime_types', 'mime_type', 'exec', 'exec_line')):
            app = cls.from_file(desktop_id, path)
            for key in ('hidden', 'no_display', 'executable', 'deleted',
                        'type', 'try_exec', 'only_show_in', 'not_show_in'):
                if key in data:
                    setattr(app, key, data[key])
            if 'executable' in data:
                app._given_executable = True
            return app

        aliases = {
            'exec': 'exec_line',
            'mime_type': 'mime_types',
            'mime_types': 'mime_types',
            'try_exec': 'try_exec',
            'no_display': 'no_display',
            'nodisplay': 'no_display',
            'only_show_in': 'only_show_in',
            'not_show_in': 'not_show_in',
            'dbus_activatable': 'dbus_activatable',
        }
        kwargs = {}
        for key, value in data.items():
            target = aliases.get(key, key)
            if target == 'mime_types' and isinstance(value, _string_types):
                value = split_association_list(value)
            kwargs[target] = value
        return cls(desktop_id, path, **kwargs)

    def finalise(self, graph):
        self._declared = set(graph.canonical(mime)
                             for mime in self.mime_types if mime)
        if not self._given_executable:
            if self.try_exec:
                self.executable = program_is_executable(self.try_exec)
            elif self.dbus_activatable:
                # A DBus activated application has no Exec line to check.
                self.executable = True
            else:
                self.executable = program_is_executable(
                    exec_program(self.exec_line))
        return self

    def shows_in(self, desktop_env):
        if not desktop_env:
            return True
        environments = [item for item in desktop_env.split(':') if item]
        lowered = set(item.lower() for item in environments)

        def matches(names):
            for name in names:
                if name in environments or name.lower() in lowered:
                    return True
            return False

        if self.only_show_in:
            return matches(self.only_show_in)
        if self.not_show_in:
            return not matches(self.not_show_in)
        return True

    def supports(self, mime, graph):
        mime = graph.canonical(mime)
        if mime in self._declared:
            return True
        for parent in graph.ancestors(mime):
            if parent in self._declared:
                return True
        return False

    def selectable(self, mime, graph, desktop_env=None):
        """Return ``(bool, reason)`` describing whether the app may be used."""
        if self.parse_error:
            return False, 'invalid_desktop_entry'
        if self.deleted:
            return False, 'deleted_by_higher_precedence_entry'
        if self.type != 'Application':
            return False, 'not_an_application'
        if self.hidden:
            return False, 'hidden'
        if self.no_display:
            return False, 'no_display'
        if not self.shows_in(desktop_env):
            return False, 'not_shown_in_current_desktop'
        if not self.executable:
            return False, 'not_executable'
        if not self.supports(mime, graph):
            return False, 'unsupported_mime_type'
        return True, None

    def as_dict(self, graph, desktop_env=None, mime=None):
        data = {
            'id': self.id,
            'path': self.path,
            'name': self.name,
            'type': self.type,
            'hidden': bool(self.hidden),
            'no_display': bool(self.no_display),
            'only_show_in': list(self.only_show_in),
            'not_show_in': list(self.not_show_in),
            'exec': self.exec_line,
            'try_exec': self.try_exec,
            'executable': bool(self.executable),
            'mime_types': list(self.mime_types),
            'deleted': bool(self.deleted),
            'dbus_activatable': bool(self.dbus_activatable),
            'parse_error': self.parse_error,
        }
        if mime:
            ok, reason = self.selectable(mime, graph, desktop_env)
            data['selectable'] = ok
            data['selectable_reason'] = reason
        return data


class DesktopInventory(object):
    """The desktop entries visible to the user."""

    def __init__(self, apps, graph, desktop_env=None):
        self.apps = OrderedDict()
        for app in apps:
            if isinstance(app, AppInfo):
                self.apps[app.id] = app.finalise(graph)
            else:
                raise TypeError('apps must contain AppInfo instances')
        self.graph = graph
        self.desktop_env = desktop_env

    @classmethod
    def from_entries(cls, entries, graph, desktop_env=None):
        apps = []
        for item in entries:
            apps.append(item if isinstance(item, AppInfo)
                        else AppInfo.from_dict(item))
        return cls(apps, graph, desktop_env)

    @classmethod
    def scan(cls, data_dirs=None, graph=None, desktop_env=None):
        if graph is None:
            graph = MimeGraph.from_data_dirs(data_dirs)
        if desktop_env is None:
            desktop_env = os.environ.get('XDG_CURRENT_DESKTOP')
        if data_dirs is None:
            data_dirs = [os.path.join(directory, 'applications')
                         for directory in BaseDirectory.xdg_data_dirs]
        else:
            data_dirs = [directory
                         if os.path.basename(os.path.normpath(directory)) ==
                         'applications'
                         else os.path.join(directory, 'applications')
                         for directory in data_dirs]

        # Higher precedence directories come first; the first valid file for
        # a desktop file ID wins and masks lower precedence copies.
        paths = OrderedDict()
        for directory in data_dirs:
            if not os.path.isdir(directory):
                continue
            for root, dirs, files in os.walk(directory):
                dirs.sort()
                for name in sorted(files):
                    if not name.endswith('.desktop'):
                        continue
                    path = os.path.join(root, name)
                    relative = os.path.relpath(path, directory)
                    desktop_id = relative.replace(os.sep, '-')
                    if desktop_id in paths:
                        continue
                    # An unreadable higher precedence file must not expose a
                    # lower precedence entry; it is reported as invalid.
                    paths[desktop_id] = AppInfo.from_file(desktop_id, path)

        apps = []
        for desktop_id, app in paths.items():
            # A Hidden=true file masks lower precedence entries with the same
            # desktop file ID; it must not fall back to the older entry.
            app.deleted = bool(app.hidden)
            apps.append(app)
        return cls(apps, graph, desktop_env)

    def get(self, desktop_id):
        return self.apps.get(desktop_id)

    def selectable(self, desktop_id, mime):
        app = self.get(desktop_id)
        if app is None:
            return False, 'unknown_application'
        return app.selectable(mime, self.graph, self.desktop_env)

    def all_mime_types(self):
        types = set()
        for app in self.apps.values():
            for mime in app.mime_types:
                types.add(self.graph.canonical(mime))
        return types

    def as_list(self, mime=None):
        return [self.apps[key].as_dict(self.graph, self.desktop_env, mime)
                for key in self.apps]


# ---------------------------------------------------------------------------
# Association layers
# ---------------------------------------------------------------------------

class AssociationFile(object):
    """One association file, preserving its raw text for rewriting."""

    def __init__(self, path=None, origin=None):
        self.path = path
        self.origin = origin or path or '<memory>'
        self.defaults = OrderedDict()
        self.added = OrderedDict()
        self.removed = OrderedDict()
        self.text = ''
        self.missing = True
        if path and os.path.isfile(path):
            try:
                with io.open(path, 'r', encoding='utf-8',
                             errors='replace') as fp:
                    self.text = fp.read()
            except IOError:
                self.text = ''
            self.missing = False
        self.defaults, self.added, self.removed = \
            _parse_association_text(self.text)

    @classmethod
    def from_dict(cls, data):
        explicit = any(key in data for key in
                       ('text', 'defaults', 'added', 'removed'))
        if data.get('path') and not explicit:
            return cls(data.get('path'), data.get('origin'))

        obj = cls.__new__(cls)
        obj.path = data.get('path')
        obj.origin = data.get('origin') or obj.path or '<memory>'
        obj.text = data.get('text') or ''
        obj.missing = False
        obj.defaults = OrderedDict(
            (canonical_mime(key), list(values))
            for key, values in (data.get('defaults') or {}).items())
        obj.added = OrderedDict(
            (canonical_mime(key), list(values))
            for key, values in (data.get('added') or {}).items())
        obj.removed = OrderedDict(
            (canonical_mime(key), list(values))
            for key, values in (data.get('removed') or {}).items())
        if not obj.text:
            obj.text = _render_association_text(
                obj.defaults, obj.added, obj.removed)
        return obj

    def with_defaults(self, defaults):
        clone = AssociationFile.__new__(AssociationFile)
        clone.path = self.path
        clone.origin = self.origin
        clone.text = self.text
        clone.missing = self.missing
        clone.defaults = defaults
        clone.added = self.added
        clone.removed = self.removed
        return clone

    def has_default(self, mime):
        return mime in self.defaults


class AssociationSet(object):
    """Ordered association files and the user file to edit."""

    def __init__(self, files, user_index=0, user_path=None):
        self.files = list(files)
        self.user_index = user_index
        self.user_path = user_path

    @property
    def user(self):
        return self.files[self.user_index]

    def defaults(self, mime):
        """Yield ``(origin, values)`` for every file that lists the type."""
        for item in self.files:
            if mime in item.defaults:
                yield item.origin, item.defaults[mime]

    def added(self, mime):
        """Combine Added/Removed Associations following file precedence.

        A higher precedence file decides an application: an entry there is
        not undone by a Removed Associations line in a lower precedence file.
        """
        order = []
        decisions = {}
        for item in self.files:
            for app in item.added.get(mime, []):
                if app not in decisions:
                    decisions[app] = True
                    order.append(app)
            for app in item.removed.get(mime, []):
                if app not in decisions:
                    decisions[app] = False
        return [app for app in order if decisions[app]]

    def keys(self):
        result = set()
        for item in self.files:
            result.update(item.defaults)
            result.update(item.added)
            result.update(item.removed)
        return result

    def with_user_defaults(self, defaults):
        files = list(self.files)
        files[self.user_index] = self.user.with_defaults(defaults)
        return AssociationSet(files, self.user_index, self.user_path)

    def reload_user(self):
        files = list(self.files)
        files[self.user_index] = AssociationFile(self.user_path,
                                                 self.user.origin)
        return AssociationSet(files, self.user_index, self.user_path)


def _user_config_file(path, config_home, desktop_env):
    """Whether ``path`` is one of the user-level association files."""
    home = os.path.abspath(config_home)
    normal = os.path.abspath(path)
    if os.path.dirname(normal) != home:
        return False
    names = [MAIN_FILE]
    if desktop_env:
        names.append(desktop_env + '-' + MAIN_FILE)
    return os.path.basename(normal) in names


def load_association_files(config_home=None, config_dirs=None,
                           data_home=None, data_dirs=None, desktop_env=None):
    """Return association files in specification precedence order."""
    if config_home is None:
        config_home = BaseDirectory.xdg_config_home
    if config_dirs is None:
        config_dirs = BaseDirectory.xdg_config_dirs
    if data_home is None:
        data_home = BaseDirectory.xdg_data_home
    if data_dirs is None:
        data_dirs = BaseDirectory.xdg_data_dirs

    paths = []

    def add_config(directory):
        if desktop_env:
            paths.append(os.path.join(directory,
                                      desktop_env + '-' + MAIN_FILE))
        paths.append(os.path.join(directory, MAIN_FILE))

    add_config(config_home)
    for directory in config_dirs:
        if os.path.abspath(directory) != os.path.abspath(config_home):
            add_config(directory)

    def add_data(directory):
        applications = os.path.join(directory, 'applications')
        if desktop_env:
            paths.append(os.path.join(applications,
                                      desktop_env + '-' + MAIN_FILE))
        paths.append(os.path.join(applications, MAIN_FILE))

    add_data(data_home)
    for directory in data_dirs:
        if os.path.abspath(directory) == os.path.abspath(data_home):
            continue
        add_data(directory)

    result = []
    seen = set()
    for path in paths:
        normal = os.path.abspath(path)
        if normal in seen:
            continue
        seen.add(normal)
        result.append(AssociationFile(path))
    return result


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class Resolution(object):
    """The effective application for one MIME type."""

    def __init__(self, app=None, source='none', origin=None,
                 inherited_from=None):
        self.app = app
        self.source = source
        self.origin = origin
        self.inherited_from = inherited_from

    def as_dict(self):
        return {
            'app': self.app,
            'source': self.source,
            'origin': self.origin,
            'inherited_from': self.inherited_from,
        }

    def __repr__(self):
        return 'Resolution(%r, %r)' % (self.app, self.source)


class Resolver(object):
    def __init__(self, associations, inventory, graph):
        self.associations = associations
        self.inventory = inventory
        self.graph = graph

    def _eligible(self, app, mime):
        return self.inventory.selectable(app, mime)[0]

    def _explicit_default(self, mime):
        for origin, values in self.associations.defaults(mime):
            for app in values:
                if self._eligible(app, mime):
                    return Resolution(app, 'default', origin)
        return None

    def _added_default(self, mime):
        added = self.associations.added(mime)
        for app in added:
            if self._eligible(app, mime):
                return Resolution(app, 'added', None)
        return None

    def resolve(self, mime):
        mime = self.graph.canonical(mime)
        found = self._explicit_default(mime)
        if found is not None:
            return found
        found = self._added_default(mime)
        if found is not None:
            return found
        for parent in self.graph.ancestors(mime):
            found = self._explicit_default(parent)
            if found is not None:
                return Resolution(found.app, found.source, found.origin,
                                  inherited_from=parent)
            found = self._added_default(parent)
            if found is not None:
                return Resolution(found.app, found.source, found.origin,
                                  inherited_from=parent)
        return Resolution()

    def resolve_all(self, mime_types):
        return dict((mime, self.resolve(mime))
                    for mime in sorted(set(mime_types)))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class Target(object):
    def __init__(self, mime, app):
        self.mime = mime
        self.app = app

    def __repr__(self):
        return 'Target(%r, %r)' % (self.mime, self.app)


class Change(object):
    def __init__(self, mime, app, action):
        self.mime = mime
        self.app = app
        self.action = action

    def apply_to(self, defaults):
        if self.action == 'remove':
            defaults.pop(self.mime, None)
        else:
            defaults[self.mime] = [self.app]

    def as_dict(self):
        return {'mime': self.mime, 'action': self.action, 'app': self.app}

    def __repr__(self):
        return 'Change(%r, %r, %r)' % (self.action, self.mime, self.app)


class Conflict(object):
    def __init__(self, mime, app=None, reason=None, detail=None,
                 caused_by=None):
        self.mime = mime
        self.app = app
        self.reason = reason
        self.detail = detail
        self.caused_by = list(caused_by or [])

    def as_dict(self):
        return {
            'mime': self.mime,
            'app': self.app,
            'reason': self.reason,
            'detail': self.detail,
            'caused_by': list(self.caused_by),
        }

    def __repr__(self):
        return 'Conflict(%r, %r, %r)' % (self.mime, self.app, self.reason)


class TargetReport(object):
    def __init__(self, mime, app, status, reason=None, actual=None):
        self.mime = mime
        self.app = app
        self.status = status
        self.reason = reason
        self.actual = actual

    def as_dict(self):
        return {
            'mime': self.mime,
            'app': self.app,
            'status': self.status,
            'reason': self.reason,
            'actual': self.actual,
        }


class Plan(object):
    def __init__(self, associations, changes, conflicts, targets, baseline,
                 universe, graph, inventory):
        self.associations = associations
        self.changes = changes
        self.conflicts = conflicts
        self.targets = targets
        self.baseline = baseline
        self.universe = universe
        self.graph = graph
        self.inventory = inventory

    @property
    def ok(self):
        return not self.conflicts

    def as_dict(self):
        return {
            'ok': self.ok,
            'user_file': self.associations.user_path,
            'changed': bool(self.changes),
            'changes': [change.as_dict() for change in self.changes],
            'conflicts': [conflict.as_dict() for conflict in self.conflicts],
            'targets': [target.as_dict() for target in self.targets],
        }


def _normalise_targets(targets):
    result = []
    for item in targets:
        if isinstance(item, Target):
            result.append(item)
        elif isinstance(item, dict):
            result.append(Target(item['mime'], item['app']))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            result.append(Target(item[0], item[1]))
        elif isinstance(item, _string_types) and '=' in item:
            mime, app = item.split('=', 1)
            result.append(Target(mime, app))
        else:
            raise ValueError('Invalid target: %r' % (item,))
    return [Target(canonical_mime(target.mime), target.app.strip())
            for target in result]


def _universe(requested, associations, inventory, graph):
    seeds = set(requested)
    seeds.update(inventory.all_mime_types())
    seeds.update(associations.keys())
    result = set()
    stack = list(seeds)
    while stack:
        mime = graph.canonical(stack.pop())
        if mime in result:
            continue
        result.add(mime)
        stack.extend(graph.ancestors(mime))
        stack.extend(graph.descendants(mime))
    return result


def _simulate(associations, changes, universe, inventory, graph):
    defaults = OrderedDict(
        (key, list(values))
        for key, values in associations.user.defaults.items())
    for change in changes:
        change.apply_to(defaults)
    simulated = associations.with_user_defaults(defaults)
    return Resolver(simulated, inventory, graph).resolve_all(universe)


def _unauthorised(results, baseline, expected):
    """Effective defaults of unrequested types that would change.

    Only types that currently have a default application are protected: a
    type without any default simply follows its parent through MIME
    inheritance, which is the behaviour the specification defines.
    """
    changed = {}
    for mime, resolution in results.items():
        if mime in expected:
            continue
        before = baseline[mime].app
        if before is None:
            continue
        if resolution.app != before:
            changed[mime] = (before, resolution.app)
    return changed


def _combinations(options):
    keys = list(options)

    def walk(index, prefix):
        if index == len(keys):
            yield list(prefix)
            return
        for choice in options[keys[index]]:
            prefix.append(choice)
            for result in walk(index + 1, prefix):
                yield result
            prefix.pop()

    for result in walk(0, []):
        yield result


def _change_key(changes):
    return (len(changes),
            tuple(sorted((change.mime, change.action)
                         for change in changes)))


def plan_associations(targets, associations=None, inventory=None,
                      graph=None, config_home=None, config_dirs=None,
                      data_home=None, data_dirs=None, desktop_env=None,
                      user_file=None):
    """Compute the minimal user-level change for ``targets``.

    ``targets`` is a list of ``(mime, desktop_id)`` pairs.  Nothing is
    written; pass the returned plan to :func:`apply_plan`.
    """
    if graph is None:
        graph = MimeGraph.from_data_dirs(data_dirs)
    if desktop_env is None:
        desktop_env = os.environ.get('XDG_CURRENT_DESKTOP')

    if inventory is None:
        inventory = DesktopInventory.scan(data_dirs, graph, desktop_env)

    if associations is None:
        if user_file is None:
            base = config_home or BaseDirectory.xdg_config_home
            user_file = os.path.join(base, MAIN_FILE)
        files = load_association_files(
            config_home, config_dirs, data_home, data_dirs, desktop_env)
        # The highest precedence file inside the user configuration
        # directory is the one a user-level change belongs in.
        user_index = None
        for index, item in enumerate(files):
            if item.path and _user_config_file(item.path, config_home or
                                               BaseDirectory.xdg_config_home,
                                               desktop_env):
                user_index = index
                break
        if user_index is None:
            files.insert(0, AssociationFile(user_file))
            user_index = 0
        else:
            user_file = files[user_index].path
        associations = AssociationSet(files, user_index, user_file)

    normalized = _normalise_targets(targets)
    requested = OrderedDict()
    for target in normalized:
        requested.setdefault(graph.canonical(target.mime), []).append(target)
    requested_mimes = set(requested)
    expected = OrderedDict((mime, group[0].app)
                           for mime, group in requested.items())

    universe = _universe(requested_mimes, associations, inventory, graph)
    resolver = Resolver(associations, inventory, graph)
    baseline = resolver.resolve_all(universe)

    conflicts = []
    reports = []
    conflicted = set()

    # Requests for the same MIME type with different applications conflict:
    # report every request and let no later request replace an earlier one.
    for mime, group in requested.items():
        apps = dedupe([target.app for target in group])
        if len(apps) > 1:
            conflicted.add(mime)
            detail = 'requested different applications: ' + ', '.join(apps)
            for target in group:
                conflicts.append(Conflict(
                    mime, target.app, 'conflicting_targets', detail))
                reports.append(TargetReport(
                    mime, target.app, 'conflict', 'conflicting_targets',
                    baseline[mime].app))

    for target in normalized:
        mime = graph.canonical(target.mime)
        if mime in conflicted:
            continue
        ok, reason = inventory.selectable(target.app, mime)
        if not ok:
            conflicts.append(Conflict(
                mime, target.app, 'ineligible_application', reason))
            reports.append(TargetReport(
                mime, target.app, 'conflict', reason, baseline[mime].app))
            conflicted.add(mime)

    if conflicts:
        return Plan(associations, [], conflicts, reports, baseline, universe,
                    graph, inventory)

    # Candidate edits.  Keeping the file untouched is always an option; a
    # combination that satisfies all requests with no edit means no write.
    options = OrderedDict()
    for mime, group in requested.items():
        target = group[0]
        choices = [None]
        if baseline[mime].app != target.app:
            choices.append(Change(mime, target.app, 'set'))
        elif baseline[mime].inherited_from:
            # Already satisfied through inheritance; changing the parent
            # would make it drift, so allow an explicit pin.
            choices.append(Change(mime, target.app, 'set'))
        if associations.user.has_default(mime):
            choices.append(Change(mime, None, 'remove'))
        options[mime] = choices

    best = None
    for combination in _combinations(options):
        changes = [change for change in combination if change is not None]
        results = _simulate(associations, changes, universe,
                            inventory, graph)
        if not _targets_ok(results, expected):
            continue
        repaired = _repair(changes, associations, baseline, expected,
                           universe, inventory, graph)
        if repaired is None:
            continue
        changes, results = repaired
        key = _change_key(changes)
        if best is None or key < best[0]:
            best = (key, changes, results)

    if best is None:
        _explain_unsatisfiable(conflicts, requested, associations, baseline,
                               universe, expected, inventory, graph)
        for mime, group in requested.items():
            reports.append(TargetReport(
                mime, group[0].app, 'conflict',
                'cannot_preserve_unrequested_defaults',
                baseline[mime].app))
        return Plan(associations, [], conflicts, reports, baseline, universe,
                    graph, inventory)

    _, changes, results = best
    for mime, group in requested.items():
        target = group[0]
        changed = any(change.mime == mime for change in changes)
        status = 'already' if baseline[mime].app == target.app else 'planned'
        reports.append(TargetReport(
            mime, target.app, status,
            'pinned' if status == 'already' and changed else None,
            baseline[mime].app))
    return Plan(associations, changes, [], reports, baseline, universe,
                graph, inventory)


def _repair(changes, associations, baseline, expected, universe, inventory,
            graph):
    """Keep every requested value and every existing unrequested default.

    A pin is an explicit user entry for a type that would otherwise change
    indirectly through MIME inheritance.  Pinning preserves the effective
    default, so no unrequested type changes even though the configuration
    gains an entry.  A requested type can also need a pin: when its request
    is already satisfied by inheritance, changing the parent would otherwise
    make it drift.

    Returns ``(changes, results)`` or ``None`` when the values cannot all be
    kept.
    """
    changes = list(changes)
    for _ in range(len(universe) + 1):
        results = _simulate(associations, changes, universe, inventory, graph)
        pins = {}

        for mime, app in expected.items():
            if results[mime].app != app:
                pins[mime] = app
        for mime, (before, _) in _unauthorised(results, baseline,
                                               expected).items():
            pins[mime] = before

        if not pins:
            return _minimise(changes, associations, baseline, expected,
                             universe, inventory, graph), results

        progress = False
        for mime in sorted(pins):
            if any(change.mime == mime for change in changes):
                continue
            if inventory.selectable(pins[mime], mime)[0]:
                changes.append(Change(mime, pins[mime], 'set'))
                progress = True
        if not progress:
            return None
    return None


def _minimise(changes, associations, baseline, expected, universe,
              inventory, graph):
    """Drop pins that turn out to be unnecessary."""
    result = list(changes)
    for change in list(result):
        if change.mime in expected and change.action == 'set':
            continue
        trial = [item for item in result if item is not change]
        results = _simulate(associations, trial, universe, inventory, graph)
        if not _targets_ok(results, expected):
            continue
        if not _unauthorised(results, baseline, expected):
            result = trial
    return result


def _targets_ok(results, expected):
    for mime, app in expected.items():
        if results[mime].app != app:
            return False
    return True


def _explain_unsatisfiable(conflicts, requested, associations, baseline,
                           universe, expected, inventory, graph):
    culprits = defaultdict(set)
    for mime, group in requested.items():
        results = _simulate(associations,
                            [Change(mime, group[0].app, 'set')],
                            universe, inventory, graph)
        for other in _unauthorised(results, baseline, expected):
            culprits[other].add(mime)

    for other in sorted(culprits):
        conflicts.append(Conflict(
            other, None, 'unauthorized_change',
            'effective default would change',
            caused_by=sorted(culprits[other])))

    if not culprits:
        for mime, group in requested.items():
            conflicts.append(Conflict(
                mime, group[0].app, 'joint_constraint',
                'requests cannot be satisfied together'))


# ---------------------------------------------------------------------------
# Rendering, applying and verification
# ---------------------------------------------------------------------------

def render_user_file(original, changes):
    """Apply changes to the ``[Default Applications]`` group.

    Unrelated lines, comments, ordering and other groups are preserved.
    """
    if not changes:
        return original

    values = {}
    removals = set()
    for change in changes:
        if change.action == 'remove':
            removals.add(change.mime)
        else:
            values[change.mime] = change.app

    lines = original.splitlines()
    trailing = not original or original.endswith('\n')

    header = re.compile(r'^\s*\[(.*)\]\s*$')
    key_re = re.compile(r'^\s*([^=;#\s][^=]*?)\s*=')

    start = end = None
    group = None
    for index, line in enumerate(lines):
        match = header.match(line)
        if match:
            if group == DEFAULT_GROUP and start is not None and end is None:
                end = index
            group = match.group(1).strip()
            if group == DEFAULT_GROUP and start is None:
                start = index + 1
    if start is not None and end is None:
        end = len(lines)

    if start is None:
        if lines and lines[-1].strip():
            lines.append('')
        lines.append('[' + DEFAULT_GROUP + ']')
        for mime in sorted(values):
            lines.append('%s=%s;' % (mime, values[mime]))
        return '\n'.join(lines) + '\n'

    body = []
    handled = set()
    for line in lines[start:end]:
        match = key_re.match(line)
        if match and not line.lstrip().startswith('#'):
            key = canonical_mime(match.group(1))
            if key in removals:
                continue
            if key in values:
                body.append('%s=%s;' % (key, values[key]))
                handled.add(key)
                continue
        body.append(line)
    for mime in sorted(values):
        if mime not in handled:
            body.append('%s=%s;' % (mime, values[mime]))

    lines = lines[:start] + body + lines[end:]
    text = '\n'.join(lines)
    if trailing:
        text += '\n'
    return text


def _render_association_text(defaults, added, removed):
    lines = ['[' + DEFAULT_GROUP + ']']
    for mime, values in defaults.items():
        lines.append('%s=%s' % (mime, ''.join(value + ';'
                                              for value in values)))
    if added:
        lines.append('')
        lines.append('[' + ADDED_GROUP + ']')
        for mime, values in added.items():
            lines.append('%s=%s' % (mime, ''.join(value + ';'
                                                  for value in values)))
    if removed:
        lines.append('')
        lines.append('[' + REMOVED_GROUP + ']')
        for mime, values in removed.items():
            lines.append('%s=%s' % (mime, ''.join(value + ';'
                                                  for value in values)))
    return '\n'.join(lines) + '\n'


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    fd, temporary = tempfile.mkstemp(prefix='.mimeapps-', dir=directory)
    try:
        with io.open(fd, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(temporary, 0o644)
        _replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _read_bytes(path):
    try:
        with io.open(path, 'rb') as fp:
            return fp.read()
    except IOError:
        return None


def _restore_bytes(path, data):
    if data is None:
        if os.path.exists(path):
            os.unlink(path)
        return
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    fd, temporary = tempfile.mkstemp(prefix='.mimeapps-restore-',
                                     dir=directory)
    try:
        with io.open(fd, 'wb') as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(temporary, 0o644)
        _replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def verify_plan(plan):
    """Read the user file back from disk and report the actual selection."""
    associations = plan.associations.reload_user()
    resolver = Resolver(associations, plan.inventory, plan.graph)
    actual = resolver.resolve_all(plan.universe)

    problems = []
    selections = []
    for report in plan.targets:
        if report.status in ('conflict', 'not_applied'):
            continue
        resolution = actual[report.mime]
        selections.append({
            'mime': report.mime,
            'expected': report.app,
            'actual': resolution.app,
            'source': resolution.source,
            'origin': resolution.origin,
            'inherited_from': resolution.inherited_from,
        })
        if resolution.app != report.app:
            problems.append('%s: expected %s, got %s'
                            % (report.mime, report.app, resolution.app))

    requested = set(report.mime for report in plan.targets
                    if report.status not in ('conflict', 'not_applied'))
    for mime in sorted(plan.universe):
        if mime in requested:
            continue
        before = plan.baseline[mime].app
        after = actual[mime].app
        if before is not None and after != before:
            problems.append('%s: default changed from %s to %s'
                            % (mime, before, after))

    return {
        'ok': not problems,
        'problems': problems,
        'selections': selections,
        'actual': dict((mime, resolution.as_dict())
                       for mime, resolution in sorted(actual.items())),
    }


def apply_plan(plan, verify=True):
    """Write the plan, read it back, and restore on verification failure."""
    path = plan.associations.user_path
    if not plan.ok:
        return {
            'ok': False,
            'written': False,
            'reason': 'conflicts',
            'plan': plan.as_dict(),
            'verification': None,
        }
    if not path:
        return {
            'ok': False,
            'written': False,
            'reason': 'no_user_file',
            'plan': plan.as_dict(),
            'verification': None,
        }

    original = _read_bytes(path)
    original_text = original.decode('utf-8', 'replace') if original else ''

    if not plan.changes:
        verification = verify_plan(plan) if verify else None
        return {
            'ok': verification['ok'] if verification else True,
            'written': False,
            'reason': 'unchanged',
            'plan': plan.as_dict(),
            'verification': verification,
        }

    text = render_user_file(original_text, plan.changes)
    if text == original_text:
        verification = verify_plan(plan) if verify else None
        return {
            'ok': verification['ok'] if verification else True,
            'written': False,
            'reason': 'unchanged',
            'plan': plan.as_dict(),
            'verification': verification,
        }

    _atomic_write(path, text)
    verification = verify_plan(plan) if verify else None
    if verification is not None and not verification['ok']:
        _restore_bytes(path, original)
        return {
            'ok': False,
            'written': False,
            'reason': 'verification_failed',
            'restored': True,
            'plan': plan.as_dict(),
            'verification': verification,
        }

    return {
        'ok': True,
        'written': True,
        'reason': 'applied',
        'plan': plan.as_dict(),
        'verification': verification,
    }


def update_associations(targets, scenario=None, **kwargs):
    """Plan, apply and verify a set of default application requests.

    ``scenario`` accepts the same mapping as :func:`load_scenario`; explicit
    keyword arguments take precedence over its entries.
    """
    if scenario is not None:
        graph, inventory, associations = load_scenario(scenario)
        kwargs.setdefault('graph', graph)
        kwargs.setdefault('inventory', inventory)
        kwargs.setdefault('associations', associations)
    return apply_plan(plan_associations(targets, **kwargs))


def describe(scenario):
    """Return the inventory, inheritance and current selections for a
    scenario without changing any file.  Used by ``--describe``.
    """
    graph, inventory, associations = load_scenario(scenario)
    targets = scenario.get('targets') or []
    requested = [canonical_mime(target['mime'] if isinstance(target, dict)
                                else target[0]) for target in targets]
    tips = set(requested)
    tips.update(inventory.all_mime_types())
    tips.update(associations.keys())
    universe = _universe(tips, associations, inventory, graph)
    resolved = Resolver(associations, inventory, graph).resolve_all(universe)

    inventory_rows = []
    for app in inventory.apps.values():
        row = app.as_dict(graph)
        row['supports'] = dict(
            (mime, app.supports(mime, graph))
            for mime in sorted(set(requested) | set(app.mime_types)))
        row['selectable_for'] = dict(
            (mime, list(app.selectable(mime, graph, inventory.desktop_env)))
            for mime in sorted(requested))
        inventory_rows.append(row)

    return {
        'inventory': inventory_rows,
        'inheritance': graph.as_dict(tips),
        'effective_defaults': dict(
            (mime, resolution.as_dict())
            for mime, resolution in sorted(resolved.items())
            if resolution.app is not None),
        'layers': [
            {
                'path': item.path,
                'origin': item.origin,
                'exists': not item.missing,
                'user': index == associations.user_index,
                'defaults': dict(item.defaults),
                'added': dict(item.added),
                'removed': dict(item.removed),
            }
            for index, item in enumerate(associations.files)
        ],
    }


# ---------------------------------------------------------------------------
# Scenario input and command line
# ---------------------------------------------------------------------------

def _association_set_from_scenario(scenario):
    config_home = scenario.get('config_home')
    desktop_env = scenario.get('desktop_env')
    user_file = scenario.get('user_file')
    if user_file is None:
        base = config_home or BaseDirectory.xdg_config_home
        user_file = os.path.join(base, MAIN_FILE)

    items = scenario.get('associations')
    if items is not None:
        files = []
        user_index = None
        for index, item in enumerate(items):
            entry = item if isinstance(item, AssociationFile) \
                else AssociationFile.from_dict(item)
            files.append(entry)
            marked_user = isinstance(item, dict) and item.get('user')
            if marked_user or (entry.path and os.path.abspath(entry.path) ==
                               os.path.abspath(user_file)):
                user_index = index
        if user_index is None:
            files.insert(0, AssociationFile(user_file))
            user_index = 0
        return AssociationSet(files, user_index, user_file)

    files = load_association_files(
        config_home=config_home,
        config_dirs=scenario.get('config_dirs'),
        data_home=scenario.get('data_home'),
        data_dirs=scenario.get('data_dirs'),
        desktop_env=desktop_env,
    )
    user_index = None
    for index, item in enumerate(files):
        if item.path and _user_config_file(
                item.path, config_home or BaseDirectory.xdg_config_home,
                desktop_env):
            user_index = index
            break
    if user_index is None:
        files.insert(0, AssociationFile(user_file))
        user_index = 0
    else:
        user_file = files[user_index].path
    return AssociationSet(files, user_index, user_file)


def load_scenario(scenario):
    """Build the graph, inventory and associations from a scenario.

    Recognised keys:

    ``data_dirs`` / ``config_home`` / ``config_dirs`` / ``data_home``
        XDG search paths.  ``data_dirs`` are data directories; their
        ``applications`` subdirectory is scanned.
    ``desktop_env``
        Value of ``$XDG_CURRENT_DESKTOP``.
    ``user_file``
        Association file to create or update.  Defaults to
        ``$XDG_CONFIG_HOME/mimeapps.list``.
    ``targets``
        List of ``{"mime": ..., "app": ...}`` requests.
    ``inventory``
        Optional explicit list of desktop entries.  Each entry needs ``id``
        and either ``path`` or the relevant fields (``type``, ``hidden``,
        ``no_display``, ``exec``, ``try_exec``, ``mime_types``,
        ``executable``, ``deleted``, ``only_show_in``, ``not_show_in``).
    ``inheritance``
        Optional mapping ``{child: [parent, ...]}`` merged with the
        ``mime/subclasses`` files found in ``data_dirs``.
    ``aliases``
        Optional mapping of MIME aliases.
    ``associations``
        Optional ordered list of association files, each with ``path`` or
        explicit ``defaults`` / ``added`` / ``removed`` maps.  Mark the layer
        to edit with ``"user": true``.
    """
    graph = MimeGraph.from_data_dirs(scenario.get('data_dirs'))
    for alias, target in (scenario.get('aliases') or {}).items():
        graph.aliases[canonical_mime(alias)] = canonical_mime(target)
    for child, parents in (scenario.get('inheritance') or {}).items():
        if isinstance(parents, _string_types):
            parents = [parents]
        graph.set_parents(child, parents)

    desktop_env = scenario.get('desktop_env')

    if scenario.get('inventory') is not None:
        inventory = DesktopInventory.from_entries(
            scenario['inventory'], graph, desktop_env)
    else:
        inventory = DesktopInventory.scan(
            scenario.get('data_dirs'), graph, desktop_env)

    associations = _association_set_from_scenario(scenario)
    return graph, inventory, associations


def main(argv=None):
    import sys

    if argv is None:
        argv = sys.argv[1:]
    flags = set(item for item in argv if item.startswith('--'))
    unknown = flags - set(['--dry-run', '--describe'])
    args = [item for item in argv if not item.startswith('--')]
    if len(args) != 1 or unknown or '-h' in argv or '--help' in argv:
        sys.stderr.write('usage: python -m xdg.MimeApps SCENARIO.json '
                         '[--dry-run | --describe]\n')
        return 2

    with io.open(args[0], 'r', encoding='utf-8') as fp:
        scenario = json.load(fp)

    if '--describe' in flags:
        result = describe(scenario)
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
        return 0

    graph, inventory, associations = load_scenario(scenario)
    plan = plan_associations(
        scenario.get('targets', []),
        associations=associations,
        inventory=inventory,
        graph=graph,
    )

    if '--dry-run' in flags:
        result = {
            'ok': plan.ok,
            'written': False,
            'reason': 'dry_run',
            'plan': plan.as_dict(),
            'verification': None,
        }
        result['proposed_file'] = render_user_file(
            associations.user.text, plan.changes)
    else:
        result = apply_plan(plan)
        result['proposed_file'] = render_user_file(
            associations.user.text, plan.changes)

    result['inventory'] = inventory.as_list()
    result['inheritance'] = graph.as_dict(
        set(target.mime for target in plan.targets) | set(plan.baseline))
    result['existing'] = dict(
        (mime, resolution.as_dict())
        for mime, resolution in sorted(plan.baseline.items())
        if resolution.app is not None)
    result['layers'] = [
        {
            'path': item.path,
            'origin': item.origin,
            'exists': not item.missing,
            'user': index == associations.user_index,
            'defaults': dict(item.defaults),
            'added': dict(item.added),
            'removed': dict(item.removed),
        }
        for index, item in enumerate(associations.files)
    ]

    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
