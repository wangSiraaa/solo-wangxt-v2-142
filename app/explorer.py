"""State-space exploration and state-graph analysis.

Breadth-first enumeration of the reachability graph of an ordinary bounded
P/T net.  Three outcomes are deliberately distinguished, as requested:

* **PROVED**       - the finite reachable set was enumerated completely and
                     every requested property holds;
* **COUNTEREXAMPLE** - a violating state/path was found; its transition
                     sequence is returned and is directly replayable;
* **TRUNCATED**    - the state budget (``max_states``) was hit: seeing no
                     problem in the first N states is *not* a proof.  The
                     unfinished boundary (visited-but-unexpanded states and
                     the one-step edges leaving it) is retained.

"搜了十万个状态没发现死锁" therefore maps to ``TRUNCATED`` (with the exact
budget and the boundary reported), never to a deadlock-freedom proof.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import networkx as nx

from .petrinet import CompiledNet, MarkingT


@dataclass
class Witness:
    kind: str                       # terminal | deadlock | exclusivity | ...
    description: str
    path: List[str]                 # transition names from M0
    final_marking: Dict[str, int]
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExplorationResult:
    net: CompiledNet
    graph: nx.DiGraph
    parents: Dict[MarkingT, Tuple[MarkingT, str]]
    depths: Dict[MarkingT, int]
    initial: MarkingT
    terminal_states: List[MarkingT]
    dead_states: List[MarkingT]
    exclusion_states: Dict[str, Tuple[MarkingT, List[List[str]]]]
    max_occupancy: List[int]
    truncated: bool
    limit: int
    expanded: set
    frontier: List[MarkingT]
    boundary_edges: List[Tuple[MarkingT, str, MarkingT, bool]]
    boundary_truncated: bool
    witnesses: Dict[str, Witness]
    cycles: List[List[str]]
    terminal_scc_count: int
    self_loop_states: List[MarkingT]
    cross_check_problems: List[str] = field(default_factory=list)

    def path_to(self, target: MarkingT) -> List[str]:
        """Reconstruct the BFS transition sequence from M0 to ``target``."""
        seq: List[str] = []
        cur = target
        while cur != self.initial:
            prev, tname = self.parents[cur]
            seq.append(tname)
            cur = prev
        seq.reverse()
        return seq


def _state_flags(net: CompiledNet, m: MarkingT, successor_count: int,
                 expanded: bool) -> Dict[str, Any]:
    terminal_id = net.terminal_id(m)
    return {
        "marking": net.as_dict(m),
        "terminal": terminal_id is not None,
        "terminal_id": terminal_id,
        # a dead end can only be certified once the state was expanded
        "dead_end": expanded and successor_count == 0,
        "exclusivity_violations": net.exclusivity_violations(m),
        "capacity_violations": net.capacity_violations(m),
    }


def explore(net: CompiledNet, max_states: int,
            max_boundary: int = 50_000) -> ExplorationResult:
    graph = nx.DiGraph()
    initial = net.initial
    parents: Dict[MarkingT, Tuple[MarkingT, str]] = {}
    depths: Dict[MarkingT, int] = {initial: 0}
    expanded: set = set()

    graph.add_node(initial, **_state_flags(net, initial, 0, False))
    queue = deque([initial])

    terminal_states: List[MarkingT] = []
    dead_states: List[MarkingT] = []
    exclusion_states: Dict[str, Tuple[MarkingT, List[List[str]]]] = {}
    witnesses: Dict[str, Witness] = {}
    max_occupancy = list(initial)
    truncated = False

    def path_to(target: MarkingT) -> List[str]:
        seq, cur = [], target
        while cur != initial:
            prev, tn = parents[cur]
            seq.append(tn)
            cur = prev
        seq.reverse()
        return seq

    def classify(m: MarkingT):
        """Record property witnesses for a freshly discovered state."""
        flags = graph.nodes[m]
        if flags["terminal"]:
            terminal_states.append(m)
            if "terminal_reachable" not in witnesses:
                witnesses["terminal_reachable"] = Witness(
                    kind="terminal",
                    description="a terminal marking is reachable",
                    path=path_to(m),
                    final_marking=net.as_dict(m),
                )
        for occ in flags["exclusivity_violations"]:
            key = ",".join(occ)
            if key not in exclusion_states:
                exclusion_states[key] = (m, occ)
                witnesses.setdefault("exclusivity:" + key, Witness(
                    kind="exclusivity",
                    description="places simultaneously marked: "
                                + ", ".join(occ),
                    path=path_to(m),
                    final_marking=net.as_dict(m),
                    detail={"group": occ},
                ))

    # classify the initial state (may itself be terminal / violating)
    classify(initial)

    while queue:
        if len(graph) >= max_states:
            truncated = True
            break
        m = queue.popleft()
        successors = net.successors(m)
        expanded.add(m)

        for tname, nxt in successors:
            for i, v in enumerate(nxt):
                if v > max_occupancy[i]:
                    max_occupancy[i] = v
            is_new = nxt not in graph
            if is_new:
                parents[nxt] = (m, tname)
                depths[nxt] = depths[m] + 1
                graph.add_node(nxt, **_state_flags(net, nxt, 0, False))
                classify(nxt)
                queue.append(nxt)
            graph.add_edge(m, nxt, transition=tname)

        flags = _state_flags(net, m, len(successors), True)
        nx.set_node_attributes(graph, {m: flags})
        if flags["dead_end"] and not (
                net.goals.get("deadlock_allows_terminal", True)
                and flags["terminal"]):
            dead_states.append(m)
            witnesses.setdefault("deadlock", Witness(
                kind="deadlock",
                description=("dead marking: no transition is enabled and it "
                             "is not a declared terminal marking"),
                path=path_to(m),
                final_marking=net.as_dict(m),
            ))

    # unfinished boundary: visited but not expanded + outgoing edges
    frontier = [m for m in graph if m not in expanded]
    boundary_edges: List[Tuple[MarkingT, str, MarkingT, bool]] = []
    boundary_truncated = False
    if truncated:
        for m in frontier:
            for tname, nxt in net.successors(m):
                boundary_edges.append((m, tname, nxt, nxt in graph))
                if len(boundary_edges) >= max_boundary:
                    boundary_truncated = True
                    break
            if boundary_truncated:
                break

    result = ExplorationResult(
        net=net,
        graph=graph,
        parents=parents,
        depths=depths,
        initial=initial,
        terminal_states=terminal_states,
        dead_states=dead_states,
        exclusion_states=exclusion_states,
        max_occupancy=max_occupancy,
        truncated=truncated,
        limit=max_states,
        expanded=expanded,
        frontier=frontier,
        boundary_edges=boundary_edges,
        boundary_truncated=boundary_truncated,
        witnesses=witnesses,
        cycles=[],
        terminal_scc_count=0,
        self_loop_states=[],
    )
    _analyse_graph(result)
    _reachability_witness(result)
    return result


def _selfloop_edges(g):
    # NetworkX >= 3 exposes self-loop edges only via the free function
    return nx.selfloop_edges(g)


def _analyse_graph(result: ExplorationResult) -> None:
    """NetworkX structural analysis: cycles and terminal SCCs."""
    g = result.graph
    result.terminal_scc_count = sum(
        1 for scc in nx.strongly_connected_components(g)
        if _is_terminal_scc(g, scc))

    cycles: List[List[str]] = []
    seen = set()
    self_loop_states = []

    for u, v in _selfloop_edges(g):
        self_loop_states.append(u)
        seq = result.path_to(u) + [g.edges[u, v]["transition"]]
        if tuple(seq) not in seen:
            seen.add(tuple(seq))
            cycles.append(seq)

    for scc in nx.strongly_connected_components(g):
        if len(scc) < 2:
            continue
        sub = g.subgraph(scc)
        if sub.number_of_edges() == 0:
            continue
        node = next(iter(scc))
        try:
            cyc = nx.find_cycle(sub, source=node, orientation="original")
        except nx.NetworkXNoCycle:
            continue
        base = next(iter(scc))
        labels = [g.edges[u, v]["transition"] for u, v, _ in cyc]
        full = result.path_to(base) + labels
        if tuple(full) not in seen:
            seen.add(tuple(full))
            cycles.append(full)
        if len(cycles) >= 20:
            break

    result.cycles = cycles
    result.self_loop_states = self_loop_states


def _is_terminal_scc(g: nx.DiGraph, scc) -> bool:
    for n in scc:
        for t in g.successors(n):
            if t not in scc:
                return False
    return True


def _reachability_witness(result: ExplorationResult) -> None:
    """Explain terminal-unreachability after a *complete* exploration.

    * a non-terminal dead end: the deadlock witness already gives the path;
    * infinite behaviour: a lasso (stem to a cyclic terminal SCC + the labels
      of one loop).  Covers the loop case with no deadlocks where termination
      is nevertheless unreachable.
    """
    net = result.net
    if (result.terminal_states or result.truncated
            or not net.goals.get("require_terminal_reachable", True)):
        return
    g = result.graph
    for scc in nx.strongly_connected_components(g):
        if not _is_terminal_scc(g, scc):
            continue
        sub = g.subgraph(scc)
        if sub.number_of_edges() == 0:
            continue  # singleton dead end: deadlock witness covers it
        node = next(iter(scc))
        try:
            cyc = nx.find_cycle(sub, source=node, orientation="original")
        except nx.NetworkXNoCycle:
            continue
        stem = result.path_to(node)
        loop = [g.edges[u, v]["transition"] for u, v, _ in cyc]
        result.witnesses["terminal_unreachable"] = Witness(
            kind="non_terminating_cycle",
            description=("no terminal marking is reachable; execution can "
                         "stay forever in a cycle (stem then loop)"),
            path=stem + loop,
            final_marking=net.as_dict(node),
            detail={"stem": stem, "loop": loop,
                    "loop_entry": net.as_dict(node)},
        )
        return
