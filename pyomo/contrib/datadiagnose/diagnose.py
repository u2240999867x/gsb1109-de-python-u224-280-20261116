# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

"""Offline diagnosis of data-file conflicts against an AbstractModel.

The :func:`find_conflicting_lines` entry point locates *minimal* groups of
data-file declarations whose combination violates the model's validation
rules when the data are loaded into a Pyomo instance.  No optimization
model is ever solved: every probe only constructs a fresh concrete
instance from a data subset and records whether construction raises.

Each probe calls :meth:`Model.create_instance` on the original (abstract)
model, which clones the model first.  Probes therefore never share
derived state: defaults, set memberships and constructed components from
one probe cannot leak into the next one.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from pyomo.dataportal.parse_datacmds import parse_data_commands

logger = logging.getLogger("pyomo.contrib.datadiagnose")

_RE_OBJECT_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_RE_NAMESPACE = re.compile(
    r"namespace\s+([A-Za-z_][\w.+\-]*)\s*\{"
)


class DataDiagnoseError(Exception):
    """Raised when the diagnostic input itself cannot be processed."""


@dataclass
class DataLine:
    """A single data-file declaration and its original location."""

    source_file: Optional[str]
    line_start: int
    line_end: int
    text: str
    namespace: Optional[str] = None

    def location(self) -> str:
        if self.line_start == self.line_end:
            where = f"{self.source_file}:{self.line_start}"
        else:
            where = f"{self.source_file}:{self.line_start}-{self.line_end}"
        if self.namespace is not None:
            where += f" (namespace {self.namespace!r})"
        return where


@dataclass
class ConflictGroup:
    """A set of data lines jointly responsible for one validation error.

    ``proven_minimal`` is ``True`` only when a deletion filter has been
    completed: removing any single line in the group then fails to
    reproduce the same error signature.  If the check budget runs out
    before the filter finishes, the group is still known to reproduce
    the error, but minimality is not proven.
    """

    lines: List[DataLine]
    error_type: str
    error_message: str
    proven_minimal: bool
    signature: tuple

    def render_lines(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class DiagnosisResult:
    """Outcome of a diagnostic run."""

    source_file: Optional[str]
    groups: List[ConflictGroup] = field(default_factory=list)
    checks_used: int = 0
    budget: Optional[int] = None
    exhausted: bool = False

    @property
    def proven_groups(self) -> List[ConflictGroup]:
        return [group for group in self.groups if group.proven_minimal]

    @property
    def candidate_groups(self) -> List[ConflictGroup]:
        return [group for group in self.groups if not group.proven_minimal]

    def write_report(self, stream=None) -> str:
        """Return a human-readable report, optionally writing it to a stream."""
        report = _format_report(self)
        if stream is not None:
            stream.write(report)
        return report


def _format_report(result: DiagnosisResult) -> str:
    source = result.source_file or "<inline data>"
    lines = [f"Data conflict diagnosis for: {source}"]
    budget = "unlimited" if result.budget is None else str(result.budget)
    lines.append(f"fresh-instance checks used: {result.checks_used} (budget: {budget})")
    if result.exhausted:
        lines.append(
            "status: BUDGET EXHAUSTED - candidate groups below are not all "
            "proven minimal"
        )
    else:
        lines.append("status: completed")
    if not result.groups:
        lines.append("no conflicting data lines found")
        return "\n".join(lines) + "\n"

    for index, group in enumerate(result.groups, start=1):
        if group.proven_minimal:
            kind = "PROVEN MINIMAL"
        else:
            kind = "CANDIDATE (not proven minimal)"
        lines.append("")
        lines.append(
            f"Conflict group {index} ({kind}, {len(group.lines)} line(s), "
            f"{group.error_type}):"
        )
        for data_line in group.lines:
            lines.append(f"  {data_line.location()}:")
            for text_line in data_line.text.splitlines():
                lines.append(f"    | {text_line}")
        lines.append(f"  error: {group.error_message}")
        if group.proven_minimal:
            lines.append(
                "  minimality: removing any single line above no longer "
                "reproduces this error"
            )
    return "\n".join(lines) + "\n"


def _normalize_exception(exc: Exception) -> tuple:
    """Build a stable identity for a validation error.

    The exception type and message are used verbatim, except that object
    memory addresses (``0x...``) are stripped so that the signature does
    not depend on which freshly cloned instance raised the error.
    """
    message = _RE_OBJECT_ADDRESS.sub("", str(exc)).strip()
    return (type(exc).__name__, message)


def scan_data_units(text: str, source_file: Optional[str] = None) -> List[DataLine]:
    """Split DAT text into declaration units with their original line ranges.

    A unit is a single ``set``/``param``/``table``/... declaration
    terminated by a semicolon, which may span several physical lines.
    Comments, blank lines and ``namespace`` wrappers are accounted for but
    do not themselves become units.
    """
    # First confirm the text parses; syntax errors are not multi-line
    # conflicts and should surface to the caller unchanged.
    parse_data_commands(data=text)

    raw_lines = text.splitlines()
    units: List[DataLine] = []
    namespace_stack: List[str] = []
    bracket_depth = 0
    line_no = 1
    stmt_start_line: Optional[int] = None
    state = "normal"  # normal | line_comment | block_comment | sq | dq
    index = 0
    total = len(text)

    def finish_statement(end_line: int) -> None:
        nonlocal stmt_start_line
        if stmt_start_line is None:
            return
        snippet = "\n".join(raw_lines[stmt_start_line - 1 : end_line])
        first_word = snippet.strip().rstrip(";").split(None, 1)[0].lower()
        if first_word in ("include", "load", "store"):
            raise DataDiagnoseError(
                f"{source_file}:{stmt_start_line}: '{first_word}' declarations "
                "reference external files and cannot be replayed through "
                "temporary subsets; inline those data instead"
            )
        if first_word in ("data", "end"):
            # file control markers carry no replayable data
            stmt_start_line = None
            return
        namespace = namespace_stack[-1] if namespace_stack else None
        units.append(
            DataLine(
                source_file=source_file,
                line_start=stmt_start_line,
                line_end=end_line,
                text=snippet,
                namespace=namespace,
            )
        )
        stmt_start_line = None

    while index < total:
        char = text[index]

        if state == "line_comment":
            if char == "\n":
                state = "normal"
                line_no += 1
            index += 1
            continue
        if state == "block_comment":
            if char == "\n":
                line_no += 1
                index += 1
            elif text.startswith("*/", index):
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if state in ("sq", "dq"):
            quote = "'" if state == "sq" else '"'
            if char == "\n":
                line_no += 1
                index += 1
            elif char == quote:
                state = "normal"
                index += 1
            else:
                index += 1
            continue

        # normal state
        if char == "\n":
            line_no += 1
            index += 1
            continue
        if char in " \t\r":
            index += 1
            continue
        if text.startswith("#", index):
            state = "line_comment"
            index += 1
            continue
        if text.startswith("/*", index):
            state = "block_comment"
            index += 2
            continue
        if char == "'":
            state = "sq"
            index += 1
            if stmt_start_line is None:
                stmt_start_line = line_no
            continue
        if char == '"':
            state = "dq"
            index += 1
            if stmt_start_line is None:
                stmt_start_line = line_no
            continue
        if namespace_stack == [] and bracket_depth == 0:
            namespace_match = _RE_NAMESPACE.match(text, index)
            if namespace_match is not None:
                matched = namespace_match.group(0)
                line_no += matched.count("\n")
                namespace_stack.append(namespace_match.group(1))
                index = namespace_match.end()
                continue
        if char in "([{":
            bracket_depth += 1
            if stmt_start_line is None:
                stmt_start_line = line_no
            index += 1
            continue
        if char in ")]}":
            if (
                char == "}"
                and bracket_depth == 0
                and namespace_stack
            ):
                # closing brace of a namespace block
                namespace_stack.pop()
                index += 1
                continue
            bracket_depth -= 1
            index += 1
            continue
        if char == ";":
            if bracket_depth != 0:
                raise DataDiagnoseError(
                    f"Unexpected ';' inside nested brackets in {source_file} "
                    f"near line {line_no}"
                )
            finish_statement(line_no)
            index += 1
            continue
        if stmt_start_line is None:
            stmt_start_line = line_no
        index += 1

    if state == "block_comment":
        raise DataDiagnoseError(
            f"Unterminated block comment in {source_file}"
        )
    if bracket_depth != 0 or namespace_stack:
        raise DataDiagnoseError(
            f"Unbalanced brackets/namespace blocks in {source_file}"
        )
    if stmt_start_line is not None:
        raise DataDiagnoseError(
            f"Declaration without terminating ';' in {source_file} "
            f"starting on line {stmt_start_line}"
        )
    return units


def _render_units(units: List[DataLine]) -> str:
    """Reconstruct DAT text from a subset of declaration units."""
    blocks: dict = {}
    order: List[Optional[str]] = []
    for unit in units:
        if unit.namespace not in blocks:
            blocks[unit.namespace] = []
            order.append(unit.namespace)
        blocks[unit.namespace].append(unit.text)

    parts = []
    for namespace in order:
        body = "\n".join(blocks[namespace])
        if namespace is None:
            parts.append(body)
        else:
            parts.append(f"namespace {namespace} {{\n{body}\n}}")
    return "\n".join(parts) + "\n"


class _SuppressConstructionLogs:
    """Silence Pyomo's construction error log while probing a subset.

    Failing probes are expected control flow here rather than user-facing
    errors, so the ``pyomo`` loggers are temporarily raised to CRITICAL.
    """

    _LOGGER_NAMES = ("pyomo", "pyomo.core")

    def __enter__(self):
        self._levels = {}
        for name in self._LOGGER_NAMES:
            pyomo_logger = logging.getLogger(name)
            self._levels[name] = pyomo_logger.level
            pyomo_logger.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for name, level in self._levels.items():
            logging.getLogger(name).setLevel(level)
        return False


class _BudgetExhausted(Exception):
    """Internal control-flow signal for an exhausted check budget."""


class _ConflictSearch:
    """Grow-then-delete search over declaration units.

    The validation conflict is treated as a monotone property over sets
    of data lines: if a subset reproduces an error, every superset that
    reaches the same construction point reproduces it as well.  Errors
    are identified by their normalized signature so that distinct
    conflicts are never conflated.
    """

    def __init__(self, model, units, tempdir, budget, namespaces):
        self._model = model
        self._units = units
        self._tempdir = tempdir
        self._namespaces = namespaces
        self.budget = budget
        self.checks_used = 0
        self._counter = 0

    def _check(self, units: List[DataLine]) -> Optional[tuple]:
        """Construct a fresh instance from ``units``; return the error signature."""
        if self.budget is not None and self.checks_used >= self.budget:
            raise _BudgetExhausted()
        self.checks_used += 1
        self._counter += 1

        if not units:
            # With no data lines at all there is no data conflict to
            # reproduce; do not spend an instantiation on it.
            return None

        filename = f"{self._tempdir}/subset_{self._counter}.dat"
        with open(filename, "w") as data_file:
            data_file.write(_render_units(units))

        # create_instance() clones the abstract model before loading the
        # data, so every call builds an independent instance whose
        # defaults, set memberships and constructed components are
        # discarded before the next probe.
        try:
            with _SuppressConstructionLogs():
                self._model.create_instance(
                    filename=filename, namespaces=self._namespaces
                )
        except Exception as exc:  # noqa: BLE001 - any validation failure is a signal
            signature = _normalize_exception(exc)
            logger.debug(
                "check %d with %d unit(s) raised %s",
                self.checks_used,
                len(units),
                signature[0],
            )
            return signature
        logger.debug(
            "check %d with %d unit(s) constructed cleanly",
            self.checks_used,
            len(units),
        )
        return None

    def run(self) -> tuple:
        groups: List[ConflictGroup] = []
        exhausted = False
        remaining = list(self._units)

        while remaining:
            try:
                signature = self._check(remaining)
            except _BudgetExhausted:
                exhausted = True
                break
            if signature is None:
                break

            try:
                seed = self._grow(remaining, signature)
                group_units, proven = self._minimize(seed, signature)
            except _BudgetExhausted:
                # The full remaining set is already known to reproduce
                # this signature, so it is a valid but unproven candidate.
                groups.append(
                    self._make_group(remaining, signature, proven_minimal=False)
                )
                exhausted = True
                break

            groups.append(
                self._make_group(group_units, signature, proven_minimal=proven)
            )
            if not proven:
                exhausted = True
                break
            removed = {id(unit) for unit in group_units}
            remaining = [unit for unit in remaining if id(unit) not in removed]

        return groups, exhausted

    def _grow(
        self, remaining: List[DataLine], signature: tuple
    ) -> List[DataLine]:
        """Find the first prefix (in file order) that reproduces the error."""
        seed: List[DataLine] = []
        for unit in remaining:
            trial = seed + [unit]
            observed = self._check(trial)
            seed = trial
            if observed is not None and observed == signature:
                return seed
        # Unreachable while the conflict property holds, but keep the
        # candidate collected so far rather than asserting.
        return seed

    def _minimize(
        self, seed: List[DataLine], signature: tuple
    ) -> tuple:
        """Deletion filter: drop every line not needed to reproduce the error."""
        group = list(seed)
        for unit in list(group):
            trial = [candidate for candidate in group if candidate is not unit]
            observed = self._check(trial)
            if observed == signature:
                # The conflict survives without this line, so it is not
                # part of this minimal group.
                group = trial
        return group, True

    @staticmethod
    def _make_group(units, signature, proven_minimal) -> ConflictGroup:
        ordered = sorted(
            units,
            key=lambda unit: (unit.source_file or "", unit.line_start),
        )
        return ConflictGroup(
            lines=ordered,
            error_type=signature[0],
            error_message=signature[1],
            proven_minimal=proven_minimal,
            signature=signature,
        )


def find_conflicts_in_text(
    model,
    text: str,
    source_file: Optional[str] = None,
    budget: Optional[int] = None,
    namespaces: Optional[List[str]] = None,
) -> DiagnosisResult:
    """Diagnose DAT text against an abstract model, without solving anything.

    Parameters
    ----------
    model:
        An unconstructed (abstract) Pyomo model.  The object is never
        modified; each probe clones it via ``create_instance``.
    text:
        The DAT file contents to diagnose.
    source_file:
        File name recorded on every returned :class:`DataLine` so that
        reported locations point back at the original file.
    budget:
        Maximum number of fresh-instance construction checks.  When the
        budget is exhausted, the run stops and any group whose deletion
        filter did not finish is flagged as a candidate rather than a
        proven minimal group.  ``None`` means unlimited.
    namespaces:
        Data namespaces to load during each probe.  ``None`` (the
        default) selects every namespace present in the text plus the
        default namespace, matching the intent of loading the whole
        file.  An explicit list restricts diagnosis to those namespaces.

    Returns
    -------
    DiagnosisResult
    """
    if budget is not None and budget <= 0:
        raise ValueError("budget must be a positive integer or None")
    if model.is_constructed():
        raise DataDiagnoseError(
            "diagnosis requires an unconstructed (abstract) model; the "
            "supplied model is already constructed"
        )

    units = scan_data_units(text, source_file=source_file)
    available = {unit.namespace for unit in units if unit.namespace is not None}
    if namespaces is None:
        selected = sorted(available)
    else:
        unknown = set(namespaces) - available
        if unknown:
            raise DataDiagnoseError(
                f"namespace(s) not present in {source_file}: "
                f"{sorted(unknown)}"
            )
        selected = list(namespaces)
    if selected:
        units = [
            unit
            for unit in units
            if unit.namespace is None or unit.namespace in selected
        ]

    from pyomo.common.tempfiles import TempfileManager

    with TempfileManager.new_context() as tempfile_context:
        tempdir = tempfile_context.mkdtemp()
        search = _ConflictSearch(model, units, tempdir, budget, selected)
        groups, exhausted = search.run()

    return DiagnosisResult(
        source_file=source_file,
        groups=groups,
        checks_used=search.checks_used,
        budget=budget,
        exhausted=exhausted,
    )


def find_conflicting_lines(
    model,
    filename: str,
    budget: Optional[int] = None,
    namespaces: Optional[List[str]] = None,
) -> DiagnosisResult:
    """Diagnose a DAT file on disk against an abstract model.

    See :func:`find_conflicts_in_text` for semantics and the meaning of
    ``budget`` and ``namespaces``.  Original file names and line numbers
    are preserved in every returned conflict group.
    """
    with open(filename, "r") as data_file:
        text = data_file.read()
    return find_conflicts_in_text(
        model,
        text,
        source_file=filename,
        budget=budget,
        namespaces=namespaces,
    )
