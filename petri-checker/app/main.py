"""FastAPI 入口：结构化模型进，检查结果出，无页面。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .db import SessionLocal, init_db
from .schemas import CheckParams, NetSpec, ReplayRequest
from . import service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="petri-checker", version="0.1.0", lifespan=lifespan)


@app.exception_handler(service.ServiceError)
def _service_error_handler(_request, exc: service.ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _model_view(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "spec_hash": row.spec_hash,
        "spec": row.spec,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _run_view(row) -> dict:
    return {
        "id": row.id,
        "model_id": row.model_id,
        "model_version": row.model_version,
        "spec_hash": row.spec_hash,
        "params": row.params,
        "status": row.status,
        "conclusive": row.conclusive,
        "summary": row.summary,
        "findings": row.findings,
        "frontier": row.frontier,
        "created_at": row.created_at.isoformat(),
    }


@app.post("/models", status_code=201)
def create_model(spec: NetSpec, session: Session = Depends(get_session)):
    return _model_view(service.create_model(session, spec))


@app.get("/models/{model_id}")
def get_model(model_id: str, session: Session = Depends(get_session)):
    return _model_view(service.get_model(session, model_id))


@app.put("/models/{model_id}")
def update_model(model_id: str, spec: NetSpec, session: Session = Depends(get_session)):
    """更新即升版本、换指纹；此后旧检查结论一律失效。"""
    return _model_view(service.update_model(session, model_id, spec))


@app.post("/models/{model_id}/checks", status_code=201)
def run_checks(model_id: str, params: CheckParams | None = None, session: Session = Depends(get_session)):
    return _run_view(service.run_check(session, model_id, params or CheckParams()))


@app.get("/models/{model_id}/checks")
def list_checks(model_id: str, session: Session = Depends(get_session)):
    return [_run_view(r) for r in service.list_check_runs(session, model_id)]


@app.get("/checks/{run_id}")
def get_check(run_id: str, session: Session = Depends(get_session)):
    return _run_view(service.get_check_run(session, run_id))


@app.post("/models/{model_id}/replay")
def replay_trace(model_id: str, req: ReplayRequest, session: Session = Depends(get_session)):
    """重演变迁序列：验证反例路径在当前模型上逐步合法。"""
    result = service.replay(session, model_id, req.transitions)
    if not result["ok"]:
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/models/{model_id}/publish", status_code=201)
def publish(model_id: str, session: Session = Depends(get_session)):
    row = service.publish(session, model_id)
    return {
        "id": row.id,
        "model_id": row.model_id,
        "check_run_id": row.check_run_id,
        "model_version": row.model_version,
        "spec_hash": row.spec_hash,
        "published_at": row.published_at.isoformat(),
    }
