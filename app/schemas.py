"""Structured request/response models for the Petri-net checking API.

Only *ordinary bounded Place/Transition nets* are accepted: places hold
indistinguishable black tokens, arcs are plain or weighted, there are no
colours, guards, inhibitor arcs or reset arcs.  Anything outside that
fragment is rejected at validation time (see ``app.petrinet.compile_model``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class PlaceIn(BaseModel):
    name: str = Field(..., min_length=1)
    # null / omitted capacity means the place is unbounded (no capacity
    # constraint); a positive integer bounds the number of black tokens.
    capacity: Optional[int] = Field(default=None, ge=1)


class TransitionIn(BaseModel):
    name: str = Field(..., min_length=1)


class ArcIn(BaseModel):
    place: str = Field(..., min_length=1)
    transition: str = Field(..., min_length=1)
    weight: int = Field(default=1, ge=1)


class GoalsIn(BaseModel):
    """Termination conditions and state exclusions.

    ``terminal_markings``: any marking satisfying any entry is a terminal.
    ``exclusive_groups``: within one group, at most one place may hold a
    token at the same time ("states that must not occur together").
    """

    terminal_markings: List["MarkingPattern"] = Field(default_factory=list)
    exclusive_groups: List[List[str]] = Field(default_factory=list)
    require_terminal_reachable: bool = True
    require_no_deadlock: bool = True
    # A dead end that is itself a terminal marking is proper termination,
    # not a deadlock.
    deadlock_allows_terminal: bool = True
    require_exclusivity: bool = True


class MarkingPattern(BaseModel):
    """A terminal marking condition.

    ``marking`` maps place -> exact token count.  With ``match='exact'`` the
    whole reachable marking has to equal it (unlisted places are zero);
    with ``match='cover'`` the listed counts are a lower bound.
    """

    marking: Dict[str, int] = Field(default_factory=dict)
    match: Literal["exact", "cover"] = "exact"


class ModelDefinitionIn(BaseModel):
    name: str = Field(..., min_length=1)
    places: List[PlaceIn]
    transitions: List[TransitionIn]
    inputs: List[ArcIn] = Field(default_factory=list, alias="inputs")
    outputs: List[ArcIn] = Field(default_factory=list, alias="outputs")
    initial_marking: Dict[str, int] = Field(default_factory=dict)
    goals: GoalsIn = Field(default_factory=GoalsIn)

    model_config = {"populate_by_name": True}


class CheckOptions(BaseModel):
    max_states: int = Field(default=100_000, ge=1, le=2_000_000)
    # Keep the fully explored state graph rows (nodes/edges) in the database.
    persist_graph: bool = True


class PublishBody(BaseModel):
    check_run_id: str
    acknowledge: Optional[
        Literal["counterexample-found", "exploration-truncated"]
    ] = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class VersionCreated(BaseModel):
    model_id: str
    version_id: str
    version_number: int
    content_hash: str
    changed: bool
    previous_version_id: Optional[str] = None


class CheckCreated(BaseModel):
    check_run_id: str
    version_id: str
    status: str
    verdict: str
    explored_states: int
    truncated: bool
    findings: Dict[str, Any]


class StepInReplay(BaseModel):
    index: int
    transition: str
    fired_from: Dict[str, int]
    marking_after: Dict[str, int]
    enabled_before: List[str]


class ReplayReport(BaseModel):
    check_run_id: str
    version_id: str
    witness_kind: str
    valid: bool
    steps: List[StepInReplay]
    final_marking: Dict[str, int]
    starts_at_initial: bool
    ends_in_terminal: bool
    is_dead_end: bool
    exclusivity_violations: List[List[str]]
    capacity_violations: List[str]


class PublishReport(BaseModel):
    model_id: str
    version_id: str
    version_number: int
    check_run_id: str
    published: bool
    blocked_reason: Optional[str] = None
    check_verdict: str
    check_status: str
    content_hash: str
    bound_content_hash: str


GoalsIn.model_rebuild()
