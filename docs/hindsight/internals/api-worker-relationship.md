# Hindsight 引擎内幕：API 与 Worker 的关系

> 代码研究对象：`vectorize-io/hindsight` @ `12f2d54f6`（main，2026-09-30）
> 方法：先直读核心文件建立基线（`worker/main.py`、`worker/poller.py`、`engine/task_backend.py`），再由 4 个独立 subagent 分别从「API 入队路径」「Worker 数据管道」「认领 SQL 与可靠性」「状态机与客户端可见性」并行深挖，最后对 9 处承重结论逐一代码核对。除特别注明外，所有 `file:line` 均已验证或由至少两个独立来源交叉印证。

---

## 0. 一句话总览

**API 和 Worker 之间没有消息中间件——PostgreSQL 的 `async_operations` 表本身就是队列。** API 收到异步请求后把任务 payload 写成一行 `pending` 记录即返回；worker（或 API 进程内嵌的 poller）用 `FOR UPDATE SKIP LOCKED` 从同一张表轮询认领，在进程内执行完整的数据管道。两者共享同一个 `MemoryEngine` 代码库，只是 task backend 不同。

```
                ┌────────────────────────── PostgreSQL ──────────────────────────┐
                │                                                                │
 ┌────────┐  写  │  async_operations (status=pending, task_payload JSONB)         │  认领
 │ Client │ ───► │  父行 batch_retain (payload=NULL) ─┐                           │ ────────► ┌─────────────┐
 └────────┘  ◄── │  子行 retain (payload=contents)    │ 事务内原子插入              │           │ Worker 进程  │
   ▲  operation_id    列: worker_id/claimed_at/serialization_key/retry_count/...  │           │ hindsight-  │
   │  轮询 GET /operations/{id}                                                    │           │ worker CLI  │
   │                                                                               │           └──────┬──────┘
   │        ┌────────────┐                                                         │                  │ executor=
   │        │ API 进程    │  lifespan 内默认也启动 poller（standalone 模式）           │                  ▼ memory.execute_task
   │        │ FastAPI     │ ────────────────────────────────────────────────────► │        ┌─────────────────────┐
   └────────│             │        （关闭 HINDSIGHT_API_WORKER_ENABLED 可纯队列化）  │        │ MemoryEngine        │
            └────────────┘                                                         │        │ 12 种任务 handler    │
                                                                                   │        │ retain/consolidate/ │
                                                                                   │        │ webhook...          │
                                                                                   │        └─────────────────────┘
```

（`hindsight-api-slim/` 为以下所有路径的缩写根。）

---

## 1. 通信机制：表即队列，三种 TaskBackend

队列抽象在 `hindsight_api/engine/task_backend.py`，全部任务就是可 JSON 序列化的 dict：

| Backend | `submit_task` 行为 | 使用方 |
|---|---|---|
| `BrokerTaskBackend`（:153-262） | 写 `async_operations`（有 operation_id 则 `UPDATE ... WHERE task_payload IS NULL` 兜底，否则 INSERT `pending` 行） | API 进程 |
| `WorkerTaskBackend`（:126-150） | **恒为 no-op**——行已经在表里，下一轮 poll 自然被认领 | worker 进程（`worker/main.py:284-289`） |
| `SyncTaskBackend`（:95-123） | 立即内联执行整个管道 | 测试/基准，生产不用（hindsight-embed 拉起的是标准 API 进程） |

关键设计（`memory_engine.py:21584-21593`）：**payload 与行在同一个 INSERT 里原子落库**。注释明说旧实现"先 INSERT 行、再由 submit_task UPDATE 补 payload"，两步之间崩溃会留下 payload 为 NULL、worker 永远认领不了的行。

## 2. API 侧：请求如何变成队列里的一行

### 2.1 retain 的异步路径（主链路）

`POST /v1/default/banks/{bank_id}/memories`，分支开关就是请求参数 `async`（`http.py:9722`），没有别的模式参数：

1. **预处理**（API 进程内，入队前）：多模态内容扁平化并落盘附件（`http.py:9639-9666`），items 按 strategy 分组（:9668-9720）。
2. **每组一次 `submit_async_retain`**（`memory_engine.py:21821`）：
   - 幂等：客户端可自带 `operation_id`，重复提交重放旧结果，被别行占用则 409（:21792-21819）；
   - 按 token 预算切成子批次（:1305-1362）；
   - **单个事务里写入**：父行 `operation_type='batch_retain'`——**不写 task_payload，它只是个状态聚合器**（INSERT 列表中无该列，:22004-22014）；每个子批一行 `operation_type='retain'`，payload 含 `contents`、`_tenant_id`、W3C traceparent（跨进程续 trace，:22036）；单文档子批带 `serialization_key=document_id`（:22075）。
3. **事务提交后**调 `submit_task`（对 Broker 是 no-op，:22103-22104），立即返回 `{operation_id, items_count, async: true}`（`http.py:9743-9752`）——**不返回 document_id 或 facts 数量**，客户端靠轮询拿结果。

### 2.2 哪些操作走队列，哪些不走

| 不走队列（API 进程内同步执行） | 走队列的 operation_type |
|---|---|
| **recall**（`http.py:5967` 直接 `recall_async`） | `retain`（+ 父聚合器 `batch_retain`）、`file_convert_retain`、`consolidation`、`graph_maintenance`、`vector_index_maintenance`、`refresh_mental_model`、`export_documents/bank`、`import_documents/bank`、`clone_bank`、`webhook_delivery` |
| **reflect**（`http.py:6162` 直接 `reflect_async`） | |

**容易误判的一点**：同步 retain（`async=false`，默认）也不是"全程同步"——事实抽取和写库在 API 进程做（`http.py:9766` → `retain_batch_async`），但完成后 `_submit_post_insert_maintenance`（`memory_engine.py:6073-6108`）仍会把 consolidation、graph 维护、向量索引维护**三个任务入队**。也就是说**任何写操作之后的重活永远在 worker 侧**。

### 2.3 API 进程默认自带 worker

`HINDSIGHT_API_WORKER_ENABLED` 默认 **True**（`config.py:1833`，注释 "API runs worker by default (standalone mode)"）。FastAPI lifespan 里直接构造 `WorkerPoller` 并 `asyncio.create_task(poller.run())`（`http.py:4936-4961`）。因此**单进程部署时 API=Worker 是同一个进程**；要分离部署只需关掉这个开关、另起若干 `hindsight-worker` 进程，两者代码完全相同。

## 3. Worker 侧：如何认领任务

### 3.1 进程结构（`worker/main.py`）

`hindsight-worker` CLI → `MemoryEngine(task_backend=WorkerTaskBackend())`（不跑迁移）→ `WorkerPoller(executor=memory.execute_task)` → 与一个 metrics/health 小型 HTTP 服务并跑（:380-383）。

### 3.2 认领 SQL（`ops_postgresql.py`）

认领谓词（所有 claim 查询共有，:1787-1792）：

```sql
o.status = 'pending' AND o.task_payload IS NOT NULL
AND (o.next_retry_at IS NULL OR o.next_retry_at <= NOW())
AND {bank串行化谓词} AND {document串行化谓词}
```

两阶段（`claim_tasks` :1825-1898）：

1. **保留池**：每种 operation_type 一条查询，`ORDER BY created_at LIMIT n FOR UPDATE SKIP LOCKED`；默认只有 consolidation 有 2 个保留 slot，保证重活不被写活饿死（`config.py:939-951`）。
2. **共享池**：一条语句内做 **bank 亏空轮转**——`rot` CTE 用 `bank_id > $1`（游标走索引 seek）选出下一个该轮到的 bank，`fifo` CTE 兜底（:1794-1823）。**关键防竞态**：CTE 读行不加锁，所以外层查询**原样重复全部谓词**再 `FOR UPDATE OF o SKIP LOCKED`，"CTE 读取之后、加锁之前被别的 worker 抢走的行会自动落选而不是被领两次"（:1760-1763）。

认领后统一 `mark_operations_processing`：`SET status='processing', worker_id=$1, claimed_at=now()`（:1900-1917）。

### 3.3 公平性与互斥的三层设计

| 层 | 机制 | 位置 |
|---|---|---|
| 租户（schema） | 轮转起点 `_next_schema_idx` + Pass1 每 schema 每池最多 1 个 + Pass2 回填 | `poller.py:524-652` |
| bank | `bank_id > 游标` 亏空轮转；bank 级谓词：同 bank 的 consolidation/graph_maintenance 有 processing 行则不可再领 | `ops.py:147-244` |
| 文档 | **`serialization_key` 互斥**：同 `(bank_id, serialization_key)` 存在 processing peer 时不可认领，且必须是最老的待领 pending——这是跨 worker 的同文档锁，append 的读-改-写语义靠它（`ops.py:91-141`）。folding 只是其上的合并优化 | |

### 3.4 认领期优化：folding

领到单文档 retain 时，在同一 claim 事务内把同文档相邻 pending retain 合并进一次执行（`poller.py:730-828`）：先做全部可能失败的规划（token 预算），最后才 `mark_operations_processing`，保证"没有窗口使 peer 被折叠但未认领"。折叠组共享同一终态（`poller.py:841-855`）。

## 4. Worker 如何处理数据（retain 为例）

### 4.1 路由（`memory_engine.py:3999-4028`）

恢复 trace 上下文 → pop `_schema` 设入 contextvar（多租户）→ 查行状态已 cancelled 则跳过 → 按 `type` 分发到 12 个 handler（`batch_retain`/`file_convert_retain`/`import_documents`/`export_documents`/`export_bank`/`import_bank`/`clone_bank`/`consolidation`/`graph_maintenance`/`vector_index_maintenance`/`refresh_mental_model`/`webhook_delivery`；未知类型直接删行不重试）。

### 4.2 retain 管道（`engine/retain/orchestrator.py`）

**一次 retain ≠ 一个事务**，是按 mini-batch（`retain_chunk_batch_size` 个 chunk）逐段提交的流水线：

```
orchestrator.retain_batch (:1269)
 ├─ Memory Defense 预筛 (:1468-1532, redact/drop/block)
 ├─ append 模式：读旧文本前插 + stale-request 检查 (:1626-1763)
 ├─ delta 尝试：content_hash 分块 diff，只抽变化 chunk (:1805-1833)
 ├─ chunking（流式，逐 content）(:1853-1864)
 └─ _streaming_retain_batch (:2309)
     ├─ 恢复检测：content_hash + 已提交 chunk hashes → 跳过已抽取部分 (:2370-2444)
     ├─ 生产者（LLM）‖ 消费者（DB）asyncio.gather 并发 (:3317-3320)
     │   生产者（每 chunk mini-batch）:
     │     ① extract_facts_from_contents — ★LLM 调用，一次返回 事实+实体+时间+因果
     │        (fact_extraction.py:3352, LLM call :2085)
     │     ② 拼日期进嵌入文本 + generate_embeddings_batch — ★embedding 调用
     │        (embedding_processing.py:49-72)
     │   消费者（每 mini-batch）:
     │     ③ Phase 1: 实体解析（trigram+共现，无 LLM，独立 autocommit 连接）
     │        (entity_processing.py:84)
     │     ④ Phase 2: 一个 conn.transaction() 事务 (:3085-3086)
     │        - lock_document_for_write: SELECT..FOR UPDATE 文档行，串行化同文档写者
     │        - documents / chunks / memory_units / unit_entities / memory_links
     │          （temporal + causal 链接）逐表写入
     │        - retain.completed webhook 行与事实同事务插入（outbox）
     ├─ facts_committed 检查点写进 result_metadata（崩溃后直接跳到收尾）(:3418-3446)
     └─ 最终 ANN pass：读回 embedding 建语义链接 memory_links，best-effort (:1927-2024)
```

崩溃恢复靠两层：内容哈希比对跳过已抽取 chunk，加 `result_metadata.facts_committed` 检查点；并发正确性靠文档行 `FOR UPDATE` + hash 接管检查。

### 4.3 任务链：retain 完成后自动派生后续任务

worker 里执行的任务同样只是往 `async_operations` 写新行（`WorkerTaskBackend.submit_task` 是 no-op，下一轮 poll 被领）：

- retain 完成 → `_submit_post_insert_maintenance` → consolidation + graph_maintenance + vector_index_maintenance
- consolidation（`consolidator.py:1602`）：按观察 scope 分组（不同 scope 绝不共享 LLM 调用，#3924），每批一次 LLM 产出 CREATE/UPDATE/DELETE 观测，**一个 LLM 响应的所有写 = 一个事务**（:2676-2678）；轮次超限则自续 consolidation；全部排空后触发满足条件的 `refresh_mental_model`
- webhook 是**事务性 outbox**：`consolidation.completed` 的 UPDATE 与 webhook_delivery 行的 INSERT 在同一事务（`memory_engine.py:4738-4802`），投递器由 poller 领取执行

### 4.4 进度上报

engine 代码通过 `set_stage("retain.phase2.insert_facts")` 之类写 contextvar（`worker/stage.py:51-63`，无 holder 时 no-op，所以同一段代码在 API/CLI 里也安全）；poller 每 30 秒打 `[WORKER_TASK] stage=... stage_age=...`，停滞则转储协程栈。

## 5. 状态机与可靠性

### 5.1 状态机

CHECK 约束初始四值 + 后续迁移加 `cancelled`（`alembic/versions/5a366d414dce_initial_schema.py:237-239`）：

```
INSERT → pending ──claim──► processing ──┬─► completed（成功；consolidation 同事务入队 webhook）
   ▲           ▲              │          ├─► failed（异常 / wall-timeout / 确定性错误）
   │           │              │          └─► cancelled（API 协同取消）
   │           │              │
   │           │    RetryTaskAt（retry_count+1, next_retry_at=退避）
   │           └────┼─────────────────────────────────┘
   │       DeferOperation（不算重试：配额重置/存储背压/未到刷新间隔）
   │       崩溃恢复：本 worker 的 processing → pending（或超 max_retries → failed）
   │
   └─ 手动 API retry（failed|cancelled → pending, retry_count 清零）
```

防复活守卫贯穿所有非 cancel 写：`WHERE status <> 'cancelled'`（issue #4131，`poller.py:891,1021,1045`）。所有兄弟子批终态后，父 `batch_retain` 由聚合逻辑提升为终态（`poller.py:901-1004`）。

### 5.2 重试策略按任务类型分档

| 类型 | 退避 | 上限 |
|---|---|---|
| consolidation | 指数封顶 `min(5·2^n, 1800)s` | **无限重试**，靠"同 bank 已有 pending consolidation 则跳过"防风暴（`memory_engine.py:4124-4135`） |
| retain 等 | 固定 60s（`worker_task_retry_backoff_seconds`） | `worker_max_retries`（默认 3） |
| webhook_delivery | 固定序列 5s/300s/1800s/7200s/18000s | 6 次后永久失败 |
| 确定性错误（完整性违规、embedding 维度等） | 不重试，直接 failed | — |
| 存储背压（gRPC UNAVAILABLE 等） | defer 120s，**不消耗重试预算**（`worker/backpressure.py`） | — |

### 5.3 无租约设计——一个诚实的缺口

**没有 `claimed_expires_at`，没有租约回收**（全仓库 grep 零命中；认领查询只看 `status='pending'`）。processing 行永远不会被第二个 worker 收走，兜底全部是"worker 自认领"路径：

- 启动恢复 `recover_own_tasks`（`poller.py:1353-1395`，`run()` 的第一件事）
- 关机释放 `release_own_tasks`（:1397-1430，drain 超时后把残行还回 pending）
- 运维旁路 `hindsight-admin decommission-worker`（`admin/cli.py:1338-1346`）
- API cancel 也能解锁僵尸 processing 行（#4131）

作者自己在注释里承认：*"a row wedged in 'processing' holds its bank until something releases it... That is a general gap in claim recovery"*（`ops.py:195-198`）。后果是 bank/document 级串行化谓词会被 wedge 的行卡住，直到上述路径介入。

### 5.4 一处过时注释（交叉发现）

`poller.py:127-131` 引用 #3002 说 "the API refuses to retry *or* cancel" processing 操作——cancel 半句已过时：#4131 之后 `DELETE /operations/{id}` 支持协同取消 processing 行（`memory_engine.py:20894-20910`），运行中任务靠 `_check_op_alive` 在批次边界检查点退出（`memory_engine.py:4433-4455`）。retry 半句仍正确（retry 端点只收 failed/cancelled）。

## 6. 客户端可见性（补充）

- 提交异步 retain 后立即得到 `operation_id`（多 strategy 组时为 `operation_ids`）；无 document_id、无 facts 数。
- 轮询端点：`GET .../operations`（列表，支持 status/type 过滤）与 `GET .../operations/{id}`（详情，`include_payload=true` 可回看原始提交参数；父操作附 `child_operations`）。
- 完成后 `result_metadata` 携带：retain → `unit_ids_count`（事实数）、`extraction_errors_count/sample`；consolidation 进度由 `progress` 字段（stage/processed/total）暴露；import/export → counts/下载 URL；webhook_delivery → 每次尝试的状态码与响应体。
- webhook 事件仅三种：`retain.completed`、`consolidation.completed`、`memory_defense.triggered`（`webhooks/models.py:9-12`）。

## 7. 设计取舍小结

| 选择 | 收益 | 代价 |
|---|---|---|
| **PG 表作队列**（无外部 MQ） | 零新增组件；任务与业务数据同库同事务（outbox 天然原子）；多租户下每 schema 一张队列表 | 轮询延迟（默认 500ms poll interval）；DB 成为吞吐上限 |
| **API 默认内嵌 worker** | 开箱即用单进程 | 部署者可能没意识到要分离；重活与 API 共享事件循环 |
| **payload 与行同 INSERT** | 消除"空行"竞态 | — |
| **无租约** | 实现简单，claimed 行绝不会被重复执行 | crash 后恢复依赖 worker 自觉 + 运维路径；wedge 行会卡住串行化谓词（作者承认的缺口） |
| **mini-batch 事务 + 检查点** | 长 retain 可恢复、可汇报进度 | 单文档原子性弱于一个大事务（靠恢复机制补） |

---

## 附：验证方式说明

- **直接核对**（本会话亲自读码）：`http.py:4938-4961`（内嵌 poller）、`http.py:9722-9752`（async 分支）、`memory_engine.py:3999-4028`（路由）、`memory_engine.py:6073-6108`（post-insert maintenance）、`memory_engine.py:22004-22076`（父子行 INSERT）、`orchestrator.py:3085-3086`（mini-batch 事务）、`ops_postgresql.py:1787-1823`（共享池认领 SQL）、`ops.py:91-141`（文档串行化谓词）、`5a366d414dce_initial_schema.py:217-240`（状态约束）。
- **subagent 读码 + 双源交叉印证**：重试分档、webhook outbox、状态机转换、表结构迁移、多租户 schema 发现。
- **单源结论**（已尽力核对但未二次独立验证）：Oracle 认领查询的 ROWNUM 改写细节（引自 `ops_oracle.py:1461-1476` 注释）。
