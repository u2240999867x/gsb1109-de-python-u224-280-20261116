"""Execute a :class:`RebuildPlan` on a Pyomo ``ConcreteModel``.

Execution semantics:

- ``REVALUE``: assign new values onto the old instance. Pyomo
  expressions hold symbolic references, so downstream components stay
  valid without reconstruction. For components with a default, keys that
  disappeared from the new load are deleted so the default applies again.
- ``RECONSTRUCT`` / ``VERIFY_FULL``: delete the component and rebuild it
  from the new data. Components are processed in upstream topological
  order so index domains are rebuilt before their dependents.

After execution, every untouched component is the identical object as
before, and the model equals a from-scratch construction on the new data
for the rebuilt part.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from pyomo.environ import Param

from .data import LoadResult
from .planner import Action, RebuildPlan


class ComponentBuilder(Protocol):
    """Builds (and in-place revalues) one named model component."""

    def construct(self, model, name: str, data: LoadResult) -> None: ...

    def revalue(self, model, name: str, data: LoadResult) -> None: ...


def apply_plan(
    model,
    plan: RebuildPlan,
    decls,
    builders: Mapping[str, ComponentBuilder],
    new_data: LoadResult,
):
    """Apply *plan* to *model* and return the model."""
    decl_map = {d.name: d for d in decls}
    for name in _topological_order(plan.actions, decl_map):
        action = plan.actions[name]
        builder = builders[name]
        if action is Action.REVALUE:
            builder.revalue(model, name, new_data)
        else:  # RECONSTRUCT / VERIFY_FULL: full rebuild from the new data
            builder.construct(model, name, new_data)
    return model


def _topological_order(actions, decl_map) -> list:
    """Kahn's algorithm over upstream edges, restricted to affected names."""
    deps = {
        name: {
            u
            for u in decl_map[name].upstream | decl_map[name].index_upstream
            if u in actions
        }
        for name in actions
    }
    order = []
    ready = sorted(n for n, d in deps.items() if not d)
    while ready:
        name = ready.pop()
        order.append(name)
        for other, other_deps in deps.items():
            if name in other_deps:
                other_deps.remove(name)
                if not other_deps:
                    ready.append(other)
    if len(order) != len(actions):
        raise ValueError("dependency cycle among affected components")
    return order


def _scalar(key):
    return key[0] if len(key) == 1 else key


class ParamFromTable:
    """Builder for a mutable Param whose values come from a single table.

    Contract: the component's declared ``value_reads`` must be exactly
    this table, so that re-assigning from the table reproduces the
    from-scratch values (including default fallback for vanished keys).
    """

    def __init__(self, table: str, index_set: str, default=None):
        self.table = table
        self.index_set = index_set
        self.default = default

    def construct(self, model, name: str, data: LoadResult) -> None:
        if hasattr(model, name):
            model.del_component(name)
        kwargs = {
            "mutable": True,
            "initialize": {
                _scalar(k): cell.value for k, cell in data.table(self.table).items()
            },
        }
        if self.default is not None:
            kwargs["default"] = self.default
        setattr(model, name, Param(getattr(model, self.index_set), **kwargs))

    def revalue(self, model, name: str, data: LoadResult) -> None:
        param = getattr(model, name)
        rows = data.table(self.table)
        for key in list(param.index_set()):
            norm = key if isinstance(key, tuple) else (key,)
            if norm in rows:
                param[key] = rows[norm].value
            elif key in param._data:
                # Key vanished from the new load: drop the explicit entry
                # so the declared default applies again.
                del param[key]
