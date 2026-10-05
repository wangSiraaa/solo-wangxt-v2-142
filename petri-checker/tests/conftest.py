import os

os.environ["PETRI_DATABASE_URL"] = "postgresql+psycopg://postgres@127.0.0.1:54329/petri_test"

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------- 用例规范 ----------

def join_missing_token_spec():
    """并行汇合缺令牌：fork 分两支，B 支没有产生 b2 的变迁，join 永远等不到。"""
    return {
        "name": "join-missing-token",
        "places": [{"name": p} for p in ["start", "a1", "a2", "b1", "b2", "done"]],
        "transitions": [
            {"name": "fork", "inputs": {"start": 1}, "outputs": {"a1": 1, "b1": 1}},
            {"name": "work_a", "inputs": {"a1": 1}, "outputs": {"a2": 1}},
            # 缺陷：b1 -> b2 的变迁缺失
            {"name": "join", "inputs": {"a2": 1, "b2": 1}, "outputs": {"done": 1}},
        ],
        "initial_marking": {"start": 1},
        "termination": {"kind": "marking_eq", "marking": {"done": 1}},
        "place_bound": 3,
    }


def loop_spec():
    """环路：4 枚令牌在 10 库所环上循环，永不停歇；done 永远等不到令牌。
    有界（令牌守恒=4）但状态多（C(13,4)=715），适合演示探索上限截断。"""
    n = 10
    return {
        "name": "token-ring",
        "places": [{"name": f"p{i}"} for i in range(n)] + [{"name": "done"}],
        "transitions": [
            {"name": f"t{i}", "inputs": {f"p{i}": 1}, "outputs": {f"p{(i + 1) % n}": 1}}
            for i in range(n)
        ],
        "initial_marking": {"p0": 1, "p1": 1, "p2": 1, "p3": 1},
        "termination": {"kind": "marking_eq", "marking": {"done": 1}},
        "place_bound": 5,
    }


def resource_contention_spec(with_resource: bool):
    """资源竞争：两工人共享一份资源。with_resource=False 是缺资源的缺陷版。"""
    places = ["idle1", "cs1", "idle2", "cs2"] + (["res"] if with_resource else [])
    transitions = []
    for i in ("1", "2"):
        acquire = {"name": f"acquire{i}", "inputs": {f"idle{i}": 1}, "outputs": {f"cs{i}": 1}}
        release = {"name": f"release{i}", "inputs": {f"cs{i}": 1}, "outputs": {f"idle{i}": 1}}
        if with_resource:
            acquire["inputs"]["res"] = 1
            release["outputs"]["res"] = 1
        transitions += [acquire, release]
    return {
        "name": "resource-contention" + ("-ok" if with_resource else "-buggy"),
        "places": [{"name": p} for p in places],
        "transitions": transitions,
        "initial_marking": {"idle1": 1, "idle2": 1, **({"res": 1} if with_resource else {})},
        "forbidden": [
            {"kind": "co_marked", "places": ["cs1", "cs2"], "label": "mutex-violation"}
        ],
        "place_bound": 3,
    }
