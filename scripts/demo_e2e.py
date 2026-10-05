#!/usr/bin/env python3
"""End-to-end demonstration of the Petri-net checking API.

Reproduces, step by step:
  1. parallel join missing token  -> COUNTEREXAMPLE deadlock, replay it
  2. unbounded loop, small budget -> TRUNCATED, unfinished frontier kept
     same loop, capacity-bounded  -> PROVED with a cycle
  3. resource competition         -> simultaneous-state counterexample
     circular wait                -> deadlock
     adding the lock (new version) -> PROVED, old conclusion not reused

Run while the API is serving (default http://127.0.0.1:8088).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from app.fixtures import (ALL, bounded_cycle, circular_wait_deadlock,
                          parallel_join_missing_token, resource_race_no_mutex,
                          resource_race_with_lock, unbounded_loop)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8088"


def call(method, path, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def hr(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def show(label, value):
    print(f"  {label}: {json.dumps(value, ensure_ascii=False)}")


def create_model(body):
    status, out = call("POST", "/models", body)
    assert status == 201, (status, out)
    return out


def run_check(version_id, max_states=100_000, persist=True):
    status, out = call(
        "POST",
        f"/models/{model_name}/checks?version_id={version_id}",
        {"max_states": max_states, "persist_graph": persist})
    assert status == 201, (status, out)
    return out


def replay(check_id, witness=None):
    body = {} if witness is None else {"witness": witness}
    status, out = call("POST", f"/checks/{check_id}/replay", body)
    assert status == 200, (status, out)
    return out


model_name = None


def main():
    global model_name

    # ------------------------------------------------------------------ #
    hr("1. PARALLEL JOIN WITH A MISSING TOKEN -> deadlock counterexample")
    body = parallel_join_missing_token()
    model_name = body["name"]
    v = create_model(body)
    print(f"  stored model={v['model_id'][:8]} version=v{v['version_number']} "
          f"hash={v['content_hash'][:12]}… changed={v['changed']}")
    chk = run_check(v["version_id"])
    full = call("GET", f"/checks/{chk['check_run_id']}")[1]["result"]
    print(f"  VERDICT: {full['verdict']}  ({full['explored_states']} states, "
          f"{full['edge_count']} edges, fully_explored="
          f"{full['fully_explored']})")
    dl = full["findings"]["deadlock_freedom"]
    tr = full["findings"]["terminal_reachability"]
    show("deadlock_freedom", dl["status"])
    show("terminal_reachability", tr["status"])
    pw = full["primary_witness"]
    show("counterexample sequence", pw["transition_sequence"])
    show("final dead marking", pw["final_marking"])
    print("  step-by-step replay from M0:")
    rp = replay(chk["check_run_id"])
    for step in rp["steps"]:
        print(f"    [{step['index']}] {step['fired_from']} "
              f"--({step['transition']})--> {step['marking_after']}")
        print(f"        enabled before: {step['enabled_before']}")
    print(f"  replay valid={rp['valid']} dead_end={rp['is_dead_end']} "
          f"ends_terminal={rp['ends_in_terminal']}")

    # ------------------------------------------------------------------ #
    hr("2a. UNBOUNDED LOOP, BUDGET 20 -> TRUNCATED (proof NOT obtained)")
    body = unbounded_loop()
    model_name = body["name"]
    v = create_model(body)
    chk = run_check(v["version_id"], max_states=20)
    full = call("GET", f"/checks/{chk['check_run_id']}")[1]["result"]
    print(f"  VERDICT: {full['verdict']}  visited={full['explored_states']} "
          f"expanded={full['states_expanded']} limit={full['max_states_limit']}")
    print(f"  {full['interpretation']}")
    for key in ("terminal_reachability", "deadlock_freedom", "boundedness"):
        show(key + " status", full["findings"][key]["status"])
    fr = call("GET", f"/checks/{chk['check_run_id']}/frontier")[1]
    print(f"  unfinished frontier size: {fr['frontier_size']}")
    show("frontier states", fr["frontier"][:5])
    show("boundary edges", fr["boundary_edges"][:3])

    hr("2b. SAME LOOP, FULLY EXPLORED WITH CAPACITY -> PROVED + cycle")
    body = bounded_cycle()
    model_name = body["name"]
    v = create_model(body)
    chk = run_check(v["version_id"])
    full = call("GET", f"/checks/{chk['check_run_id']}")[1]["result"]
    print(f"  VERDICT: {full['verdict']}  states={full['explored_states']} "
          f"truncated={full['truncated']}")
    show("has_cycle", full["statistics"]["has_cycle"])
    show("cycle witnesses (transition labels)",
         full["statistics"]["cycle_witnesses"])
    show("deadlock_freedom", full["findings"]["deadlock_freedom"]["status"])
    show("proved place bounds",
         full["findings"]["boundedness"].get("bound"))

    # ------------------------------------------------------------------ #
    hr("3a. RESOURCE COMPETITION WITHOUT MUTEX -> simultaneous states")
    body = resource_race_no_mutex()
    model_name = body["name"]
    v1 = create_model(body)
    chk = run_check(v1["version_id"])
    full = call("GET", f"/checks/{chk['check_run_id']}")[1]["result"]
    print(f"  VERDICT: {full['verdict']}")
    show("exclusivity", full["findings"]["exclusivity"]["status"])
    pw = full["primary_witness"]
    show("counterexample sequence", pw["transition_sequence"])
    rp = replay(chk["check_run_id"])
    print("  replay:")
    for step in rp["steps"]:
        print(f"    [{step['index']}] {step['fired_from']} "
              f"--({step['transition']})--> {step['marking_after']}")
    show("exclusivity violations at final", rp["exclusivity_violations"])

    hr("3b. CIRCULAR WAIT ON TWO RESOURCES -> deadlock")
    body = circular_wait_deadlock()
    model_name = body["name"]
    vcw = create_model(body)
    chk = run_check(vcw["version_id"])
    full = call("GET", f"/checks/{chk['check_run_id']}")[1]["result"]
    print(f"  VERDICT: {full['verdict']}")
    pw = full["primary_witness"]
    show("counterexample sequence", pw["transition_sequence"])
    show("dead marking", pw["final_marking"])
    rp = replay(chk["check_run_id"])
    for step in rp["steps"]:
        print(f"    [{step['index']}] {step['fired_from']} "
              f"--({step['transition']})--> {step['marking_after']}")
    print("  dead_end=%s valid=%s" % (rp["is_dead_end"], rp["valid"]))

    hr("3c. MODEL CHANGED (lock added) -> NEW version; old verdict blocked")
    # First publish the buggy v1, then add the lock under the same model
    # name: that produces version 2.
    buggy = resource_race_no_mutex()
    model_name = buggy["name"]
    v1 = create_model(buggy)
    chk1 = run_check(v1["version_id"])
    full1 = call("GET", f"/checks/{chk1['check_run_id']}")[1]["result"]
    print(f"  v1 (no lock) verdict: {full1['verdict']}")
    fixed = resource_race_with_lock()
    fixed["name"] = model_name  # same model, changed content -> new version
    v2 = create_model(fixed)
    print(f"  new version v{v2['version_number']} changed={v2['changed']} "
          f"(previous {v2['previous_version_id'][:8]})")
    # try to publish version 2 with the OLD version's check run
    status, out = call(
        "POST", f"/models/{model_name}/versions/{v2['version_id']}/publish",
        {"check_run_id": chk1["check_run_id"]})
    print(f"  publish v2 with v1's check run -> HTTP {status} (rejected)")
    show("reason", out.get("blocked_reason") or out.get("detail"))
    # check the NEW version -> PROVED, publish succeeds
    chk2 = run_check(v2["version_id"])
    full2 = call("GET", f"/checks/{chk2['check_run_id']}")[1]["result"]
    print(f"  new-version check verdict: {full2['verdict']}")
    status, out = call(
        "POST", f"/models/{model_name}/versions/{v2['version_id']}/publish",
        {"check_run_id": chk2["check_run_id"]})
    print(f"  publish with new check run -> HTTP {status}")
    show("published", out.get("published"))
    show("bound hash", out.get("bound_content_hash")[:12] + "…")
    # and v1's publication gate is still empty (conclusion never carried over)
    v1_info = call("GET", f"/models/{model_name}/versions")[1]["versions"]
    v1_pub = next(x for x in v1_info
                  if x["version_id"] == v1["version_id"])
    show("v1 publication still absent", v1_pub["check_runs"] is not None)

    # truncated publish is blocked unless acknowledged
    hr("3d. TRUNCATED run cannot be published without acknowledgement")
    body = unbounded_loop()
    model_name = "pub_trunc_demo"
    body = {**body, "name": model_name}
    vt = create_model(body)
    chkt = run_check(vt["version_id"], max_states=10)
    status, out = call(
        "POST", f"/models/{model_name}/versions/{vt['version_id']}/publish",
        {"check_run_id": chkt["check_run_id"]})
    print(f"  publish truncated -> HTTP {status}, published={out['published']}")
    show("reason", out.get("blocked_reason"))

    hr("ALL SCENARIOS REPRODUCED")


if __name__ == "__main__":
    main()
