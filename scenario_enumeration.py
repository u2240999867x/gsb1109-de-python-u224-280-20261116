"""Enumerate legal measurement scenarios under joint constraints.

A measurement table provides, for each parameter, several candidate
observations.  Observations may carry a calibration choice (instrument +
period).  Scenarios are enumerated by joint constraints over the *evidence*
(the chosen observations), not just the values:

* Calibration consistency: observations from the same instrument must all
  come from the same calibration period -- calibrations from different
  periods of one instrument are never mixed in a scenario.
* Every parameter value in a scenario can be traced back to the concrete
  observation and calibration choice that produced it.
* Equivalent scenarios (identical parameter values, different evidence)
  coexist; they are never deduplicated by the parameter-value dict.
* When the output budget (``max_scenarios``) is exhausted, the result
  reports the confirmed scenarios plus the unexplored frontier and is
  marked ``complete=False`` -- enumeration completeness is never faked.
* Building model instances from scenarios is isolated: a scenario that
  fails to build is recorded as a failure and does not pollute the model
  instances already built for other scenarios.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = [
    'CalibrationChoice',
    'Observation',
    'Scenario',
    'UnexploredBranch',
    'EnumerationResult',
    'enumerate_scenarios',
    'BuildFailure',
    'BuildResult',
    'build_models',
    'make_pyomo_factory',
]


@dataclass(frozen=True)
class CalibrationChoice:
    """A specific calibration of one instrument for one time period."""

    instrument: str
    period: str
    calibration_id: str


@dataclass(frozen=True)
class Observation:
    """One candidate measurement for a parameter.

    ``calibration`` is ``None`` when the observation does not depend on a
    shared instrument calibration.
    """

    obs_id: str
    parameter: str
    value: Any
    calibration: Optional[CalibrationChoice] = None


# A joint constraint receives the *partial* evidence gathered so far
# (parameter -> Observation) and returns False if the (partial) assignment
# is already infeasible.  Constraints must tolerate partial assignments:
# parameters not yet assigned are simply absent from the mapping.
JointConstraint = Callable[[Mapping[str, Observation]], bool]


@dataclass
class Scenario:
    """One legal combination of observations, with full provenance."""

    index: int
    evidence: dict  # parameter -> Observation
    calibrations: dict  # instrument -> CalibrationChoice

    @property
    def values(self):
        """Parameter values (derived view; evidence stays authoritative)."""
        return {p: obs.value for p, obs in self.evidence.items()}

    @property
    def value_key(self):
        """Key of parameter values only -- equal for equivalent scenarios."""
        return tuple((p, obs.value) for p, obs in self.evidence.items())

    @property
    def evidence_key(self):
        """Key of the evidence combination -- unique per scenario."""
        return tuple((p, obs.obs_id) for p, obs in self.evidence.items())


@dataclass
class UnexploredBranch:
    """A frontier node that was not expanded because the budget ran out."""

    assigned: dict  # parameter -> Observation (evidence fixed so far)
    remaining: tuple  # parameter names not yet branched on


@dataclass
class EnumerationResult:
    scenarios: list
    complete: bool
    unexplored: list = field(default_factory=list)
    pruned: int = 0

    @property
    def truncated(self):
        return not self.complete

    def equivalent_groups(self):
        """Group scenarios by parameter values, keeping all evidence combos."""
        groups = {}
        for sc in self.scenarios:
            groups.setdefault(sc.value_key, []).append(sc)
        return groups


def enumerate_scenarios(
    candidates: Mapping[str, Sequence[Observation]],
    constraints: Iterable[JointConstraint] = (),
    max_scenarios: Optional[int] = None,
) -> EnumerationResult:
    """Enumerate legal scenarios under the joint constraints.

    ``candidates`` maps each parameter name to its candidate observations.
    ``constraints`` are joint predicates over (partial) evidence.
    ``max_scenarios`` caps the output budget; when the budget is hit the
    result is marked incomplete and the unexplored frontier is reported.
    """
    constraints = tuple(constraints)
    parameters = list(candidates)
    for param, obs_list in candidates.items():
        for obs in obs_list:
            if obs.parameter != param:
                raise ValueError(
                    f"observation {obs.obs_id!r} is filed under parameter "
                    f"{param!r} but says parameter {obs.parameter!r}"
                )

    scenarios = []
    unexplored = []
    pruned = 0
    complete = True
    evidence = {}
    calib_by_instrument = {}

    def budget_exhausted():
        return max_scenarios is not None and len(scenarios) >= max_scenarios

    def dfs(depth):
        nonlocal complete, pruned
        if budget_exhausted():
            complete = False
            unexplored.append(
                UnexploredBranch(
                    assigned=dict(evidence),
                    remaining=tuple(parameters[depth:]),
                )
            )
            return
        if depth == len(parameters):
            scenarios.append(
                Scenario(
                    index=len(scenarios),
                    evidence=dict(evidence),
                    calibrations=dict(calib_by_instrument),
                )
            )
            return
        param = parameters[depth]
        for obs in candidates[param]:
            cal = obs.calibration
            if cal is not None:
                prev = calib_by_instrument.get(cal.instrument)
                if prev is not None and prev != cal:
                    # Would mix calibrations from different periods of the
                    # same instrument -- not allowed.
                    pruned += 1
                    continue
            evidence[param] = obs
            if cal is not None:
                calib_by_instrument[cal.instrument] = cal
            if all(c(evidence) for c in constraints):
                dfs(depth + 1)
            else:
                pruned += 1
            del evidence[param]
            if cal is not None:
                # Restore previous binding (None if this branch introduced it)
                if prev is None:
                    del calib_by_instrument[cal.instrument]
                else:
                    calib_by_instrument[cal.instrument] = prev

    dfs(0)
    return EnumerationResult(
        scenarios=scenarios,
        complete=complete,
        unexplored=unexplored,
        pruned=pruned,
    )


@dataclass
class BuildFailure:
    scenario: Scenario
    error: Exception


@dataclass
class BuildResult:
    """Models built per scenario; failures are isolated and recorded."""

    built: list = field(default_factory=list)  # list of (Scenario, model)
    failures: list = field(default_factory=list)  # list of BuildFailure

    @property
    def models(self):
        return [model for _, model in self.built]


def build_models(
    scenarios: Iterable[Scenario],
    factory: Callable[[Scenario], Any],
) -> BuildResult:
    """Build one model instance per scenario, isolating failures.

    A scenario whose factory call raises is recorded in ``failures``; every
    other scenario's model instance is unaffected.
    """
    result = BuildResult()
    for sc in scenarios:
        try:
            model = factory(sc)
        except Exception as err:
            result.failures.append(BuildFailure(scenario=sc, error=err))
        else:
            result.built.append((sc, model))
    return result


def make_pyomo_factory(template=None):
    """Factory that applies scenario values to a Pyomo model.

    If ``template`` (a ConcreteModel) is given, each scenario gets its own
    ``template.clone()`` so instances never share mutable state.  Otherwise
    a fresh ConcreteModel is created per scenario.  Each parameter name in
    the scenario must resolve to a model component with ``set_value``.
    The scenario's evidence (observation + calibration provenance) is
    attached to the instance as ``_scenario_evidence`` so every parameter
    value can be traced back to its source.
    """

    def factory(scenario):
        from pyomo.environ import ConcreteModel

        model = template.clone() if template is not None else ConcreteModel()
        for param, value in scenario.values.items():
            comp = model.component(param)
            if comp is None:
                raise AttributeError(
                    f"model has no component named {param!r} "
                    f"(scenario {scenario.index})"
                )
            comp.set_value(value)
        model._scenario_evidence = dict(scenario.evidence)
        model._scenario_calibrations = dict(scenario.calibrations)
        return model

    return factory
