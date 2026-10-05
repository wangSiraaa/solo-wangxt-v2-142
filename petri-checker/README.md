# petri-checker

普通有界 Petri 网的模型检查 API。FastAPI 接收结构化模型，SNAKES 执行点火语义，
NetworkX 承载状态图，PostgreSQL 保存模型与结果。无页面。

## 核心原则

**“搜索了十万个状态没发现死锁”与“证明无死锁”是两种不同结果。**
因此每次检查运行都区分：

| 字段 | 含义 |
|---|---|
| `status: completed` | 状态空间完整展开，结论是证明 |
| `status: truncated` | 触及 `max_states` 上限，`conclusive=false`，`frontier` 保留未展开的边界状态 |
| `conclusive: false` | 已探索区域无反例 ≠ 无反例；发布门禁拒绝此类结论 |

同理，终态在截断运行中只能是 `unknown`，绝不报 `unreachable`。

## 语义边界（明确支持的范围）

- 普通网：令牌无数据（SNAKES `dot`），弧权为正整数（内部展开为 `MultiArc`）；
- K-有界：`place_bound` 声明每库所令牌上限，越界状态记为 `bound_exceeded` 发现且不再展开；
- 不支持：抑制弧、变迁优先级、颜色/时间网——提交即被校验拒绝。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/models` | 提交结构化模型；校验初始标识、变迁输入/输出、终止条件、禁止约束（全部问题一次返回 422） |
| PUT | `/models/{id}` | 更新模型：版本 +1、`spec_hash` 更换，**旧检查结论即刻失效** |
| POST | `/models/{id}/checks` | 执行检查：`max_states`、`place_bound`、`max_frontier_kept` 可调 |
| GET | `/models/{id}/checks`、`/checks/{run_id}` | 查询历史运行 |
| POST | `/models/{id}/replay` | 重演变迁序列：逐步点火，任一步不使能则 400 并指出步号 |
| POST | `/models/{id}/publish` | 发布门禁（见下） |

### 检查内容

1. **死锁**：出度为 0 的标识；满足终止条件的死锁标注 `is_expected_terminal`，不算错误；
2. **终态可达性**：`marking_eq`（精确）或 `marking_cover`（覆盖）；可达时给出见证序列；
3. **禁止状态**：`co_marked`（不得同时有令牌）、`max_tokens`、`total_tokens`；
4. 每个反例附 `trace`——从初始标识可重演的变迁序列（用 `/replay` 验证）。

### 发布门禁

`publish` 仅当同时满足才放行，否则 409：

- 最近一次检查运行的 `spec_hash` == 当前模型 `spec_hash`（模型未变）；
- 该运行 `conclusive == true`（探索完整，非截断、无越界）；
- 该运行无 `severity=error` 的发现。

## 运行

```bash
./run.sh          # 启动 PostgreSQL(54329) + uvicorn(8000)
```

测试（三个诊断用例：并行汇合缺令牌 / 环路截断 / 资源竞争 + 版本失效）：

```bash
/workspace/venv/bin/python3 -m pytest tests/ -q
```

## 存储

- `models`：规范 JSONB + `spec_hash` + 单调版本号；
- `check_runs`：绑定 `spec_hash` 的结论（摘要、发现、边界）；
- `publications`：发布记录，指向具体检查运行。
