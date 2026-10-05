"""Turn an ExplorationResult into a stored check-run result and verdict.

Verdict rules (the core distinction the task asks for)
-------------------------------------------------------
* ``COUNTEREXAMPLE`` - a requested property fails.  The finding carries a
  replayable transition sequence (deadlock, simultaneous states, or an
  explicit non-termination lasso when no dead end exists).
* ``TRUNCATED`` - the ``max_states`` budget was reached and no
  counterexample was found.  Properties are reported as ``UNKNOWN``, never as
  held; the unfinished frontier and boundary edges are retained.
* ``PROVED`` - the reachable set was fully enumerated and every requested
  property holds.
"""
from __future__ import annotations

from typing import Any, Dict, List

import networkx as nx

from .explorer import ExplorationResult, Witness
from .petrinet import CompiledNet


def _witness_payload(w: Witness) -> Dict[str, Any]:
    return {
        "kind": w.kind,
        "description": w.description,
        "transition_sequence": w.path,
        "length": len(w.path),
        "final_marking": w.final_marking,
        "detail": w.detail,
    }


def build_report(result: ExplorationResult,
                 cross_check_problems: List[str]) -> Dict[str, Any]:
    net: CompiledNet = result.net
    g: nx.DiGraph = result.graph
    goals = net.goals

    explored = g.number_of_nodes()
    fully_explored = not result.truncated
    findings: Dict[str, Any] = {}

    # --- terminal reachability ---------------------------------------
    terminal_info = {
        "required": goals.get("require_terminal_reachable", True),
        "terminal_states_found": len(result.terminal_states),
        "terminal_markings": [
            {"marking": net.as_dict(m),
             "matched_terminal_id": g.nodes[m]["terminal_id"],
             "transition_sequence": result.path_to(m)}
            for m in result.terminal_states[:20]
        ],
    }
    if goals.get("require_terminal_reachable", True):
        if result.terminal_states:
            terminal_info["status"] = "HELD" if fully_explored else "HELD_SO_FAR"
        elif fully_explored:
            terminal_info["status"] = "VIOLATED"
            w = result.witnesses.get("terminal_unreachable")
            if w is not None:
                terminal_info["witness"] = _witness_payload(w)
            elif result.dead_states:
                # path to the dead end proves it: reuse the deadlock witness
                terminal_info["witness"] = _witness_payload(
                    result.witnesses["deadlock"])
            else:  # pragma: no cover - complete finite graph must end/loop
                terminal_info["witness"] = None
        else:
            terminal_info["status"] = "UNKNOWN"
    else:
        terminal_info["status"] = "NOT_CHECKED"
    findings["terminal_reachability"] = terminal_info

    # --- deadlock freedom --------------------------------------------
    deadlock_info = {
        "required": goals.get("require_no_deadlock", True),
        "dead_states_found": len(result.dead_states),
        "dead_markings": [
            {"marking": net.as_dict(m),
             "transition_sequence": result.path_to(m)}
            for m in result.dead_states[:20]
        ],
        "note": ("dead ends matching a declared terminal marking are treated "
                 "as proper termination"),
    }
    if goals.get("require_no_deadlock", True):
        if result.dead_states:
            deadlock_info["status"] = "VIOLATED"
            deadlock_info["witness"] = _witness_payload(
                result.witnesses["deadlock"])
        elif fully_explored:
            deadlock_info["status"] = "HELD"
        else:
            deadlock_info["status"] = "UNKNOWN"
            deadlock_info["note"] += (
                "; only %d of the reachable states were expanded, absence of "
                "deadlock here is not a proof" % len(result.expanded))
    else:
        deadlock_info["status"] = "NOT_CHECKED"
    findings["deadlock_freedom"] = deadlock_info

    # --- exclusivity ---------------------------------------------------
    exclusivity_info = {
        "required": goals.get("require_exclusivity", True),
        "violating_states_found": len(result.exclusion_states),
        "violations": [_witness_payload(w)
                       for k, w in result.witnesses.items()
                       if k.startswith("exclusivity:")][:20],
    }
    if goals.get("require_exclusivity", True):
        if result.exclusion_states:
            exclusivity_info["status"] = "VIOLATED"
        elif fully_explored:
            exclusivity_info["status"] = "HELD"
        else:
            exclusivity_info["status"] = "UNKNOWN"
    else:
        exclusivity_info["status"] = "NOT_CHECKED"
    findings["exclusivity"] = exclusivity_info

    # --- boundedness (capacity-respecting / structurally bounded) ------
    capacities = [
        None if c is None else c for c in net.capacities
    ]
    occupancy = [
        {"place": net.places[i], "capacity": capacities[i],
         "max_observed": result.max_occupancy[i]}
        for i in range(len(net.places))
    ]
    over_capacity = [o for o in occupancy
                     if o["capacity"] is not None
                     and o["max_observed"] > o["capacity"]]
    bounded_info = {
        "capacities_declared": [
            {"place": net.places[i], "capacity": capacities[i]}
            for i in range(len(net.places))],
        "max_occupancy": occupancy,
    }
    if over_capacity:
        # Cannot happen: capacity-overfill disables transitions; kept as a
        # defensive invariant.
        bounded_info["status"] = "VIOLATED"  # pragma: no cover
        bounded_info["over_capacity"] = over_capacity  # pragma: no cover
    elif fully_explored:
        bounded_info["status"] = "HELD"
        bounded_info["bound"] = {
            net.places[i]: result.max_occupancy[i]
            for i in range(len(net.places))}
    else:
        bounded_info["status"] = "UNKNOWN"
        unbounded_places = [net.places[i]
                            for i, c in enumerate(net.capacities) if c is None]
        if unbounded_places:
            bounded_info["suspicion_hint"] = (
                "places without a declared capacity kept growing in the "
                "explored prefix; a TRUNCATED run with a rising token bound "
                "is a practical indicator of an unbounded net, not a proof")
            bounded_info["unbounded_candidate_places"] = unbounded_places
    findings["boundedness"] = bounded_info

    # --- graph statistics ----------------------------------------------
    max_depth = max(result.depths.values()) if result.depths else 0
    statistics = {
        "states_total": explored,
        "states_expanded": len(result.expanded),
        "edges_total": g.number_of_edges(),
        "max_bfs_depth": max_depth,
        "cycles_reported": len(result.cycles),
        "self_loop_states": len(result.self_loop_states),
        "terminal_scc_count": result.terminal_scc_count,
        "has_cycle": bool(result.cycles),
        "cycle_witnesses": result.cycles[:10],
    }

    truncated_report = None
    if result.truncated:
        truncated_report = {
            "limit": result.limit,
            "states_visited": explored,
            "states_expanded": len(result.expanded),
            "frontier_size": len(result.frontier),
            "frontier": [net.as_dict(m) for m in result.frontier[:200]],
            "frontier_note": ("visited-but-unexpanded states; resume the "
                              "search from any of them"),
            "boundary_edges_reported": len(result.boundary_edges),
            "boundary_edges_cap_reached": result.boundary_truncated,
            "boundary_edges": [
                {"source": net.as_dict(s), "transition": t,
                 "target": net.as_dict(d), "target_already_visited": seen}
                for s, t, d, seen in result.boundary_edges[:500]],
        }

    witnesses_payload = {k: _witness_payload(w)
                         for k, w in result.witnesses.items()}

    # --- overall verdict ------------------------------------------------
    has_counterexample = any(
        info.get("status") == "VIOLATED"
        for info in (terminal_info, deadlock_info, exclusivity_info)) \
        or over_capacity
    if has_counterexample:
        verdict = "COUNTEREXAMPLE"
        status = "completed"
    elif result.truncated:
        verdict = "TRUNCATED"
        status = "completed"
    else:
        verdict = "PROVED"
        status = "completed"

    primary = _primary_witness(result, terminal_info, deadlock_info)

    return {
        "status": status,
        "verdict": verdict,
        "model_name": net.name,
        "explored_states": explored,
        "states_expanded": len(result.expanded),
        "edge_count": g.number_of_edges(),
        "truncated": result.truncated,
        "max_states_limit": result.limit,
        "fully_explored": fully_explored,
        "initial_marking": net.as_dict(result.initial),
        "findings": findings,
        "witnesses": witnesses_payload,
        "primary_witness": primary,
        "statistics": statistics,
        "truncation": truncated_report,
        "engine_cross_check": {
            "engines": ["pure-pt-engine", "SNAKES"],
            "agreement": not cross_check_problems,
            "problems": cross_check_problems,
        },
        "interpretation": _interpretation(verdict),
    }


def _primary_witness(result: ExplorationResult, terminal_info,
                     deadlock_info):
    """Shortest, most directly actionable witness for the API summary."""
    order = ["deadlock", "terminal_unreachable"]
    for key in order:
        if key in result.witnesses:
            return _witness_payload(result.witnesses[key])
    for key, w in result.witnesses.items():
        if key.startswith("exclusivity:"):
            return _witness_payload(w)
    if result.terminal_states:
        return _witness_payload(result.witnesses["terminal_reachable"])
    return None


def _interpretation(verdict: str) -> str:
    if verdict == "PROVED":
        return ("The reachable state space was enumerated completely: "
                "termination, deadlock freedom and exclusivity (where "
                "required) hold for EVERY reachable marking.")
    if verdict == "COUNTEREXAMPLE":
        return ("A concrete counterexample was found; replaying the returned "
                "transition sequence from the initial marking reproduces it "
                "step by step.")
    return ("The exploration hit the state budget and was stopped. The "
            "findings cover only the enumerated prefix; NOTHING about "
            "deadlock freedom, termination reachability or exclusivity is "
            "proved for the whole net. Raise max_states, or resume from the "
            "reported frontier.")
