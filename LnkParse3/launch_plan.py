"""Read-only preview of the launch action described by a Windows shortcut.

The module builds a *launch plan* (target, working directory, ordered
arguments) for RPA-style preview without executing anything. Command-line
arguments are split only when the caller explicitly declares a Windows
argument interpretation rule; without a declared rule the raw argument
string is preserved as-is instead of being guessed with shell rules.
"""

import json
import re


PLAN_FORMAT = "lnk-launch-plan/1"

# Argument interpretation rules a caller may declare.
RULE_COMMAND_LINE_TO_ARGV_W = "command_line_to_argv_w"
RULE_MSVCRT = "msvcrt"
RULE_RAW = "raw"

KNOWN_RULES = (RULE_COMMAND_LINE_TO_ARGV_W, RULE_MSVCRT, RULE_RAW)
SPLITTING_RULES = (RULE_COMMAND_LINE_TO_ARGV_W, RULE_MSVCRT)

_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?:$|[^\\/])")
_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^(?:\\\\|//)")

_WHITESPACE = " \t"


def split_windows_command_line(command_line, rule=RULE_COMMAND_LINE_TO_ARGV_W):
    """Split a Windows command-line string into ordered arguments.

    Implements the backslash/quotation handling documented for
    CommandLineToArgvW and the Microsoft C runtime:

    - Arguments are delimited by spaces or tabs outside of quotes.
    - A run of ``2n`` backslashes followed by a quotation mark yields ``n``
      literal backslashes, and the quotation mark toggles quoted mode.
    - A run of ``2n+1`` backslashes followed by a quotation mark yields ``n``
      literal backslashes plus a literal quotation mark.
    - Backslashes not followed by a quotation mark are literal.
    - An empty quoted string (``""``) produces an empty argument.

    With the ``msvcrt`` rule, two quotation marks inside quoted mode yield a
    single literal quotation mark (Microsoft C runtime behavior). With the
    ``command_line_to_argv_w`` rule, quotation marks only toggle quoted mode.
    """
    if rule not in SPLITTING_RULES:
        raise ValueError(f"Cannot split arguments with rule {rule!r}")

    arguments = []
    current = []
    in_quotes = False
    started = False
    index = 0
    length = len(command_line)

    while index < length:
        char = command_line[index]

        if char == "\\":
            run_end = index
            while run_end < length and command_line[run_end] == "\\":
                run_end += 1
            run_length = run_end - index
            if run_end < length and command_line[run_end] == '"':
                current.append("\\" * (run_length // 2))
                if run_length % 2:
                    current.append('"')
                else:
                    in_quotes = not in_quotes
                index = run_end + 1
            else:
                current.append("\\" * run_length)
                index = run_end
            started = True
        elif char == '"':
            if (
                rule == RULE_MSVCRT
                and in_quotes
                and index + 1 < length
                and command_line[index + 1] == '"'
            ):
                current.append('"')
                index += 2
            else:
                in_quotes = not in_quotes
                index += 1
            started = True
        elif char in _WHITESPACE and not in_quotes:
            if started:
                arguments.append("".join(current))
                current = []
                started = False
            index += 1
        else:
            current.append(char)
            started = True
            index += 1

    if started:
        arguments.append("".join(current))

    return arguments


def classify_path(path):
    """Classify a Windows path as absolute, UNC, drive-relative or relative."""
    if _DRIVE_RELATIVE_RE.match(path):
        return "drive_relative"
    if _UNC_RE.match(path):
        return "unc"
    if _ABSOLUTE_RE.match(path):
        return "absolute"
    return "relative"


def resolve_drive_relative(path, machine_config=None):
    """Resolve a drive-relative path (e.g. ``C:work``) using machine config.

    A drive-relative path depends on the per-drive current directory of the
    machine where the shortcut would run, so it is resolved only when the
    caller supplies a machine configuration of the form
    ``{'drive_current_directories': {'C': 'C:\\\\Windows\\\\System32'}}``.
    Returns the resolved path, or ``None`` when it cannot be resolved.
    """
    if not machine_config:
        return None

    drives = machine_config.get("drive_current_directories") or {}
    drive = path[0].upper()
    current_dir = None
    for letter, directory in drives.items():
        if letter.upper().rstrip(":") == drive:
            current_dir = directory
            break
    if not current_dir:
        return None

    remainder = path[2:]
    if not remainder:
        return current_dir
    separator = "" if current_dir.endswith(("\\", "/")) else "\\"
    return f"{current_dir}{separator}{remainder}"


class LaunchPlan:
    """A read-only preview of how a shortcut would be launched.

    The target identity is kept separate from any path-shaped strings that
    appear inside the arguments, even when they look identical.
    """

    def __init__(
        self,
        target_identity=None,
        target_source=None,
        working_directory=None,
        resolved_working_directory=None,
        working_directory_kind=None,
        argument_rule=RULE_RAW,
        raw_arguments=None,
        arguments=None,
        extras=None,
    ):
        self.target_identity = target_identity
        self.target_source = target_source
        self.working_directory = working_directory
        self.resolved_working_directory = resolved_working_directory
        self.working_directory_kind = working_directory_kind
        self.argument_rule = argument_rule
        self.raw_arguments = raw_arguments
        self.arguments = arguments
        self.extras = dict(extras) if extras else {}

    def __eq__(self, other):
        if not isinstance(other, LaunchPlan):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self):
        return (
            f"{type(self).__name__}(target_identity={self.target_identity!r}, "
            f"argument_rule={self.argument_rule!r}, arguments={self.arguments!r})"
        )

    @classmethod
    def from_lnk(cls, lnk, argument_rule=None, machine_config=None):
        """Build a launch plan from a parsed :class:`LnkFile`.

        ``argument_rule`` declares how the argument string is interpreted.
        When no rule is declared, the raw argument string is kept untouched.
        ``machine_config`` is used only to resolve a drive-relative working
        directory; without it the raw value is preserved.
        """
        rule = argument_rule or RULE_RAW
        if rule not in KNOWN_RULES:
            raise ValueError(f"Unknown argument interpretation rule: {rule!r}")

        target_identity, target_source = cls._target_from_lnk(lnk)

        working_directory = None
        if lnk.has_working_dir():
            working_directory = lnk.string_data.working_directory()
        working_directory_kind, resolved_working_directory = cls._resolve_working_directory(
            working_directory, machine_config
        )

        raw_arguments = None
        if lnk.has_arguments():
            raw_arguments = lnk.string_data.command_line_arguments()

        arguments = None
        if rule in SPLITTING_RULES and raw_arguments is not None:
            arguments = split_windows_command_line(raw_arguments, rule=rule)

        return cls(
            target_identity=target_identity,
            target_source=target_source,
            working_directory=working_directory,
            resolved_working_directory=resolved_working_directory,
            working_directory_kind=working_directory_kind,
            argument_rule=rule,
            raw_arguments=raw_arguments,
            arguments=arguments,
        )

    @staticmethod
    def _target_from_lnk(lnk):
        info = getattr(lnk, "info", None)
        if info is not None:
            base = None
            if hasattr(info, "local_base_path"):
                base = info.local_base_path_unicode() or info.local_base_path()
            if not base and hasattr(info, "net_name"):
                base = info.net_name_unicode() or info.net_name()
            if base:
                suffix = info.common_path_suffix_unicode() or info.common_path_suffix() or ""
                return base + suffix, "link_info"

        if lnk.has_relative_path():
            relative_path = lnk.string_data.relative_path()
            if relative_path:
                return relative_path, "relative_path"

        return None, None

    @staticmethod
    def _resolve_working_directory(working_directory, machine_config):
        if working_directory is None:
            return None, None

        kind = classify_path(working_directory)
        if kind == "drive_relative":
            resolved = resolve_drive_relative(working_directory, machine_config)
            if resolved is None:
                return "drive_relative_unresolved", None
            return "drive_relative_resolved", resolved
        if kind in ("absolute", "unc"):
            return kind, working_directory
        return kind, None

    def to_dict(self):
        """Serialize the plan; unknown extension fields are kept verbatim."""
        plan = dict(self.extras)
        plan["format"] = PLAN_FORMAT
        plan["target"] = {
            "identity": self.target_identity,
            "source": self.target_source,
        }
        plan["working_directory"] = {
            "raw": self.working_directory,
            "resolved": self.resolved_working_directory,
            "kind": self.working_directory_kind,
        }
        plan["arguments"] = {
            "rule": self.argument_rule,
            "raw": self.raw_arguments,
            "argv": list(self.arguments) if self.arguments is not None else None,
        }
        return plan

    @classmethod
    def from_dict(cls, data):
        """Read back a serialized plan, preserving unknown extension fields."""
        payload = dict(data)

        plan_format = payload.pop("format", PLAN_FORMAT)
        if plan_format != PLAN_FORMAT:
            raise ValueError(f"Unsupported launch plan format: {plan_format!r}")

        target = payload.pop("target", None) or {}
        working_directory = payload.pop("working_directory", None) or {}
        arguments = payload.pop("arguments", None) or {}

        argv = arguments.get("argv")
        return cls(
            target_identity=target.get("identity"),
            target_source=target.get("source"),
            working_directory=working_directory.get("raw"),
            resolved_working_directory=working_directory.get("resolved"),
            working_directory_kind=working_directory.get("kind"),
            argument_rule=arguments.get("rule", RULE_RAW),
            raw_arguments=arguments.get("raw"),
            arguments=list(argv) if argv is not None else None,
            extras=payload,
        )

    def to_json(self, **kwargs):
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_json(cls, data):
        return cls.from_dict(json.loads(data))
