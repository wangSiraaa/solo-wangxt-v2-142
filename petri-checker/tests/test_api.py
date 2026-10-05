"""三个诊断用例的逐步复现 + 模型变更后发布门禁失效。"""
import pytest

from .conftest import (
    join_missing_token_spec,
    loop_spec,
    resource_contention_spec,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _create(client, spec):
    r = await client.post("/models", json=spec)
    assert r.status_code == 201, r.text
    return r.json()


async def _check(client, model_id, **params):
    r = await client.post(f"/models/{model_id}/checks", json=params)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 用例 1：并行汇合缺令牌 ----------

async def test_join_missing_token(client):
    model = await _create(client, join_missing_token_spec())
    run = await _check(client, model["id"])

    # 状态空间很小，必须完整探索、结论确凿
    assert run["status"] == "completed"
    assert run["conclusive"] is True

    # 终态不可达：join 永远等不到 b2
    assert run["summary"]["terminal"]["status"] == "unreachable"

    # 死锁：fork+work_a 之后 a2 与 b1 各持一枚令牌，join 无法使能
    deadlocks = [f for f in run["findings"] if f["type"] == "deadlock"]
    assert len(deadlocks) == 1
    assert deadlocks[0]["state"] == {"a2": 1, "b1": 1}
    assert deadlocks[0]["trace"] == ["fork", "work_a"]

    # 反例序列可重演：逐步点火后停在同一标识
    replay = await client.post(
        f"/models/{model['id']}/replay", json={"transitions": deadlocks[0]["trace"]}
    )
    assert replay.status_code == 200
    body = replay.json()
    assert body["ok"] is True
    assert body["steps"][-1]["marking"] == {"a2": 1, "b1": 1}

    # 重演一个不可点火的序列必须被拒绝并指出步号
    bad = await client.post(
        f"/models/{model['id']}/replay", json={"transitions": ["fork", "join"]}
    )
    assert bad.status_code == 400
    assert bad.json()["failed_at"] == 1

    # 有错误发现，发布必须被拒
    pub = await client.post(f"/models/{model['id']}/publish")
    assert pub.status_code == 409


# ---------- 用例 2：环路 + 探索上限 ----------

async def test_loop_truncation_keeps_frontier(client):
    model = await _create(client, loop_spec())

    # 压低上限：搜索 40 个状态没发现死锁，不等于无死锁
    run = await _check(client, model["id"], max_states=40)
    assert run["status"] == "truncated"
    assert run["conclusive"] is False
    assert run["summary"]["frontier_size"] > 0
    assert len(run["frontier"]) > 0  # 未完成边界被保留
    # 边界状态是未展开的环上标识（令牌守恒：每状态恰 4 枚）
    assert all(sum(s.values()) == 4 for s in run["frontier"])
    # 终态没找到，但结论只能是 unknown 而非 unreachable
    assert run["summary"]["terminal"]["status"] == "unknown"

    # 不确定的结论不能发布
    pub = await client.post(f"/models/{model['id']}/publish")
    assert pub.status_code == 409
    assert "inconclusive" in pub.json()["detail"]["message"]

    # 提高上限后完整探索：715 个状态全部展开，终态不可达是确凿结论，
    # 且无死锁（环上令牌永远可以移动）
    full = await _check(client, model["id"], max_states=2000)
    assert full["status"] == "completed"
    assert full["conclusive"] is True
    assert full["summary"]["states_explored"] == 715
    assert full["summary"]["terminal"]["status"] == "unreachable"
    assert full["summary"]["deadlock_count"] == 0


# ---------- 用例 3：资源竞争 + 模型变更使旧结论失效 ----------

async def test_resource_contention_and_stale_publish(client):
    # 缺陷版：没有共享资源，cs1/cs2 可同时有令牌
    buggy = await _create(client, resource_contention_spec(with_resource=False))
    run = await _check(client, buggy["id"])
    assert run["conclusive"] is True

    violations = [f for f in run["findings"] if f["type"] == "forbidden_co_marked"]
    assert violations, "expected a mutex violation"
    v = violations[0]
    assert "mutex-violation" in v["detail"]
    assert v["state"]["cs1"] == 1 and v["state"]["cs2"] == 1
    assert v["trace"] == ["acquire1", "acquire2"] or v["trace"] == ["acquire2", "acquire1"]

    # 反例可重演
    replay = await client.post(
        f"/models/{buggy['id']}/replay", json={"transitions": v["trace"]}
    )
    assert replay.status_code == 200
    final = replay.json()["steps"][-1]["marking"]
    assert final["cs1"] == 1 and final["cs2"] == 1

    # 修复模型：加入共享资源。模型一变，旧检查结论立即失效
    fixed_spec = resource_contention_spec(with_resource=True)
    updated = await client.put(f"/models/{buggy['id']}", json=fixed_spec)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["spec_hash"] != buggy["spec_hash"]

    stale = await client.post(f"/models/{buggy['id']}/publish")
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert "model changed" in detail["message"]
    assert detail["model_version"] == 2
    assert detail["last_run_version"] == 1

    # 对修复后的模型重新检查：互斥约束不再被违反，可以发布
    clean = await _check(client, buggy["id"])
    assert clean["conclusive"] is True
    assert clean["summary"]["error_count"] == 0
    assert not [f for f in clean["findings"] if f["type"] == "forbidden_co_marked"]

    pub = await client.post(f"/models/{buggy['id']}/publish")
    assert pub.status_code == 201
    body = pub.json()
    assert body["model_version"] == 2
    assert body["check_run_id"] == clean["id"]


# ---------- 规范校验 ----------

async def test_validation_rejects_bad_spec(client):
    spec = join_missing_token_spec()
    spec["initial_marking"] = {"ghost": 1}                     # 初始标识引用未知库所
    spec["transitions"][0]["outputs"]["nowhere"] = 1           # 变迁输出引用未知库所
    spec["termination"] = {"kind": "marking_eq", "marking": {"void": 1}}  # 终止条件引用未知库所
    r = await client.post("/models", json=spec)
    assert r.status_code == 422
    issues = r.json()["detail"]["issues"]
    assert any("ghost" in i and "initial_marking" in i for i in issues)
    assert any("nowhere" in i and "output" in i for i in issues)
    assert any("void" in i and "termination" in i for i in issues)


async def test_initial_marking_beyond_bound_rejected(client):
    spec = join_missing_token_spec()
    spec["initial_marking"] = {"start": 5}   # 超过 place_bound=3
    r = await client.post("/models", json=spec)
    assert r.status_code == 422
    assert any("place_bound" in i for i in r.json()["detail"]["issues"])
