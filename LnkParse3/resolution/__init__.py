"""Offline target resolution for Windows shell links.

Public entry points::

    from LnkParse3.resolution import resolve, MachineProfile

    result = resolve(lnk, profile)
    print(result.status, result.selected_target)

Resolution is pure: it never opens the target, never enumerates a directory and
never reads the machine it runs on.  The machine profile supplies the
environment, volumes, share mappings and catalog that are used as evidence.
"""

from LnkParse3.resolution.facts import extract_facts
from LnkParse3.resolution.facts import facts_from_dict
from LnkParse3.resolution.facts import facts_from_lnk
from LnkParse3.resolution.facts import LnkFacts
from LnkParse3.resolution.profile import CatalogEntry
from LnkParse3.resolution.profile import MachineProfile
from LnkParse3.resolution.profile import ResolutionPolicy
from LnkParse3.resolution.profile import ShareMapping
from LnkParse3.resolution.profile import Volume
from LnkParse3.resolution.resolver import Candidate
from LnkParse3.resolution.resolver import Conflict
from LnkParse3.resolution.resolver import ResolutionResult
from LnkParse3.resolution.resolver import resolve
from LnkParse3.resolution.resolver import Resolver


__all__ = [
    "Candidate",
    "CatalogEntry",
    "Conflict",
    "LnkFacts",
    "MachineProfile",
    "ResolutionPolicy",
    "ResolutionResult",
    "Resolver",
    "ShareMapping",
    "Volume",
    "extract_facts",
    "facts_from_dict",
    "facts_from_lnk",
    "resolve",
]
