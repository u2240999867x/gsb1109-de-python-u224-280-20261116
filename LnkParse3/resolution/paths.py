"""Small, filesystem-free helpers for Windows path handling.

The resolver must never probe the machine it happens to run on.  Everything in
this module is pure string manipulation, which makes it safe to use with a
catalog that describes another machine.
"""

import ntpath
import re


_ENV_VAR = re.compile(r"%([^%]+)%")


def normalize_path(path):
    """Return a stable Windows-looking path without touching the filesystem."""
    if path is None:
        return ""
    path = str(path).strip().strip("\x00")
    if not path:
        return ""
    path = path.replace("/", "\\")
    # ntpath.normpath keeps the two leading separators of a UNC path and
    # removes ``.``/``..`` components.  It does not resolve a drive against
    # any machine state, so it is safe here.
    normalized = ntpath.normpath(path)
    if normalized == ".":
        return ""
    return normalized


def path_key(path):
    """Case-insensitive key used for Windows path comparisons."""
    return normalize_path(path).casefold()


def is_absolute_path(path):
    normalized = normalize_path(path)
    if not normalized:
        return False
    drive, _ = ntpath.splitdrive(normalized)
    return bool(drive) and (normalized.startswith("\\\\") or normalized[1:3] == ":\\")


def join_path(base, child):
    """Join two Windows path fragments, respecting absolute children."""
    child = normalize_path(child)
    if not child:
        return normalize_path(base)
    if is_absolute_path(child):
        return child
    base = normalize_path(base)
    if not base:
        return child
    _, tail = ntpath.splitdrive(base)
    # ``ntpath.join`` treats a child beginning with a separator as a new
    # absolute path.  Shell item names occasionally arrive with one leading
    # separator; for those the shell item is still relative to the current
    # chain, so strip it before joining.
    if child.startswith("\\") and tail:
        child = child.lstrip("\\")
    return normalize_path(ntpath.join(base, child))


def split_unc(path):
    """Split ``\\\\server\\share\\tail`` into ``\\\\server\\share`` and tail."""
    normalized = normalize_path(path)
    if not normalized.startswith("\\\\"):
        return None, normalized
    parts = normalized[2:].split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None, normalized
    root = "\\\\" + parts[0] + "\\" + parts[1]
    tail = "\\".join(parts[2:])
    return root, tail


def unc_key(path):
    root, tail = split_unc(path)
    if root is None:
        return None
    return root.casefold() + ("\\" + tail.casefold() if tail else "")


def relative_to_root(path, root):
    """Return the part of *path* below *root*, or ``None``."""
    path_n = normalize_path(path)
    root_n = normalize_path(root)
    if not path_n or not root_n:
        return None
    path_key_ = path_n.casefold()
    root_key = root_n.casefold().rstrip("\\")
    if path_key_ == root_key:
        return ""
    if path_key_.startswith(root_key + "\\"):
        return path_n[len(root_n.rstrip("\\")) + 1 :]
    return None


def is_under(path, root):
    return relative_to_root(path, root) is not None


def expand_environment(value, environment):
    """Expand ``%NAME%`` placeholders using *environment* only.

    Returns ``(expanded, used, missing)``.  Unknown variables are deliberately
    left in place and reported so the caller can keep the candidate as an
    unresolved branch instead of inventing a value.
    """
    if value is None:
        return "", [], []
    if not environment:
        environment = {}

    lookup = {str(k).casefold(): str(v) for k, v in environment.items()}
    used = []
    missing = []
    text = str(value)
    expanded = text

    for _ in range(8):
        changed = False

        def replace(match):
            nonlocal changed
            name = match.group(1)
            key = name.casefold()
            if key in lookup:
                changed = True
                if name not in used:
                    used.append(name)
                return lookup[key]
            if name not in missing:
                missing.append(name)
            return match.group(0)

        expanded = _ENV_VAR.sub(replace, expanded)
        if not changed or not _ENV_VAR.search(expanded):
            break

    return normalize_path(expanded), used, missing


def looks_like_guid(value):
    if not isinstance(value, str):
        return False
    return bool(
        re.fullmatch(
            r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?",
            value.strip(),
        )
    )


def normalize_guid(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    return text.upper()


def normalize_drive(value):
    if not value:
        return None
    text = str(value).strip().rstrip("\\").upper()
    if len(text) == 1 and text.isalpha():
        text += ":"
    if len(text) == 2 and text[0].isalpha() and text[1] == ":":
        return text
    return None


def drive_root(drive):
    drive = normalize_drive(drive)
    return drive + "\\" if drive else ""


def split_drive(path):
    drive, tail = ntpath.splitdrive(normalize_path(path))
    if drive.startswith("\\\\"):
        return None, normalize_path(path)
    return (normalize_drive(drive) if drive else None), tail


def path_is_directory_like(path):
    normalized = normalize_path(path)
    return bool(normalized) and normalized.endswith("\\")


def basename(path):
    return ntpath.basename(normalize_path(path))
