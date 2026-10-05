"""Compile/validate structured P/T-net models and fire transitions.

Two engines share one source of truth (the compiled arc tables):

* ``snakes_successors`` builds the net with SNAKES and computes successor
  markings using SNAKES' own enablement/firing code.  It validates that the
  submitted model really is an ordinary bounded P/T net and it is used to
  cross-check the fast engine (initial state, sampled states, every step of
  a returned counter-example replay).
* ``successors`` is a pure-Python token-counting engine used for the actual
  state-space exploration; benchmarks show ~2m successor evaluations per
  second versus ~100k through full SNAKES ``set_marking`` cycles.

Semantics explicitly supported (ordinary bounded nets):

* black, indistinguishable tokens; a marking is place -> token count;
* arcs have a positive integer *weight* (consume/produce N tokens);
* a transition is enabled when every input place has at least the arc
  weight tokens AND firing keeps every finite-capacity place within its
  capacity.  Capacity overfill disables the transition ("bounded net"
  semantics, overflow is not an error state);
* source transitions (no inputs) are enabled in every marking, sink and
  isolated transitions are allowed;
* no colours, variables, guards, inhibitor/test/flush arcs.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from snakes.nets import (Marking, MultiArc, MultiSet, PetriNet, Place,
                         Substitution, Transition, Value, dot)

from .schemas import ModelDefinitionIn

MarkingT = Tuple[int, ...]  # counts in compiled place order


class ModelError(ValueError):
    """A semantic validation error on a submitted model.

    ``code`` is a stable machine-readable identifier; ``detail`` names the
    offending element.
    """

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class TerminalPattern:
    required: Dict[int, int]
    exact: bool


@dataclass
class CompiledNet:
    name: str
    places: Tuple[str, ...]
    capacities: Tuple[Optional[int], ...]
    transitions: Tuple[str, ...]
    # transition name -> input/output place index -> weight
    pre: Dict[str, Dict[int, int]]
    post: Dict[str, Dict[int, int]]
    initial: MarkingT
    terminals: Tuple[TerminalPattern, ...]
    exclusive_groups: Tuple[Tuple[int, ...], ...]
    goals: dict = field(default_factory=dict)

    # -- marking helpers --------------------------------------------------
    def as_dict(self, marking: Sequence[int]) -> Dict[str, int]:
        return {self.places[i]: n for i, n in enumerate(marking) if n > 0}

    def is_terminal(self, marking: Sequence[int]) -> bool:
        for pat in self.terminals:
            if pat.exact:
                if all(marking[i] == n for i, n in pat.required.items()):
                    return True
            elif all(marking[i] >= n for i, n in pat.required.items()):
                return True
        return False

    def terminal_id(self, marking: Sequence[int]) -> Optional[int]:
        for i, pat in enumerate(self.terminals):
            if pat.exact:
                if all(marking[k] == v for k, v in pat.required.items()):
                    return i
            elif all(marking[k] >= v for k, v in pat.required.items()):
                return i
        return None

    def exclusivity_violations(self, marking: Sequence[int]) -> List[List[str]]:
        out = []
        for group in self.exclusive_groups:
            occupied = [self.places[i] for i in group if marking[i] > 0]
            if len(occupied) > 1:
                out.append(occupied)
        return out

    def capacity_violations(self, marking: Sequence[int]) -> List[str]:
        return [self.places[i] for i, cap in enumerate(self.capacities)
                if cap is not None and marking[i] > cap]

    # -- fast engine ------------------------------------------------------
    def enabled(self, marking: Sequence[int], t: str) -> bool:
        tin = self.pre[t]
        if any(marking[p] < w for p, w in tin.items()):
            return False
        tout = self.post[t]
        for p, produced in tout.items():
            cap = self.capacities[p]
            if cap is not None:
                after = marking[p] - tin.get(p, 0) + produced
                if after > cap:
                    return False
        return True

    def fire(self, marking: MarkingT, t: str) -> MarkingT:
        m = list(marking)
        for p, w in self.pre[t].items():
            m[p] -= w
        for p, w in self.post[t].items():
            m[p] += w
        return tuple(m)

    def successors(self, marking: MarkingT) -> List[Tuple[str, MarkingT]]:
        """Enabled transitions in declared order, each with its successor.

        Two transitions may lead to the same successor marking; the explorer
        de-duplicates states but keeps every labelled edge.
        """
        out: List[Tuple[str, MarkingT]] = []
        for t in self.transitions:
            if self.enabled(marking, t):
                out.append((t, self.fire(marking, t)))
        return out


# ---------------------------------------------------------------------------
# Compilation / validation
# ---------------------------------------------------------------------------


def compile_model(model: ModelDefinitionIn) -> CompiledNet:
    if not model.places:
        raise ModelError("EMPTY_NET", "the net has no places")
    if not model.transitions:
        raise ModelError("EMPTY_NET", "the net has no transitions")

    place_names = [p.name for p in model.places]
    trans_names = [t.name for t in model.transitions]
    if len(set(place_names)) != len(place_names):
        dup = sorted({n for n in place_names if place_names.count(n) > 1})
        raise ModelError("DUPLICATE_PLACE", "duplicate place name",
                         {"places": dup})
    if len(set(trans_names)) != len(trans_names):
        dup = sorted({n for n in trans_names if trans_names.count(n) > 1})
        raise ModelError("DUPLICATE_TRANSITION", "duplicate transition name",
                         {"transitions": dup})

    pidx = {n: i for i, n in enumerate(place_names)}
    tidx = {n: i for i, n in enumerate(trans_names)}
    capacities = tuple(p.capacity for p in model.places)

    pre: Dict[str, Dict[int, int]] = {t: {} for t in trans_names}
    post: Dict[str, Dict[int, int]] = {t: {} for t in trans_names}

    def fold(arcs, table, side):
        seen = set()
        for a in arcs:
            if a.place not in pidx:
                raise ModelError("UNKNOWN_PLACE",
                                 f"{side} arc references unknown place",
                                 {"arc": a.model_dump()})
            if a.transition not in tidx:
                raise ModelError("UNKNOWN_TRANSITION",
                                 f"{side} arc references unknown transition",
                                 {"arc": a.model_dump()})
            key = (a.transition, a.place)
            if key in seen:
                raise ModelError(
                    "PARALLEL_ARCS",
                    f"multiple {side} arcs between the same place/transition "
                    "are not allowed in ordinary nets; use arc weight",
                    {"place": a.place, "transition": a.transition})
            seen.add(key)
            table[a.transition][pidx[a.place]] = (
                table[a.transition].get(pidx[a.place], 0) + a.weight)

    fold(model.inputs, pre, "input")
    fold(model.outputs, post, "output")

    # initial marking
    initial = [0] * len(place_names)
    for p, n in model.initial_marking.items():
        if n < 0:
            raise ModelError("NEGATIVE_MARKING",
                             "initial marking counts must be non-negative",
                             {"place": p})
        if p not in pidx:
            raise ModelError("UNKNOWN_PLACE",
                             "initial marking references unknown place",
                             {"place": p})
        initial[pidx[p]] = n
    initial_t = tuple(initial)
    for i, cap in enumerate(capacities):
        if cap is not None and initial_t[i] > cap:
            raise ModelError("INITIAL_OVER_CAPACITY",
                             "initial marking exceeds place capacity",
                             {"place": place_names[i], "tokens": initial_t[i],
                              "capacity": cap})

    goals = model.goals
    # terminal patterns
    terminals = []
    for pat in goals.terminal_markings:
        required = {}
        for p, n in pat.marking.items():
            if n < 0:
                raise ModelError("NEGATIVE_MARKING",
                                 "terminal marking counts must be non-negative",
                                 {"place": p})
            if p not in pidx:
                raise ModelError("UNKNOWN_PLACE",
                                 "terminal marking references unknown place",
                                 {"place": p})
            required[pidx[p]] = n
        terminals.append(TerminalPattern(required=required, exact=pat.match
                                         == "exact"))

    # exclusive groups
    groups = []
    for gi, group in enumerate(goals.exclusive_groups):
        if len(group) < 2:
            raise ModelError("BAD_EXCLUSIVE_GROUP",
                             "exclusive groups need at least two places",
                             {"group": group})
        idxs = []
        for p in group:
            if p not in pidx:
                raise ModelError("UNKNOWN_PLACE",
                                 "exclusive group references unknown place",
                                 {"place": p})
            idxs.append(pidx[p])
        if len(set(idxs)) != len(idxs):
            raise ModelError("BAD_EXCLUSIVE_GROUP",
                             "duplicate place inside exclusive group",
                             {"group_index": gi})
        groups.append(tuple(idxs))

    compiled = CompiledNet(
        name=model.name,
        places=tuple(place_names),
        capacities=capacities,
        transitions=tuple(trans_names),
        pre=pre,
        post=post,
        initial=initial_t,
        terminals=tuple(terminals),
        exclusive_groups=tuple(groups),
        goals=goals.model_dump(),
    )

    # Build exactly the same net in SNAKES: rejects anything outside the
    # supported fragment and gives an independent firing implementation.
    _build_snakes(compiled)

    # Initial marking exclusivity is checked by the caller via the compiled
    # net (it is a property, not a structural error).
    return compiled


def _arc_annotation(weight: int):
    if weight == 1:
        return Value(dot)
    return MultiArc([Value(dot) for _ in range(weight)])


def _snakes_marking(marking: Sequence[int], places: Sequence[str]) -> Marking:
    return Marking({places[i]: MultiSet([dot] * n)
                    for i, n in enumerate(marking) if n > 0})


def _build_snakes(net: CompiledNet) -> PetriNet:
    snet = PetriNet("snakes:" + net.name[:40])
    for p in net.places:
        snet.add_place(Place(p))
    for t in net.transitions:
        snet.add_transition(Transition(t))
    for t in net.transitions:
        for p, w in net.pre[t].items():
            snet.add_input(net.places[p], t, _arc_annotation(w))
        for p, w in net.post[t].items():
            snet.add_output(net.places[p], t, _arc_annotation(w))
    snet.set_marking(_snakes_marking(net.initial, net.places))
    return snet


# ---------------------------------------------------------------------------
# SNAKES independent engine (validation + cross-check + replay)
# ---------------------------------------------------------------------------


def snakes_successors(net: CompiledNet,
                      marking: MarkingT) -> List[Tuple[str, MarkingT]]:
    """Successor markings as computed by SNAKES itself.

    Capacity filtering is applied afterwards: SNAKES is a coloured-net tool
    without place capacities, so bounded-net capacity is our semantic layer.
    """
    snet = _build_snakes(net)
    snet.set_marking(_snakes_marking(marking, net.places))
    result = []
    for tname in net.transitions:
        trans = snet.transition(tname)
        # SNAKES' `cross([])` yields no bindings, hence source transitions
        # (no input arcs) are reported as having no modes -- handle the one
        # empty binding explicitly, which is exactly the P/T semantics.
        modes = list(trans.modes())
        if not net.pre[tname]:
            modes = [Substitution()]
        if not modes:
            continue
        # In ordinary P/T nets every enabled transition has exactly one mode.
        mode = modes[0]
        candidate = snet.copy()
        candidate.transition(tname).fire(mode)
        new_marking = tuple(
            len(list(candidate.place(p).tokens)) for p in net.places)
        # capacity: overfill disables the transition
        if all(net.capacities[i] is None
               or new_marking[i] <= net.capacities[i]
               for i in range(len(net.places))):
            result.append((tname, new_marking))
    return result


def cross_check_sample(net: CompiledNet, states: List[MarkingT],
                       cap: int = 200, seed: int = 1729) -> List[str]:
    """Compare the fast engine against SNAKES on up to ``cap`` states.

    Returns a list of human-readable discrepancies (empty == agreement).
    Used on the initial state, sampled BFS states and full replay paths.
    """
    rng = random.Random(seed)
    sample = list(states)
    if len(sample) > cap:
        sample = rng.sample(sample, cap)
    problems = []
    for m in sample:
        fast = sorted((t, s) for t, s in net.successors(m))
        try:
            ref = sorted(snakes_successors(net, m))
        except Exception as exc:  # pragma: no cover - defensive
            problems.append(f"SNAKES engine error at {net.as_dict(m)}: {exc}")
            continue
        if fast != ref:
            problems.append(
                f"successor mismatch at {net.as_dict(m)}: "
                f"fast={[(t, net.as_dict(s)) for t, s in fast]} "
                f"snakes={[(t, net.as_dict(s)) for t, s in ref]}")
    return problems
