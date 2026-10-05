"""状态空间探索与状态图分析。

探索：BFS，点火语义由 SNAKES 执行（modes() 判使能、fire() 求后继），
状态图用 NetworkX DiGraph 承载（节点=标识，边=变迁名）。

关键区分（本模块存在的理由）：
- “探索了 max_states 个状态没发现死锁” ≠ “无死锁”。触及上限时
  truncated=True、conclusive=False，未展开的边界状态原样保留在 frontier，
  结论只能是“已探索部分无反例”，不能当作证明。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import networkx as nx
import snakes.nets as nets

from .builder import read_marking, set_marking
from .schemas import ForbiddenSpec, NetSpec, TerminationSpec


@dataclass
class Finding:
    type: str
    severity: str           # "error" | "info"
    detail: str
    state: dict[str, int]
    trace: list[str]        # 从初始标识可重演的变迁序列

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "detail": self.detail,
            "state": self.state,
            "trace": self.trace,
        }


@dataclass
class AnalysisResult:
    states_explored: int
    transitions_fired: int
    truncated: bool
    conclusive: bool
    frontier: list[dict[str, int]]          # 触及上限时未展开的边界
    frontier_size: int                       # 边界真实大小（可能大于保留条数）
    deadlocks: list[dict[str, Any]]
    terminal: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "states_explored": self.states_explored,
            "transitions_fired": self.transitions_fired,
            "truncated": self.truncated,
            "conclusive": self.conclusive,
            "frontier_size": self.frontier_size,
            "deadlock_count": len(self.deadlocks),
            "terminal": self.terminal,
            "error_count": sum(1 for f in self.findings if f.severity == "error"),
        }


def _marking_dict(place_names: list[str], state: tuple[int, ...]) -> dict[str, int]:
    return {p: c for p, c in zip(place_names, state) if c > 0}


def _terminal_satisfied(term: TerminationSpec, marking: dict[str, int]) -> bool:
    if term.kind == "marking_eq":
        full = {p: 0 for p in marking} | marking
        target = term.marking
        keys = set(full) | set(target)
        return all(full.get(k, 0) == target.get(k, 0) for k in keys)
    # marking_cover
    return all(marking.get(p, 0) >= c for p, c in term.marking.items())


def _forbidden_violation(f: ForbiddenSpec, marking: dict[str, int]) -> Optional[str]:
    if f.kind == "co_marked":
        if all(marking.get(p, 0) >= 1 for p in f.places):
            return f"places {f.places} are simultaneously marked"
    elif f.kind == "max_tokens":
        for p in f.places:
            if marking.get(p, 0) > f.bound:
                return f"place '{p}' holds {marking[p]} tokens > bound {f.bound}"
    elif f.kind == "total_tokens":
        total = sum(marking.get(p, 0) for p in f.places)
        if total > f.bound:
            return f"places {f.places} hold {total} tokens in total > bound {f.bound}"
    return None


def explore(
    net: nets.PetriNet,
    spec: NetSpec,
    max_states: int,
    place_bound: int,
    max_frontier_kept: int = 100,
) -> AnalysisResult:
    place_names = sorted(p.name for p in spec.places)
    initial = tuple(spec.initial_marking.get(p, 0) for p in place_names)

    graph = nx.DiGraph()
    graph.add_node(initial, marking=_marking_dict(place_names, initial))

    queue: deque[tuple[int, ...]] = deque([initial])
    seen = {initial}
    truncated = False
    frontier: list[tuple[int, ...]] = []
    frontier_size = 0
    fired = 0
    bound_exceeded_states: list[tuple[int, ...]] = []

    while queue:
        if len(seen) > max_states:
            truncated = True
            frontier = list(queue)
            frontier_size = len(frontier)
            break
        state = queue.popleft()
        set_marking(net, place_names, state)
        for t in net.transition():
            modes = t.modes()
            if not modes:
                continue
            t.fire(modes[0])  # 普通网每个变迁至多一个空代换模式
            fired += 1
            succ = read_marking(net, place_names)
            set_marking(net, place_names, state)  # 恢复现场，继续枚举下一变迁
            over = [place_names[i] for i, c in enumerate(succ) if c > place_bound]
            if succ not in seen:
                seen.add(succ)
                graph.add_node(succ, marking=_marking_dict(place_names, succ))
                if over:
                    # 越出声明的 K-有界语义：记录但不展开（展开将无边无际）
                    graph.nodes[succ]["exceeds_bound"] = True
                    bound_exceeded_states.append(succ)
                else:
                    queue.append(succ)
            graph.add_edge(state, succ, transition=t.name)
    else:
        frontier = []
        frontier_size = 0

    # ---- 分析 ----
    def trace_to(target: tuple[int, ...]) -> list[str]:
        path = nx.shortest_path(graph, initial, target)
        return [graph.edges[a, b]["transition"] for a, b in zip(path, path[1:])]

    findings: list[Finding] = []

    # 1) 死锁：出度为 0 的标识。满足终止条件的死锁是“正常终态”，单独标注。
    deadlocks: list[dict[str, Any]] = []
    for node in graph.nodes:
        if graph.out_degree(node) > 0:
            continue
        marking = _marking_dict(place_names, node)
        is_terminal = spec.termination is not None and _terminal_satisfied(spec.termination, marking)
        entry = {"state": marking, "trace": trace_to(node), "is_expected_terminal": is_terminal}
        deadlocks.append(entry)
        if not is_terminal:
            findings.append(Finding(
                type="deadlock",
                severity="error",
                detail="marking with no enabled transition"
                       + (" (exploration truncated: more may exist)" if truncated else ""),
                state=marking,
                trace=entry["trace"],
            ))

    # 2) 终态可达性
    terminal_info: dict[str, Any]
    if spec.termination is not None:
        hit = next(
            (n for n in graph.nodes
             if _terminal_satisfied(spec.termination, _marking_dict(place_names, n))),
            None,
        )
        if hit is not None:
            terminal_info = {
                "status": "reachable",
                "witness_state": _marking_dict(place_names, hit),
                "witness_trace": trace_to(hit),
            }
        elif truncated or bound_exceeded_states:
            # 没搜到 ≠ 不可达：还有未展开的边界
            terminal_info = {"status": "unknown", "reason": "exploration incomplete (frontier remains)"}
            findings.append(Finding(
                type="terminal_unknown",
                severity="error",
                detail="terminal state not found in explored region; exploration incomplete",
                state={},
                trace=[],
            ))
        else:
            terminal_info = {"status": "unreachable"}
            findings.append(Finding(
                type="terminal_unreachable",
                severity="error",
                detail="terminal marking is not reachable (state space fully explored)",
                state={},
                trace=[],
            ))
    else:
        terminal_info = {"status": "not_specified"}

    # 3) 禁止状态（不允许同时出现 / 越界）
    for node in graph.nodes:
        marking = _marking_dict(place_names, node)
        for f in spec.forbidden:
            reason = _forbidden_violation(f, marking)
            if reason:
                findings.append(Finding(
                    type=f"forbidden_{f.kind}",
                    severity="error",
                    detail=(f.label + ": " if f.label else "") + reason,
                    state=marking,
                    trace=trace_to(node),
                ))

    # 4) 越出声明界：网不是声明的 K-有界网
    for node in bound_exceeded_states:
        marking = _marking_dict(place_names, node)
        findings.append(Finding(
            type="bound_exceeded",
            severity="error",
            detail=f"marking exceeds declared place_bound={place_bound}; "
                   "successors of this state were not explored",
            state=marking,
            trace=trace_to(node),
        ))

    conclusive = not truncated and not bound_exceeded_states
    return AnalysisResult(
        states_explored=len(seen),
        transitions_fired=fired,
        truncated=truncated,
        conclusive=conclusive,
        frontier=[_marking_dict(place_names, s) for s in frontier[:max_frontier_kept]],
        frontier_size=frontier_size,
        deadlocks=deadlocks,
        terminal=terminal_info,
        findings=findings,
    )


def replay(net: nets.PetriNet, spec: NetSpec, sequence: list[str]) -> dict[str, Any]:
    """在 SNAKES 上逐步重演变迁序列；任一步不使能即失败并指出步号。"""
    place_names = sorted(p.name for p in spec.places)
    set_marking(net, place_names, tuple(spec.initial_marking.get(p, 0) for p in place_names))
    steps = []
    known = {t.name for t in net.transition()}
    for i, name in enumerate(sequence):
        if name not in known:
            return {"ok": False, "failed_at": i, "reason": f"unknown transition '{name}'", "steps": steps}
        t = net.transition(name)
        modes = t.modes()
        if not modes:
            return {
                "ok": False,
                "failed_at": i,
                "reason": f"transition '{name}' is not enabled at step {i}",
                "steps": steps,
            }
        t.fire(modes[0])
        steps.append({
            "step": i,
            "transition": name,
            "marking": _marking_dict(place_names, read_marking(net, place_names)),
        })
    return {"ok": True, "steps": steps}
