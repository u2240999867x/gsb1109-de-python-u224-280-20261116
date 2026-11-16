"""
Desktop Actions support on top of the XDG Desktop Entry Specification
http://standards.freedesktop.org/desktop-entry-spec/

A desktop entry of Type=Application may advertise extra actions through an
``Actions=`` key and matching ``[Desktop Action <id>]`` groups.

Only the desktop file that actually wins the XDG data directory lookup is
read: when a user file overrides a system file with the same desktop file id,
the groups of the shadowed file are never merged in, and removing the
override makes the system file effective again on the next lookup. Action
ids are scoped to the application that declares them.
"""

import os.path

import xdg.Locale
from xdg.DesktopEntry import DesktopEntry
from xdg.Exceptions import ParsingError

ACTION_GROUP_PREFIX = "Desktop Action "


def _localized_value(entry, key, group, languages):
    """Return ``key``'s value in ``group``, following the given languages.

    Each language is normalized and expanded with xdg.Locale, so e.g.
    ``de_DE.UTF-8`` also tries ``de_DE`` and ``de``. The unlocalized key is
    used as the final fallback. With ``languages=None`` the global locale
    settings used by :class:`~xdg.DesktopEntry.DesktopEntry` apply.
    """
    if languages is None:
        return entry.get(key, group=group, locale=True)

    content = entry.content.get(group, {})
    for lang in xdg.Locale.expand_languages(list(languages)):
        localized_key = "%s[%s]" % (key, lang)
        value = content.get(localized_key)
        if value:
            return value
    return content.get(key, "")


class DesktopAction:
    """One executable Desktop Action declared inside a desktop entry.

    Actions are always reached through their owning
    :class:`DesktopApplication`; the action id is only meaningful inside
    that application.
    """

    def __init__(self, entry, action_id, group=None):
        self.entry = entry
        self.id = action_id
        self.group = group or ACTION_GROUP_PREFIX + action_id

    def getId(self):
        """Return the action identifier (unique within one application)."""
        return self.id

    def getGroup(self):
        """Return the INI group name backing this action."""
        return self.group

    def getExec(self):
        """Return the action's Exec command line."""
        return self.entry.get("Exec", group=self.group)

    def getName(self, languages=None):
        """Return the localized action name.

        languages -- optional list of language identifiers to try before
        falling back to the unlocalized Name.
        """
        return _localized_value(self.entry, "Name", self.group, languages)

    def getFileName(self):
        """Return the desktop file this action was read from."""
        return self.entry.getFileName()

    def getSource(self):
        """Return the (filename, group) pair backing this action."""
        return (self.entry.getFileName(), self.group)

    def __str__(self):
        return self.id

    def __repr__(self):
        return "<DesktopAction %s>" % self.id


class DesktopApplication:
    """An Application desktop entry and the Desktop Actions it really exposes.

    Only the contents of the effective desktop file are used. Shadowed files
    carrying the same desktop file id are kept for provenance
    (:meth:`getShadowedFiles`) but never contribute keys or actions.
    """

    def __init__(self, desktop_file_id, entry, shadowed_files=None):
        self.id = desktop_file_id
        self.entry = entry
        self.shadowed_files = list(shadowed_files or [])
        self.actions = []
        self.errors = {
            "missing_groups": [],
            "duplicate_references": [],
            "empty_commands": [],
        }
        self._resolve_actions()

    @classmethod
    def from_file(cls, filename, shadowed_files=None):
        """Build a DesktopApplication from one explicit desktop file."""
        entry = DesktopEntry(filename)
        desktop_file_id = os.path.basename(filename)
        return cls(desktop_file_id, entry, shadowed_files)

    @classmethod
    def discover(cls, data_dirs=None):
        """Scan ``<data_dir>/applications`` for effective desktop entries.

        data_dirs defaults to :data:`xdg.BaseDirectory.xdg_data_dirs`,
        ordered by precedence (the user data home comes first). The first
        file found for a desktop file id wins; later files with the same
        id are recorded as shadowed and never parsed for actions.

        Returns a dict mapping desktop file id to DesktopApplication.
        """
        if data_dirs is None:
            from xdg.BaseDirectory import xdg_data_dirs
            data_dirs = xdg_data_dirs

        effective = {}
        shadowed = {}
        for data_dir in data_dirs:
            applications_dir = os.path.join(data_dir, "applications")
            if not os.path.isdir(applications_dir):
                continue
            for dirpath, dirnames, filenames in os.walk(applications_dir):
                dirnames.sort()
                for filename in sorted(filenames):
                    if not filename.endswith(".desktop"):
                        continue
                    path = os.path.join(dirpath, filename)
                    relative = os.path.relpath(path, applications_dir)
                    desktop_file_id = relative.replace(os.sep, "-")
                    if desktop_file_id in effective:
                        shadowed.setdefault(desktop_file_id, []).append(path)
                    else:
                        effective[desktop_file_id] = path

        applications = {}
        for desktop_file_id, path in effective.items():
            try:
                entry = DesktopEntry(path)
            except ParsingError:
                continue
            applications[desktop_file_id] = cls(
                desktop_file_id, entry, shadowed.get(desktop_file_id))
        return applications

    def _resolve_actions(self):
        # Only the effective file is ever consulted.
        content = self.entry.content.get(self.entry.defaultGroup, {})
        references = self.entry.getList(content.get("Actions", ""))

        seen = set()
        for action_id in references:
            if action_id in seen:
                self.errors["duplicate_references"].append(action_id)
                continue
            seen.add(action_id)

            group = ACTION_GROUP_PREFIX + action_id
            if not self.entry.hasGroup(group):
                self.errors["missing_groups"].append(action_id)
                continue
            if not self.entry.get("Exec", group=group).strip():
                self.errors["empty_commands"].append(action_id)
                continue

            self.actions.append(DesktopAction(self.entry, action_id, group))

    def getId(self):
        """Return the desktop file id, e.g. ``org.example.Foo.desktop``."""
        return self.id

    def getDesktopEntry(self):
        """Return the backing :class:`~xdg.DesktopEntry.DesktopEntry`."""
        return self.entry

    def getFileName(self):
        """Return the path of the effective desktop file."""
        return self.entry.getFileName()

    def getSource(self):
        """Return the path of the effective desktop file."""
        return self.entry.getFileName()

    def getShadowedFiles(self):
        """Return lower-precedence files with the same desktop file id."""
        return list(self.shadowed_files)

    def getName(self, languages=None):
        """Return the localized application Name.

        languages -- optional list of language identifiers to try before
        falling back to the unlocalized Name.
        """
        return _localized_value(
            self.entry, "Name", self.entry.defaultGroup, languages)

    def getActions(self):
        """Return the executable actions, in Actions= order.

        Referenced ids without a group, duplicates beyond the first
        reference and groups with an empty Exec are excluded; see
        :meth:`getErrors`. Groups that the Actions= list does not
        reference never become actions.
        """
        return list(self.actions)

    def getAction(self, action_id):
        """Return the action with this id, or None.

        The lookup is scoped to this application only; the same id may
        exist independently in another application.
        """
        for action in self.actions:
            if action.getId() == action_id:
                return action
        return None

    def getErrors(self):
        """Return a dict of action resolution problems.

        Keys are ``missing_groups`` (referenced id without a group),
        ``duplicate_references`` (ids referenced more than once) and
        ``empty_commands`` (groups without a usable Exec command).
        """
        return {
            "missing_groups": list(self.errors["missing_groups"]),
            "duplicate_references": list(self.errors["duplicate_references"]),
            "empty_commands": list(self.errors["empty_commands"]),
        }

    def getUnreferencedActionGroups(self):
        """Return ``[Desktop Action ...]`` groups not referenced by Actions=.

        Such groups stay ordinary INI groups and are never exposed as
        executable actions.
        """
        referenced = set(action.getGroup() for action in self.actions)
        referenced.update(
            ACTION_GROUP_PREFIX + action_id
            for action_id in
            self.errors["missing_groups"] + self.errors["empty_commands"])
        return [group for group in self.entry.groups()
                if group.startswith(ACTION_GROUP_PREFIX)
                and group not in referenced]

    def __str__(self):
        return self.getName() or self.id

    def __repr__(self):
        return "<DesktopApplication %s>" % self.id


def discover_desktop_applications(data_dirs=None):
    """Module-level convenience wrapper for :meth:`DesktopApplication.discover`."""
    return DesktopApplication.discover(data_dirs)
