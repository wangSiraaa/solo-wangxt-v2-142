"""规范校验 + SNAKES 网构建。

校验分三层，全部问题一次性收集返回（422）：
1. 初始标识：只引用已声明库所、计数非负、不超过 place_bound；
2. 变迁输入/输出：只引用已声明库所、权重为正（schema 层）、变迁不得无输入亦无输出；
3. 终止条件与禁止约束：只引用已声明库所、计数不越界。
"""
from __future__ import annotations

import snakes.nets as nets

from .schemas import NetSpec


def validate_spec(spec: NetSpec) -> list[str]:
    issues: list[str] = []
    place_names = [p.name for p in spec.places]
    place_set = set(place_names)

    dup_places = {n for n in place_names if place_names.count(n) > 1}
    if dup_places:
        issues.append(f"duplicate place names: {sorted(dup_places)}")

    t_names = [t.name for t in spec.transitions]
    dup_trans = {n for n in t_names if t_names.count(n) > 1}
    if dup_trans:
        issues.append(f"duplicate transition names: {sorted(dup_trans)}")

    # 初始标识
    for place, count in spec.initial_marking.items():
        if place not in place_set:
            issues.append(f"initial_marking references unknown place '{place}'")
        elif not isinstance(count, int) or count < 0:
            issues.append(f"initial_marking['{place}'] must be a non-negative integer, got {count!r}")
        elif count > spec.place_bound:
            issues.append(
                f"initial_marking['{place}']={count} exceeds declared place_bound={spec.place_bound}"
            )

    # 变迁输入/输出
    for t in spec.transitions:
        if not t.inputs and not t.outputs:
            issues.append(f"transition '{t.name}' has neither inputs nor outputs")
        for place in t.inputs:
            if place not in place_set:
                issues.append(f"transition '{t.name}' input references unknown place '{place}'")
        for place in t.outputs:
            if place not in place_set:
                issues.append(f"transition '{t.name}' output references unknown place '{place}'")

    # 终止条件
    if spec.termination is not None:
        for place, count in spec.termination.marking.items():
            if place not in place_set:
                issues.append(f"termination references unknown place '{place}'")
            elif count > spec.place_bound:
                issues.append(
                    f"termination['{place}']={count} exceeds declared place_bound={spec.place_bound}"
                )

    # 禁止约束
    for i, f in enumerate(spec.forbidden):
        tag = f.label or f"forbidden[{i}]({f.kind})"
        for place in f.places:
            if place not in place_set:
                issues.append(f"{tag} references unknown place '{place}'")
        if f.kind in ("max_tokens", "total_tokens") and not f.places:
            issues.append(f"{tag} must list at least one place")
        if f.kind == "co_marked" and len(f.places) < 2:
            issues.append(f"{tag}: co_marked needs at least two places")

    return issues


def build_net(spec: NetSpec) -> nets.PetriNet:
    """按普通有界网语义构建 SNAKES 网：dot 令牌，权重弧展开为 MultiArc。"""
    net = nets.PetriNet(spec.name)
    for p in spec.places:
        net.add_place(nets.Place(p.name, [nets.dot] * spec.initial_marking.get(p.name, 0)))
    for t in spec.transitions:
        net.add_transition(nets.Transition(t.name))
        for place, w in t.inputs.items():
            arc = nets.Value(nets.dot) if w == 1 else nets.MultiArc([nets.Value(nets.dot)] * w)
            net.add_input(place, t.name, arc)
        for place, w in t.outputs.items():
            arc = nets.Value(nets.dot) if w == 1 else nets.MultiArc([nets.Value(nets.dot)] * w)
            net.add_output(place, t.name, arc)
    return net


def set_marking(net: nets.PetriNet, place_names: list[str], state: tuple[int, ...]) -> None:
    """把 SNAKES 网重置到给定标识（state 与 place_names 对齐）。"""
    for name, count in zip(place_names, state):
        net.place(name).reset([nets.dot] * count)


def read_marking(net: nets.PetriNet, place_names: list[str]) -> tuple[int, ...]:
    return tuple(len(net.place(name).tokens) for name in place_names)
