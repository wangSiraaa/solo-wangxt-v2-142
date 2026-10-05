"""Replay a reported transition sequence step by step.

Every step is computed by the fast P/T engine AND independently by SNAKES;
a disagreement is surfaced instead of being hidden.  The replay is the
"可重演" guarantee behind each counterexample: start at the stored initial
marking, fire the named transitions in order, and observe the violating
state appear.
"""
from __future__ import annotations

from typing import List

from .petrinet import CompiledNet, MarkingT, snakes_successors
from .schemas import ReplayReport, StepInReplay


def replay(net: CompiledNet, sequence: List[str],
           witness_kind: str, check_run_id: str,
           version_id: str) -> ReplayReport:
    marking: MarkingT = net.initial
    steps: List[StepInReplay] = []
    discrepancies: List[str] = []
    valid = True
    starts_at_initial = True

    for i, tname in enumerate(sequence):
        if tname not in net.pre:
            valid = False
            discrepancies.append(f"step {i}: unknown transition {tname!r}")
            break
        enabled_before = [t for t in net.transitions if net.enabled(marking, t)]
        if not net.enabled(marking, tname):
            valid = False
            discrepancies.append(
                f"step {i}: transition {tname!r} is not enabled at "
                f"{net.as_dict(marking)}")
            break

        fired_from = net.as_dict(marking)
        next_marking = net.fire(marking, tname)

        # independent SNAKES computation of this very step
        ref = snakes_successors(net, marking)
        ref_targets = {t: s for t, s in ref}
        if tname not in ref_targets:
            valid = False
            discrepancies.append(
                f"step {i}: SNAKES reports {tname!r} as not enabled")
            break
        if ref_targets[tname] != next_marking:
            valid = False
            discrepancies.append(
                f"step {i}: SNAKES successor {net.as_dict(ref_targets[tname])}"
                f" differs from engine successor {net.as_dict(next_marking)}")
            break

        steps.append(StepInReplay(
            index=i,
            transition=tname,
            fired_from=fired_from,
            marking_after=net.as_dict(next_marking),
            enabled_before=enabled_before,
        ))
        marking = next_marking

    final = net.as_dict(marking)
    ends_terminal = net.terminal_id(marking) is not None
    is_dead_end = net.successors(marking) == []
    excl = net.exclusivity_violations(marking)
    cap = net.capacity_violations(marking)

    if discrepancies:
        valid = False

    return ReplayReport(
        check_run_id=check_run_id,
        version_id=version_id,
        witness_kind=witness_kind,
        valid=valid,
        steps=steps,
        final_marking=final,
        starts_at_initial=starts_at_initial,
        ends_in_terminal=ends_terminal,
        is_dead_end=is_dead_end,
        exclusivity_violations=excl,
        capacity_violations=cap,
    )
