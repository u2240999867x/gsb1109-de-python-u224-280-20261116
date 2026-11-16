"""
Implementation of the XDG Menu Specification
http://standards.freedesktop.org/menu-spec/

Example code:

from xdg.Menu import parse, Menu, MenuEntry

def print_menu(menu, tab=0):
  for submenu in menu.Entries:
    if isinstance(submenu, Menu):
      print ("\t" * tab) + unicode(submenu)
      print_menu(submenu, tab+1)
    elif isinstance(submenu, MenuEntry):
      print ("\t" * tab) + unicode(submenu.DesktopEntry)

print_menu(parse())
"""

import os
import locale
import subprocess
import ast
import sys
try:
    import xml.etree.cElementTree as etree
except ImportError:
    import xml.etree.ElementTree as etree

from xdg.BaseDirectory import xdg_data_dirs, xdg_config_dirs
from xdg.DesktopEntry import DesktopEntry
from xdg.Exceptions import ParsingError
from xdg.util import PY3

import xdg.Locale
import xdg.Config


def _ast_const(name):
    if sys.version_info >= (3, 4):
        name = ast.literal_eval(name)
        if sys.version_info >= (3, 8):
            return ast.Constant(name)
        else:
            return ast.NameConstant(name)
    else:
        return ast.Name(id=name, ctx=ast.Load())


def _strxfrm(s):
    """Wrapper around locale.strxfrm that accepts unicode strings on Python 2.

    See Python bug #2481.
    """
    if (not PY3) and isinstance(s, unicode):
        s = s.encode('utf-8')
    return locale.strxfrm(s)


DELETED = "Deleted"
NO_DISPLAY = "NoDisplay"
HIDDEN = "Hidden"
EMPTY = "Empty"
NOT_SHOW_IN = "NotShowIn"
NO_EXEC = "NoExec"
EXCLUDED = "Excluded"
NOT_INCLUDED = "NotIncluded"
ALLOCATED = "Allocated"
NOT_IN_LAYOUT = "NotInLayout"
MISSING_MERGE_FILE = "MissingMergeFile"


class ExclusionReason:
    """A record explaining why an item is absent from a menu.

    ``Reason`` is one of the module level constants (``EXCLUDED``,
    ``NOT_INCLUDED``, ``NOT_IN_LAYOUT``, ``ALLOCATED``,
    ``MISSING_MERGE_FILE``, ``NO_DISPLAY``, ``HIDDEN``, ``DELETED``,
    ``EMPTY``, ``NOT_SHOW_IN`` or ``NO_EXEC``).  When the reason is an
    ``<Exclude>`` rule, ``Rule`` points at the matching :class:`Rule` and
    ``Detail`` contains its XML source.
    """

    def __init__(self, menu, entry, reason, rule=None, detail=""):
        self.Menu = menu
        self.Entry = entry
        self.Reason = reason
        self.Rule = rule
        self.Detail = detail
        self.DesktopFileID = getattr(entry, "DesktopFileID", None)
        if hasattr(entry, "ExclusionReasons"):
            already = any(old.Menu is menu and old.Reason == reason
                          for old in entry.ExclusionReasons)
            if not already:
                entry.ExclusionReasons.append(self)
        if hasattr(entry, "ExcludedFrom") and menu not in entry.ExcludedFrom:
            entry.ExcludedFrom.append(menu)

    def __str__(self):
        return "%s: %s" % (self.Reason, self.DesktopFileID)

    def __repr__(self):
        return "<ExclusionReason %r for %r>" % (self.Reason,
                                                self.DesktopFileID)

    # Lowercase aliases are convenient for callers that do not use the
    # historical CamelCase style of this module.
    @property
    def menu(self):
        return self.Menu

    @property
    def entry(self):
        return self.Entry

    @property
    def reason(self):
        return self.Reason

    @property
    def rule(self):
        return self.Rule

    @property
    def detail(self):
        return self.Detail


class Menu:
    """Menu containing sub menus under menu.Entries

    Contains both Menu and MenuEntry items.
    """
    def __init__(self):
        # Public stuff
        self.Name = ""
        self.Directory = None
        self.Entries = []
        self.Doc = ""
        self.Filename = ""
        self.Depth = 0
        self.Parent = None
        self.NotInXml = False

        # Can be True, False, DELETED, NO_DISPLAY, HIDDEN, EMPTY or NOT_SHOW_IN
        self.Show = True
        self.Visible = 0

        # Private stuff, only needed for parsing
        self.AppDirs = []
        self.DefaultLayout = None
        self.Deleted = None
        self.Directories = []
        self.DirectoryDirs = []
        self.Layout = None
        self.MenuEntries = []
        self.Moves = []
        self.OnlyUnallocated = None
        self.Rules = []
        self.Submenus = []

        # Entries which were not shown in this menu, with the reason why.
        # Every entry which was considered for this menu but not added to it
        # is recorded here, so the decision can be inspected later.
        self.ExcludedEntries = []
        self.ExcludedSubmenus = []
        # Exclusion records produced while applying Include/Exclude rules.
        # sort() combines these with visibility/layout records.
        self._generation_exclusions = []
        self._layout_exclusions = []
        self._visibility_exclusions = []
        # The merged menu files which contributed to this menu, in order.
        self.MergeFiles = []
        # Merge files which could not be read, and cycles which were cut.
        self.MissingMergeFiles = []
        self.MergeCycles = []
        self.InvalidMergeFiles = []
        # Stable identity for merge bookkeeping (menu names may be changed by
        # moves, and equal names are merged into one Menu object).
        self._identity = object()

    def __str__(self):
        return self.Name

    def __add__(self, other):
        for dir in other.AppDirs:
            self.AppDirs.append(dir)

        for dir in other.DirectoryDirs:
            self.DirectoryDirs.append(dir)

        for directory in other.Directories:
            self.Directories.append(directory)

        if other.Deleted is not None:
            self.Deleted = other.Deleted

        if other.OnlyUnallocated is not None:
            self.OnlyUnallocated = other.OnlyUnallocated

        if other.Layout:
            self.Layout = other.Layout

        if other.DefaultLayout:
            self.DefaultLayout = other.DefaultLayout

        for rule in other.Rules:
            self.Rules.append(rule)

        for move in other.Moves:
            self.Moves.append(move)

        for submenu in other.Submenus:
            self.addSubmenu(submenu)

        for filename in other.MergeFiles:
            if filename not in self.MergeFiles:
                self.MergeFiles.append(filename)
        for filename in other.MissingMergeFiles:
            if filename not in self.MissingMergeFiles:
                self.MissingMergeFiles.append(filename)
        for filename in other.MergeCycles:
            if filename not in self.MergeCycles:
                self.MergeCycles.append(filename)
        for filename in other.InvalidMergeFiles:
            if filename not in self.InvalidMergeFiles:
                self.InvalidMergeFiles.append(filename)
        for record in other._generation_exclusions:
            record.Menu = self
            if (record.Entry in self.MenuEntries or
                    not isinstance(record.Entry, MenuEntry)):
                self._generation_exclusions.append(record)
        for record in other._layout_exclusions:
            record.Menu = self
        for record in other._visibility_exclusions:
            record.Menu = self

        return self

    # FIXME: Performance: cache getName()
    def __cmp__(self, other):
        return locale.strcoll(self.getName(), other.getName())

    def _key(self):
        """Key function for locale-aware sorting."""
        return _strxfrm(self.getName())

    def __lt__(self, other):
        try:
            other = other._key()
        except AttributeError:
            pass
        return self._key() < other

    def __eq__(self, other):
        try:
            return self.Name == unicode(other)
        except NameError:  # unicode() becomes str() in Python 3
            return self.Name == str(other)

    """ PUBLIC STUFF """
    def getEntries(self, show_hidden=False):
        """Interator for a list of Entries visible to the user."""
        for entry in self.Entries:
            if show_hidden:
                yield entry
            elif entry.Show is True:
                yield entry

    # FIXME: Add searchEntry/seaqrchMenu function
    # search for name/comment/genericname/desktopfileid
    # return multiple items

    def getMenuEntry(self, desktopfileid, deep=False):
        """Searches for a MenuEntry with a given DesktopFileID."""
        for menuentry in self.MenuEntries:
            if menuentry.DesktopFileID == desktopfileid:
                return menuentry
        if deep:
            for submenu in self.Submenus:
                submenu.getMenuEntry(desktopfileid, deep)

    def getMenu(self, path):
        """Searches for a Menu with a given path."""
        array = path.split("/", 1)
        for submenu in self.Submenus:
            if submenu.Name == array[0]:
                if len(array) > 1:
                    return submenu.getMenu(array[1])
                else:
                    return submenu

    def getPath(self, org=False, toplevel=False):
        """Returns this menu's path in the menu structure."""
        parent = self
        names = []
        while 1:
            if org:
                names.append(parent.Name)
            else:
                names.append(parent.getName())
            if parent.Depth > 0:
                parent = parent.Parent
            else:
                break
        names.reverse()
        path = ""
        if not toplevel:
            names.pop(0)
        for name in names:
            path = os.path.join(path, name)
        return path

    def getName(self):
        """Returns the menu's localised name."""
        try:
            return self.Directory.DesktopEntry.getName()
        except AttributeError:
            return self.Name

    def getGenericName(self):
        """Returns the menu's generic name."""
        try:
            return self.Directory.DesktopEntry.getGenericName()
        except AttributeError:
            return ""

    def getComment(self):
        """Returns the menu's comment text."""
        try:
            return self.Directory.DesktopEntry.getComment()
        except AttributeError:
            return ""

    def getIcon(self):
        """Returns the menu's icon, filename or simple name"""
        try:
            return self.Directory.DesktopEntry.getIcon()
        except AttributeError:
            return ""

    def getExcludedEntries(self, deep=False):
        """Returns records for entries which this menu did not show.

        Each record is an :class:`ExclusionReason` explaining whether the
        entry matched an ``<Exclude>`` rule, was not matched by any
        ``<Include>`` rule, is hidden, was excluded by the layout, and so on.
        With ``deep=True`` records from submenus are included as well.
        """
        records = list(self.ExcludedEntries)
        if deep:
            for submenu in self.Submenus:
                records.extend(submenu.getExcludedEntries(True))
        return records

    def getExclusionReason(self, desktopfileid, deep=False):
        """Return the exclusion records for one DesktopFileID in this menu."""
        return [record for record in self.getExcludedEntries(deep)
                if record.DesktopFileID == desktopfileid]

    def getExclusionReasons(self, deep=False):
        """Return all exclusion records for this menu."""
        return self.getExcludedEntries(deep)

    def getExcludedSubmenus(self, deep=False):
        """Return exclusion records for submenus which are not visible."""
        if deep:
            return [record for record in self.getExcludedEntries(True)
                    if isinstance(record.Entry, Menu)]
        return list(self.ExcludedSubmenus)

    def getAppearances(self, deep=False, visible=True):
        """Return a mapping of DesktopFileID to the menus showing it.

        An application may legitimately be shown in more than one menu; this
        method makes those relationships explicit.  With ``deep=True`` the
        submenus are traversed as well.  By default only entries which are
        actually visible are reported; pass ``visible=False`` to include
        allocated entries hidden by the layout or visibility checks.
        """
        appearances = {}
        for entry in self.MenuEntries:
            if visible and self not in entry.Appearances:
                continue
            appearances.setdefault(entry.DesktopFileID, []).append(self)
        if deep:
            for submenu in self.Submenus:
                for desktopfileid, menus in submenu.getAppearances(
                        True, visible).items():
                    target = appearances.setdefault(desktopfileid, [])
                    for menu in menus:
                        if menu not in target:
                            target.append(menu)
        return appearances

    def getEntryAppearances(self, desktopfileid, deep=True, visible=True):
        """Return the menus in which ``desktopfileid`` is shown."""
        result = [menu for menu in [self] if any(
            entry.DesktopFileID == desktopfileid and
            (not visible or self in entry.Appearances)
            for entry in self.MenuEntries)]
        if deep:
            for submenu in self.Submenus:
                for menu in submenu.getEntryAppearances(
                        desktopfileid, True, visible):
                    if menu not in result:
                        result.append(menu)
        return result

    def sort(self):
        self.Entries = []
        self.Visible = 0
        self._layout_exclusions = []
        self._visibility_exclusions = []

        for menuentry in self.MenuEntries:
            if self in menuentry.Appearances:
                menuentry.Appearances.remove(self)

        for submenu in self.Submenus:
            submenu.sort()

        _submenus = set()
        _entries = set()

        for order in self.Layout.order:
            if order[0] == "Filename":
                _entries.add(order[1])
            elif order[0] == "Menuname":
                _submenus.add(order[1])

        for order in self.Layout.order:
            if order[0] == "Separator":
                separator = Separator(self)
                if len(self.Entries) > 0 and isinstance(self.Entries[-1], Separator):
                    separator.Show = False
                self.Entries.append(separator)
            elif order[0] == "Filename":
                menuentry = self.getMenuEntry(order[1])
                if menuentry:
                    if menuentry not in self.Entries:
                        self.Entries.append(menuentry)
                else:
                    self._layout_exclusions.append(
                        ExclusionReason(self, _NamedEntry(order[1]),
                                        NOT_IN_LAYOUT,
                                        detail=order[1]))
            elif order[0] == "Menuname":
                submenu = self.getMenu(order[1])
                if submenu:
                    if submenu.Layout.inline:
                        self.merge_inline(submenu)
                    elif submenu not in self.Entries:
                        self.Entries.append(submenu)
                else:
                    self._layout_exclusions.append(
                        ExclusionReason(self, _NamedEntry(order[1]),
                                        NOT_IN_LAYOUT,
                                        detail=order[1]))
            elif order[0] == "Merge":
                if order[1] == "files" or order[1] == "all":
                    self.MenuEntries.sort()
                    for menuentry in self.MenuEntries:
                        if menuentry.DesktopFileID not in _entries:
                            if menuentry not in self.Entries:
                                self.Entries.append(menuentry)
                if order[1] == "menus" or order[1] == "all":
                    self.Submenus.sort()
                    for submenu in self.Submenus:
                        if submenu.Name not in _submenus:
                            if submenu.Layout.inline:
                                self.merge_inline(submenu)
                            elif submenu not in self.Entries:
                                self.Entries.append(submenu)

        # Entries explicitly named in the layout are the only ones which may
        # appear when the layout has no <Merge> element.  Record the rest so
        # callers can see why they are absent.  An explicit but empty <Layout>
        # intentionally shows nothing.
        layout_has_merge = any(order[0] == "Merge" for order in self.Layout.order)
        if self.Layout.explicit and not layout_has_merge:
            for menuentry in self.MenuEntries:
                if menuentry not in self.Entries:
                    self._layout_exclusions.append(
                        ExclusionReason(self, menuentry, NOT_IN_LAYOUT,
                                        detail=menuentry.DesktopFileID))
            for submenu in self.Submenus:
                if submenu not in self.Entries:
                    self._layout_exclusions.append(
                        ExclusionReason(self, submenu, NOT_IN_LAYOUT,
                                        detail=submenu.Name))

        # getHidden / NoDisplay / OnlyShowIn / NotOnlyShowIn / Deleted / NoExec
        for entry in self.Entries:
            entry.Show = True
            self.Visible += 1
            reason = None
            if isinstance(entry, Menu):
                if entry.Deleted is True:
                    reason = DELETED
                elif isinstance(entry.Directory, MenuEntry):
                    if entry.Directory.DesktopEntry.getNoDisplay():
                        reason = NO_DISPLAY
                    elif entry.Directory.DesktopEntry.getHidden():
                        reason = HIDDEN
            elif isinstance(entry, MenuEntry):
                if entry.DesktopEntry.getNoDisplay():
                    reason = NO_DISPLAY
                elif entry.DesktopEntry.getHidden():
                    reason = HIDDEN
                elif entry.DesktopEntry.getTryExec() and not entry.DesktopEntry.findTryExec():
                    reason = NO_EXEC
                elif xdg.Config.windowmanager:
                    if (entry.DesktopEntry.getOnlyShowIn() != [] and (
                            xdg.Config.windowmanager not in entry.DesktopEntry.getOnlyShowIn()
                        )
                    ) or (
                        xdg.Config.windowmanager in entry.DesktopEntry.getNotShowIn()
                    ):
                        reason = NOT_SHOW_IN
            elif isinstance(entry, Separator):
                self.Visible -= 1
            if reason is not None:
                entry.Show = reason
                self.Visible -= 1
                self._visibility_exclusions.append(
                    ExclusionReason(self, entry, reason))
        # remove separators at the beginning and at the end
        if len(self.Entries) > 0:
            if isinstance(self.Entries[0], Separator):
                self.Entries[0].Show = False
        if len(self.Entries) > 1:
            if isinstance(self.Entries[-1], Separator):
                self.Entries[-1].Show = False

        # show_empty tag
        for entry in self.Entries[:]:
            if isinstance(entry, Menu) and not entry.Layout.show_empty and entry.Visible == 0:
                entry.Show = EMPTY
                self.Visible -= 1
                self._visibility_exclusions.append(
                    ExclusionReason(self, entry, EMPTY))
                if entry.NotInXml is True:
                    self.Entries.remove(entry)

        self.ExcludedEntries = (list(self._generation_exclusions) +
                                list(self._layout_exclusions) +
                                list(self._visibility_exclusions))
        self.ExcludedSubmenus = [r for r in self.ExcludedEntries
                                 if isinstance(r.Entry, Menu)]

        # Record where this menu's entries are actually visible.  Unlike
        # MenuEntry.Parents, this excludes entries hidden by the layout or by
        # NoDisplay/Hidden/Deleted/etc.
        for entry in self.Entries:
            if isinstance(entry, MenuEntry) and entry.Show is True:
                if self not in entry.Appearances:
                    entry.Appearances.append(self)

    """ PRIVATE STUFF """
    def addSubmenu(self, newmenu):
        for submenu in self.Submenus:
            if submenu == newmenu:
                submenu += newmenu
                break
        else:
            self.Submenus.append(newmenu)
            newmenu.Parent = self
            newmenu.Depth = self.Depth + 1

    # inline tags
    def merge_inline(self, submenu):
        """Appends a submenu's entries to this menu
        See the <Menuname> section of the spec about the "inline" attribute
        """
        if len(submenu.Entries) == 1 and submenu.Layout.inline_alias:
            menuentry = submenu.Entries[0]
            menuentry.DesktopEntry.set("Name", submenu.getName(), locale=True)
            menuentry.DesktopEntry.set("GenericName", submenu.getGenericName(), locale=True)
            menuentry.DesktopEntry.set("Comment", submenu.getComment(), locale=True)
            self.Entries.append(menuentry)
        elif len(submenu.Entries) <= submenu.Layout.inline_limit or submenu.Layout.inline_limit == 0:
            if submenu.Layout.inline_header:
                header = Header(submenu.getName(), submenu.getGenericName(), submenu.getComment())
                self.Entries.append(header)
            for entry in submenu.Entries:
                self.Entries.append(entry)
        else:
            self.Entries.append(submenu)


class Move:
    "A move operation"
    def __init__(self, old="", new=""):
        self.Old = old
        self.New = new

    def __cmp__(self, other):
        return cmp(self.Old, other.Old)


class Layout:
    "Menu Layout class"
    def __init__(self, show_empty=False, inline=False, inline_limit=4,
                 inline_header=True, inline_alias=False, explicit=False):
        self.show_empty = show_empty
        self.inline = inline
        self.inline_limit = inline_limit
        self.inline_header = inline_header
        self.inline_alias = inline_alias
        self.explicit = explicit
        self._order = []
        self._default_order = [
            ['Merge', 'menus'],
            ['Merge', 'files']
        ]

    @property
    def order(self):
        if self._order or self.explicit:
            return self._order
        return self._default_order

    @order.setter
    def order(self, order):
        self._order = order


class Rule:
    """Include / Exclude Rules Class"""

    TYPE_INCLUDE, TYPE_EXCLUDE = 0, 1

    @classmethod
    def fromFilename(cls, type, filename):
        tree = ast.Expression(
            body=ast.Compare(
                left=ast.Str(filename),
                ops=[ast.Eq()],
                comparators=[ast.Attribute(
                    value=ast.Name(id='menuentry', ctx=ast.Load()),
                    attr='DesktopFileID',
                    ctx=ast.Load()
                )]
            ),
            lineno=1, col_offset=0
        )
        ast.fix_missing_locations(tree)
        rule = Rule(type, tree)
        return rule

    def __init__(self, type, expression, source=None, filename=None):
        # Type is TYPE_INCLUDE or TYPE_EXCLUDE
        self.Type = type
        # expression is ast.Expression
        self.expression = expression
        self.code = compile(self.expression, '<compiled-menu-rule>', 'eval')
        # The XML that produced the rule, for diagnostics and exclusion records.
        self.Source = source
        self.Filename = filename

    def __str__(self):
        return ast.dump(self.expression)

    def matches(self, menuentry):
        """Return True if the rule applies to ``menuentry``."""
        return bool(eval(self.code))

    def apply(self, menuentries, run):
        for menuentry in menuentries:
            if run == 2 and (menuentry.MatchedInclude is True or
                             menuentry.Allocated is True):
                continue
            if self.matches(menuentry):
                if self.Type is Rule.TYPE_INCLUDE:
                    menuentry.Add = True
                    menuentry.MatchedInclude = True
                    menuentry.IncludeRule = self
                else:
                    menuentry.Add = False
                    menuentry.ExcludeRule = self
        return menuentries


class MenuEntry:
    "Wrapper for 'Menu Style' Desktop Entries"

    TYPE_USER = "User"
    TYPE_SYSTEM = "System"
    TYPE_BOTH = "Both"

    def __init__(self, filename, dir="", prefix=""):
        # Create entry
        self.DesktopEntry = DesktopEntry(os.path.join(dir, filename))
        self.setAttributes(filename, dir, prefix)

        # Can True, False DELETED, HIDDEN, EMPTY, NOT_SHOW_IN or NO_EXEC
        self.Show = True

        # Semi-Private
        self.Original = None
        # Menus in which this entry is allocated (selected by the menu rules).
        self.Parents = []
        # Menus in which this entry is actually visible after layout and
        # visibility checks.
        self.Appearances = []
        # Every menu which considered this entry and the reason it was not
        # shown there.
        self.ExclusionReasons = []
        self.ExcludedFrom = []

        # Private Stuff
        self.Allocated = False
        self.Add = False
        self.MatchedInclude = False
        self.IncludeRule = None
        self.ExcludeRule = None

        # Caching
        self.Categories = self.DesktopEntry.getCategories()

    def save(self):
        """Save any changes to the desktop entry."""
        if self.DesktopEntry.tainted:
            self.DesktopEntry.write()

    def getDir(self):
        """Return the directory containing the desktop entry file."""
        return self.DesktopEntry.filename.replace(self.Filename, '')

    def getType(self):
        """Return the type of MenuEntry, System/User/Both"""
        if not xdg.Config.root_mode:
            if self.Original:
                return self.TYPE_BOTH
            elif xdg_data_dirs[0] in self.DesktopEntry.filename:
                return self.TYPE_USER
            else:
                return self.TYPE_SYSTEM
        else:
            return self.TYPE_USER

    def setAttributes(self, filename, dir="", prefix=""):
        self.Filename = filename
        self.Prefix = prefix
        self.DesktopFileID = os.path.join(prefix, filename).replace("/", "-")

        if not os.path.isabs(self.DesktopEntry.filename):
            self.__setFilename()

    def updateAttributes(self):
        if self.getType() == self.TYPE_SYSTEM:
            self.Original = MenuEntry(self.Filename, self.getDir(), self.Prefix)
            self.__setFilename()

    def __setFilename(self):
        if not xdg.Config.root_mode:
            path = xdg_data_dirs[0]
        else:
            path = xdg_data_dirs[1]

        if self.DesktopEntry.getType() == "Application":
            dir_ = os.path.join(path, "applications")
        else:
            dir_ = os.path.join(path, "desktop-directories")

        self.DesktopEntry.filename = os.path.join(dir_, self.Filename)

    def __cmp__(self, other):
        return locale.strcoll(self.DesktopEntry.getName(), other.DesktopEntry.getName())

    def _key(self):
        """Key function for locale-aware sorting."""
        return _strxfrm(self.DesktopEntry.getName())

    def __lt__(self, other):
        try:
            other = other._key()
        except AttributeError:
            pass
        return self._key() < other

    def __eq__(self, other):
        if self.DesktopFileID == str(other):
            return True
        else:
            return False

    def __repr__(self):
        return self.DesktopFileID


class Separator:
    "Just a dummy class for Separators"
    def __init__(self, parent):
        self.Parent = parent
        self.Show = True


class Header:
    "Class for Inline Headers"
    def __init__(self, name, generic_name, comment):
        self.Name = name
        self.GenericName = generic_name
        self.Comment = comment

    def __str__(self):
        return self.Name


class _NamedEntry:
    """Placeholder for a layout item which could not be resolved."""

    def __init__(self, desktopfileid):
        self.DesktopFileID = desktopfileid
        self.Name = desktopfileid

    def __str__(self):
        return self.DesktopFileID


TYPE_DIR, TYPE_FILE = 0, 1


def _check_file_path(value, filename, type):
    path = os.path.dirname(filename)
    if not os.path.isabs(value):
        value = os.path.join(path, value)
    value = os.path.abspath(value)
    if not os.path.exists(value):
        return False
    if type == TYPE_DIR and os.path.isdir(value):
        return value
    if type == TYPE_FILE and os.path.isfile(value):
        return value
    return False


def _get_menu_file_path(filename):
    dirs = list(xdg_config_dirs)
    if xdg.Config.root_mode is True:
        dirs.pop(0)
    for d in dirs:
        menuname = os.path.join(d, "menus", filename)
        if os.path.isfile(menuname):
            return menuname


def _to_bool(value):
    if isinstance(value, bool):
        return value
    return value.lower() == "true"


# remove duplicate entries from a list
def _dedupe(_list):
    _set = {}
    _list.reverse()
    _list = [_set.setdefault(e, e) for e in _list if e not in _set]
    _list.reverse()
    return _list


class MergeContext:
    """State used while following one chain of <MergeFile> elements.

    A menu file may legitimately be merged into several different menus, so
    merged files must not be tracked globally.  A file which is already on the
    current chain, however, would recurse forever.  Keeping the chain on the
    context makes merges finite without preventing the same file from being
    reused in sibling branches.
    """

    def __init__(self, root=None):
        self.stack = []
        if root:
            self.stack.append(os.path.abspath(root))

    def push(self, filename):
        self.stack.append(os.path.abspath(filename))

    def pop(self):
        self.stack.pop()

    def contains(self, filename):
        return os.path.abspath(filename) in self.stack

    def resolve(self, value, filename):
        if os.path.isabs(value):
            return os.path.abspath(value)
        return os.path.abspath(os.path.join(os.path.dirname(filename), value))


class XMLMenuBuilder(object):

    def __init__(self, debug=False):
        self.debug = debug
        self._merge_context = MergeContext()
        self._missing_merge_files = []
        self._invalid_merge_files = []
        self._merge_cycles = []
        self._merged_targets = set()

    def parse(self, filename=None):
        """Load an applications.menu file.

        filename : str, optional
          The default is ``$XDG_CONFIG_DIRS/menus/${XDG_MENU_PREFIX}applications.menu``.
        """
        # convert to absolute path
        if filename and not os.path.isabs(filename):
            filename = _get_menu_file_path(filename)
        # use default if no filename given
        if not filename:
            candidate = os.environ.get('XDG_MENU_PREFIX', '') + "applications.menu"
            filename = _get_menu_file_path(candidate)
        if not filename:
            raise ParsingError('File not found', "/etc/xdg/menus/%s" % candidate)
        filename = os.path.abspath(filename)
        # check if it is a .menu file
        if not filename.endswith(".menu"):
            raise ParsingError('Not a .menu file', filename)
        # create xml parser
        try:
            tree = etree.parse(filename)
        except:
            raise ParsingError('Not a valid .menu file', filename)

        # parse menufile
        self._directory_dirs = set()
        self._merge_context = MergeContext(filename)
        self._missing_merge_files = []
        self._invalid_merge_files = []
        self._merge_cycles = []
        self._merged_targets = set()
        self.cache = MenuEntryCache()

        menu = self.parse_menu(tree.getroot(), filename)
        menu.tree = tree
        menu.filename = filename
        menu.Filename = filename
        menu.MergeFiles = [filename]

        self.handle_moves(menu)
        self.post_parse(menu)

        # generate the menu
        self.generate_not_only_allocated(menu)
        self.generate_only_allocated(menu)

        # and finally sort
        menu.sort()

        return menu

    def get_merge_files(self, menu):
        """Return every menu file merged into ``menu`` or its submenus."""
        filenames = []
        if menu.Filename:
            filenames.append(menu.Filename)
        for filename in menu.MergeFiles:
            if filename not in filenames:
                filenames.append(filename)
        for submenu in menu.Submenus:
            for filename in self.get_merge_files(submenu):
                if filename not in filenames:
                    filenames.append(filename)
        return filenames

    def get_missing_merge_files(self):
        """Return records for merge files which could not be found."""
        return list(self._missing_merge_files)

    def get_merge_cycles(self):
        """Return records for merge files skipped to break a cycle."""
        return list(self._merge_cycles)

    def get_invalid_merge_files(self):
        """Return records for merge files which could not be parsed."""
        return list(self._invalid_merge_files)

    def parse_menu(self, node, filename):
        menu = Menu()
        menu.Filename = filename
        self.parse_node(node, filename, menu)
        return menu

    def parse_node(self, node, filename, parent=None, merged_root=False):
        for child in node:
            tag, text = child.tag, child.text
            text = text.strip() if text else None
            if tag == 'Menu':
                menu = self.parse_menu(child, filename)
                parent.addSubmenu(menu)
            elif tag == 'AppDir' and text:
                self.parse_app_dir(text, filename, parent)
            elif tag == 'DefaultAppDirs':
                self.parse_default_app_dir(filename, parent)
            elif tag == 'DirectoryDir' and text:
                self.parse_directory_dir(text, filename, parent)
            elif tag == 'DefaultDirectoryDirs':
                self.parse_default_directory_dir(filename, parent)
            elif tag == 'Name' and text:
                # The root <Name> of a merge file names the file's own menu,
                # not the menu it is merged into.
                if not merged_root:
                    parent.Name = text
            elif tag == 'Directory' and text:
                parent.Directories.append(text)
            elif tag == 'OnlyUnallocated':
                parent.OnlyUnallocated = True
            elif tag == 'NotOnlyUnallocated':
                parent.OnlyUnallocated = False
            elif tag == 'Deleted':
                parent.Deleted = True
            elif tag == 'NotDeleted':
                parent.Deleted = False
            elif tag == 'Include' or tag == 'Exclude':
                parent.Rules.append(self.parse_rule(child, filename))
            elif tag == 'MergeFile':
                if child.attrib.get("type", None) == "parent":
                    self.parse_merge_file("applications.menu", child, filename, parent)
                elif text:
                    self.parse_merge_file(text, child, filename, parent)
            elif tag == 'MergeDir' and text:
                self.parse_merge_dir(text, child, filename, parent)
            elif tag == 'DefaultMergeDirs':
                self.parse_default_merge_dirs(child, filename, parent)
            elif tag == 'Move':
                parent.Moves.append(self.parse_move(child))
            elif tag == 'Layout':
                parent.Layout = self.parse_layout(child)
            elif tag == 'DefaultLayout':
                parent.DefaultLayout = self.parse_layout(child)
            elif tag == 'LegacyDir' and text:
                self.parse_legacy_dir(text, child.attrib.get("prefix", ""), filename, parent)
            elif tag == 'KDELegacyDirs':
                self.parse_kde_legacy_dirs(filename, parent)

    def parse_layout(self, node):
        layout = Layout(
            show_empty=_to_bool(node.attrib.get("show_empty", False)),
            inline=_to_bool(node.attrib.get("inline", False)),
            inline_limit=int(node.attrib.get("inline_limit", 4)),
            inline_header=_to_bool(node.attrib.get("inline_header", True)),
            inline_alias=_to_bool(node.attrib.get("inline_alias", False)),
            explicit=True
        )
        order = []
        for child in node:
            tag, text = child.tag, child.text
            text = text.strip() if text else None
            if tag == "Menuname" and text:
                order.append([
                    "Menuname",
                    text,
                    _to_bool(child.attrib.get("show_empty", False)),
                    _to_bool(child.attrib.get("inline", False)),
                    int(child.attrib.get("inline_limit", 4)),
                    _to_bool(child.attrib.get("inline_header", True)),
                    _to_bool(child.attrib.get("inline_alias", False))
                ])
            elif tag == "Separator":
                order.append(['Separator'])
            elif tag == "Filename" and text:
                order.append(["Filename", text])
            elif tag == "Merge":
                order.append([
                    "Merge",
                    child.attrib.get("type", "all")
                ])
        layout.order = order
        return layout

    def parse_move(self, node):
        old, new = "", ""
        for child in node:
            tag, text = child.tag, child.text
            text = text.strip() if text else None
            if tag == "Old" and text:
                old = text
            elif tag == "New" and text:
                new = text
        return Move(old, new)

    # ---------- <Rule> parsing

    def parse_rule(self, node, filename=None):
        type = Rule.TYPE_INCLUDE if node.tag == 'Include' else Rule.TYPE_EXCLUDE
        tree = ast.Expression(lineno=1, col_offset=0)
        expr = self.parse_bool_op(node, ast.Or())
        if expr:
            tree.body = expr
        else:
            tree.body = _ast_const('False')
        ast.fix_missing_locations(tree)
        return Rule(type, tree, source=self._rule_source(node), filename=filename)

    def _rule_source(self, node):
        try:
            return etree.tostring(node, encoding="unicode")
        except (AttributeError, TypeError):
            try:
                return etree.tostring(node).decode("utf-8", "replace")
            except (AttributeError, TypeError):
                return ""

    def parse_bool_op(self, node, operator):
        values = []
        for child in node:
            rule = self.parse_rule_node(child)
            if rule:
                values.append(rule)
        num_values = len(values)
        if num_values > 1:
            return ast.BoolOp(operator, values)
        elif num_values == 1:
            return values[0]
        return None

    def parse_rule_node(self, node):
        tag = node.tag
        if tag == 'Or':
            return self.parse_bool_op(node, ast.Or())
        elif tag == 'And':
            return self.parse_bool_op(node, ast.And())
        elif tag == 'Not':
            expr = self.parse_bool_op(node, ast.Or())
            return ast.UnaryOp(ast.Not(), expr) if expr else None
        elif tag == 'All':
            return _ast_const('True')
        elif tag == 'Category':
            category = node.text
            return ast.Compare(
                left=ast.Str(category),
                ops=[ast.In()],
                comparators=[ast.Attribute(
                    value=ast.Name(id='menuentry', ctx=ast.Load()),
                    attr='Categories',
                    ctx=ast.Load()
                )]
            )
        elif tag == 'Filename':
            filename = node.text
            return ast.Compare(
                left=ast.Str(filename),
                ops=[ast.Eq()],
                comparators=[ast.Attribute(
                    value=ast.Name(id='menuentry', ctx=ast.Load()),
                    attr='DesktopFileID',
                    ctx=ast.Load()
                )]
            )

    # ---------- App/Directory Dir Stuff

    def parse_app_dir(self, value, filename, parent):
        value = _check_file_path(value, filename, TYPE_DIR)
        if value:
            parent.AppDirs.append(value)

    def parse_default_app_dir(self, filename, parent):
        for d in reversed(xdg_data_dirs):
            self.parse_app_dir(os.path.join(d, "applications"), filename, parent)

    def parse_directory_dir(self, value, filename, parent):
        value = _check_file_path(value, filename, TYPE_DIR)
        if value:
            parent.DirectoryDirs.append(value)

    def parse_default_directory_dir(self, filename, parent):
        for d in reversed(xdg_data_dirs):
            self.parse_directory_dir(os.path.join(d, "desktop-directories"), filename, parent)

    # ---------- Merge Stuff

    def _record_merge_file(self, filename, parent):
        filename = os.path.abspath(filename)
        if filename not in parent.MergeFiles:
            parent.MergeFiles.append(filename)

    def parse_merge_file(self, value, child, filename, parent):
        if child.attrib.get("type", None) == "parent":
            for d in xdg_config_dirs:
                rel_file = filename.replace(d, "").strip("/")
                if rel_file != filename:
                    for p in xdg_config_dirs:
                        if d == p:
                            continue
                        candidate = os.path.join(p, rel_file)
                        if os.path.isfile(candidate):
                            self.merge_file(candidate, child, parent)
                            break
        else:
            value = self._merge_context.resolve(value, filename)
            if os.path.isfile(value):
                self.merge_file(value, child, parent)
            else:
                self._record_missing_merge_file(value, parent)

    def parse_merge_dir(self, value, child, filename, parent):
        value = _check_file_path(value, filename, TYPE_DIR)
        if value:
            for item in sorted(os.listdir(value)):
                try:
                    if item.endswith(".menu"):
                        self.merge_file(os.path.join(value, item), child, parent)
                except UnicodeDecodeError:
                    continue

    def parse_default_merge_dirs(self, child, filename, parent):
        basename = os.path.splitext(os.path.basename(filename))[0]
        for d in reversed(xdg_config_dirs):
            self.parse_merge_dir(os.path.join(d, "menus", basename + "-merged"), child, filename, parent)

    def _record_missing_merge_file(self, filename, parent):
        record = {
            "Filename": filename,
            "Menu": parent,
        }
        self._missing_merge_files.append(record)
        if filename not in parent.MissingMergeFiles:
            parent.MissingMergeFiles.append(filename)
        placeholder = _NamedEntry(os.path.basename(filename))
        detail = "Merge file not found: %s" % filename
        parent._generation_exclusions.append(
            ExclusionReason(parent, placeholder, MISSING_MERGE_FILE,
                            detail=detail))

    def merge_file(self, filename, child, parent):
        filename = os.path.abspath(filename)
        # A file which is already in the current merge chain would recurse
        # forever.  The same file in another branch is fine, so the chain is
        # tracked per merge branch instead of globally.
        if self._merge_context.contains(filename):
            record = {
                "Filename": filename,
                "Menu": parent,
            }
            self._merge_cycles.append(record)
            if filename not in parent.MergeCycles:
                parent.MergeCycles.append(filename)
            if self.debug:
                raise ParsingError('Infinite MergeFile loop detected', filename)
            return
        # Merging one file into the same menu more than once cannot add
        # anything new, and in a densely connected merge graph it is the
        # difference between linear work and exponential traversal.  The key
        # includes the receiving menu, so the same file may still be merged
        # into separate sibling menus.
        target_key = (filename, id(parent._identity))
        if target_key in self._merged_targets:
            return
        self._merged_targets.add(target_key)
        # load file
        try:
            tree = etree.parse(filename)
        except IOError:
            self._record_missing_merge_file(filename, parent)
            if self.debug:
                raise ParsingError('File not found', filename)
            return
        except Exception as exc:
            record = {
                "Filename": filename,
                "Menu": parent,
                "Error": exc,
            }
            self._invalid_merge_files.append(record)
            if filename not in parent.InvalidMergeFiles:
                parent.InvalidMergeFiles.append(filename)
            if self.debug:
                raise ParsingError('Not a valid .menu file', filename)
            return

        self._record_merge_file(filename, parent)
        self._merge_context.push(filename)
        try:
            root = tree.getroot()
            self.parse_node(root, filename, parent, merged_root=True)
        finally:
            self._merge_context.pop()

    # ---------- Legacy Dir Stuff

    def parse_legacy_dir(self, dir_, prefix, filename, parent):
        m = self.merge_legacy_dir(dir_, prefix, filename, parent)
        if m:
            parent += m

    def merge_legacy_dir(self, dir_, prefix, filename, parent):
        dir_ = _check_file_path(dir_, filename, TYPE_DIR)
        if dir_ and dir_ not in self._directory_dirs:
            self._directory_dirs.add(dir_)
            m = Menu()
            m.AppDirs.append(dir_)
            m.DirectoryDirs.append(dir_)
            m.Name = os.path.basename(dir_)
            m.NotInXml = True

            for item in os.listdir(dir_):
                try:
                    if item == ".directory":
                        m.Directories.append(item)
                    elif os.path.isdir(os.path.join(dir_, item)):
                        m.addSubmenu(self.merge_legacy_dir(
                            os.path.join(dir_, item),
                            prefix,
                            filename,
                            parent
                        ))
                except UnicodeDecodeError:
                    continue

            self.cache.add_menu_entries([dir_], prefix, True)
            menuentries = self.cache.get_menu_entries([dir_], False)

            for menuentry in menuentries:
                categories = menuentry.Categories
                if len(categories) == 0:
                    r = Rule.fromFilename(Rule.TYPE_INCLUDE, menuentry.DesktopFileID)
                    m.Rules.append(r)
                if not dir_ in parent.AppDirs:
                    categories.append("Legacy")
                    menuentry.Categories = categories

            return m

    def parse_kde_legacy_dirs(self, filename, parent):
        try:
            proc = subprocess.Popen(
                ['kde-config', '--path', 'apps'],
                stdout=subprocess.PIPE,
                universal_newlines=True
            )
            output = proc.communicate()[0].splitlines()
        except OSError:
            # If kde-config doesn't exist, ignore this.
            return
        try:
            for dir_ in output[0].split(":"):
                self.parse_legacy_dir(dir_, "kde", filename, parent)
        except IndexError:
            pass

    def post_parse(self, menu):
        # unallocated / deleted
        if menu.Deleted is None:
            menu.Deleted = False
        if menu.OnlyUnallocated is None:
            menu.OnlyUnallocated = False

        # Layout Tags
        if not menu.Layout or not menu.DefaultLayout:
            if menu.DefaultLayout:
                menu.Layout = menu.DefaultLayout
            elif menu.Layout:
                if menu.Depth > 0:
                    menu.DefaultLayout = menu.Parent.DefaultLayout
                else:
                    menu.DefaultLayout = Layout()
            else:
                if menu.Depth > 0:
                    menu.Layout = menu.Parent.DefaultLayout
                    menu.DefaultLayout = menu.Parent.DefaultLayout
                else:
                    menu.Layout = Layout()
                    menu.DefaultLayout = Layout()

        # add parent's app/directory dirs
        if menu.Depth > 0:
            menu.AppDirs = menu.Parent.AppDirs + menu.AppDirs
            menu.DirectoryDirs = menu.Parent.DirectoryDirs + menu.DirectoryDirs

        # remove duplicates
        menu.Directories = _dedupe(menu.Directories)
        menu.DirectoryDirs = _dedupe(menu.DirectoryDirs)
        menu.AppDirs = _dedupe(menu.AppDirs)

        # go recursive through all menus
        for submenu in menu.Submenus:
            self.post_parse(submenu)

        # reverse so handling is easier
        menu.Directories.reverse()
        menu.DirectoryDirs.reverse()
        menu.AppDirs.reverse()

        # get the valid .directory file out of the list
        for directory in menu.Directories:
            for dir in menu.DirectoryDirs:
                if os.path.isfile(os.path.join(dir, directory)):
                    menuentry = MenuEntry(directory, dir)
                    if not menu.Directory:
                        menu.Directory = menuentry
                    elif menuentry.getType() == MenuEntry.TYPE_SYSTEM:
                        if menu.Directory.getType() == MenuEntry.TYPE_USER:
                            menu.Directory.Original = menuentry
            if menu.Directory:
                break

    # Finally generate the menu
    def _apply_rules(self, menu, menuentries, run):
        """Apply the menu's rules and say why each entry was kept or dropped.

        Returns the list of entries which are included in this menu.  A record
        is added to the menu for every entry which was considered but left out,
        including entries which no Include rule matched at all.

        Rules are evaluated in document order and the last matching rule wins,
        as required by the menu specification.
        """
        excluded = []
        has_include = any(rule.Type is Rule.TYPE_INCLUDE
                          for rule in menu.Rules)
        for menuentry in menuentries:
            menuentry.Add = False
            menuentry.MatchedInclude = False
            menuentry.ExcludeRule = None
            menuentry.IncludeRule = None

            matched_rule = None
            for rule in menu.Rules:
                if rule.matches(menuentry):
                    matched_rule = rule
                    if rule.Type is Rule.TYPE_INCLUDE:
                        menuentry.Add = True
                        menuentry.MatchedInclude = True
                        menuentry.IncludeRule = rule
                    else:
                        menuentry.Add = False
                        menuentry.ExcludeRule = rule

            if menuentry.Add:
                continue
            if matched_rule is not None and matched_rule.Type is Rule.TYPE_EXCLUDE:
                excluded.append((menuentry, EXCLUDED, matched_rule))
            elif has_include:
                # The entry was a candidate, but no Include rule selected it.
                excluded.append((menuentry, NOT_INCLUDED, matched_rule))

        menu._generation_exclusions.extend(
            ExclusionReason(menu, entry, reason, rule,
                            rule.Source if rule is not None else "")
            for entry, reason, rule in excluded
        )
        return [entry for entry in menuentries if entry.Add is True]

    def generate_not_only_allocated(self, menu):
        for submenu in menu.Submenus:
            self.generate_not_only_allocated(submenu)

        if menu.OnlyUnallocated is False:
            self.cache.add_menu_entries(menu.AppDirs)
            candidates = self.cache.get_menu_entries(menu.AppDirs)
            for menuentry in self._apply_rules(menu, candidates, 1):
                if menu not in menuentry.Parents:
                    menuentry.Parents.append(menu)
                menuentry.Add = False
                menuentry.Allocated = True
                if menuentry not in menu.MenuEntries:
                    menu.MenuEntries.append(menuentry)

    def generate_only_allocated(self, menu):
        for submenu in menu.Submenus:
            self.generate_only_allocated(submenu)

        if menu.OnlyUnallocated is True:
            self.cache.add_menu_entries(menu.AppDirs)
            candidates = self.cache.get_menu_entries(menu.AppDirs)
            unallocated = []
            allocated = []
            for entry in candidates:
                if entry.Allocated:
                    allocated.append(entry)
                else:
                    unallocated.append(entry)
            menu._generation_exclusions.extend(
                ExclusionReason(menu, entry, ALLOCATED) for entry in allocated)
            for menuentry in self._apply_rules(menu, unallocated, 2):
                if menu not in menuentry.Parents:
                    menuentry.Parents.append(menu)
                if menuentry not in menu.MenuEntries:
                    menu.MenuEntries.append(menuentry)

    def handle_moves(self, menu):
        for submenu in menu.Submenus:
            self.handle_moves(submenu)
        # parse move operations
        for move in menu.Moves:
            move_from_menu = menu.getMenu(move.Old)
            if move_from_menu:
                # FIXME: this is assigned, but never used...
                move_to_menu = menu.getMenu(move.New)

                menus = move.New.split("/")
                oldparent = None
                while len(menus) > 0:
                    if not oldparent:
                        oldparent = menu
                    newmenu = oldparent.getMenu(menus[0])
                    if not newmenu:
                        newmenu = Menu()
                        newmenu.Name = menus[0]
                        if len(menus) > 1:
                            newmenu.NotInXml = True
                        oldparent.addSubmenu(newmenu)
                    oldparent = newmenu
                    menus.pop(0)

                newmenu += move_from_menu
                move_from_menu.Parent.Submenus.remove(move_from_menu)


class MenuEntryCache:
    "Class to cache Desktop Entries"
    def __init__(self):
        self.cacheEntries = {}
        self.cacheEntries['legacy'] = []
        self.cache = {}

    def add_menu_entries(self, dirs, prefix="", legacy=False):
        for dir_ in dirs:
            if not dir_ in self.cacheEntries:
                self.cacheEntries[dir_] = []
                self.__addFiles(dir_, "", prefix, legacy)

    def __addFiles(self, dir_, subdir, prefix, legacy):
        for item in os.listdir(os.path.join(dir_, subdir)):
            if item.endswith(".desktop"):
                try:
                    menuentry = MenuEntry(os.path.join(subdir, item), dir_, prefix)
                except ParsingError:
                    continue

                self.cacheEntries[dir_].append(menuentry)
                if legacy:
                    self.cacheEntries['legacy'].append(menuentry)
            elif os.path.isdir(os.path.join(dir_, subdir, item)) and not legacy:
                self.__addFiles(dir_, os.path.join(subdir, item), prefix, legacy)

    def get_menu_entries(self, dirs, legacy=True):
        entries = []
        ids = set()
        # handle legacy items
        appdirs = dirs[:]
        if legacy:
            appdirs.append("legacy")
        # cache the results again
        key = "".join(appdirs)
        try:
            return self.cache[key]
        except KeyError:
            pass
        for dir_ in appdirs:
            for menuentry in self.cacheEntries[dir_]:
                try:
                    if menuentry.DesktopFileID not in ids:
                        ids.add(menuentry.DesktopFileID)
                        entries.append(menuentry)
                    elif menuentry.getType() == MenuEntry.TYPE_SYSTEM:
                        # FIXME: This is only 99% correct, but still...
                        idx = entries.index(menuentry)
                        entry = entries[idx]
                        if entry.getType() == MenuEntry.TYPE_USER:
                            entry.Original = menuentry
                except UnicodeDecodeError:
                    continue
        self.cache[key] = entries
        return entries


def parse(filename=None, debug=False):
    """Helper function.
    Equivalent to calling xdg.Menu.XMLMenuBuilder().parse(filename)
    """
    return XMLMenuBuilder(debug).parse(filename)
