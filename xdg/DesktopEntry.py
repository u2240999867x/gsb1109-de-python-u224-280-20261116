"""
Complete implementation of the XDG Desktop Entry Specification
http://standards.freedesktop.org/desktop-entry-spec/

Not supported:
- Encoding: Legacy Mixed
- Does not check URL's
- Does not completly validate deprecated/kde items
- Does not completly check categories
"""

from xdg.IniFile import IniFile, is_ascii
import xdg.Locale
from xdg.Exceptions import (ParsingError, NoKeyError, NoGroupError, ExecError)
from xdg.util import which
import os.path
import re
import warnings

# Field codes recognized by the Exec key, see "Exec variables" in the spec.
_EXEC_SIMPLE_CODES = frozenset(('c', 'i', 'k'))
_EXEC_SINGLE_RESOURCE_CODES = frozenset(('f', 'u'))
_EXEC_MULTI_RESOURCE_CODES = frozenset(('F', 'U'))
_EXEC_RESOURCE_CODES = (_EXEC_SINGLE_RESOURCE_CODES
                        | _EXEC_MULTI_RESOURCE_CODES)
_EXEC_FIELD_CODES = _EXEC_SIMPLE_CODES | _EXEC_RESOURCE_CODES
_EXEC_ESCAPABLE = ('"', '\\', '`', '$')


def _tokenize_exec(exec_string):
    """Split an Exec value into tokens made of ('text'|'code', value) parts.

    Quoting rules of the desktop entry spec are applied here; substituted
    resource values never pass through this function, so quotes and backslashes
    inside user supplied file names are never treated as Exec syntax.
    """
    tokens = []
    parts = None
    in_quotes = False
    index = 0
    length = len(exec_string)

    while index < length:
        char = exec_string[index]

        if in_quotes:
            if char == '"':
                in_quotes = False
                index += 1
            elif char == '\\':
                if index + 1 == length:
                    raise ExecError(
                        "Text ended just after a '\\' character")
                next_char = exec_string[index + 1]
                if next_char in _EXEC_ESCAPABLE:
                    parts.append(('text', next_char))
                else:
                    # An unrecognized escape keeps the backslash verbatim.
                    parts.append(('text', char))
                    parts.append(('text', next_char))
                index += 2
            elif char == '%':
                # The spec states that field codes inside a quoted argument
                # are not expanded, so '%' is just an ordinary character here.
                parts.append(('text', char))
                index += 1
            else:
                parts.append(('text', char))
                index += 1
            continue

        if char in ' \t\n':
            if parts is not None:
                tokens.append(parts)
                parts = None
            index += 1
        elif char == '"':
            if parts is None:
                parts = []
            in_quotes = True
            index += 1
        elif char == '\\':
            if index + 1 == length:
                raise ExecError(
                    "Text ended just after a '\\' character")
            next_char = exec_string[index + 1]
            if next_char in _EXEC_ESCAPABLE:
                if parts is None:
                    parts = []
                parts.append(('text', next_char))
                index += 2
            else:
                raise ExecError(
                    "Escape character '\\' can only be used to insert "
                    "\" \\ ` $ characters")
        elif char == '%':
            if index + 1 == length:
                raise ExecError(
                    "Text ended just after a '%' character")
            code = exec_string[index + 1]
            if code == '%':
                if parts is None:
                    parts = []
                parts.append(('text', '%'))
            elif code in _EXEC_FIELD_CODES:
                if parts is None:
                    parts = []
                parts.append(('code', code))
            else:
                raise ExecError(
                    "Unrecognised or reserved field code: %%%s" % code)
            index += 2
        else:
            if parts is None:
                parts = []
            parts.append(('text', char))
            index += 1

    if in_quotes:
        raise ExecError("Text ended before the closing quote was found")
    if parts is not None:
        tokens.append(parts)
    return tokens


def _exec_resource_code(tokens):
    """Return the resource field code used, validating the code combination."""
    resource_codes = []
    for parts in tokens:
        for kind, value in parts:
            if kind == 'code' and value in _EXEC_RESOURCE_CODES:
                resource_codes.append(value)
    if len(set(resource_codes)) > 1:
        joined = ', '.join('%%%s' % code for code in resource_codes)
        raise ExecError(
            "Exec value combines mutually exclusive resource field "
            "codes: %s" % joined)
    return resource_codes[0] if resource_codes else None


def _expand_exec_token(parts, icon, display_name, location, resource):
    """Expand one pre-parsed Exec token into zero or more argv elements."""
    arguments = []
    buffer = []

    def flush():
        if buffer:
            arguments.append(''.join(buffer))
            del buffer[:]

    for kind, value in parts:
        if kind == 'text':
            buffer.append(value)
        elif value == 'i':
            if icon:
                flush()
                arguments.append('--icon')
                arguments.append(icon)
        elif value == 'c':
            if display_name:
                buffer.append(display_name)
        elif value == 'k':
            if location:
                buffer.append(location)
        elif value in _EXEC_SINGLE_RESOURCE_CODES:
            if resource is not None:
                buffer.append(resource)
        else:
            # Multi-resource codes (%F, %U): each resource is its own argv
            # element. Any text accumulated next to the code is flushed first
            # as a separate element rather than joined to the resources.
            if resource is not None:
                flush()
                arguments.extend(resource)
    flush()
    return arguments


def _expand_exec(exec_string, icon='', display_name='', location='',
                 resources=()):
    """Expand an Exec string into one argv list per planned launch."""
    tokens = _tokenize_exec(exec_string)
    resource_code = _exec_resource_code(tokens)

    if resource_code is None:
        # No resource code: the optional resources never reach the command
        # line and there is exactly one launch.
        planned_resources = [None]
    elif resource_code in _EXEC_SINGLE_RESOURCE_CODES:
        # Single-resource codes cannot share a command line, so one launch is
        # planned per resource instead of stuffing them into one command.
        planned_resources = list(resources) if resources else [None]
    else:
        # Multi-resource codes collect every resource into a single launch.
        planned_resources = [list(resources) if resources else None]

    launches = []
    for resource in planned_resources:
        argv = []
        for parts in tokens:
            argv.extend(_expand_exec_token(
                parts, icon, display_name, location, resource))
        if not argv:
            raise ExecError(
                "Exec value %r does not produce any arguments" % exec_string)
        launches.append(argv)
    return launches


class DesktopEntry(IniFile):
    "Class to parse and validate Desktop Entries"

    defaultGroup = 'Desktop Entry'

    def __init__(self, filename=None):
        """Create a new DesktopEntry.
        
        If filename exists, it will be parsed as a desktop entry file. If not,
        or if filename is None, a blank DesktopEntry is created.
        """
        self.content = dict()
        if filename and os.path.exists(filename):
            self.parse(filename)
        elif filename:
            self.new(filename)

    def __str__(self):
        return self.getName()

    def parse(self, file):
        """Parse a desktop entry file.
        
        This can raise :class:`~xdg.Exceptions.ParsingError`,
        :class:`~xdg.Exceptions.DuplicateGroupError` or
        :class:`~xdg.Exceptions.DuplicateKeyError`.
        """
        IniFile.parse(self, file, ["Desktop Entry", "KDE Desktop Entry"])
    
    def findTryExec(self):
        """Looks in the PATH for the executable given in the TryExec field.

        Returns the full path to the executable if it is found, None if not.
        Raises :class:`~xdg.Exceptions.NoKeyError` if TryExec is not present.
        """
        tryexec = self.get('TryExec', strict=True)
        return which(tryexec)

    def parseExec(self, resources=(), action=None):
        """Expand the Exec key into the argv lists of every planned launch.

        No process is started: this only builds argument lists. Each returned
        inner list is one complete command line (program plus arguments); the
        number of lists depends on the resource field code used in Exec:

        * no resource code, or a multi-resource code (``%F``/``%U``): one
          list, with every resource collected together;
        * a single-resource code (``%f``/``%u``): one list per resource.

        :param resources: local file names and/or URLs to open. The strings
            are substituted verbatim, so quotes, ``%`` and backslashes they
            contain are never interpreted as Exec syntax.
        :param action: name of a ``Desktop Action`` to expand instead of the
            main entry. Pass either the bare action id (``"Window"``) or the
            full group name (``"Desktop Action Window"``).
        :raises ~xdg.Exceptions.NoKeyError: if the selected group has no
            Exec key.
        :raises ~xdg.Exceptions.NoGroupError: if the named action does not
            exist.
        :raises ~xdg.Exceptions.ExecError: if the Exec value is malformed or
            uses an illegal field code combination.
        """
        if action is None:
            group = self.defaultGroup
        elif action.startswith("Desktop Action "):
            group = action
        else:
            group = "Desktop Action %s" % action

        if group not in self.content:
            raise NoGroupError(group, self.filename)

        exec_string = self.get('Exec', group=group, strict=True)
        icon = self.get('Icon', group=group, locale=True)
        display_name = self.get('Name', group=group, locale=True)
        return _expand_exec(exec_string,
                            icon=icon,
                            display_name=display_name,
                            location=self.filename,
                            resources=list(resources))

    # start standard keys
    def getType(self):
        return self.get('Type')
    def getVersion(self):
        """deprecated, use getVersionString instead """
        return self.get('Version', type="numeric")
    def getVersionString(self):
        return self.get('Version')
    def getName(self):
        return self.get('Name', locale=True)
    def getGenericName(self):
        return self.get('GenericName', locale=True)
    def getNoDisplay(self):
        return self.get('NoDisplay', type="boolean")
    def getComment(self):
        return self.get('Comment', locale=True)
    def getIcon(self):
        return self.get('Icon', locale=True)
    def getHidden(self):
        return self.get('Hidden', type="boolean")
    def getOnlyShowIn(self):
        return self.get('OnlyShowIn', list=True)
    def getNotShowIn(self):
        return self.get('NotShowIn', list=True)
    def getTryExec(self):
        return self.get('TryExec')
    def getExec(self):
        return self.get('Exec')
    def getPath(self):
        return self.get('Path')
    def getTerminal(self):
        return self.get('Terminal', type="boolean")
    def getMimeType(self):
        """deprecated, use getMimeTypes instead """
        return self.get('MimeType', list=True, type="regex")
    def getMimeTypes(self):
        return self.get('MimeType', list=True)
    def getCategories(self):
        return self.get('Categories', list=True)
    def getStartupNotify(self):
        return self.get('StartupNotify', type="boolean")
    def getStartupWMClass(self):
        return self.get('StartupWMClass')
    def getURL(self):
        return self.get('URL')
    # end standard keys

    # start kde keys
    def getServiceTypes(self):
        return self.get('ServiceTypes', list=True)
    def getDocPath(self):
        return self.get('DocPath')
    def getKeywords(self):
        return self.get('Keywords', list=True, locale=True)
    def getInitialPreference(self):
        return self.get('InitialPreference')
    def getDev(self):
        return self.get('Dev')
    def getFSType(self):
        return self.get('FSType')
    def getMountPoint(self):
        return self.get('MountPoint')
    def getReadonly(self):
        return self.get('ReadOnly', type="boolean")
    def getUnmountIcon(self):
        return self.get('UnmountIcon', locale=True)
    # end kde keys

    # start deprecated keys
    def getMiniIcon(self):
        return self.get('MiniIcon', locale=True)
    def getTerminalOptions(self):
        return self.get('TerminalOptions')
    def getDefaultApp(self):
        return self.get('DefaultApp')
    def getProtocols(self):
        return self.get('Protocols', list=True)
    def getExtensions(self):
        return self.get('Extensions', list=True)
    def getBinaryPattern(self):
        return self.get('BinaryPattern')
    def getMapNotify(self):
        return self.get('MapNotify')
    def getEncoding(self):
        return self.get('Encoding')
    def getSwallowTitle(self):
        return self.get('SwallowTitle', locale=True)
    def getSwallowExec(self):
        return self.get('SwallowExec')
    def getSortOrder(self): 
        return self.get('SortOrder', list=True)
    def getFilePattern(self):
        return self.get('FilePattern', type="regex")
    def getActions(self):
        return self.get('Actions', list=True)
    # end deprecated keys

    # desktop entry edit stuff
    def new(self, filename):
        """Make this instance into a new, blank desktop entry.
        
        If filename has a .desktop extension, Type is set to Application. If it
        has a .directory extension, Type is Directory. Other extensions will
        cause :class:`~xdg.Exceptions.ParsingError` to be raised.
        """
        if os.path.splitext(filename)[1] == ".desktop":
            type = "Application"
        elif os.path.splitext(filename)[1] == ".directory":
            type = "Directory"
        else:
            raise ParsingError("Unknown extension", filename)

        self.content = dict()
        self.addGroup(self.defaultGroup)
        self.set("Type", type)
        self.filename = filename
    # end desktop entry edit stuff

    # validation stuff
    def checkExtras(self):
        # header
        if self.defaultGroup == "KDE Desktop Entry":
            self.warnings.append('[KDE Desktop Entry]-Header is deprecated')

        # file extension
        if self.fileExtension == ".kdelnk":
            self.warnings.append("File extension .kdelnk is deprecated")
        elif self.fileExtension != ".desktop" and self.fileExtension != ".directory":
            self.warnings.append('Unknown File extension')

        # Type
        try:
            self.type = self.content[self.defaultGroup]["Type"]
        except KeyError:
            self.errors.append("Key 'Type' is missing")

        # Name
        try:
            self.name = self.content[self.defaultGroup]["Name"]
        except KeyError:
            self.errors.append("Key 'Name' is missing")

    def checkGroup(self, group):
        # check if group header is valid
        if not (group == self.defaultGroup \
        or re.match("^Desktop Action [a-zA-Z0-9-]+$", group) \
        or (re.match("^X-", group) and is_ascii(group))):
            self.errors.append("Invalid Group name: %s" % group)
        else:
            #OnlyShowIn and NotShowIn
            if ("OnlyShowIn" in self.content[group]) and ("NotShowIn" in self.content[group]):
                self.errors.append("Group may either have OnlyShowIn or NotShowIn, but not both")

    def checkKey(self, key, value, group):
        # standard keys     
        if key == "Type":
            if value == "ServiceType" or value == "Service" or value == "FSDevice":
                self.warnings.append("Type=%s is a KDE extension" % key)
            elif value == "MimeType":
                self.warnings.append("Type=MimeType is deprecated")
            elif not (value == "Application" or value == "Link" or value == "Directory"):
                self.errors.append("Value of key 'Type' must be Application, Link or Directory, but is '%s'" % value)

            if self.fileExtension == ".directory" and not value == "Directory":
                self.warnings.append("File extension is .directory, but Type is '%s'" % value)
            elif self.fileExtension == ".desktop" and value == "Directory":
                self.warnings.append("Files with Type=Directory should have the extension .directory")

            if value == "Application":
                if "Exec" not in self.content[group]:
                    self.warnings.append("Type=Application needs 'Exec' key")
            if value == "Link":
                if "URL" not in self.content[group]:
                    self.warnings.append("Type=Link needs 'URL' key")

        elif key == "Version":
            self.checkValue(key, value)

        elif re.match("^Name"+xdg.Locale.regex+"$", key):
            pass # locale string

        elif re.match("^GenericName"+xdg.Locale.regex+"$", key):
            pass # locale string

        elif key == "NoDisplay":
            self.checkValue(key, value, type="boolean")

        elif re.match("^Comment"+xdg.Locale.regex+"$", key):
            pass # locale string

        elif re.match("^Icon"+xdg.Locale.regex+"$", key):
            self.checkValue(key, value)

        elif key == "Hidden":
            self.checkValue(key, value, type="boolean")

        elif key == "OnlyShowIn":
            self.checkValue(key, value, list=True)
            self.checkOnlyShowIn(value)

        elif key == "NotShowIn":
            self.checkValue(key, value, list=True)
            self.checkOnlyShowIn(value)

        elif key == "TryExec":
            self.checkValue(key, value)
            self.checkType(key, "Application")

        elif key == "Exec":
            self.checkValue(key, value)
            self.checkType(key, "Application")

        elif key == "Path":
            self.checkValue(key, value)
            self.checkType(key, "Application")

        elif key == "Terminal":
            self.checkValue(key, value, type="boolean")
            self.checkType(key, "Application")
        
        elif key == "Actions":
            self.checkValue(key, value, list=True)
            self.checkType(key, "Application")

        elif key == "MimeType":
            self.checkValue(key, value, list=True)
            self.checkType(key, "Application")

        elif key == "Categories":
            self.checkValue(key, value)
            self.checkType(key, "Application")
            self.checkCategories(value)
        
        elif re.match("^Keywords"+xdg.Locale.regex+"$", key):
            self.checkValue(key, value, type="localestring", list=True)
            self.checkType(key, "Application")

        elif key == "StartupNotify":
            self.checkValue(key, value, type="boolean")
            self.checkType(key, "Application")

        elif key == "StartupWMClass":
            self.checkType(key, "Application")

        elif key == "URL":
            self.checkValue(key, value)
            self.checkType(key, "URL")

        # kde extensions
        elif key == "ServiceTypes":
            self.checkValue(key, value, list=True)
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif key == "DocPath":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif key == "InitialPreference":
            self.checkValue(key, value, type="numeric")
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif key == "Dev":
            self.checkValue(key, value)
            self.checkType(key, "FSDevice")
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif key == "FSType":
            self.checkValue(key, value)
            self.checkType(key, "FSDevice")
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif key == "MountPoint":
            self.checkValue(key, value)
            self.checkType(key, "FSDevice")
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif key == "ReadOnly":
            self.checkValue(key, value, type="boolean")
            self.checkType(key, "FSDevice")
            self.warnings.append("Key '%s' is a KDE extension" % key)

        elif re.match("^UnmountIcon"+xdg.Locale.regex+"$", key):
            self.checkValue(key, value)
            self.checkType(key, "FSDevice")
            self.warnings.append("Key '%s' is a KDE extension" % key)

        # deprecated keys
        elif key == "Encoding":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif re.match("^MiniIcon"+xdg.Locale.regex+"$", key):
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "TerminalOptions":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "DefaultApp":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "Protocols":
            self.checkValue(key, value, list=True)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "Extensions":
            self.checkValue(key, value, list=True)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "BinaryPattern":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "MapNotify":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif re.match("^SwallowTitle"+xdg.Locale.regex+"$", key):
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "SwallowExec":
            self.checkValue(key, value)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "FilePattern":
            self.checkValue(key, value, type="regex", list=True)
            self.warnings.append("Key '%s' is deprecated" % key)

        elif key == "SortOrder":
            self.checkValue(key, value, list=True)
            self.warnings.append("Key '%s' is deprecated" % key)

        # "X-" extensions
        elif re.match("^X-[a-zA-Z0-9-]+", key):
            pass

        else:
            self.errors.append("Invalid key: %s" % key)

    def checkType(self, key, type):
        if not self.getType() == type:
            self.errors.append("Key '%s' only allowed in Type=%s" % (key, type))

    def checkOnlyShowIn(self, value):
        values = self.getList(value)
        valid = ["GNOME", "KDE", "LXDE", "MATE", "Razor", "ROX", "TDE", "Unity",
                 "XFCE", "Old"]
        for item in values:
            if item not in valid and item[0:2] != "X-":
                self.errors.append("'%s' is not a registered OnlyShowIn value" % item);

    def checkCategories(self, value):
        values = self.getList(value)

        main = ["AudioVideo", "Audio", "Video", "Development", "Education", "Game", "Graphics", "Network", "Office", "Science", "Settings", "System", "Utility"]
        if not any(item in main for item in values):
            self.errors.append("Missing main category")

        additional = ['Building', 'Debugger', 'IDE', 'GUIDesigner', 'Profiling', 'RevisionControl', 'Translation', 'Calendar', 'ContactManagement', 'Database', 'Dictionary', 'Chart', 'Email', 'Finance', 'FlowChart', 'PDA', 'ProjectManagement', 'Presentation', 'Spreadsheet', 'WordProcessor', '2DGraphics', 'VectorGraphics', 'RasterGraphics', '3DGraphics', 'Scanning', 'OCR', 'Photography', 'Publishing', 'Viewer', 'TextTools', 'DesktopSettings', 'HardwareSettings', 'Printing', 'PackageManager', 'Dialup', 'InstantMessaging', 'Chat', 'IRCClient', 'Feed', 'FileTransfer', 'HamRadio', 'News', 'P2P', 'RemoteAccess', 'Telephony', 'TelephonyTools', 'VideoConference', 'WebBrowser', 'WebDevelopment', 'Midi', 'Mixer', 'Sequencer', 'Tuner', 'TV', 'AudioVideoEditing', 'Player', 'Recorder', 'DiscBurning', 'ActionGame', 'AdventureGame', 'ArcadeGame', 'BoardGame', 'BlocksGame', 'CardGame', 'KidsGame', 'LogicGame', 'RolePlaying', 'Shooter', 'Simulation', 'SportsGame', 'StrategyGame', 'Art', 'Construction', 'Music', 'Languages', 'ArtificialIntelligence', 'Astronomy', 'Biology', 'Chemistry', 'ComputerScience', 'DataVisualization', 'Economy', 'Electricity', 'Geography', 'Geology', 'Geoscience', 'History', 'Humanities', 'ImageProcessing', 'Literature', 'Maps', 'Math', 'NumericalAnalysis', 'MedicalSoftware', 'Physics', 'Robotics', 'Spirituality', 'Sports', 'ParallelComputing', 'Amusement', 'Archiving', 'Compression', 'Electronics', 'Emulator', 'Engineering', 'FileTools', 'FileManager', 'TerminalEmulator', 'Filesystem', 'Monitor', 'Security', 'Accessibility', 'Calculator', 'Clock', 'TextEditor', 'Documentation', 'Adult', 'Core', 'KDE', 'GNOME', 'XFCE', 'GTK', 'Qt', 'Motif', 'Java', 'ConsoleOnly']
        allcategories = additional + main
        
        for item in values:
            if item not in allcategories and not item.startswith("X-"):
                self.errors.append("'%s' is not a registered Category" % item);
    
    def checkCategorie(self, value):
        """Deprecated alias for checkCategories - only exists for backwards
        compatibility.
        """
        warnings.warn("checkCategorie is deprecated, use checkCategories",
                                                            DeprecationWarning)
        return self.checkCategories(value)

