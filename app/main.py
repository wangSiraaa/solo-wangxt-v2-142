"""FastAPI entry point for the Petri-net checking service.

Endpoints
---------
POST   /models                       create a named model / a new immutable
                                     version when the body changed
GET    /models/{name}                model + current version summary
GET    /models/{name}/versions       all versions (newest first)
POST   /models/{name}/checks         run a check on an EXPLICIT version
GET    /checks/{id}                  full stored check report
GET    /checks/{id}/graph            explored state graph (paged nodes/edges)
GET    /checks/{id}/frontier         unfinished boundary of a truncated run
POST   /checks/{id}/replay           replay (any) witness sequence stepwise
POST   /models/{name}/versions/{vid}/publish
                                     release gate; binds to one exact version
GET    /health                       DB + engine readiness (no UI)
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as database
from .checker import build_report
from .explorer import explore
from .petrinet import ModelError, compile_model, cross_check_sample
from .replay import replay
from .schemas import (CheckCreated, CheckOptions, ModelDefinitionIn,
                      PublishBody, PublishReport, ReplayReport,
                      VersionCreated)

app = FastAPI(
    title="Petri-net checking API",
    version="1.0.0",
    description=("Ordinary bounded P/T-net verification: initial marking, "
                 "transition inputs/outputs, terminal reachability, "
                 "deadlocks and mutually-exclusive states. No HTML/UI."),
)


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


@app.exception_handler(ModelError)
async def model_error_handler(_request, exc: ModelError):
    return JSONResponse(status_code=422,
                        content={"error": exc.code, "message": exc.message,
                                 "detail": exc.detail})


def _content_hash(definition: Dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _definition_dict(body: ModelDefinitionIn) -> Dict[str, Any]:
    # dump through the validated schema so server-side canonicalisation is
    # independent of the client's key order / missing optionals
    return json.loads(body.model_dump_json(by_alias=True))


# ---------------------------------------------------------------------------
# Models & versions
# ---------------------------------------------------------------------------


@app.post("/models", response_model=VersionCreated, status_code=201)
def create_model_version(body: ModelDefinitionIn,
                         db: Session = Depends(database.get_session)
                         ) -> VersionCreated:
    """Create a model or append a version.

    Structural/semantic validation happens here (via SNAKES too); an invalid
    net is rejected before anything is stored.  Same name + same content is
    idempotent and returns the existing version with ``changed=false``.
    """
    # Validate first; raises ModelError -> 422 (also builds it in SNAKES).
    compile_model(body)

    definition = _definition_dict(body)
    content_hash = _content_hash(definition)

    model = db.scalars(
        select(database.Model).where(database.Model.name == body.name)
    ).first()

    if model is None:
        model_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        model = database.Model(model_id=model_id, name=body.name,
                               current_version_id=version_id)
        db.add(model)
        db.flush()
        version = database.Version(
            version_id=version_id, model_id=model_id, version_number=1,
            content_hash=content_hash, definition=definition)
        db.add(version)
        db.commit()
        return VersionCreated(model_id=model_id, version_id=version_id,
                              version_number=1, content_hash=content_hash,
                              changed=True, previous_version_id=None)

    latest = db.get(database.Version, model.current_version_id)
    if latest.content_hash == content_hash:
        return VersionCreated(
            model_id=model.model_id, version_id=latest.version_id,
            version_number=latest.version_number, content_hash=content_hash,
            changed=False, previous_version_id=None)

    version_id = str(uuid.uuid4())
    version = database.Version(
        version_id=version_id, model_id=model.model_id,
        version_number=latest.version_number + 1,
        content_hash=content_hash, definition=definition)
    db.add(version)
    model.current_version_id = version_id
    db.commit()
    return VersionCreated(
        model_id=model.model_id, version_id=version_id,
        version_number=latest.version_number + 1,
        content_hash=content_hash, changed=True,
        previous_version_id=latest.version_id)


def _get_model_or_404(db: Session, name: str) -> database.Model:
    model = db.scalars(
        select(database.Model).where(database.Model.name == name)
    ).first()
    if model is None:
        raise HTTPException(404, f"model {name!r} not found")
    return model


@app.get("/models/{name}")
def get_model(name: str, db: Session = Depends(database.get_session)):
    model = _get_model_or_404(db, name)
    current = db.get(database.Version, model.current_version_id)
    publication = db.scalars(
        select(database.Publication)
        .where(database.Publication.version_id == current.version_id)
    ).first()
    return {
        "model_id": model.model_id,
        "name": model.name,
        "current_version": {
            "version_id": current.version_id,
            "version_number": current.version_number,
            "content_hash": current.content_hash,
            "created_at": current.created_at.isoformat(),
            "publication": None if publication is None else {
                "publication_id": publication.publication_id,
                "check_run_id": publication.check_run_id,
                "bound_content_hash": publication.bound_content_hash,
                "verdict": publication.verdict,
                "acknowledged": publication.acknowledged,
            },
        },
    }


@app.get("/models/{name}/versions")
def list_versions(name: str, db: Session = Depends(database.get_session)):
    model = _get_model_or_404(db, name)
    versions = db.scalars(
        select(database.Version)
        .where(database.Version.model_id == model.model_id)
        .order_by(database.Version.version_number.desc())
    ).all()
    out = []
    for v in versions:
        runs = db.execute(
            select(database.CheckRun.check_run_id,
                   database.CheckRun.verdict,
                   database.CheckRun.created_at)
            .where(database.CheckRun.version_id == v.version_id)
            .order_by(database.CheckRun.created_at.desc())
        ).all()
        out.append({
            "version_id": v.version_id,
            "version_number": v.version_number,
            "content_hash": v.content_hash,
            "created_at": v.created_at.isoformat(),
            "is_current": v.version_id == model.current_version_id,
            "check_runs": [
                {"check_run_id": r.check_run_id, "verdict": r.verdict,
                 "created_at": r.created_at.isoformat()} for r in runs],
        })
    return {"model_id": model.model_id, "name": name, "versions": out}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@app.post("/models/{name}/checks", response_model=CheckCreated,
          status_code=201)
def create_check(name: str, options: Optional[CheckOptions] = None,
                 version_id: Optional[str] = Query(default=None),
                 db: Session = Depends(database.get_session)
                 ) -> CheckCreated:
    """Run a check against an EXPLICIT model version.

    ``version_id`` must be given: conclusions are never attached to a
    moving "latest" alias.  Omitting it is an error (use
    GET /models/{name} to learn the current id).
    """
    model = _get_model_or_404(db, name)
    if version_id is None:
        raise HTTPException(
            400, "version_id query parameter is required; checks always bind "
                 "to one exact immutable version")
    version = db.get(database.Version, version_id)
    if version is None or version.model_id != model.model_id:
        raise HTTPException(404, "version not found for this model")

    options = options or CheckOptions()
    compiled = compile_model(ModelDefinitionIn.model_validate(version.definition))

    check_id = str(uuid.uuid4())
    try:
        result = explore(compiled, max_states=options.max_states)

        # Independent engine agreement: initial state, all states when small,
        # otherwise a deterministic random sample.
        sample_cap = 500
        states = list(result.graph.nodes)
        problems = cross_check_sample(compiled, states, cap=sample_cap)
        result.cross_check_problems = problems

        report = build_report(result, cross_check_problems=problems)

        run = database.CheckRun(
            check_run_id=check_id, version_id=version.version_id,
            status=report["status"], verdict=report["verdict"],
            explored_states=report["explored_states"],
            edge_count=report["edge_count"],
            max_states_limit=options.max_states,
            truncated=report["truncated"], result=report)
        db.add(run)
        db.flush()

        if options.persist_graph:
            _persist_graph(db, check_id, result)

        db.commit()
        return CheckCreated(
            check_run_id=check_id, version_id=version.version_id,
            status=report["status"], verdict=report["verdict"],
            explored_states=report["explored_states"],
            truncated=report["truncated"], findings=report["findings"])
    except ModelError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        db.rollback()
        run = database.CheckRun(
            check_run_id=check_id, version_id=version.version_id,
            status="error", verdict="INVALID", explored_states=0,
            edge_count=0, max_states_limit=options.max_states,
            truncated=False, result=None, error={"message": str(exc)})
        db.add(run)
        db.commit()
        raise HTTPException(500, f"check failed: {exc}")


def _persist_graph(db: Session, check_id: str, result) -> None:
    """Bulk-insert the explored graph and the truncation boundary."""
    net = result.net
    nodes = list(result.graph.nodes)
    # state_index follows BFS discovery order: initial is 0
    index_of = {m: i for i, m in enumerate(nodes)}
    frontier_set = set(result.frontier)

    node_rows = [
        database.StateNode(
            check_run_id=check_id,
            state_index=index_of[m],
            marking=net.as_dict(m), depth=result.depths[m],
            is_terminal=bool(result.graph.nodes[m]["terminal"]),
            is_dead_end=bool(result.graph.nodes[m]["dead_end"]),
            expanded=m in result.expanded,
            on_frontier=m in frontier_set,
            exclusivity_violations=(
                result.graph.nodes[m]["exclusivity_violations"]),
        )
        for m in nodes
    ]
    db.bulk_save_objects(node_rows)

    edge_rows = []
    in_graph_pairs = {(u, v) for u, v in result.graph.edges}
    for u, v, data in result.graph.edges(data=True):
        edge_rows.append(database.StateEdge(
            check_run_id=check_id,
            src_index=index_of[u], dst_index=index_of[v],
            transition=data["transition"], is_boundary=False))
    for s, tname, d, _seen in result.boundary_edges:
        if d in index_of and (s, d) in in_graph_pairs:
            continue  # already stored as an in-graph edge
        edge_rows.append(database.StateEdge(
            check_run_id=check_id,
            src_index=index_of[s],
            dst_index=index_of[d] if d in index_of else -1,
            transition=tname, is_boundary=True))
    db.bulk_save_objects(edge_rows)


def _get_run(db: Session, check_id: str) -> database.CheckRun:
    run = db.get(database.CheckRun, check_id)
    if run is None:
        raise HTTPException(404, f"check run {check_id!r} not found")
    return run


@app.get("/checks/{check_id}")
def get_check(check_id: str, db: Session = Depends(database.get_session)):
    run = _get_run(db, check_id)
    return {
        "check_run_id": run.check_run_id,
        "version_id": run.version_id,
        "status": run.status,
        "verdict": run.verdict,
        "explored_states": run.explored_states,
        "edge_count": run.edge_count,
        "max_states_limit": run.max_states_limit,
        "truncated": run.truncated,
        "created_at": run.created_at.isoformat(),
        "result": run.result,
        "error": run.error,
    }


@app.get("/checks/{check_id}/graph")
def get_graph(check_id: str, limit: int = Query(default=500, ge=1, le=5000),
              offset: int = Query(default=0, ge=0),
              include_edges: bool = True,
              db: Session = Depends(database.get_session)):
    run = _get_run(db, check_id)
    nodes = db.scalars(
        select(database.StateNode)
        .where(database.StateNode.check_run_id == check_id)
        .order_by(database.StateNode.state_index)
        .limit(limit).offset(offset)).all()
    payload: Dict[str, Any] = {
        "check_run_id": check_id,
        "verdict": run.verdict,
        "node_offset": offset,
        "nodes_returned": len(nodes),
        "nodes_total": run.explored_states,
        "nodes": [{
            "state_index": n.state_index, "marking": n.marking,
            "depth": n.depth, "is_terminal": n.is_terminal,
            "is_dead_end": n.is_dead_end, "expanded": n.expanded,
            "on_frontier": n.on_frontier,
            "exclusivity_violations": n.exclusivity_violations,
        } for n in nodes],
    }
    if include_edges:
        q = select(database.StateEdge).where(
            database.StateEdge.check_run_id == check_id)
        if offset:
            q = q.where(database.StateEdge.src_index >= offset)
        q = q.where(database.StateEdge.src_index < offset + limit)
        edges = db.scalars(q.limit(limit * 10)).all()
        payload["edges"] = [{
            "src_index": e.src_index, "dst_index": e.dst_index,
            "transition": e.transition, "is_boundary": e.is_boundary,
        } for e in edges]
    return payload


@app.get("/checks/{check_id}/frontier")
def get_frontier(check_id: str,
                 db: Session = Depends(database.get_session)):
    run = _get_run(db, check_id)
    if not run.truncated:
        return {"check_run_id": check_id, "truncated": False,
                "frontier": [], "boundary_edges": []}
    result_json = run.result or {}
    trunc = result_json.get("truncation") or {}
    return {
        "check_run_id": check_id,
        "truncated": True,
        "limit": trunc.get("limit"),
        "states_visited": trunc.get("states_visited"),
        "states_expanded": trunc.get("states_expanded"),
        "frontier": trunc.get("frontier", []),
        "frontier_size": trunc.get("frontier_size"),
        "boundary_edges": trunc.get("boundary_edges", []),
        "boundary_edges_reported": trunc.get("boundary_edges_reported"),
        "boundary_edges_cap_reached": trunc.get("boundary_edges_cap_reached"),
    }


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@app.post("/checks/{check_id}/replay", response_model=ReplayReport)
def replay_check(check_id: str,
                 body: Optional[Dict[str, Any]] = None,
                 db: Session = Depends(database.get_session)) -> ReplayReport:
    """Replay a witness.

    Body ``{"witness": "deadlock" | "terminal_reachable" |
    "exclusivity:p1,p2" | "terminal_unreachable"}`` (default: primary
    witness).  An arbitrary sequence can be replayed with
    ``{"sequence": ["t1", "t2"]}``.
    """
    run = _get_run(db, check_id)
    version = db.get(database.Version, run.version_id)
    compiled = compile_model(ModelDefinitionIn.model_validate(version.definition))

    body = body or {}
    if "sequence" in body:
        sequence = [str(x) for x in body["sequence"]]
        witness_kind = body.get("witness", "custom")
    else:
        result_json = run.result or {}
        wanted = body.get("witness") or (
            (result_json.get("primary_witness") or {}).get("kind")
            if result_json.get("primary_witness") else None)
        witnesses = result_json.get("witnesses", {})
        if wanted in witnesses:
            w = witnesses[wanted]
        elif wanted is None:
            w = result_json.get("primary_witness")
        else:
            # kind-based selection: keys look like "exclusivity:crit1,crit2"
            match = next((v for k, v in witnesses.items()
                          if k == wanted or k.startswith(str(wanted) + ":")),
                         None)
            w = match
        if not w:
            raise HTTPException(404, f"witness {wanted!r} not found")
        sequence = w["transition_sequence"]
        witness_kind = w["kind"]

    return replay(compiled, sequence, witness_kind=witness_kind,
                  check_run_id=check_id, version_id=version.version_id)


# ---------------------------------------------------------------------------
# Publishing gate
# ---------------------------------------------------------------------------


@app.post("/models/{name}/versions/{version_id}/publish",
          response_model=PublishReport)
def publish_version(name: str, version_id: str, body: PublishBody,
                    db: Session = Depends(database.get_session)
                    ) -> PublishReport:
    """Publish a version's check conclusion.

    The conclusion is bound to the exact ``(version_id, content_hash,
    check_run_id)`` triple.  A changed model produces a new version with no
    publication and no reusable conclusion: only a check run *of that
    version* can pass the gate.
    """
    model = _get_model_or_404(db, name)
    version = db.get(database.Version, version_id)
    if version is None or version.model_id != model.model_id:
        raise HTTPException(404, "version not found for this model")

    run = db.get(database.CheckRun, body.check_run_id)
    if run is None or run.version_id != version.version_id:
        raise HTTPException(
            409,
            "check run does not belong to THIS version; a changed model "
            "cannot be released on an older version's conclusion")

    blocked = None
    if run.status != "completed":
        blocked = f"check run status is {run.status!r}, not completed"
    elif run.verdict == "COUNTEREXAMPLE":
        if body.acknowledge != "counterexample-found":
            blocked = ("check found a counterexample; re-run the witness "
                       "via /replay, then acknowledge with "
                       "acknowledge='counterexample-found' to force")
    elif run.verdict == "TRUNCATED":
        if body.acknowledge != "exploration-truncated":
            blocked = ("exploration was truncated at the state budget; no "
                       "proof exists yet (raise max_states and re-check), or "
                       "acknowledge='exploration-truncated' to force")
    elif run.verdict != "PROVED":
        blocked = f"cannot publish verdict {run.verdict!r}"

    report = PublishReport(
        model_id=model.model_id, version_id=version.version_id,
        version_number=version.version_number, check_run_id=run.check_run_id,
        published=blocked is None, blocked_reason=blocked,
        check_verdict=run.verdict, check_status=run.status,
        content_hash=version.content_hash,
        bound_content_hash=version.content_hash)

    if blocked is not None:
        return report

    existing = db.scalars(
        select(database.Publication)
        .where(database.Publication.version_id == version.version_id)
    ).first()
    if existing is None:
        db.add(database.Publication(
            publication_id=str(uuid.uuid4()), model_id=model.model_id,
            version_id=version.version_id, check_run_id=run.check_run_id,
            bound_content_hash=version.content_hash, verdict=run.verdict,
            acknowledged=body.acknowledge))
        db.commit()
    return report


@app.get("/health")
def health(db: Session = Depends(database.get_session)):
    from sqlalchemy import text
    from snakes.nets import dot  # engine import smoke check
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "postgresql",
            "engines": ["snakes", "networkx"], "black_token": str(dot)}
