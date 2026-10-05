"""Prepared demonstration models.

1. ``parallel_join_missing_token`` - a fork/join workflow where the fork
   forgets to spawn branch B: the join waits for a token that can never
   arrive (deadlock) and the terminal is unreachable.
2. ``unbounded_loop`` - a source transition keeps producing tokens; with a
   small state budget the run is TRUNCATED with its unfinished boundary kept
   (this is the "搜了十万个状态没有结论" case).
3. ``bounded_cycle`` - the same loop idea but capacity-bounded: complete
   finite state space, PROVED, cycle reported.
4. ``resource_race_no_mutex`` / ``resource_race_with_lock`` - mutual
   exclusion violated vs. correctly serialised by a lock.
5. ``circular_wait_deadlock`` - two processes grab resources in opposite
   order: interleaving p1_take_r1 then p2_take_r2 deadlocks.
"""
from __future__ import annotations

from typing import Any, Dict


def parallel_join_missing_token() -> Dict[str, Any]:
    return {
        "name": "parallel_join_missing_token",
        "places": [
            {"name": "start", "capacity": 1},
            {"name": "branchA_done", "capacity": 1},
            {"name": "branchB_ready", "capacity": 1},
            {"name": "branchB_done", "capacity": 1},
            {"name": "end", "capacity": 1},
        ],
        "transitions": [
            {"name": "fork"},
            {"name": "workB"},
            {"name": "join"},
        ],
        "inputs": [
            {"place": "start", "transition": "fork"},
            {"place": "branchA_done", "transition": "join"},
            {"place": "branchB_done", "transition": "join"},
            {"place": "branchB_ready", "transition": "workB"},
        ],
        "outputs": [
            # Defect: fork only signals branch A; branch B never receives
            # its token, so join can never fire.
            {"place": "branchA_done", "transition": "fork"},
            {"place": "end", "transition": "join"},
            {"place": "branchB_done", "transition": "workB"},
        ],
        "initial_marking": {"start": 1},
        "goals": {
            "terminal_markings": [
                {"marking": {"end": 1}, "match": "exact"}],
        },
    }


def unbounded_loop() -> Dict[str, Any]:
    return {
        "name": "unbounded_loop",
        "places": [{"name": "p"}],
        "transitions": [{"name": "produce"}],
        "inputs": [],
        "outputs": [{"place": "p", "transition": "produce"}],
        "initial_marking": {},
        "goals": {
            "terminal_markings": [
                {"marking": {"p": 3}, "match": "exact"}],
        },
    }


def bounded_cycle() -> Dict[str, Any]:
    return {
        "name": "bounded_cycle",
        "places": [{"name": "a", "capacity": 1},
                   {"name": "b", "capacity": 1}],
        "transitions": [{"name": "t"}, {"name": "u"}],
        "inputs": [{"place": "a", "transition": "t"},
                   {"place": "b", "transition": "u"}],
        "outputs": [{"place": "b", "transition": "t"},
                    {"place": "a", "transition": "u"}],
        "initial_marking": {"a": 1},
        "goals": {"terminal_markings": [],
                  "require_terminal_reachable": False},
    }


def resource_race_no_mutex() -> Dict[str, Any]:
    return {
        "name": "resource_race_no_mutex",
        "places": [
            {"name": "idle1", "capacity": 1},
            {"name": "crit1", "capacity": 1},
            {"name": "idle2", "capacity": 1},
            {"name": "crit2", "capacity": 1},
        ],
        "transitions": [
            {"name": "acq1"}, {"name": "rel1"},
            {"name": "acq2"}, {"name": "rel2"},
        ],
        "inputs": [
            {"place": "idle1", "transition": "acq1"},
            {"place": "crit1", "transition": "rel1"},
            {"place": "idle2", "transition": "acq2"},
            {"place": "crit2", "transition": "rel2"},
        ],
        "outputs": [
            {"place": "crit1", "transition": "acq1"},
            {"place": "idle1", "transition": "rel1"},
            {"place": "crit2", "transition": "acq2"},
            {"place": "idle2", "transition": "rel2"},
        ],
        "initial_marking": {"idle1": 1, "idle2": 1},
        "goals": {
            "terminal_markings": [],
            "require_terminal_reachable": False,
            "exclusive_groups": [["crit1", "crit2"]],
        },
    }


def resource_race_with_lock() -> Dict[str, Any]:
    model = resource_race_no_mutex()
    model = {**model, "name": "resource_race_with_lock",
             "places": list(model["places"])
                       + [{"name": "lock", "capacity": 1}],
             "inputs": list(model["inputs"]) + [
                 {"place": "lock", "transition": "acq1"},
                 {"place": "lock", "transition": "acq2"}],
             "outputs": list(model["outputs"]) + [
                 {"place": "lock", "transition": "rel1"},
                 {"place": "lock", "transition": "rel2"}],
             "initial_marking": {"idle1": 1, "idle2": 1, "lock": 1}}
    return model


def circular_wait_deadlock() -> Dict[str, Any]:
    return {
        "name": "circular_wait_deadlock",
        "places": [
            {"name": "r1", "capacity": 1}, {"name": "r2", "capacity": 1},
            {"name": "p1_idle", "capacity": 1},
            {"name": "p1_holds_r1", "capacity": 1},
            {"name": "p1_crit", "capacity": 1},
            {"name": "p2_idle", "capacity": 1},
            {"name": "p2_holds_r2", "capacity": 1},
            {"name": "p2_crit", "capacity": 1},
        ],
        "transitions": [
            {"name": "p1_take_r1"}, {"name": "p1_take_r2"},
            {"name": "p1_release"},
            {"name": "p2_take_r2"}, {"name": "p2_take_r1"},
            {"name": "p2_release"},
        ],
        "inputs": [
            {"place": "p1_idle", "transition": "p1_take_r1"},
            {"place": "r1", "transition": "p1_take_r1"},
            {"place": "p1_holds_r1", "transition": "p1_take_r2"},
            {"place": "r2", "transition": "p1_take_r2"},
            {"place": "p1_crit", "transition": "p1_release"},
            {"place": "p2_idle", "transition": "p2_take_r2"},
            {"place": "r2", "transition": "p2_take_r2"},
            {"place": "p2_holds_r2", "transition": "p2_take_r1"},
            {"place": "r1", "transition": "p2_take_r1"},
            {"place": "p2_crit", "transition": "p2_release"},
        ],
        "outputs": [
            {"place": "p1_holds_r1", "transition": "p1_take_r1"},
            {"place": "p1_crit", "transition": "p1_take_r2"},
            {"place": "p1_idle", "transition": "p1_release"},
            {"place": "r1", "transition": "p1_release"},
            {"place": "r2", "transition": "p1_release"},
            {"place": "p2_holds_r2", "transition": "p2_take_r2"},
            {"place": "p2_crit", "transition": "p2_take_r1"},
            {"place": "p2_idle", "transition": "p2_release"},
            {"place": "r2", "transition": "p2_release"},
            {"place": "r1", "transition": "p2_release"},
        ],
        "initial_marking": {"r1": 1, "r2": 1, "p1_idle": 1, "p2_idle": 1},
        "goals": {
            "terminal_markings": [],
            "require_terminal_reachable": False,
            "exclusive_groups": [["p1_crit", "p2_crit"]],
        },
    }


def resource_race_no_mutex_fixed() -> Dict[str, Any]:
    """Variant of the mutex example with the initial defect repaired:
    adding the lock arc is exactly the kind of change that creates a NEW
    model version whose old check conclusion must not be reused."""
    return resource_race_with_lock()


ALL = {
    "parallel_join_missing_token": parallel_join_missing_token,
    "unbounded_loop": unbounded_loop,
    "bounded_cycle": bounded_cycle,
    "resource_race_no_mutex": resource_race_no_mutex,
    "resource_race_with_lock": resource_race_with_lock,
    "circular_wait_deadlock": circular_wait_deadlock,
}
