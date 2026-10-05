"""Unit tests: compilation, semantics, validation and SNAKES cross-check."""
import pytest

from app.petrinet import ModelError, compile_model, cross_check_sample, \
    snakes_successors
from app.schemas import ModelDefinitionIn


def m(body):
    return ModelDefinitionIn.model_validate(body)


BASE = {
    "name": "n",
    "places": [{"name": "p"}, {"name": "q"}, {"name": "r"}],
    "transitions": [{"name": "t"}],
    "inputs": [{"place": "p", "transition": "t"}],
    "outputs": [{"place": "q", "transition": "t"}],
    "initial_marking": {"p": 1},
}


def test_basic_enable_fire():
    net = compile_model(m(BASE))
    assert net.enabled(net.initial, "t")
    s = net.fire(net.initial, "t")
    assert net.as_dict(s) == {"q": 1}
    assert net.successors(net.initial) == [("t", s)]
    # after firing t, nothing is enabled (dead end)
    assert net.successors(s) == []


def test_weighted_arcs():
    body = {
        "name": "w",
        "places": [{"name": "p", "capacity": 5}, {"name": "q"}],
        "transitions": [{"name": "t"}],
        "inputs": [{"place": "p", "transition": "t", "weight": 2}],
        "outputs": [{"place": "q", "transition": "t", "weight": 3}],
        "initial_marking": {"p": 2},
    }
    net = compile_model(m(body))
    s = net.fire(net.initial, "t")
    assert net.as_dict(s) == {"q": 3}
    # only one token -> not enabled
    assert not net.enabled((1, 0), "t")


def test_capacity_disables_firing():
    body = {
        "name": "cap",
        "places": [{"name": "p", "capacity": 1},
                   {"name": "q", "capacity": 1}],
        "transitions": [{"name": "t"}],
        "inputs": [{"place": "p", "transition": "t"}],
        "outputs": [{"place": "q", "transition": "t"}],
        "initial_marking": {"p": 1, "q": 1},
    }
    net = compile_model(m(body))
    # q already at capacity 1 -> t would overfill, disabled
    assert not net.enabled(net.initial, "t")
    assert net.successors(net.initial) == []


def test_source_transition_enabled_everywhere():
    body = {
        "name": "src",
        "places": [{"name": "p"}],
        "transitions": [{"name": "gen"}],
        "inputs": [],
        "outputs": [{"place": "p", "transition": "gen"}],
        "initial_marking": {},
    }
    net = compile_model(m(body))
    assert net.enabled(net.initial, "gen")
    s = net.fire(net.initial, "gen")
    assert net.as_dict(s) == {"p": 1}
    # SNAKES reports the source transition too (with the empty-binding fix)
    assert snakes_successors(net, net.initial) == [("gen", s)]


def test_initial_over_capacity_rejected():
    body = dict(BASE, places=[{"name": "p", "capacity": 1},
                              {"name": "q"}, {"name": "r"}],
                initial_marking={"p": 2})
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "INITIAL_OVER_CAPACITY"


def test_unknown_place_in_arcs_rejected():
    body = dict(BASE, inputs=[{"place": "nope", "transition": "t"}])
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "UNKNOWN_PLACE"


def test_parallel_arcs_rejected():
    body = dict(BASE, inputs=[
        {"place": "p", "transition": "t"},
        {"place": "p", "transition": "t"}])
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "PARALLEL_ARCS"


def test_duplicate_names_rejected():
    body = dict(BASE, places=[{"name": "p"}, {"name": "p"}])
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "DUPLICATE_PLACE"


def test_negative_initial_rejected():
    body = dict(BASE, initial_marking={"p": -1})
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "NEGATIVE_MARKING"


def test_empty_net_rejected():
    body = dict(BASE, places=[])
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "EMPTY_NET"


def test_exclusive_group_validation():
    body = dict(BASE, goals={
        "terminal_markings": [],
        "exclusive_groups": [["p", "ghost"]],
    })
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "UNKNOWN_PLACE"

    body = dict(BASE, goals={
        "terminal_markings": [],
        "exclusive_groups": [["p"]],
    })
    with pytest.raises(ModelError) as e:
        compile_model(m(body))
    assert e.value.code == "BAD_EXCLUSIVE_GROUP"


def test_fast_engine_matches_snakes_on_sampled_states():
    from app.fixtures import circular_wait_deadlock, resource_race_with_lock
    for factory in (circular_wait_deadlock, resource_race_with_lock):
        net = compile_model(m(factory()))
        states = [net.initial]
        # BFS collect every state
        seen, queue = {net.initial}, [net.initial]
        while queue:
            cur = queue.pop()
            for _, nxt in net.successors(cur):
                if nxt not in seen:
                    seen.add(nxt)
                    states.append(nxt)
                    queue.append(nxt)
        problems = cross_check_sample(net, states, cap=len(states))
        assert problems == [], problems
