"""Tests for BFS verdicts: counterexamples, truncation boundary, proofs."""
from app.explorer import explore
from app.petrinet import compile_model
from app.schemas import ModelDefinitionIn
from app.checker import build_report
from app.fixtures import (bounded_cycle, circular_wait_deadlock,
                          parallel_join_missing_token, resource_race_no_mutex,
                          resource_race_with_lock, unbounded_loop)
from app.petrinet import cross_check_sample
from app.replay import replay


def run(body, max_states=100_000):
    net = compile_model(ModelDefinitionIn.model_validate(body))
    result = explore(net, max_states=max_states)
    probs = cross_check_sample(net, list(result.graph.nodes), cap=1000)
    report = build_report(result, probs)
    assert probs == []
    return net, result, report


def test_join_missing_token_is_deadlock_counterexample():
    net, result, report = run(parallel_join_missing_token())
    assert report["verdict"] == "COUNTEREXAMPLE"
    assert report["findings"]["deadlock_freedom"]["status"] == "VIOLATED"
    assert report["findings"]["terminal_reachability"]["status"] == "VIOLATED"
    seq = report["primary_witness"]["transition_sequence"]
    assert seq == ["fork"]
    # replay reproduces the dead end from M0
    rp = replay(net, seq, "deadlock", "c", "v")
    assert rp.valid and rp.is_dead_end and not rp.ends_in_terminal


def test_unbounded_loop_truncation_keeps_boundary():
    net, result, report = run(unbounded_loop(), max_states=15)
    assert report["verdict"] == "TRUNCATED"
    assert report["truncated"] is True
    assert report["explored_states"] == 15
    # nothing is proved on a truncated run
    assert report["findings"]["deadlock_freedom"]["status"] == "UNKNOWN"
    assert report["findings"]["boundedness"]["status"] == "UNKNOWN"
    # unfinished boundary retained
    trunc = report["truncation"]
    assert trunc["frontier"] == [{"p": 14}]
    assert trunc["boundary_edges"][0]["target"] == {"p": 15}
    assert trunc["boundary_edges"][0]["target_already_visited"] is False


def test_truncated_and_proved_are_different_verdicts():
    """The central distinction: N states without a deadlock != proved."""
    _, _, small = run(unbounded_loop(), max_states=10)
    _, _, tiny = run(unbounded_loop(), max_states=2)
    assert small["verdict"] == tiny["verdict"] == "TRUNCATED"
    assert small["fully_explored"] is False

    # bounded cycle is finite -> genuinely proved despite having a cycle
    net, result, report = run(bounded_cycle())
    assert report["verdict"] == "PROVED"
    assert report["statistics"]["has_cycle"] is True
    assert report["findings"]["deadlock_freedom"]["status"] == "HELD"
    assert report["findings"]["boundedness"]["status"] == "HELD"


def test_resource_race_mutual_exclusion_violation_and_repair():
    net, result, report = run(resource_race_no_mutex())
    assert report["verdict"] == "COUNTEREXAMPLE"
    assert report["findings"]["exclusivity"]["status"] == "VIOLATED"
    seq = report["primary_witness"]["transition_sequence"]
    assert seq == ["acq1", "acq2"]
    rp = replay(net, seq, "exclusivity", "c", "v")
    assert rp.valid
    assert rp.exclusivity_violations == [["crit1", "crit2"]]

    net2, result2, report2 = run(resource_race_with_lock())
    assert report2["verdict"] == "PROVED"
    assert report2["findings"]["exclusivity"]["status"] == "HELD"


def test_circular_wait_deadlock_replayable():
    net, result, report = run(circular_wait_deadlock())
    assert report["verdict"] == "COUNTEREXAMPLE"
    seq = report["primary_witness"]["transition_sequence"]
    assert seq == ["p1_take_r1", "p2_take_r2"]
    rp = replay(net, seq, "deadlock", "c", "v")
    assert rp.valid and rp.is_dead_end
    assert rp.final_marking == {"p1_holds_r1": 1, "p2_holds_r2": 1}


def test_terminal_marking_is_not_a_deadlock():
    body = {
        "name": "proper_end",
        "places": [{"name": "a", "capacity": 1},
                   {"name": "b", "capacity": 1}],
        "transitions": [{"name": "t"}],
        "inputs": [{"place": "a", "transition": "t"}],
        "outputs": [{"place": "b", "transition": "t"}],
        "initial_marking": {"a": 1},
        "goals": {"terminal_markings": [
            {"marking": {"b": 1}, "match": "exact"}]},
    }
    net, result, report = run(body)
    assert report["verdict"] == "PROVED"
    assert report["findings"]["deadlock_freedom"]["status"] == "HELD"
    assert report["findings"]["terminal_reachability"]["status"] == "HELD"


def test_initial_state_can_be_a_witness():
    body = {
        "name": "init_bad",
        "places": [{"name": "a"}, {"name": "b"}],
        "transitions": [{"name": "t"}],
        "inputs": [],
        "outputs": [],
        "initial_marking": {"a": 1, "b": 1},
        "goals": {"terminal_markings": [],
                  "require_terminal_reachable": False,
                  "exclusive_groups": [["a", "b"]]},
    }
    net, result, report = run(body)
    assert report["verdict"] == "COUNTEREXAMPLE"
    assert report["primary_witness"]["transition_sequence"] == []
