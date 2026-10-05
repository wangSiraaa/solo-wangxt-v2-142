# Petri-net checking API

检查普通**有界 P/T 网（ordinary bounded Place/Transition nets）**的状态空间，验证：

* 初始标识（initial marking）是否合法（容量、非负、引用完整）；
* 变迁的输入/输出弧（支持正整数弧权，禁止平行弧）；
* **终止条件可达性**（终态标识 exact / cover 两种匹配）；
* **死锁**（无任何变迁可触发、且不是声明过的终态）；
* **互斥状态**（exclusive groups：一组库所不允许同时有托肯）。

核心立场：**"搜了十万个状态没发现死锁" ≠ "证明无死锁"**。检查结论严格区分为：

| verdict | 含义 |
|---|---|
| `PROVED` | 可达状态空间被**完整枚举**，所要求的全部性质成立 |
| `COUNTEREXAMPLE` | 找到反例，返回**可重演的变迁序列**（从 M₀ 出发逐步点火） |
| `TRUNCATED` | 触及 `max_states` 探索上限；未完成的**边界**（已访问未展开状态 + 出边）被保留，性质一律记为 `UNKNOWN`，不得当作证明 |

## 技术栈

* **FastAPI**：只提供结构化 JSON API，不做页面；
* **SNAKES**：执行明确支持的普通有界网语义——用它建网、做合法性校验，并用它独立计算后继标识来交叉核对探索引擎（初态、采样状态、重演的每一步都比对）；
* **NetworkX**：状态图分析（SCC、环、自环、终态 SCC、环上的不可终止 lasso 证人）；
* **PostgreSQL**：模型、不可变版本、检查结果、状态图节点/边、发布记录全部持久化（JSONB 存结构化模型与报告）。

### 支持/不支持的语义

* 托肯为不可区分黑点，标识 = 库所→托肯数；
* 弧权为正整数（一次消耗/产生 N 个）；
* 库所容量可选；点火后会超出容量的变迁**不可触发**（有界网语义）；
* 允许源变迁（无输入弧，恒可触发）、汇变迁、孤立变迁；
* 不支持颜色、变量、哨词、抑制弧、测试弧、reset/flush 弧——提交即 422 拒绝；
* 同一对 place/transition 间不允许平行弧（用弧权表达）。

## 运行

需要 Python 3.11+ 与一个 PostgreSQL 实例。

```bash
pip install -r requirements.txt

# 数据库连接串（默认值如下；改成你自己的实例）
export PETRI_DATABASE_URL="postgresql+psycopg2://petri@localhost:55432/petridb?host=/tmp"

uvicorn app.main:app --host 127.0.0.1 --port 8088
# 表结构在启动时自动创建
```

交互式文档：`http://127.0.0.1:8088/docs`（OpenAPI/Swagger，本服务唯一的"界面"）。

测试使用独立库：

```bash
createdb petritest
pytest tests/ -q          # 28 tests
```

端到端诊断复现（要求服务已启动）：

```bash
python3 scripts/demo_e2e.py
```

## API 摘要

```text
POST   /models                                     创建模型；内容变化则产生新版本
GET    /models/{name}                              模型与当前版本（含发布状态）
GET    /models/{name}/versions                     全部版本及各自的检查记录
POST   /models/{name}/checks?version_id=...        对【明确指定】的版本执行检查
GET    /checks/{id}                                完整检查报告
GET    /checks/{id}/graph                          状态图（分页节点/边）
GET    /checks/{id}/frontier                       TRUNCATED 时的未完成边界
POST   /checks/{id}/replay                         逐步重演证人变迁序列
POST   /models/{name}/versions/{vid}/publish       发布入口（结论绑定精确版本）
GET    /health
```

### 结构化模型示例

```json
{
  "name": "fork_join",
  "places": [
    {"name": "start", "capacity": 1},
    {"name": "a_done", "capacity": 1},
    {"name": "b_done", "capacity": 1},
    {"name": "end", "capacity": 1}
  ],
  "transitions": [
    {"name": "fork"}, {"name": "workB"}, {"name": "join"}
  ],
  "inputs": [
    {"place": "start",  "transition": "fork"},
    {"place": "a_done", "transition": "join"},
    {"place": "b_done", "transition": "join"},
    {"place": "b_done", "transition": "workB", "weight": 1}
  ],
  "outputs": [
    {"place": "a_done", "transition": "fork"},
    {"place": "end",    "transition": "join"}
  ],
  "initial_marking": {"start": 1},
  "goals": {
    "terminal_markings": [{"marking": {"end": 1}, "match": "exact"}],
    "exclusive_groups": [],
    "require_terminal_reachable": true,
    "require_no_deadlock": true,
    "deadlock_allows_terminal": true,
    "require_exclusivity": true
  }
}
```

> 该模型的 `fork` 漏发了 B 分支托肯，检查会给出 `COUNTEREXAMPLE`，
> 死锁证人序列为 `["fork"]`，重演后停在 `{a_done:1}` 死标识。

检查请求体：`{"max_states": 100000, "persist_graph": true}`。

### 报告里三类性质的状态

每个性质（终态可达 / 无死锁 / 互斥 / 有界性）取：

* `HELD`：完整枚举后证明成立；
* `VIOLATED`：反例 + 变迁序列；
* `UNKNOWN`：探索被上限截断，前缀内未发现问题——**这不是证明**；
* `HELD_SO_FAR`：截断前缀内已到达终态，但整体性质仍不作保；
* `NOT_CHECKED`：该性质未要求检查。

`witnesses` 中的序列可直接 `POST /checks/{id}/replay`，返回每一步点火前标识、
当时可触发的变迁集合、点火后标识，并用 SNAKES 独立引擎逐步复核。

## 模型变更与发布绑定

* 模型按名字保存；提交体经规范化 JSON 做 SHA-256 内容寻址。内容变化 →
  自动产生**不可变新版本**（version_number 递增），检查只能针对明确的
  `version_id` 发起，不存在"挂在最新版本上"的浮动结论。
* 发布入口把结论绑定到 `(version_id, content_hash, check_run_id)` 三元组：
  * 用别的版本（含旧版本）的 check run 发布本版本 → **409 拒绝**；
  * `COUNTEREXAMPLE` 必须显式 `acknowledge="counterexample-found"` 才能强制发布；
  * `TRUNCATED` 必须显式 `acknowledge="exploration-truncated"`（即承认"还没证明"）才能强制发布；
  * 只有 `PROVED` 的检查可直接通过。
* 因此模型一旦改变，新版本的发布门是空的，**无法沿用旧模型的检查结论**。

## 内置用例（`app/fixtures.py`，`scripts/demo_e2e.py` 逐步复现）

1. **并行汇合缺令牌** `parallel_join_missing_token`：fork 漏发一个分支，
   join 永远等不到令牌 → 死锁反例，终态不可达；
2. **环路**：
   * `unbounded_loop`：源变迁无限产托肯，小预算下 `TRUNCATED`，
     保留 `frontier` 与 `boundary_edges`（"搜了十万个状态"的正确形态）；
   * `bounded_cycle`：容量有界的双库所环，完整枚举 → `PROVED` 且报告环证人；
3. **资源竞争**：
   * `resource_race_no_mutex`：无锁，两进程可同时进入临界区 →
     互斥反例 `["acq1","acq2"]`；
   * `resource_race_with_lock`：加锁后 → `PROVED`；
   * `circular_wait_deadlock`：两进程以相反顺序申请 r1/r2，
     `["p1_take_r1","p2_take_r2"]` 后循环等待死锁。

## 性能

纯 Python 后继引擎（约 200 万次后继判定/秒）负责枚举，SNAKES 负责语义校验与
交叉核对：在本机枚举 100,000 个状态 / 199,106 条边约 1.1 秒；连同状态图
（节点+边+边界）写入 PostgreSQL 约 8 秒。
