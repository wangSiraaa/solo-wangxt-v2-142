"""End-to-end HTTP API tests against PostgreSQL."""
from app.fixtures import (circular_wait_deadlock, resource_race_no_mutex,
                          resource_race_with_lock, unbounded_loop,
                          parallel_join_missing_token)


def post(client, path, body=None, params=None):
    return client.post(path, json=body, params=params or {})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "postgresql"


def test_invalid_model_returns_422(client):
    bad = {
        "name": "bad",
        "places": [{"name": "p", "capacity": 1}],
        "transitions": [{"name": "t"}],
        "inputs": [{"place": "ghost", "transition": "t"}],
        "outputs": [],
        "initial_marking": {},
    }
    r = post(client, "/models", bad)
    assert r.status_code == 422
    assert r.json()["error"] == "UNKNOWN_PLACE"


def test_full_diagnosis_flow_join_missing_token(client):
    body = parallel_join_missing_token()
    r = post(client, "/models", body)
    assert r.status_code == 201
    v = r.json()
    assert v["version_number"] == 1

    r = post(client, f"/models/{body['name']}/checks",
             params={"version_id": v["version_id"]})
    assert r.status_code == 201
    chk = r.json()
    assert chk["verdict"] == "COUNTEREXAMPLE"

    full = client.get(f"/checks/{chk['check_run_id']}").json()["result"]
    seq = full["primary_witness"]["transition_sequence"]

    r = client.post(f"/checks/{chk['check_run_id']}/replay", json={})
    assert r.status_code == 200
    rp = r.json()
    assert rp["valid"] and rp["is_dead_end"]
    assert [s["transition"] for s in rp["steps"]] == seq

    # graph persisted with the dead end flagged
    g = client.get(f"/checks/{chk['check_run_id']}/graph").json()
    assert g["nodes_total"] == 2
    assert any(n["is_dead_end"] for n in g["nodes"])


def test_truncated_run_persists_frontier_and_boundary(client):
    body = unbounded_loop()
    v = post(client, "/models", body).json()
    chk = post(client, f"/models/{body['name']}/checks",
               {"max_states": 8}, {"version_id": v["version_id"]}).json()
    assert chk["verdict"] == "TRUNCATED"

    fr = client.get(f"/checks/{chk['check_run_id']}/frontier").json()
    assert fr["truncated"] is True
    assert fr["frontier"] == [{"p": 7}]
    assert fr["boundary_edges"][0]["target"] == {"p": 8}

    g = client.get(f"/checks/{chk['check_run_id']}/graph").json()
    frontier_nodes = [n for n in g["nodes"] if n["on_frontier"]]
    assert len(frontier_nodes) == 1
    assert frontier_nodes[0]["expanded"] is False


def test_check_requires_explicit_version(client):
    body = unbounded_loop()
    post(client, "/models", body)
    r = post(client, f"/models/{body['name']}/checks", {})
    assert r.status_code == 400


def test_changed_model_blocks_old_conclusion_and_publishes_new(client):
    buggy = resource_race_no_mutex()
    v1 = post(client, "/models", buggy).json()
    chk1 = post(client, f"/models/{buggy['name']}/checks",
                params={"version_id": v1["version_id"]}).json()
    assert chk1["verdict"] == "COUNTEREXAMPLE"

    fixed = resource_race_with_lock()
    fixed["name"] = buggy["name"]
    v2 = post(client, "/models", fixed).json()
    assert v2["version_number"] == 2 and v2["changed"] is True

    # v2 cannot be published using v1's check run
    r = post(client, f"/models/{buggy['name']}/versions/"
                     f"{v2['version_id']}/publish",
             {"check_run_id": chk1["check_run_id"]})
    assert r.status_code == 409

    # v1's counterexample cannot be force-published silently
    r = post(client, f"/models/{buggy['name']}/versions/"
                     f"{v1['version_id']}/publish",
             {"check_run_id": chk1["check_run_id"]})
    assert r.status_code == 200 and r.json()["published"] is False

    # the new version's own PROVED run passes the gate
    chk2 = post(client, f"/models/{buggy['name']}/checks",
                params={"version_id": v2["version_id"]}).json()
    assert chk2["verdict"] == "PROVED"
    r = post(client, f"/models/{buggy['name']}/versions/"
                     f"{v2['version_id']}/publish",
             {"check_run_id": chk2["check_run_id"]})
    assert r.status_code == 200 and r.json()["published"] is True
    assert r.json()["bound_content_hash"] == r.json()["content_hash"]

    # the model record points at v2, and v2 is published, v1 is not
    info = client.get(f"/models/{buggy['name']}").json()
    assert info["current_version"]["version_id"] == v2["version_id"]
    assert info["current_version"]["publication"] is not None


def test_unchanged_body_is_idempotent_version(client):
    body = circular_wait_deadlock()
    v1 = post(client, "/models", body).json()
    v_again = post(client, "/models", body).json()
    assert v_again["changed"] is False
    assert v_again["version_id"] == v1["version_id"]


def test_truncated_publish_requires_acknowledgement(client):
    body = unbounded_loop()
    v = post(client, "/models", {**body, "name": "truncpub"}).json()
    chk = post(client, "/models/truncpub/checks",
               {"max_states": 5}, {"version_id": v["version_id"]}).json()
    r = post(client, "/models/truncpub/versions/"
                     f"{v['version_id']}/publish",
             {"check_run_id": chk["check_run_id"]})
    assert r.json()["published"] is False
    r = post(client, "/models/truncpub/versions/"
                     f"{v['version_id']}/publish",
             {"check_run_id": chk["check_run_id"],
              "acknowledge": "exploration-truncated"})
    assert r.json()["published"] is True


def test_engines_agree_on_all_stored_checks(client):
    body = resource_race_with_lock()
    v = post(client, "/models", body).json()
    chk = post(client, f"/models/{body['name']}/checks",
               params={"version_id": v["version_id"]}).json()
    full = client.get(f"/checks/{chk['check_run_id']}").json()["result"]
    assert full["engine_cross_check"]["agreement"] is True
    assert full["engine_cross_check"]["problems"] == []
