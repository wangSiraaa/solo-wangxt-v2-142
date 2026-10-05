"""PostgreSQL persistence: models, immutable versions, check runs, results.

Design points
-------------
* A *model* is a named entity; every submitted body that differs from the
  current one creates a new immutable **version** (content-addressed by a
  SHA-256 hash of its canonical JSON).  Check runs always bind to an exact
  ``version_id``/``content_hash`` pair, and a publishing entry binds to the
  exact version it attests.  A changed model therefore gets a new version
  whose "publish" gate starts empty -- an old version's check conclusion can
  never silently carry over.
* Structured bodies and results live in JSONB so the API accepts genuinely
  structured models without inventing columns per net.
"""
from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, ForeignKey,
                        Integer, String, Text, UniqueConstraint,
                        create_engine, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (DeclarativeBase, relationship, sessionmaker)


def _deferred_fk(column: str) -> ForeignKey:
    """FK checked at COMMIT, allowing model<->version inserts in either
    order inside one transaction."""
    return ForeignKey(column, ondelete="RESTRICT",
                      deferrable=True, initially="DEFERRED")

DATABASE_URL = os.environ.get(
    "PETRI_DATABASE_URL",
    "postgresql+psycopg2://petri@localhost:55432/petridb?host=/tmp",
)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, future=True,
                            expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Model(Base):
    __tablename__ = "petri_models"

    model_id = Column(String(36), primary_key=True)
    name = Column(Text, nullable=False, unique=True)
    current_version_id = Column(
        String(36), _deferred_fk("petri_versions.version_id"),
        nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    versions = relationship("Version", back_populates="model",
                            foreign_keys="Version.model_id")


class Version(Base):
    __tablename__ = "petri_versions"
    __table_args__ = (UniqueConstraint("model_id", "version_number"),
                      UniqueConstraint("content_hash"))

    version_id = Column(String(36), primary_key=True)
    model_id = Column(String(36), ForeignKey("petri_models.model_id"),
                      nullable=False)
    version_number = Column(Integer, nullable=False)
    content_hash = Column(Text, nullable=False)
    definition = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("Model", back_populates="versions",
                         foreign_keys=[model_id])
    check_runs = relationship("CheckRun", back_populates="version")
    publication = relationship("Publication", back_populates="version",
                               uselist=False)


class CheckRun(Base):
    __tablename__ = "petri_check_runs"

    check_run_id = Column(String(36), primary_key=True)
    version_id = Column(String(36), ForeignKey("petri_versions.version_id"),
                        nullable=False, index=True)
    status = Column(Text, nullable=False)        # completed | error
    verdict = Column(Text, nullable=False)       # PROVED | COUNTEREXAMPLE |
                                                 # TRUNCATED | INVALID
    explored_states = Column(Integer, nullable=False, default=0)
    edge_count = Column(Integer, nullable=False, default=0)
    max_states_limit = Column(Integer, nullable=False)
    truncated = Column(Boolean, nullable=False, default=False)
    result = Column(JSONB, nullable=True)
    error = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    version = relationship("Version", back_populates="check_runs")


class StateNode(Base):
    """Persisted node of the explored state graph (when requested)."""
    __tablename__ = "petri_state_nodes"
    __table_args__ = (UniqueConstraint("check_run_id", "state_index"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    check_run_id = Column(String(36),
                          ForeignKey("petri_check_runs.check_run_id"),
                          nullable=False, index=True)
    state_index = Column(Integer, nullable=False)
    marking = Column(JSONB, nullable=False)
    depth = Column(Integer, nullable=False)
    is_terminal = Column(Boolean, nullable=False, default=False)
    is_dead_end = Column(Boolean, nullable=False, default=False)
    expanded = Column(Boolean, nullable=False, default=False)
    on_frontier = Column(Boolean, nullable=False, default=False)
    exclusivity_violations = Column(JSONB, nullable=False, default=list)


class StateEdge(Base):
    __tablename__ = "petri_state_edges"
    __table_args__ = (
        UniqueConstraint("check_run_id", "src_index", "dst_index",
                         "transition"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    check_run_id = Column(String(36),
                          ForeignKey("petri_check_runs.check_run_id"),
                          nullable=False, index=True)
    src_index = Column(Integer, nullable=False)
    dst_index = Column(Integer, nullable=False)
    transition = Column(Text, nullable=False)
    # edges leaving the visited-but-unexpanded boundary of a truncated run
    is_boundary = Column(Boolean, nullable=False, default=False)


class Publication(Base):
    """A release gate: the conclusion attested for one exact version."""
    __tablename__ = "petri_publications"

    publication_id = Column(String(36), primary_key=True)
    model_id = Column(String(36), ForeignKey("petri_models.model_id"),
                      nullable=False)
    version_id = Column(String(36),
                        ForeignKey("petri_versions.version_id"),
                        nullable=False, unique=True)
    check_run_id = Column(String(36),
                          ForeignKey("petri_check_runs.check_run_id"),
                          nullable=False)
    bound_content_hash = Column(Text, nullable=False)
    verdict = Column(Text, nullable=False)
    acknowledged = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    version = relationship("Version", back_populates="publication",
                           foreign_keys=[version_id])


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
