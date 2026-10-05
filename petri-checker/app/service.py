"""业务逻辑：模型版本化、检查执行、发布门禁。

发布门禁的核心不变量：
结论绑定 spec_hash。模型一旦更新（version/spec_hash 变化），
历史检查运行即刻失效——发布入口只接受“当前 spec_hash + 完整探索 + 无错误发现”的运行。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analyzer, builder
from .db import CheckRunRow, ModelRow, PublicationRow, utcnow
from .schemas import CheckParams, NetSpec


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def spec_hash(spec: NetSpec) -> str:
    canonical = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_model(session: Session, spec: NetSpec) -> ModelRow:
    issues = builder.validate_spec(spec)
    if issues:
        raise ServiceError(422, {"message": "spec validation failed", "issues": issues})
    row = ModelRow(
        id=str(uuid.uuid4()),
        name=spec.name,
        version=1,
        spec=spec.model_dump(mode="json"),
        spec_hash=spec_hash(spec),
    )
    session.add(row)
    session.commit()
    return row


def update_model(session: Session, model_id: str, spec: NetSpec) -> ModelRow:
    row = _get_model(session, model_id)
    issues = builder.validate_spec(spec)
    if issues:
        raise ServiceError(422, {"message": "spec validation failed", "issues": issues})
    row.version += 1
    row.spec = spec.model_dump(mode="json")
    row.spec_hash = spec_hash(spec)
    row.updated_at = utcnow()
    session.commit()
    return row


def get_model(session: Session, model_id: str) -> ModelRow:
    return _get_model(session, model_id)


def _get_model(session: Session, model_id: str) -> ModelRow:
    row = session.get(ModelRow, model_id)
    if row is None:
        raise ServiceError(404, f"model '{model_id}' not found")
    return row


def run_check(session: Session, model_id: str, params: CheckParams) -> CheckRunRow:
    row = _get_model(session, model_id)
    spec = NetSpec(**row.spec)
    bound = params.place_bound or spec.place_bound

    net = builder.build_net(spec)
    result = analyzer.explore(
        net, spec,
        max_states=params.max_states,
        place_bound=bound,
        max_frontier_kept=params.max_frontier_kept,
    )

    run = CheckRunRow(
        id=str(uuid.uuid4()),
        model_id=row.id,
        model_version=row.version,
        spec_hash=row.spec_hash,
        params=params.model_dump(mode="json"),
        status="truncated" if result.truncated else "completed",
        conclusive=result.conclusive,
        summary=result.summary(),
        findings=[f.as_dict() for f in result.findings],
        frontier=result.frontier,
    )
    session.add(run)
    session.commit()
    return run


def get_check_run(session: Session, run_id: str) -> CheckRunRow:
    row = session.get(CheckRunRow, run_id)
    if row is None:
        raise ServiceError(404, f"check run '{run_id}' not found")
    return row


def list_check_runs(session: Session, model_id: str) -> list[CheckRunRow]:
    _get_model(session, model_id)
    return list(session.scalars(
        select(CheckRunRow)
        .where(CheckRunRow.model_id == model_id)
        .order_by(CheckRunRow.created_at.desc())
    ))


def replay(session: Session, model_id: str, sequence: list[str]) -> dict[str, Any]:
    row = _get_model(session, model_id)
    spec = NetSpec(**row.spec)
    net = builder.build_net(spec)
    return analyzer.replay(net, spec, sequence)


def publish(session: Session, model_id: str) -> PublicationRow:
    """发布门禁：只有“当前模型版本的、完整的、无错误发现的”检查运行才放行。"""
    row = _get_model(session, model_id)
    latest: Optional[CheckRunRow] = session.scalars(
        select(CheckRunRow)
        .where(CheckRunRow.model_id == model_id)
        .order_by(CheckRunRow.created_at.desc())
        .limit(1)
    ).first()

    if latest is None:
        raise ServiceError(409, "no check run exists for this model; run checks first")

    if latest.spec_hash != row.spec_hash:
        raise ServiceError(409, {
            "message": "model changed since the last check run; previous conclusions are void",
            "model_version": row.version,
            "model_spec_hash": row.spec_hash,
            "last_run_version": latest.model_version,
            "last_run_spec_hash": latest.spec_hash,
            "action": "re-run POST /models/{id}/checks before publishing",
        })

    if not latest.conclusive:
        raise ServiceError(409, {
            "message": "latest check run is inconclusive (exploration truncated or bound exceeded); "
                       "absence of findings in the explored region is not a proof",
            "run_id": latest.id,
            "status": latest.status,
            "frontier_size": latest.summary.get("frontier_size"),
            "action": "raise max_states / place_bound and re-run checks",
        })

    errors = [f for f in latest.findings if f.get("severity") == "error"]
    if errors:
        raise ServiceError(409, {
            "message": f"latest check run has {len(errors)} error finding(s); fix the model first",
            "run_id": latest.id,
            "findings": errors,
        })

    pub = PublicationRow(
        id=str(uuid.uuid4()),
        model_id=row.id,
        check_run_id=latest.id,
        model_version=row.version,
        spec_hash=row.spec_hash,
    )
    session.add(pub)
    session.commit()
    return pub
