# Hindsight Serverless 多租户架构方案：基于源码的可落地设计

**结论先行：Hindsight 的代码已经为"共享计算池 + 独立持久数据面"的 Serverless 形态打好了大部分地基——异步执行模型是数据库拉取式队列（`async_operations` + `FOR UPDATE SKIP LOCKED` + SQL 谓词串行化），租户模型是 schema-per-tenant 全限定表名（不依赖 `search_path`，天然兼容连接池代理），推理组件（LLM/Embedding/Reranker）全部有远程 Provider 实现。需要开发的重点是平台层（前门、租户注册表、配额计量、worker 回收控制器），引擎侧只需少量开关补丁。形态选择容器 Serverless（Knative/KEDA 或托管等价物），不是 FaaS。**

分析基线：Hindsight `12f2d54f643baddacb98cd547c89b1a50c5c3dcc`（main，2026-09）。下文 `core/` 指 `hindsight-api-slim/hindsight_api/`，其余路径相对仓库根。本文区分**源码事实**（带 file:line）、**设计建议**（标 🟧，需开发）与**待验证假设**（第 10 节集中列出）。未部署、未压测；所有容量与延迟数字均为需实测的预算目标，不是承诺。

颜色图例（沿用 memory-platform-architecture 约定）：🟦 Hindsight 已有开源组件；🟩 其他开源组件；🟧 需开发的平台逻辑；🟪 商业/托管组件；灰色为外部客户侧。颜色表示来源，不表示已集成或已验收。

本方案与已有研究的关系：整体拓扑沿用 [V6 共享集群与 Serverless](../cognee/v6-cluster-serverless/architecture.md) 的 Cell/Query-Ingest 分层思想；整引擎替换边界沿用 [V4 MemoryProvider 设计](../cognee/v4-engine-plugins/architecture.md)；Hindsight 运行时事实与 [11-hindsight-runtime 证据](../cognee/evidence/11-hindsight-runtime.md) 一致并在此基础上展开。本文是 Hindsight 视角的对应落地设计。

---

## 1. 源码事实：Serverless 化的既有基础

### 1.1 请求面已经接近无状态

| 事实 | 代码证据 | 对 Serverless 的意义 |
|---|---|---|
| recall/reflect 是只读路径，不持久化任何请求状态 | reflect 内联执行、可被客户端断连取消（`core/engine/memory_engine.py:15050`、`15192` 墙钟 300s）；recall 无写路径 | 任意实例可服务任意请求，无粘性 |
| 租户上下文是 per-request 的 contextvar，不是会话 | `_current_schema` ContextVar（`memory_engine.py:92`），每个引擎方法调用 `_authenticate_tenant` 设置（`memory_engine.py:2855-2892`） | 无会话亲和；async 安全 |
| 所有 SQL 用全限定表名，**从不把租户 schema 放进 `search_path`** | `fq_table()` 返回 `schema.table`（`core/engine/schema.py:16-27`）；运行时守卫 `validate_sql_schema` 拒绝非限定表名（`memory_engine.py:460-510`）；有测试明确断言此行为（`tests/test_schema_mode_extensions.py`） | 连接无会话状态依赖 → **PgBouncer transaction 模式直接可用**，这是 Serverless 连接管理的决定性前提 |
| 连接层已为 pooler 适配 | `statement_cache_size=0`、`_NoResetConnection` 省去释放时 RESET 往返、`application_name` 每次 acquire 重设、会话 GUC 每次 acquire 用一条批量 `SELECT set_config(...)` 重放（`core/engine/db/postgresql.py:42-94, 160-218, 305-315`；`HINDSIGHT_API_DB_SESSION_SETUP_ON_ACQUIRE`） | 事务级 pooler 下行为正确，issue #3499/#3491 就是为此修的 |
| 运行时禁用 advisory lock | 索引 DDL 锁为进程内 asyncio 锁 + 重试吸收跨进程竞争（`core/engine/db/ops_postgresql.py:77-80`） | 不会因 pooler 事务边界导致锁悬挂 |
| MCP 有无状态模式 | `HINDSIGHT_API_MCP_STATELESS=true`（`config.py:678`） | SSE 有状态会话不钉住实例 |
| 读路径可走只读副本 | `HINDSIGHT_API_READ_DATABASE_URL` 独立副本连接池（`memory_engine.py:5290-5305`） | 读写分离开箱即用 |

### 1.2 异步面已经是"数据库拉取式队列"

这是对 Serverless 最有利的事实：**整个后台执行模型不需要引入任何消息 broker**。

| 机制 | 代码证据 | Serverless 含义 |
|---|---|---|
| 任务 = `async_operations` 表行（每租户 schema 一份），含 status/worker_id/retry/serialization_key/payload | 初始迁移 `core/alembic/versions/5a366d414dce...py:217-243` + `l7g8h9i0j1k2` worker 列 | 队列与数据同库同事务（**事务性入队**），天然高可靠 |
| 领取 = `FOR UPDATE SKIP LOCKED` | `core/engine/db/ops_postgresql.py:1598-1724` | **任意数量 worker 并发安全**——临时性（ephemeral）worker 就是安全的 |
| 并发控制在 claim SQL 谓词里，不在进程内存 | `bank_serialization_sql`：每 bank 同时只有一个 consolidation/graph_maintenance 在飞，且批内只领最旧 pending（`core/engine/db/ops.py:144-198`，含设计说明）；`document_serialization_sql`：同文档 retain 按提交顺序领取 | 跨进程正确性由数据库保证；进程可以随时生死 |
| 提交时去重（幂等入队） | `_submit_async_operation` 用 `FOR NO KEY UPDATE` 锁 bank 行 + `dedupe_by_bank`/`dedupe_in_flight_payload_key`（`memory_engine.py:21535, 21597-21632`） | 多实例重复提交无害 |
| webhook 是事务性 outbox | 投递行与业务写同事务插入（`memory_engine.py:4209-4281`；`webhooks/manager.py`） | at-least-once 投递，无丢失窗口 |
| 重试/延期/墙钟上限 | `RetryTaskAt`、`DeferOperation`、按操作类型的 wall ceiling（`core/worker/poller.py:79-149`） | 故障恢复协议已在引擎内 |
| Worker 与 API 是同一二进制的两种进程形态 | `hindsight-worker` 独立入口（`core/worker/main.py`）；API 内置 worker 默认开启，可关（`HINDSIGHT_API_WORKER_ENABLED`，默认 true，`config.py:917, 5053`）；Helm 已按此拆分（`helm/hindsight/templates/worker-statefulset.yaml:41`，`api-deployment.yaml:62-64`） | **分层部署是官方支持的形态，纯配置可达** |
| Worker 身份恢复 | worker_id 默认 hostname，容器内有专门告警（`core/utils.py:29-49`）；启动只回收**自己**的 processing 行（`poller.py:1295-1332`）；优雅退出释放自己的任务（`poller.py:1752-1756`）；`hindsight-admin decommission-worker` 手动回收（`admin/cli.py:1327-1347`）；Helm 用 StatefulSet pod 名注入 worker_id（`worker-statefulset.yaml:62-66`） | 身份协议存在但**无租约自动接管**（见 2.4 障碍） |
| 维护循环幂等、无 leader 选举 | `MaintenanceLoop` 在每个进程运行，靠事务内去重 + SKIP LOCKED 分块删除保证安全（`core/engine/maintenance.py:30-52, 104-235`）；跨租户发现走 PL/pgSQL 例程 `public.schemas_with_pending_work()`（迁移 `b6d2f8a4c1e7`） | 多副本天然安全，但需要控制成本（见 2.3） |
| 维护任务全量清单 | retention 清扫（1h）、consolidation 对账（300s）、mental-model cron 刷新扫描（300s）、终态操作清理（900s）；图/向量索引维护；12 种任务类型见 `memory_engine.py:3999-4028` | 后台作业清单完整可枚举 |

### 1.3 推理组件的远程化缝隙已存在

| 组件族 | 抽象 | 已有远程实现 | 引用 |
|---|---|---|---|
| LLM | `LLMInterface` ABC（`call`/`call_with_tools` + 能力协商方法） | 30+ provider id（openai/anthropic/gemini/vertexai/litellm/bedrock/deepseek/groq/minimax/fireworks/openrouter/zai/…），多成员故障转移链 `MultiLLMProvider` | `core/engine/llm_interface.py:61-415`；工厂 `llm_wrapper.py:447-814` |
| Embeddings | `Embeddings` ABC（`provider_name`/`dimension`/`initialize`/`encode` + 统一批处理/限流） | tei（原生 HTTP）、openai、cohere、zeroentropy、litellm(-sdk)、google 等 13+ | `core/engine/embeddings.py:109-274, 2090-2316` |
| Reranker | `CrossEncoderModel` ABC | tei、cohere、siliconflow、zeroentropy、litellm(-sdk)、google、alibaba、openrouter 等 14+；`FlashRankCrossEncoder` 文档注释明说是为 serverless 设计（ONNX CPU、无 torch） | `core/engine/cross_encoder.py:60-142, 1134-1136, 2034-2203` |

关键冷启动事实：全远程化 + 声明维度（如 `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS`）+ 跳过 LLM 探活（`HINDSIGHT_API_SKIP_LLM_VERIFICATION`）后，`initialize()` 退化为 DB 连接 + 少量网络探测（TEI 的 `/info` + 测试嵌入，带重试）。Helm 已有可选 TEI sidecar 部署模板。

### 1.4 租户与配置层级

- **租户 = schema，bank = schema 内的业务分区**：`TenantExtension.authenticate(RequestContext) → TenantContext(schema_name)` 是唯一契约（`core/extensions/tenant.py:24-34, 83-111`），每请求调用、服务端不缓存。`RequestContext` 携带 `api_key/tenant_id/api_key_id/allowed_bank_ids/extra_headers`（`core/models.py:14-51`；额外 header 需 `HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS` 显式放行）。
- **配置三级合并**：全局 env → 租户（扩展回调 `get_tenant_config`，每请求、不缓存）→ bank（`banks.config` JSONB，进程内 30s TTL 缓存，`HINDSIGHT_API_BANK_INFO_CACHE_TTL_SECONDS`）；约 50 个可配置字段；**凭据字段永不进 bank 配置**（`_CREDENTIAL_FIELDS`，写路径强制拒绝，`config_resolver.py:524-547`）。合并是浅拷贝 setattr，专为热路径设计（`config_resolver.py:209-265`）。
- **配额/计量钩子已预留**：`OperationValidatorExtension` 19 个钩子，含 body 解析前的 `precheck`（可 402 拒绝，`api/http.py:5281-5347`）和携带 token 用量（`llm_input_tokens/llm_cached_input_tokens/processed_content_tokens`）的 `on_retain_complete` 等完成钩子（`extensions/operation_validator.py:236-291`）——**为计费而设计，只差实现**。
- **bank 级操作面**：`filter_bank_list`、bank 读写校验钩子存在；核心引擎不强制 `allowed_bank_ids`（是声明的死表面）——**bank ACL 必须由平台在 validator/前门实现**。

### 1.5 部署基线

Helm chart（`helm/hindsight/`）已提供：API Deployment + HPA + PDB + ingress + ServiceMonitor、可选 worker StatefulSet、可选 TEI 部署、内置或外部 PG。13 个 docker-compose 变体（external-pg、tei、s3-file-storage 等）证明外置组件路径已被验证。官方托管产品 Hindsight Cloud 的存在（docstring 多处引用 `hindsight_cloud` 私有扩展包）印证这些扩展槽就是其 SaaS 的实现路径。

---

## 2. 源码事实：Serverless 化的障碍清单

| # | 障碍 | 代码位置 | 影响 | 对策（节） |
|---|---|---|---|---|
| 2.1 | 默认数据库是**进程内嵌 pg0 Postgres** | `database_url` 默认 `"pg0"`（`memory_engine.py:2199`；`core/pg0.py`） | 与"数据面独立于计算面"根本冲突 | 配置：外部 DATABASE_URL（§7.1） |
| 2.2 | **启动时默认全 schema 迁移** | `HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP` 默认 true（`config.py:5026`）；启动扇出迁移所有 `list_tenants()` schema（`memory_engine.py:5162-5211`） | 冷启动要遍历全部租户 schema；2 万 schema 量级"大半小时"（迁移模块按此调优，`migrations.py:287-325`） | 配置关闭 + 控制面迁移 Job（§4.4、§7.1） |
| 2.3 | **MaintenanceLoop / 轮询循环在每个进程无条件启动** | `MemoryEngine.initialize()` 启动（`memory_engine.py:5404-5410`），无开关 | API 层每个实例都做发现扫描，副本多时浪费且放大 DB 压力 | **需小补丁**：加开关，API 层关闭（§7.2-补丁1） |
| 2.4 | **Worker 无租约接管**：心跳只记录进度不是租约；恢复只认自己的 worker_id | 进度写 `result_metadata.progress`（`memory_engine.py:4457-4498`）；恢复 `_reclaim_own_processing_tasks` 仅本 worker（`poller.py:1295-1332`） | 节点丢失/SIGKILL 后，processing 行卡住并**钉死该 bank** 的 consolidation（`ops.py:195-198` 明示此 gap） | 平台回收控制器：watch pod 终止 → `decommission-worker`（§3.4、§7.2-补丁3） |
| 2.5 | JIT 租户开通在**请求路径上跑完整 Alembic** | 扩展调 `ExtensionContext.run_migration(schema)`（`extensions/context.py:105-159`），static-keys/Supabase 扩展带进程内缓存与 advisory lock（`migrations.py:199-208, 417-461`） | 冷启动实例反复触发（advisory lock 保证安全但慢）；开通延迟不可控 | 开通移到控制面异步 Job，请求路径只读注册表（§4.4） |
| 2.6 | **Embedding 维度是引擎级单例**，绑定 pgvector 列宽 | 引擎单实例 embeddings；列宽迁移仅空表可行（`migrations.py:623-693`）；每次调用校验维度（`retain/embedding_utils.py:137`） | 不能按 bank 换 embedding 模型；全库换维度 = 全量重嵌入 | Cell 级维度冻结 + 新 Cell 迁移（§8.2） |
| 2.7 | 多 worker 指标用**本地 tempdir + flock** | `core/metrics_multiworker.py:58-80`（fcntl，POSIX-only，按 ppid 目录） | 多节点不工作 | 不设 `HINDSIGHT_API_METRICS_WORKER_LABEL`，改 OTel push（§7.1） |
| 2.8 | 本地模型与子进程依赖 | 默认 `local` embeddings/reranker（SentenceTransformers/ONNX）；llama.cpp 子进程 + `~/.hindsight/models`；OAuth 凭据 JSON 文件锁（`providers/llamacpp_llm.py:43-53`、`oauth_store_lock.py:54-121`） | 镜像体积、内存、文件系统假设 | 全远程 Provider（§7.1） |
| 2.9 | 同步 retain 内联跑 LLM（每 chunk 一次调用） | 默认 `async=false` 全流水线在请求内（`api/http.py:1309-1313` → `retain_batch_async`，`memory_engine.py:5681`）；LLM 调用数 ≈ chunk 数（`fact_extraction.py:2600-2621`） | 请求分钟级，与归零/短超时冲突 | 平台 precheck 按 content_length 强制大内容走 async（§3.2） |
| 2.10 | 控制面**单租户硬编码** | 全局唯一 dataplane URL + 共享 key（`hindsight-control-plane/src/lib/hindsight-client.ts:14-15`） | 不能直接当多租户控制台 | 平台自建前门；控制面留给单租户自托管（§3.3） |
| 2.11 | 连接池默认 min 5 / max 100 | `config.py:1772-1776` | 实例扇出打爆 PG 连接 | 调小 + PgBouncer（§5.6） |
| 2.12 | `CREATE INDEX CONCURRENTLY`（向量索引维护）需要 autocommit 专用会话 | 向量维护在裸连接上跑（`memory_engine.py:22525-22594`；`engine/vector_index_health.py:320-407`）；迁移用 session advisory lock（`migrations.py:370-375` 明确 pooler 场景需直连） | pooler 后这些路径会坏 | `HINDSIGHT_API_MIGRATION_DATABASE_URL` 直连旁路（§5.6） |
| 2.13 | schema 级删除工具缺失 | bank 删除 API 有（`api/http.py:8212-8225`）；admin CLI 无 drop-schema/decommission 租户命令 | 租户注销需新工具 | 🟧 租户删除 Job（§6.5） |

---

## 3. 目标架构总览

### 3.1 总架构图

~~~mermaid
flowchart TB
  C["SDK / MCP 客户端 / coding-agents / 平台产品 UI"]:::external

  subgraph PLAT["平台层 🟧（需开发）"]
    FD["平台前门 / 产品 API<br/>终端用户认证 · 租户→Cell 路由 · bank ACL · 限流"]:::platform
    REG[("平台元数据库（独立 PG）<br/>tenants / api_keys / bindings / quotas / usage_events")]:::oss
    CTRL["平台控制器<br/>租户开通 Job · 版本升级 · worker 回收(decommission)"]:::platform
    OBS["OTel Collector / Prometheus<br/>指标·追踪·审计汇聚"]:::oss
  end

  C --> FD
  FD --> REG
  CTRL --> REG

  subgraph CELL["共享 Cell A（一个 K8s 命名空间 = 一组兼容运行配置）"]
    direction TB
    subgraph API["API 层：无状态请求面 0..N（可归零）"]
      A1["hindsight-api 容器<br/>recall / reflect / 同步 retain / 异步受理<br/>worker=off · migrations=off · 远程推理"]:::hs
    end
    subgraph WK["Worker 层：常驻拉取 1..M（HPA/KEDA）"]
      W1["hindsight-worker 容器<br/>retain 流水线 / consolidation / 知识页刷新<br/>MaintenanceLoop · 稳定 worker_id"]:::hs
    end
    TEI["TEI 服务<br/>embeddings + rerank（可换任意远程 Provider）"]:::oss
  end

  FD -->|"内部服务凭据 + 用户断言 header"| A1
  subgraph DATA["持久数据面（独立于计算生死）"]
    PB["PgBouncer<br/>transaction 模式"]:::oss
    PG[("托管 PostgreSQL + pgvector 🟪<br/>public: 跨租户发现例程<br/>tenant_x schema ×N: 每租户 22 表<br/>tenant_y schema ×N: …")]:::oss
    RO[("只读副本（可选）")]:::oss
    S3[("对象存储 S3/GCS/Azure<br/>文件原件（file_storage 后端）")]:::oss
  end
  A1 --> PB
  W1 --> PB
  PB --> PG
  PG --> RO
  A1 -.读路径可选.-> RO
  WK -.迁移/CONCURRENTLY 直连旁路.-> PG

  subgraph EXT["外部推理"]
    LLM["LLM API（openai/anthropic/gemini/…）"]:::external
  end
  A1 --> LLM
  W1 --> LLM
  A1 --> TEI
  W1 --> TEI
  W1 -->|"webhook（事务性 outbox）"| C

  KEDA["KEDA / HPA<br/>按队列深度指标扩缩 Worker"]:::oss -.->|"读 backlog 指标"| PG
  KEDA -.-> W1
  KN["Knative / 托管容器平台<br/>API 层按请求扩缩与归零"]:::oss -.-> A1
  A1 --> OBS
  W1 --> OBS

  CELLB["专属 Cell B（强隔离/合规/独立地域租户）<br/>同一套镜像与配置模板，独立 PG 与推理池"]:::platform
  FD -.放置策略.-> CELLB

  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

图中的每条边都对应已存在的代码路径：前门→API 用内部凭据 + `HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS` 放行的用户断言（`tenant.py:70-81`）；API→PG 全限定表名（§1.1）；API→`async_operations` 受理即落盘；Worker 拉取（§1.2）；Worker→LLM/TEI 走既有 Provider（§1.3）；webhook 走 outbox。

### 3.2 计算分层与形态选择：为什么是容器 Serverless 而非 FaaS

| 执行角色 | 形态 | 从零唤醒 | 归零边界 |
|---|---|---|---|
| API 层（recall/reflect/小 retain/异步受理） | Knative KPA 或 Cloud Run/Fargate 等托管容器 | 共享入口收 HTTP 触发副本 | 无在途请求即可归零；生产 cell 建议保留 min 1-2 热副本保 recall 延迟 |
| Worker 层 | StatefulSet + HPA（首选）或 KEDA | 常驻 min 1（拉取模型必须有人守着队列） | 缩容走优雅退出（已有 30s drain + 释放自己的任务）；**不建议归零** |
| 租户开通/版本升级/回收 | Kubernetes Job / 定时控制器 | 控制面事件 | Job 等真实终态后退出 |
| 推理（TEI） | 常驻 Deployment（可再伸缩） | 不归零 | 按指标伸缩 |
| 持久层 | 托管 PG（多 AZ + PITR） | 不归零 | 存储费用常在；"Serverless"承诺的是计算弹性与免运维，不是零账单 |

**不选 FaaS（Lambda 等）的代码依据**：① 引擎 import 实测约 6.2s（`core/main.py:45-58` 注释记录了优化过程），加 `initialize()` 探测，FaaS 每次冷启动代价过高；② 同步 retain 请求内联跑 LLM 分钟级（§2.9），与 FaaS 时限和计费模型冲突；③ `WorkerPoller` 是 500ms 常驻轮询（`config.py:1835`），FaaS 请求驱动模型需要重写引擎执行模型；④ asyncpg 长连接池 + 长事务与 FaaS 冻结/回收冲突。容器 Serverless 用容器镜像层吃掉 import 成本，长请求可配置到分钟级超时，与现有代码零冲突。

**同步/异步 retain 的平台策略**（利用现有 precheck）：平台 validator 在 `precheck`（body 解析前，拿得到 `content_length`，`operation_validator.py:98-125`）按内容长度阈值强制大内容走 `async=true`（受理即持久化为 `async_operations` 行，返回 202 + operation_id——引擎原生语义）。同步路径只留小内容，API 层归零才可行。

### 3.3 Cell 划分（沿用 V6 概念，落到 Hindsight 的约束上）

Cell = 一个 K8s 命名空间 + 一个 PG 实例 + 一个推理池 + 一份引擎运行配置（版本、embedding 模型与**维度**、ANN/全文扩展选择）。划分依据来自代码的三个硬约束：

1. **维度单例**（§2.6）→ 换 embedding 模型（维度变）= 开新 Cell，不在 Cell 内换。
2. **`TenantContext` 只能返回 schema 名，不能选数据库**（§1.4）→ 不同 PG 实例 = 不同 Cell = 不同 Hindsight 部署实例；**跨 Cell 分区在部署层做，不改引擎**。
3. **凭据是进程级静态配置**（§1.4）→ LLM/推理服务凭据按 Cell 配置，符合"不同信任等级/地域分池"。

租户→Cell 放置策略由平台注册表决定（默认共享 Cell；强隔离/合规/大客户 → 专属 Cell；地域驻留 → 对应地域 Cell）。共享 Cell 内部才是 schema-per-tenant（§6）。

### 3.4 新增平台组件清单（🟧 全部代码量估计）

| 组件 | 职责 | 实现缝隙（全部走已有扩展槽/CLI，不改引擎核心） | 预估规模 |
|---|---|---|---|
| 平台前门 | 终端认证、租户→Cell 路由、bank ACL、限流 | 自研网关服务；数据面鉴权走 `TenantExtension` + passthrough headers | 中 |
| 平台租户扩展 | 内部凭据验证 → 查注册表（缓存）→ 返回 schema | `TenantExtension` 子类（参考 static-keys 扩展结构，`hindsight-extensions/static-keys-tenant/.../extension.py:263-321`） | 小 |
| 平台 validator | precheck 配额/402、大内容强制 async、on_complete 计量（token 字段已备好）、bank ACL、`filter_bank_list` | `OperationValidatorExtension` 子类 | 中 |
| 租户开通控制器 | 调 `hindsight-admin run-db-migration --schema`（`admin/cli.py:622-674`）为 Job；管理租户状态机 | K8s Job + 注册表状态机 | 小 |
| Worker 回收控制器 | watch pod 终止/驱逐 → `decommission-worker <id>`；兜底对账超时 processing 行 | K8s controller 或 CronJob 对账（查 worker-status CLI 已有） | 小 |
| 租户删除工具 | bank 级已有 API；schema 级 drop + 对象清理需新工具 | admin CLI 扩展 | 小 |
| 引擎 MaintenanceLoop 开关 | API 进程不跑维护循环 | 引擎小补丁（§7.2-补丁1） | 极小 |

---

## 4. 数据流转

### 4.1 retain（异步）端到端

~~~mermaid
sequenceDiagram
  autonumber
  actor C as 客户端
  participant FD as 平台前门 🟧
  participant API as API 实例（无状态 🟦）
  participant PG as 引擎 PG（tenant schema）
  participant SC as 伸缩器 KEDA/HPA 🟩
  participant W as Worker 实例 🟦
  participant INF as TEI / LLM API

  C->>FD: POST /retain（大内容，precheck 判定走异步）
  FD->>API: 转发（内部凭据 + 用户断言 header）
  API->>API: TenantExtension.authenticate → schema（contextvar）
  API->>PG: 事务：父 batch_retain + 子操作行<br/>（payload 内嵌 _schema；bank 行锁去重）
  API-->>C: 202 + operation_id（受理已持久化，实例可死）
  Note over SC,W: 此时可以没有任何热 Worker
  SC->>PG: 读队列指标（backlog gauge / schemas_with_pending_work）
  SC->>W: 扩容
  W->>PG: list_tenants() 发现 schema；claim FOR UPDATE SKIP LOCKED<br/>（bank/文档串行化谓词 + 同文档折叠）
  loop 每 chunk 批次（流式 mini-batch，逐批提交）
    W->>INF: 事实抽取 LLM（每 chunk 1 次）+ 批量 embedding
    W->>PG: Phase1 实体解析（trigram，DB 内算法无 LLM）<br/>Phase2 事务插入 facts/entities/links（content_hash 归属校验）<br/>Phase3 语义 ANN 补链
  end
  W->>PG: 提交 consolidation（水位标记，按 bank 去重）<br/>webhook outbox 同事务落盘
  W->>PG: 标记操作终态；进度/心跳写 result_metadata
  W->>W: 拉起 webhook_delivery / consolidation 任务（同队列）
  C->>FD: GET operation 状态 / 接收 webhook（HMAC 签名）
~~~

要点：**受理与执行解耦点 = `async_operations` 行**（第 4 步）。API 实例在 202 之后随时可死，不影响任务；Worker 死亡由 worker_id 恢复协议 + 回收控制器兜底（§7.2）。consolidation 完成后按 `refresh_after_consolidation` 触发知识页（mental model）刷新，或由 MaintenanceLoop 的 cron 扫描补发——两条路径都有 in-flight 去重（`memory_engine.py:22643-22734`）。

### 4.2 recall（同步读路径）

~~~mermaid
sequenceDiagram
  autonumber
  actor C as 客户端
  participant FD as 平台前门 🟧
  participant API as API 实例 🟦
  participant CFG as 配置解析
  participant PG as 引擎 PG（可走只读副本）
  participant INF as TEI（embedding + rerank）

  C->>FD: POST /recall
  FD->>API: 转发
  API->>API: authenticate → schema（contextvar）
  API->>CFG: resolve_full_config：全局 env ← 租户扩展回调 ← banks.config（30s TTL 缓存）
  API->>INF: 查询 embedding（encode_query）
  par 四路并行检索（全部 WHERE bank_id，schema 限定）
    API->>PG: 语义：HNSW 部分索引（per bank × fact_type）
  and
    API->>PG: BM25：search_vector（5 种后端之一）
  and
    API->>PG: 图：memory_links 链接扩展
  and
    API->>PG: 时间：事件字段过滤
  end
  API->>API: RRF 融合
  API->>INF: cross-encoder rerank（可按 bank 配置关闭）
  API-->>C: 结果（全程只读，不写任何持久状态）
~~~

要点：recall 不写业务状态，实例完全可换。可观测性写入（audit/llm_trace）是 fire-and-forget，进程死即丢（引擎接受的有损语义，`engine/audit.py:183-196`）——平台若需精确计量，以 validator 的 on_complete 钩子为准（钩子在请求内同步执行）。

### 4.3 consolidation 闭环（后台）

水位驱动：`consolidated_at IS NULL` 的记忆按 observation scope 分组 → LLM 批次（每批一次主调用 + 逐动作 embedding + 可选去重仲裁调用）→ 短事务写 observations + 打水位 → 触发 mental model 刷新（`consolidator.py:1602-2245`；每 scope 策略是最新特性 `4133ae85c`，`consolidator.py:1089-1268`）。并发安全三防线：提交去重（bank 行锁 + pending-only）→ claim 谓词（每 bank 一个在飞）→ 进程内 per-scope 锁（仅排序单 run 内的 LLM 批次）。**已知缺口**：卡在 processing 的行钉死该 bank（§2.4），是回收控制器存在的第一理由。

### 4.4 租户开通与版本迁移

~~~mermaid
sequenceDiagram
  autonumber
  actor OP as 运营/自助注册
  participant PC as 平台控制器 🟧
  participant REG as 平台元数据库 🟧
  participant JOB as 迁移 Job（hindsight-admin 🟦）
  participant PG as 引擎 PG

  OP->>PC: 创建租户（plan / cell / 地域）
  PC->>REG: INSERT tenants(status=provisioning) + api_keys
  PC->>JOB: 启动 Job：run-db-migration --schema tenant_x<br/>（直连 PG，绕过 pooler；advisory lock 防并发）
  JOB->>PG: CREATE SCHEMA + 22 表 + 每租户例程 + 维度/向量扩展 + 扩展自有表
  JOB-->>PC: 成功/失败（重试策略）
  PC->>REG: status=active
  Note over PC,REG: 数据面 TenantExtension 此后只读注册表（进程内缓存）<br/>请求路径零 DDL（对比现状 JIT 开通的请求路径 Alembic，§2.5）
  PC->>JOB: 引擎版本升级 = 同一 Job 机制全 schema 迁移（ProcessPool 并发，可分批）
~~~

---

## 5. 数据存储设计

### 5.1 存储拓扑

~~~mermaid
flowchart TB
  subgraph EPG["引擎 PG（每 Cell 一个 🟪托管/🟩自建）"]
    direction TB
    PUB["public schema<br/>跨租户发现例程：schemas_with_pending_work()<br/>banks_needing_consolidation() 等 PL/pgSQL"]:::oss
    TA["tenant_a schema（≈22 表）"]:::oss
    TB2["tenant_b schema（同构）"]:::oss
    TN["tenant_n schema（同构）"]:::oss
  end
  subgraph PPG["平台 PG（独立实例或独立库+独立凭据 🟧）"]
    REGT["tenants / api_keys / bindings<br/>quotas / usage_events / provision_jobs"]:::platform
  end
  OBJ[("对象存储<br/>documents 原件 / attachments（S3/GCS/Azure 后端，引擎已实现）")]:::oss
  K8S[("K8s Secrets / 云 KMS<br/>LLM·推理·DB 凭据（引擎强制：凭据永不入 bank 配置）")]:::platform

  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

### 5.2 引擎库：每租户 schema 的表清单（源码事实）

权威清单 = admin CLI 的 `BACKUP_TABLES`（`core/admin/cli.py:58-83`，CI 断言与真实 schema 一致），按职责分组：

| 职责 | 表 | 存储要点 |
|---|---|---|
| 记忆主体 | `documents`, `chunks`, `memory_units`, `invalidated_memory_units`, `attachments`, `file_storage` | `memory_units` 是核心：text + **embedding 向量列** + 时间字段 + `fact_type('world'/'experience'/'observation')` + JSONB metadata + `consolidated_at` 水位；向量索引是 **per (bank × fact_type) 的部分 HNSW 索引**（迁移 `d5e6f7a8b9c0`），新 bank 建时即建、健康扫描对账（`engine/vector_index_health.py:320-407`） |
| 图与实体 | `entities`, `unit_entities`, `entity_cooccurrences`, `memory_links` | 图 = 关系表 + 查询逻辑；实体名 bank 内唯一 `(bank_id, LOWER(canonical_name))` + trigram GIN；复合外键 `(document_id, bank_id)` 跨表约束 bank 归属 |
| 派生知识 | `mental_models`, `mental_model_history`, `observation_history`, `knowledge_pages`, `directives` | mental models 自带 embedding + search_vector（可检索）；知识页 = 固化的 mental model 树 |
| 任务与操作 | `async_operations`, `graph_maintenance_queue`, `entity_maintenance_queue`, `webhooks` | **队列即表**：status/worker_id/claimed_at/retry/serialization_key；outbox 行 |
| 审计与观测 | `audit_log`, `llm_requests` | 按 bank 配置开关；retention 由 MaintenanceLoop 清扫 |
| 配置 | `banks` | `bank_id` 主键 + `disposition` + `config` JSONB（层级配置第三级） |

另有携带向量列的派生表（`migrations.py:731-735`）。ANN 后端（pgvector-HNSW / vchord / pgvectorscale-DiskANN / AlloyDB ScaNN）与全文后端（native tsvector / vchord bm25 / pgroonga / pg_search / pg_textsearch）均按 Cell 配置选择并在迁移时对账（`migrations.py:696-1353`）。

### 5.3 平台元数据（🟧 新增，存平台 PG）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `tenants` | tenant_id, schema_name, cell_id, status(provisioning/active/frozen/deleting), plan, region | schema 命名规则约束 63 字节（NAMEDATALEN，static-keys 扩展已处理碰撞） |
| `api_keys` | key_hash, tenant_id, scopes, allowed_bank_ids, status, rotated_at | 终端用户 key 只存哈希；数据面用独立内部凭据 |
| `bindings` | space/bank → provider_kind, provider_instance(Hindsight Cell), binding_revision | **整引擎替换的切换点**（§8.3） |
| `quotas` / `usage_events` | 配额上限；operation 类型 + token 用量（validator on_complete 钩子的 token 字段直落此表） | 计费与公平调度的数据源 |
| `provision_jobs` | type(schema_create/version_upgrade/drop), target, status, worker | 开通/升级/删除的状态机，幂等键防重复 |

平台库与引擎库**分库或至少分角色分凭据**：平台故障/泄露不波及记忆数据，反之亦然；两边迁移权限独立。

### 5.4 配置：存哪里、怎么解析

- **全局级**（进程 env，K8s ConfigMap/Secret 注入）：基础设施与凭据。`HindsightConfig.from_env()` 一次解析进进程缓存（`config.py:5403-5413`）。
- **租户级**（平台注册表，经扩展回调）：`get_tenant_config` 每请求被调、引擎不缓存——**平台扩展必须自己做进程内缓存**（读注册表 + 短 TTL），这是 agent 明确警告过的性能要点（`config_resolver.py:185-207`）。
- **bank 级**（`banks.config` JSONB）：写路径行锁重校验 + JSONB merge（`config_resolver.py:647-703`），读路径 30s TTL 进程缓存，**跨实例最终一致（≤30s）**——引擎明示接受此权衡（`engine/bank_info_cache.py:14-31`）。SaaS 若要写后立即全局生效，🟧 可加短 TTL 或注册表版本号广播，属可选优化不是必需。
- **凭据**（LLM/推理/DB key）：只进 env/Secret，写入 bank 配置会被强制拒绝（§1.4）。

### 5.5 高可靠设计

| 维度 | 方案 | 依据/缺口 |
|---|---|---|
| 引擎库 HA | 托管 PG 多 AZ + 同步副本 + 自动故障转移 + PITR（Neon/RDS/AlloyDB/Cloud SQL 均可；Neon 等 PG 兼容端点即用——引擎本来就是标准 PG 协议） | 只读副本支持已内置（§1.1） |
| 队列可靠性 | 入队与业务写同库同事务；at-least-once；重试退避；墙钟上限；claim 幂等 | §1.2；无 exactly-once——副作用（LLM 调用）可能重复，靠 dedupe/adjudication 与 idempotent 插入兜底 |
| 任务恢复 | worker_id 身份 + 优雅退出释放 + 回收控制器 + `recover`/`decommission-worker` CLI | §2.4 gap 由控制器补 |
| 备份 | 物理层托管快照/PITR；逻辑层 `hindsight-admin backup/restore --schema`（二进制 COPY zip，FK 有序 TRUNCATE，容忍列漂移）+ `export-bank/import-bank` | `admin/cli.py:117-464`；**按 schema 粒度做租户级逻辑备份** |
| 元数据可靠性 | 平台库同样托管 HA；`usage_events` 追加式，容忍重放去重 | 🟧 |
| 单点风险 | PgBouncer 自身（无状态可水平扩展 + keepalive）；K8s 控制面；Cell 内 PG 实例 | 跨 Cell 无单一故障点 |

### 5.6 连接管理（pooler 策略）

- **API/Worker 全部经 PgBouncer transaction 模式**：引擎已为此设计（§1.1）——无预备语句、每次 acquire 重放 GUC、无运行时 advisory lock。`db_statement_timeout=600s` 默认（`config.py:1772-1776`），retain 插入事务与 consolidation 批事务都在限内，但 pooler 的 `query_timeout`/`idle_transaction_timeout` 需与之配套（🟩 配置项，标注待验证）。
- **直连旁路**：`HINDSIGHT_API_MIGRATION_DATABASE_URL` 指向 PG 本体——迁移（session advisory lock）与 `CREATE INDEX CONCURRENTLY`（autocommit 专用连接）必须绕过 pooler，引擎已明确支持（`migrations.py:370-375`；`memory_engine.py:22525-22594`）。
- **容量公式**：`每实例 pool max（建议 10-20）× 实例数上限 + 直连 Job + 迁移直连 ≤ PG max_connections`；API 层 `HINDSIGHT_API_DB_POOL_MIN_SIZE` 调小（如 1）以控制归零再扩容时的连接风暴；PgBouncer `default_pool_size` 按此预算。
- 引擎自身的并发预算（`recall_max_concurrent=32`、`retain_max_concurrent=4`、admission lanes）都是**每进程**的（`api/admission.py:33-35`）——这是特性不是缺陷：实例级背压天然随扩缩容线性缩放；全局上限交给伸缩器 max replicas + 平台 validator。

---

## 6. 租户隔离设计

### 6.1 隔离层级

~~~mermaid
flowchart LR
  REQ["请求（Bearer / 断言）"]:::external --> L1["① 认证层<br/>前门验证终端身份 · 数据面 TenantExtension<br/>验证内部凭据+查注册表（每请求，无缓存于引擎侧）"]:::platform
  L1 --> L2["② Schema 层（租户）<br/>TenantContext(schema_name) → contextvar<br/>→ fq_table 全限定表名 + validate_sql_schema 守卫<br/>= 每 schema ≈22 表的物理分组"]:::hs
  L2 --> L3["③ Bank 层（业务分区）<br/>每查询 WHERE bank_id + 复合外键<br/>+ bank 串行化/唯一索引<br/>+ 平台 validator 的 bank ACL（filter_bank_list/validate hooks）"]:::hs
  L3 --> L4["④ 配额与公平层<br/>validator precheck（402）· admission lanes<br/>· worker 按 schema 轮转 + bank 游标公平领取"]:::platform
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

### 6.2 租户数据怎么存（源码机制）

- **一租户一 schema，一 schema 多 bank**：所有业务表带 `bank_id`，`banks` 表本身不带。租户开通即 `CREATE SCHEMA` + 22 表 + per-bank 索引例程挂载（§4.4）。
- **运行时强制**：查询文本全部经 `fq_table()`（`engine/schema.py:16-27`），守卫 `validate_sql_schema` 用正则拒绝对受保护表的非限定引用（`memory_engine.py:285-298, 460-510`）——schema 隔离是**集中强制**的；bank 隔离是**每查询约定**（每个 store 方法显式传 bank_id）+ 复合外键/唯一索引的结构性防护。
- **后台任务带租户**：任务行本身位于租户 schema 的 `async_operations` 表中，**行所在的 schema 即租户上下文**；poller 领取时将其注入任务字典（`worker/poller.py:1208`，引擎消费端 `memory_engine.py:4630`）；跨租户发现例程在 public schema 唯一副本（`schema.py:30-44`）。
- **审计/追踪按 bank**；指标 tenant 标签（=schema 名）默认关闭防基数爆炸，按需开启（`metrics.py:652-660`）。
- **文件**：原件可走 S3/GCS/Azure 后端（`engine/storage/`），`file_storage` 表记引用——对象存储桶按 Cell 分桶 + 前缀带 tenant/bank，桶策略隔离。

### 6.3 隔离强度阶梯（设计建议）

| 等级 | 拓扑 | 隔离语义 | 适用 |
|---|---|---|---|
| L0（现状默认） | 共享 PG + 共享 DB 角色，schema 分区 | 应用层隔离：引擎保证查询不跨 schema；**单一应用凭据可见所有 schema**——应用层被攻破即全租户暴露 | 内部/试用 |
| L1（推荐默认） | L0 + 前门双层认证 + 平台 validator ACL + 审计 + 配额 | 未授权访问被前门与 validator 双层拦截；数据面凭据不离开集群 | 标准 SaaS 多租户 |
| L2（专属 Cell） | 独立 PG 实例 + 独立 Hindsight 部署 + 独立凭据/网络 | 故障域、容量、版本、合规、地域全独立；数据面凭据物理隔离 | 强隔离/合规/大客户 |

**为什么不用 RLS 或每租户 DB 角色**：RLS 是"表内列值 + 策略"模型，与 schema-per-tenant 的"物理分表"模型不对位（要么放弃 schema 分区改单表加 tenant_id 列——重写全部 22 表与索引模型，且失去 per-bank 部分向量索引；要么给 schema 模型硬加 RLS 无处着力）。每租户 DB 角色则与"单进程单连接池"架构冲突（`TenantContext` 只返回 schema 名；每租户角色 = 每租户连接池 = 推翻连接模型）。**分层结论：Cell 内 schema 分区（继承引擎设计），跨 Cell 物理隔离（部署层实现，零引擎改动）**——这与引擎作者自己的托管产品思路一致（Hindsight Cloud 建在同一扩展槽上）。

### 6.4 bank 级权限与公平

核心引擎不消费 `allowed_bank_ids`（§1.4）——bank ACL 的执行点：前门（产品语义）+ 平台 validator（`validate_bank_read/write`、`filter_bank_list` 钩子）。公平性：worker 的 schema 轮转 + bank 游标（deficit round-robin）保证大租户不独占 worker 槽位（`poller.py:382-393, 524-652`）；伸缩指标取"有活干的 bank/schema 数"而非"总队列深度"（同 bank 的 100 个 consolidation 只值 1 个 worker——沿用 V6 §5 的伸缩指标告诫）。

### 6.5 租户生命周期

| 阶段 | 动作 | 工具 |
|---|---|---|
| 开通 | 注册表行(provisioning) → 迁移 Job（直连+advisory lock）→ active → 发 key | `hindsight-admin run-db-migration --schema`（`admin/cli.py:622-674`）|
| 冻结 | validator precheck 对 frozen 租户统一 402/403；worker 停领该 schema（list_tenants 过滤） | 平台扩展 |
| 删除 | 先 `DELETE /banks/{id}`（引擎 API，`http.py:8212-8225`）逐 bank 清数据 → 🟧 新增 drop-schema Job（DROP SCHEMA CASCADE + 对象存储前缀清理 + 注册表置 deleted） | **gap：admin CLI 无 schema drop 命令，需开发** |
| 迁移（Cell 间） | `export-bank` → 目标 Cell `import-bank`（引擎已有）→ 注册表切绑定 → 旧侧冻结清理 | `admin/cli.py` |

### 6.6 剩余风险（诚实清单）

1. 共享 Cell 内单应用凭据可见全部 schema（→ L2 解法在部署层，见 6.3）。
2. 引擎每进程缓存（bank config 30s、租户扩展缓存）意味着配置变更跨实例最迟 TTL 粒度生效。
3. `chunks` 主键是单列 chunk_id（格式含 bank 前缀，已有跨 bank 碰撞守卫 `ChunkIdOwnedByAnotherBank`，issue #4244）——结构性但被守卫覆盖。
4. 卡死的 processing 行钉死 bank（回收控制器是缓解不是根除；根除需引擎加租约/fencing，属上游演进）。
5. 租户级删除与备份恢复演练在数千 schema 规模下的时长未实测（§10）。

---

## 7. 无状态化改造清单

### 7.1 纯配置达成（全部为已存在的环境变量，已逐一核实）

| 变量 | Serverless 取值 | 消除的状态 |
|---|---|---|
| `HINDSIGHT_API_DATABASE_URL` | 外部 PG（经 PgBouncer） | 进程内嵌 pg0 |
| `HINDSIGHT_API_MIGRATION_DATABASE_URL` | PG 直连 | 迁移/CONCURRENTLY 对 pooler 的依赖 |
| `HINDSIGHT_API_READ_DATABASE_URL` | 只读副本 DSN（可选） | — |
| `HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP` | `false` | 冷启动全 schema 迁移 |
| `HINDSIGHT_API_WORKER_ENABLED` | `false`（仅 API 层） | API 进程内 worker 循环 |
| `HINDSIGHT_API_WORKER_ID` | StatefulSet pod 名（worker 层） | 身份恢复 |
| `HINDSIGHT_API_SKIP_LLM_VERIFICATION` | `true`（或保留告警） | 启动时逐 provider 网络探活 |
| `HINDSIGHT_API_EMBEDDINGS_PROVIDER` / `_TEI_URL` | `tei` + 服务地址 | 本地 SentenceTransformers 权重 |
| `HINDSIGHT_API_RERANKER_PROVIDER` / `_TEI_URL` | `tei`（或 cohere 等） | 本地 cross-encoder 权重 |
| `HINDSIGHT_API_MCP_STATELESS` | `true` | MCP SSE 会话钉实例 |
| `HINDSIGHT_API_DB_POOL_MIN_SIZE` / `_MAX_SIZE` | `1` / `10~20` | 连接风暴 |
| `HINDSIGHT_API_TENANT_EXTENSION` | 平台扩展 🟧 | — |
| `HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION` | 平台 validator 🟧 | — |
| `HINDSIGHT_API_METRICS_BACKLOG_ENABLED` | 仅 worker/指标实例 `true` | 每实例 30s 全 schema 扫描 |

不设 `HINDSIGHT_API_METRICS_WORKER_LABEL`（多 worker 本地 tempdir+flock 机制即不启用）；指标走 OTel push（`/metrics` 与 OTel collector 都已支持）。**不引入 llama.cpp/Copilot/Cursor 等 本地子进程 Provider**（凭据是 OAuth 文件锁那套，与容器弹性冲突）。

### 7.2 需要的引擎侧小补丁（🟧，均为小改动）

1. **MaintenanceLoop 开关**：现于 `MemoryEngine.initialize()` 无条件启动（`memory_engine.py:5404-5410`）。加 `HINDSIGHT_API_MAINTENANCE_ENABLED`（或并入 worker_enabled 语义），API 层进程不跑——多副本 API 每个都做发现扫描是纯浪费（幂等安全但费钱，`maintenance.py:30-52` 自述）。**这是本方案唯一建议改引擎核心的必选项。**
2. **租户删除**：admin CLI 增 `drop-schema`（复用 backup 的表清单逻辑）。
3. **（可选）伸缩指标 SQL 视图**：为 KEDA 提供"有活 bank 数"单一数值查询（现有 PL/pgSQL 例程 + backlog gauge 已可组合，做一个视图更干净）。

### 7.3 冷启动预算与归零策略

冷启动构成（全远程化配置后）：容器拉起（镜像层缓存）+ Python import（~3-6s，`core/main.py:45-58` 记录了 6.2s 的量级与懒加载优化）+ `initialize()`（DB 连接池 + TEI `/info`/测试嵌入探测（带重试，可声明维度跳过部分探测）+ dateparser/tokenizer 初始化）。**预算目标：p95 ≤ 15s（待实测验证，§10）**。策略：

- 生产 Cell：API 层 `min-scale=1~2`（recall 延迟敏感），scale-to-zero 用于开发/低优先级 Cell。
- Worker 层不归零（拉取模型），min 1 常驻 + HPA 扩容。
- Knative `concurrency` target 设为 admission lanes 可承载值（recall 每进程默认 32 并发）。
- 镜像：复用官方 `ghcr.io/vectorize-io/hindsight-api` 基线 + 平台扩展层叠（`hindsight-extensions/*/Dockerfile` 已示范 overlay 模式），不装本地模型权重（镜像从 GB 级降到百 MB 级）。

---

## 8. 可替换性设计（"比 Hindsight 更强的组件"怎么换）

### 8.1 四层替换边界

~~~mermaid
flowchart TB
  subgraph P["平台产品层 🟧"]
    BIND["bindings 绑定表<br/>space → provider_kind + instance + revision"]:::platform
  end
  subgraph E1["层1·整引擎替换（平台级）"]
    HP["HindsightProvider（本方案 Cell）"]:::hs
    OP["其他引擎 Provider<br/>（更强的新组件 / Mem0 / 自研…）"]:::oss
  end
  subgraph CELLX["层2·引擎内可换面（Hindsight Cell 内）"]
    INF2["推理组件：LLM/Embedding/Reranker<br/>ABC + 30+/13+/14+ 已有实现"]:::hs
    IDX["检索引擎：ANN（pgvector/vchord/DiskANN/ScaNN）<br/>+ 全文（5 后端）——配置级切换+索引对账"]:::hs
    MEM["记忆存储切片：MemoriesExtension<br/>memory_units+链接表整体外置（接口已备）"]:::hs
    DBB["DB 后端：DataAccessOps+SQLDialect ABC<br/>Oracle 已交付；Neon 等兼容 PG 即用"]:::hs
  end
  BIND --> HP
  BIND -.切换.-> OP
  HP --> CELLX
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

### 8.2 各层替换成本与硬约束

| 层 | 替换动作 | 成本 | 硬约束/诚实边界 |
|---|---|---|---|
| 推理组件（Cell 内） | 改 env（如 TEI→新 embedding 服务），重启滚动 | **配置级，当天** | **维度冻结**：换不同维度的模型 = 列宽迁移，仅空表可行（`migrations.py:623-693`）→ 必须**开新 Cell + 源重放**（平台保留原件，§8.3），或在 Cell 建立时就选好可长期演进的模型/服务 |
| ANN/全文引擎（Cell 内） | 改 env + `repair-bank` 对账索引 | 配置级 | 同 Cell 内切换，索引重建有窗口 |
| 记忆存储切片 | 实现 `MemoriesExtension`（`engine/memories/base.py:770`）：`memory_units`+链接表整体换成外部存储，documents/chunks/banks/entities/队列仍留 SQL | 代码级（一个扩展包） | 语义合同需逐项验收（id 分配、recall 返回全量行、`store_owned` 迁移行为）；检索质量回归必须做 |
| DB 后端 | 实现 `DataAccessOps`+`SQLDialect`+迁移效果 | 大（Oracle 花了 ~3300 行） | PG 生态兼容端点（Neon 等）零改动即用；非 PG 形状（HTTP 数据库、独立向量库）= 重写队列谓词/索引模型，**不建议** |
| 整引擎 | 绑定表切 provider + 新 Cell 类型 | 平台级 | 见 8.3 |

### 8.3 整引擎替换协议（平台层，沿用 V4 的 MemoryProvider 合同）

**前提：平台持有来源真相**（documents 原件 + 版本 + 用户纠正），Hindsight 只是被绑定的记忆引擎之一。替换协议：新引擎 Cell 上线 → 按绑定表把目标 space 的来源**重放**（retain 幂等：content_hash/operation_id 去重保证重放安全）→ LLM-as-judge 对照评测（hindsight-system-evals 的方法可复用于双向评测）→ 绑定表 CAS 切换 → 双读观察期 → 旧 Cell 冻结归档。**不承诺**两引擎私有表互迁（V4 已论证不可能也不必要）；回滚 = 切回旧绑定（旧 Cell 数据未动）。

这套协议同时解决 §8.2 的维度冻结问题：embedding 模型升级就是"新 Cell + 源重放"的一次特例。

---

## 9. 实施阶段与验收门槛

| 阶段 | 交付 | 必须通过的门槛 |
|---|---|---|
| A：拆分验证（纯配置） | 外部 PG + API/worker 分进程 + 全远程推理 + pooler；用现有 Helm/docker-compose(tei) 起双节点 | 两实例并发服务与领取无串数据；`validate_sql_schema` 无告警；PgBouncer transaction 模式全回归通过 |
| B：平台多租户 | 租户注册表 + 平台 TenantExtension + validator（配额/ACL/强制 async）+ 开通 Job | 双租户并发复用实例零串扰；frozen 租户全操作被拒；20 内容超限请求被 precheck 402/转异步；开通不发生在请求路径 |
| C：弹性与运维 | Worker HPA/KEDA + 回收控制器 + OTel 指标 + MaintenanceLoop 补丁 | 杀 worker pod 后其 processing 行在 SLA 内被回收、对应 bank 恢复 consolidation；扩缩容期间无任务丢失/重复执行超预期（at-least-once 边界内）；指标无租户标签基数爆炸 |
| D：归零与冷启动 | Knative/托管容器 API 层 + min-scale 策略 | 冷启动 p95 实测 ≤ 预算；归零后首请求路径全通；同步 retain 超时策略生效 |
| E：规模与替换 | 多 Cell 放置 + 租户迁移（export/import）+ 新引擎 Cell 试点 | 千级 schema 迁移/备份窗口实测达标；引擎替换演练（重放+对照评测+切绑定+回滚）完整走通 |

---

## 10. 待验证假设清单

1. **冷启动实测**：全远程化配置下 import + initialize 的 p50/p95（预算 15s 是目标不是结论）。
2. **KEDA 对 StatefulSet 的直接伸缩支持**（或退化方案：HPA + 外部对账控制器）；以及"有活 bank 数"伸缩指标的实际效果（同 bank 串行化下的扩容正确性，含"已启动未 claim 容器不引发持续过扩"）。
3. **PgBouncer 大规模行为**：每次 acquire 的批量 `set_config` 在高 QPS 下的开销；`statement_timeout=600s` 与 pooler `query_timeout` 组合下的长事务边界。
4. **千级 schema 的运维窗口**：run-db-migration 全量时长、backup --schema 时长、`schemas_with_pending_work()` 轮询成本（迁移代码按 2 万 schema 调优是作者陈述，本方案规模需自测）。
5. **回收控制器 SLA**：pod 失联 → decommission → bank 解锁的端到端时间；期间该 bank consolidation 停摆是否可接受。
6. **bank 配置 30s TTL 最终一致性**对产品功能（如配置改完立即召回验证）是否可感；不可感则需 🟧 注册表版本号广播。
7. **TEI 探活与维度声明**在冷启动路径的实际耗时（部分 provider 支持声明维度跳过探测，TEI 路径需实测）。
8. **MemoriesExtension 外置存储**的语义完整性（若走该替换路线）。
9. **同步 retain 的内容阈值**（precheck 强制异步的 content_length 界限）需按 LLM 延迟实测标定。

---

## 附：与 V6（Cognee 版）的关键差异速览

| 维度 | Cognee V6 | 本方案（Hindsight） |
|---|---|---|
| 异步底座 | 需平台自建持久 Job/attempt 协议 | **引擎自带** `async_operations` 队列 + SKIP LOCKED + 谓词串行化（省掉 V6 §4 的整套作业协议） |
| 图/向量存储 | Neo4j Enterprise + PGVector 分离，需跨引擎一致性 | 单 PG（向量/全文/图/队列同库同事务）——**少一类一致性协议** |
| 租户隔离 | Dataset database（图库能力）+ PG schema | schema-per-tenant 全限定表名，pooler 兼容性更好 |
| 写协调 | 需设计 Space 写所有权/发布协议 | bank/文档级串行化谓词已在 claim SQL；缺租约接管（回收控制器补） |
| 引擎定位 | 整引擎插件（MemoryProvider） | 同为整引擎插件；引擎内另有三层可换面（推理/索引/存储切片） |
