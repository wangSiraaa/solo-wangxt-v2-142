"""结构化输入模型：普通有界 Petri 网的声明式描述。

语义边界（明确支持的范围）：
- 普通网：令牌无数据，库所容量以 place_bound（K-有界）声明；
- 弧权为正整数（SNAKES 内部以 MultiArc 展开）；
- 无抑制弧、无优先级、无时间——SNAKES 支持但本 API 不开放的语义一律拒绝。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class PlaceSpec(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TransitionSpec(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    inputs: dict[str, int] = Field(default_factory=dict)   # 库所 -> 权重
    outputs: dict[str, int] = Field(default_factory=dict)

    @field_validator("inputs", "outputs")
    @classmethod
    def weights_positive(cls, v: dict[str, int]) -> dict[str, int]:
        for place, w in v.items():
            if not isinstance(w, int) or w < 1:
                raise ValueError(f"arc weight on '{place}' must be a positive integer, got {w!r}")
        return v


class TerminationSpec(BaseModel):
    """终止条件：marking_eq 为精确标识（未列出的库所须为 0）；marking_cover 为覆盖（>=）。"""
    kind: Literal["marking_eq", "marking_cover"] = "marking_eq"
    marking: dict[str, int]

    @field_validator("marking")
    @classmethod
    def counts_non_negative(cls, v: dict[str, int]) -> dict[str, int]:
        for place, c in v.items():
            if not isinstance(c, int) or c < 0:
                raise ValueError(f"termination count on '{place}' must be >= 0, got {c!r}")
        return v


class ForbiddenSpec(BaseModel):
    """不允许出现的状态：
    - co_marked:   列出的库所不得同时全部有令牌（互斥类约束）；
    - max_tokens:  单一库所令牌数不得超过 bound；
    - total_tokens: 列出的库所令牌总数不得超过 bound。
    """
    kind: Literal["co_marked", "max_tokens", "total_tokens"]
    places: list[str] = Field(min_length=1)
    bound: int = 0
    label: str = ""

    @field_validator("bound")
    @classmethod
    def bound_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("bound must be >= 0")
        return v


class NetSpec(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    places: list[PlaceSpec] = Field(min_length=1)
    transitions: list[TransitionSpec] = Field(min_length=1)
    initial_marking: dict[str, int] = Field(default_factory=dict)
    termination: Optional[TerminationSpec] = None
    forbidden: list[ForbiddenSpec] = Field(default_factory=list)
    place_bound: int = Field(default=5, ge=1, le=1000)


class CheckParams(BaseModel):
    max_states: int = Field(default=10000, ge=1, le=2_000_000)
    place_bound: Optional[int] = Field(default=None, ge=1, le=1000)  # 覆盖 spec 中的声明
    max_frontier_kept: int = Field(default=100, ge=1, le=1000)       # 触及上限时保留的边界条数


class ReplayRequest(BaseModel):
    transitions: list[str]
