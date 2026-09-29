# 华为云落地：Hindsight Serverless 多租户架构（V2.1，经五维评审修订）

**一句话：五层架构——APIG 接入层 → 平台控制层 → 引擎层（ECS 伸缩组：hindsight-api / hindsight-worker / PgBouncer / TEI）→ 存储层（RDS PG 引擎实例 schema-per-tenant + RDS PG 平台元数据库 + OBS 原件与来源账本）→ 可观测弹性层（指标采集器直调 AS API）。平台持有来源账本与全部元数据；引擎经"EngineProvider 接口 + 引擎接入合同"接入，可整体替换。**

本文是 [`serverless-multi-tenant.md`](serverless-multi-tenant.md)（V1，云中立设计与引擎源码事实论证）的华为云落地版。V2.0 草案经 **5 个独立评审维度**（产品契合 / 多租户安全 / 可靠性 / 弹性容量 / 接口解耦）审查后修订为本版——共采纳 **19 项严重、23 项建议**级发现，评审记录见附录 A。相对 V2.0 的关键修正：

| # | V2.0 的缺陷（评审发现） | 本版修正 |
|---|---|---|
| 1 | EngineProvider 缺取消/删除/擦除，重放协议会让已删数据复活 | §6.2 补 `cancel/retry/delete_source/erase_bank_data` 等写生命周期 |
| 2 | "平台持来源真相"对 inline 文本不成立，无账本表 | §3.2 新增 `spaces`/`sources` 账本；产品模式"先记账本再 retain"；直通模式显式标注不可重放窗口 |
| 3 | 引擎钩子直写平台库 → "改 bindings 一行即替换"名不副实 | §6.5 计量事件化（复用引擎事务性 webhook outbox）+ 引擎接入合同清单 |
| 4 | 连接预算算术不自洽且漏只读副本/平台库/CONCURRENTLY 直连 | §8 修正为五池分列公式 |
| 5 | 读己之写未处理（副本延迟下"存完即查"查不到） | §5.2 明确 Day-1 recall 全走主库，副本仅分析负载 |
| 6 | API 组用 CPU 伸缩对长 IO 请求失明（lanes 满但 CPU 低，且 admission 统计不进 metrics） | §5.3 改 lane 占用率指标 + §7.2 引擎补丁暴露 admission gauges |
| 7 | `decommission-worker` 是单 schema 命令，千级 schema 无法运维 | §5.5 改跨 schema 批量对账 + 引擎 CLI `--all-tenants` 补丁 |
| 8 | 恢复机制自身单点（控制器/采集器/迁移 Job 无 HA 无对账） | §9 新增 F11-F13 |
| 9 | schema 迁移回退被高估（数据迁移 downgrade 故意 no-op） | §9 F10 拆镜像回滚/schema 回退 + expand-contract 契约 |
| 10 | H7 核实方法写错（引擎用 obstore 非 boto）；H1 未分层（pgvector 硬依赖 ≥0.5.0 / pg_trgm 软依赖） | §7 重写假设清单 |

分析基线：Hindsight `12f2d54f643baddacb98cd547c89b1a50c5c3dcc`；`core/` 指 `hindsight-api-slim/hindsight_api/`。**华为云产品能力描述基于模型常识，在线核实通道在本环境不可用（详见附录 A 评审方法声明）——第 7 节为完整假设清单，上线前必须逐项 POC。** 图例：🟦 Hindsight 已有开源组件；🟩 开源组件；🟧 需开发的平台逻辑；🟪 华为云付费组件。源码事实（带 file:line）/ 设计建议 / 待验证假设三者分离。

---

## 1. 总体分层架构

### 1.1 分层总图

~~~mermaid
flowchart TB
  C["租户客户端<br/>SDK / REST / MCP / coding-agents / 产品 UI"]:::external

  subgraph L1["L1 接入层（边界与治理）"]
    APIG["API 网关 APIG 🟪<br/>TLS 终结·限流·自定义认证·审计日志<br/>断言 header 强制 override 语义(H4a)"]:::commercial
    ELB["ELB 🟪（VPC 内，无公网 IP）<br/>引擎 ECS 组负载均衡(直通模式)"]:::commercial
  end

  subgraph L2["L2 平台控制层（产品大脑，ECS 组，双实例 HA）"]
    AUTH["平台鉴权/路由服务 🟧<br/>验 key→租户→Cell·注入断言 header<br/>·配额 precheck 端点·bank ACL"]:::platform
    MCPG["平台 MCP 网关 🟧(产品模式)<br/>工具面由引擎 capabilities 派生"]:::platform
    CTRL["平台控制器 🟧(双实例+幂等Job)<br/>租户开通·worker 回收·版本升级<br/>·绑定管理·对账 Job"]:::platform
    MEV["计量事件消费 🟧<br/>引擎 webhook outbox→usage_events"]:::platform
  end

  subgraph L3["L3 引擎层（可替换计算，ECS 伸缩组 ×4）"]
    direction TB
    APIG2["hindsight-api 组 🟦 AS(min 2)<br/>无状态：worker=off·迁移=off·全远程推理"]:::hs
    WRK["hindsight-worker 组 🟦 AS(min 1)<br/>worker_id=ECS实例ID·MaintenanceLoop"]:::hs
    PB["PgBouncer 组 🟩(min 2,不缩零)<br/>transaction 模式·连接预算唯一控制点"]:::oss
    TEI["TEI 推理组 🟩(权重预烘焙镜像)<br/>embeddings + rerank"]:::oss
  end

  subgraph L4["L4 存储层（独立于计算生死）"]
    RDSE[("RDS PG 实例A·引擎库 🟪 HA主备<br/>public: 跨租户例程<br/>tenant schema ×N: 每租户 22 表<br/>pgvector≥0.5(HNSW)·pg_trgm")]:::commercial
    RDSP[("RDS PG 实例B·平台库 🟪<br/>tenants/spaces/sources/api_keys<br/>/bindings/quotas/usage_events…")]:::commercial
    OBS[("OBS 🟪<br/>原件桶(版本控制开启)<br/>归档桶·按 Cell 分桶")]:::commercial
  end

  subgraph L5["L5 可观测与弹性层"]
    COL["指标采集器 🟧(双活)<br/>查 public 例程聚合 Cell 级单值<br/>滞回+冷却后直调 AS API(首选)"]:::platform
    CES["CES 云监控 🟪(指标断流告警)"]:::commercial
    AOM["AOM/LTS 🟪 日志·APM(OTLP)"]:::commercial
  end

  C --> APIG
  APIG -->|"/v1 产品语义"| AUTH
  APIG -.直通模式(Day 1).-> ELB
  C -.MCP.-> MCPG
  AUTH --> ELB
  ELB --> APIG2
  MCPG --> ELB
  AUTH -.配额precheck(薄HTTP).-> APIG2
  CTRL -.开通/回收/升级/对账.-> RDSE
  CTRL --> RDSP
  APIG2 --> PB
  WRK --> PB
  PB --> RDSE
  APIG2 -.文件.-> OBS
  WRK -.事务性 webhook outbox.-> MEV
  MEV --> RDSP
  APIG2 --> TEI
  WRK --> TEI
  APIG2 & WRK -.LLM.-> LLMP["LLM API 🟪<br/>(引擎原生支持 deepseek/智谱zai/火山/minimax…)"]:::external
  WRK -.webhook.-> C
  APIG2 & WRK -.OTLP/日志.-> AOM
  APIG2 & WRK -.metrics.-> COL
  COL --> CES
  COL -.伸缩.-> APIG2 & WRK

  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

与 V2.0 图的差别（评审修正）：① 计量不再由引擎直写平台库——worker 经引擎**事务性 webhook outbox** 发事件，L2 计量消费落库（§6.5）；② 新增平台 MCP 网关（产品模式，替换引擎时 MCP 客户端不断裂，§6.6）；③ 指标采集器直调 AS API 为首选伸缩路径（§5.3）；④ L2 全部组件标注双实例 HA（§9 F11/F12）。

### 1.2 层间契约

| 层 | 对上承诺（接口） | 对下依赖（要求） | 无状态性 |
|---|---|---|---|
| L1 APIG | 稳定对外 API 与域名、TLS、限流、认证触发、审计 | L2 鉴权服务可用；L3 ELB 健康端点 | 网关无业务状态 |
| L2 平台控制层 | ① 鉴权断言（tenant/space/bank ACL）② 路由（租户→Cell→引擎端点）③ 配额 precheck（fail-close）④ 来源账本写入 ⑤ 租户/绑定生命周期 API ⑥ 计量落账 ⑦ 对账 | L4 平台库（真相源）；L3 引擎 admin 面 | 状态全在平台库；实例任意扩缩 |
| L3 引擎层 | **引擎接入合同**（§6.5）：EngineProvider 语义 + 租户解析 + 配额钩子 + 事件外发 + metrics + 健康报告 | L4 RDS/OBS/TEI/LLM 稳定端点；worker_id 稳定性 | API 组完全无状态；worker 有状态性外置到 `async_operations`（V1 §1.2） |
| L4 存储层 | 持久与一致（HA/PITR/备份）、schema 隔离边界、S3 兼容对象接口 | — | 天然持久 |
| L5 可观测弹性 | 指标采集、断流自检、伸缩执行 | 各层暴露指标 | 无业务状态 |

### 1.3 L1 的两种工作模式（演进关系）

| | 直通模式（Day 1，最小落地） | 产品模式（Day 2，替换就绪） |
|---|---|---|
| REST 路径 | APIG（自定义认证→L2）→ ELB → hindsight-api | APIG → 平台产品 API → 按 `bindings` 路由任意引擎 |
| MCP 路径 | **绑定 Hindsight MCP 工具形状，不承诺稳定**（平台扩展必须实现 `authenticate_mcp`；**严禁设置 `HINDSIGHT_API_MCP_AUTH_TOKEN`**——它使 MCP 跳过租户验证落到默认 schema，是多租户旁路，`core/api/mcp.py:463-473`） | 平台 MCP 网关，工具面从引擎 capabilities 派生（§6.6），客户端只见平台工具 |
| 认证 | APIG 自定义认证→L2 断言 header（引擎放行机制 `tenant.py:70-81`）；引擎 TenantExtension 二次验证 | 产品 API 全权认证；引擎只验内部服务凭据 |
| 写入账本 | **绕过 sources 账本——此期间数据不在重放覆盖范围**，迁移产品模式前需对账补录（§6.4-②c） | 先记账本（OBS+sources 行）再调引擎 retain（§5.1） |
| 引擎替换 | 需客户端迁移（提供限期弃用的兼容垫片，§6.7） | 新引擎完成接入合同后切 `bindings`（注意：不是"一行"，见 §6.5） |
| 建议 | 首发与自托管 | SaaS 主路径；两模式并存（不同 API 前缀） |

---

### 1.4 架构细化：连接机制与"无状态"的物理实现

本节回答到连接机制级别：ELB 怎么挂 API 组、会话是否亲和、API 与 worker 到底怎么对接、客户数据落在哪个 PG、worker 无状态的准确含义、横向扩展时发生什么。

**先给全案最重要的一句话：API 组与 Worker 组之间没有任何连接——没有服务发现、没有消息中间件、没有 IP 感知。它们唯一的"对接点"是 RDS 里的一张表（`async_operations`）。** API 只往表里 INSERT，worker 只从表里 SELECT。理解了这一点，"无状态"和"横向扩展"就全部顺理成章。

#### 1.4.1 接入层：ELB → API 组怎么接、会话是否固定

~~~mermaid
flowchart TB
  U["同一用户的两次请求"]:::external -->|"HTTPS :443"| ELB["ELB 监听器<br/>443 → 后端服务器组 api-tg（HTTP:8888）<br/>调度 = 加权轮询 · **无会话保持**<br/>健康检查 GET /health，3 次失败摘除"]:::commercial
  ELB -->|"第 1 次请求可能落"| A1["api-ecs-1"]:::hs
  ELB -.->|"第 2 次请求可能落"| A2["api-ecs-2"]:::hs
  ELB -.-> A3["api-ecs-N"]:::hs
  subgraph ASG["AS 伸缩组（api-tg 的成员来源）"]
    A1
    A2
    A3
  end
  ASG2["AS 扩容：新 ECS → 容器就绪 → 健康检查通过<br/>→ **自动注册进 api-tg**（缩容反之自动摘除）"]:::platform -.-> ELB
  A1 & A2 & A3 --> ST[("每实例上没有任何会话/任务/本地数据：<br/>请求上下文=contextvar（随请求生死）<br/>租户映射/bank配置=只读缓存（miss 自举）<br/>任务/数据/账本=全部在 RDS/OBS")]:::oss
  SG["安全组：引擎 8888 端口仅接受 ELB 与 L2 鉴权服务"]:::platform -.-> A1
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

| 问题 | 答案 |
|---|---|
| ELB 怎么对接 API 组 | ELB 监听器（443 终结 TLS）→ **后端服务器组**（协议 HTTP、端口 8888、健康检查 `GET /health`）；后端组成员 = AS 伸缩组的实例，**AS 与后端服务器组关联后扩缩容自动注册/摘除**，无需手工改 ELB |
| 用户会话固定到某个 API 吗 | **不固定，加权轮询。** 为什么不需要：① 请求无会话状态（上下文在 contextvar 里随请求生死，`core/engine/memory_engine.py:92`）；② MCP 走 stateless 模式（§7.1）；③ 实例内的租户/bank 缓存是共享数据源的只读副本，任何实例 miss 后自举。**建议显式不开启源 IP 会话保持**——粘性反而延长故障实例的流量恢复。唯一"粘"的是 HTTP keep-alive 连接复用（连接级，不是会话级，连接断了重建零成本） |
| 多个 ECS 怎么"组成"无状态 | 同一镜像 + 同一份 env 配置（AS 组的启动模板），**实例之间互不知晓、互不通信**。无状态不是"把状态共享出去"，而是"每实例只持有可丢弃的派生数据"：能丢的（缓存、游标）丢了自重建；不能丢的（任务、数据、配置真相）根本不在实例上 |

#### 1.4.2 API 组与 Worker 组的对接方式：一张表，零直连

~~~mermaid
flowchart TB
  A["hindsight-api 实例（受理请求的那个，任意一台）"]:::hs
  T[("async_operations 表（每租户 schema 各一份）<br/>列：status · worker_id · claimed_at · retry_count<br/>· serialization_key · bank_id · task_payload(内嵌 _schema)")]:::oss
  W1["worker-ecs-1（worker_id=实例ID）"]:::hs
  W2["worker-ecs-2"]:::hs
  A -->|"① 受理：与业务写**同一个数据库事务**里 INSERT 任务行<br/>（幂等：bank 行锁去重）"| T
  A --> OUT["202 + operation_id 返回客户端<br/>（此后这台 API 实例死了也无所谓——任务已在表里）"]:::external
  T -->|"② 每 500ms 轮询：SELECT ... WHERE status='pending'<br/>**FOR UPDATE SKIP LOCKED** + bank/文档串行化谓词"| W1
  T -->|"同一谓词（行级互斥，天然不冲突）"| W2
  W1 -.->|"③ 执行：payload._schema 告诉它去哪个 schema 干活"| T
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

| 问题 | 答案 |
|---|---|
| API 组如何对接 worker 集群 | **不对接。** API 只做 INSERT 后即返回；worker 主动轮询领取。API 不知道有几台 worker、worker 不知道哪台 API 提交的——两侧只共享表结构。这就是"以数据库为队列"的解耦（引擎源码事实：领取谓词 `core/engine/db/ops.py:144-198`，SKIP LOCKED `ops_postgresql.py:1598-1724`） |
| 引擎层（API）如何"定位"worker 层 | **不定位。** 没有服务发现这一步。需要知道 worker 的只有**平台控制器**（为了回收/巡检），它看的是 AS 实例列表 + 跨 schema 的 `worker_id` 巡检（§5.5）——那是运维面，不在数据路径上 |
| 为什么这样设计 | ① 两侧独立扩缩容/崩溃互不影响；② 任务与业务数据同事务=受理即持久（无消息中间件的丢消息窗口）；③ 并发控制在 SQL 谓词里，对任意数量 worker 正确（V1 §1.2） |

#### 1.4.3 Worker 与存储层的对接 + "worker 无状态"的准确含义

~~~mermaid
flowchart TB
  W["worker ECS 实例（worker_id = ECS 实例 ID，cloud-init 注入）"]:::hs
  PB["PgBouncer（transaction 模式 ×2）"]:::oss
  RDS[("RDS 引擎库")]:::commercial
  W -->|"连接构成（每实例）：<br/>1 条轮询连接（schema 发现 + claim 批量领取）<br/>+ 任务执行连接（≤ slots，retain 插入/consolidation 批事务）"| PB
  PB --> RDS
  W -.->|"迁移 / CREATE INDEX CONCURRENTLY：**直连旁路**<br/>（HINDSIGHT_API_MIGRATION_DATABASE_URL，绕过 pooler）"| RDS
  W --> TEI["TEI / LLM API"]:::oss
  MEM["实例内存里只有：<br/>轮询游标（租户/bank 公平轮转——丢失只影响公平性重启即重建）<br/>活跃任务表（重启即放弃，由 DB 行接管）<br/>只读缓存（租户/bank 配置）"]:::platform -.存在于.- W
  ST2["任务的**全部**状态在 DB 行上：<br/>status=processing · worker_id=本实例 · claimed_at<br/>进度 → result_metadata.progress（心跳式）"]:::oss
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
~~~

**"worker 无状态"的准确表述：无本地持久状态，但有身份记账。** worker 不是纯无状态（它持有 processing 中的任务），关键是：① 执行中任务的状态权威在 DB 行（worker_id 只是记账标记，**不是亲和性**——任何 worker 都能执行任何任务，只要行被重置为 pending）；② 实例内存里的一切（游标、活跃表、缓存）都是可丢弃的派生物；③ 崩溃恢复协议：死实例的行留在 processing → 回收控制器跨 schema 重置为 pending（§5.5）→ 任意活 worker 在下一次轮询领取（at-least-once）。**所以"杀掉任意 worker 换一台新的"是安全操作——这就是它支持横向扩展的原因。**

#### 1.4.4 客户数据定位链：某个客户的数据在哪个 PG

~~~mermaid
flowchart LR
  REQ["租户甲的请求<br/>POST /v1/default/banks/team-proj/memories/recall"]:::external --> S1["① 验断言（HMAC）→ tenant_id = 甲"]:::platform
  S1 --> S2["② 注册表缓存命中/查询：<br/>甲 → cell_id=cell-a，schema=t_3f2a91c0d4e2"]:::platform
  S2 --> S3["③ cell-a = 这套引擎部署对应的<br/>**RDS PG 实例 A**（引擎库）"]:::commercial
  S3 --> S4["④ 该实例上 schema t_3f2a91c0d4e2<br/>的 22 张表"]:::oss
  S4 --> S5["⑤ SQL：\"t_3f2a91c0d4e2\".memory_units<br/>WHERE bank_id = 'team-proj'（bank = schema 内行分区）"]:::hs
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

| 问题 | 答案 |
|---|---|
| 某个客户的数据存在哪个 PG | 四级定位链：**租户 →（平台注册表 `tenants.cell_id`）→ Cell →（该 Cell 的引擎 RDS 实例）→ schema（`tenant_schemas.schema_name`）→ 表内 bank_id 行**。一个租户的全部数据整体在一个 schema 里，不跨 Cell 分裂；强隔离租户的 Cell 是独立 RDS 实例（§4.3） |
| 引擎怎么"找到"这个 schema | 请求时：TenantExtension 验断言 → 查注册表（进程内 TTL 缓存）→ 返回 schema 名 → contextvar → **所有 SQL 拼全限定表名**（`fq_table`，`core/engine/schema.py:16-27`）。任务执行时：payload 内嵌 `_schema`，worker 恢复上下文。**没有任何"数据源切换/连接切换"——同一个连接池，只是 SQL 文本里的 schema 前缀不同**（这就是 pooler 友好的原因，V1 §1.1） |

#### 1.4.5 横向扩展：加一台实例到底发生什么

| | API 组 +1 实例 | Worker 组 +1 实例 |
|---|---|---|
| 触发 | 采集器判定 lane 占用率超阈（§5.3）→ 调 AS API | 采集器判定有活 bank 数超阈 → 调 AS API |
| AS 动作 | 按启动模板建 ECS → 拉容器（镜像预烘焙，无模型权重）→ `initialize()`（DB 连接+TEI 探活）→ `/health` 就绪 | 同左 + poller 启动开始轮询 |
| 接入流量 | 健康检查通过 → **自动注册进 ELB 后端** → 立即分到请求（无预热：缓存 miss 自举） | **没有人需要知道它**——下一次 claim 的 SKIP LOCKED 自然把行分给它 |
| 生效上限 | 连接预算（§8.1 反推 AS max）；lane 并发 × 实例数 | slots × 实例数；**同 bank 串行化谓词意味着单 bank 不因加 worker 变快**——并行度来自 bank/文档维度 |
| 减实例 | ELB 摘除 + 排空在途请求；无状态零损失 | 优雅退出：30s drain + 释放自己的 processing 行；异常终止 → 回收控制器（§5.5） |

**有效扩展的边界（重要）**：加 worker 对"很多 bank 各有一点活"的场景线性有效；对"一个 bank 堆了 100 个 consolidation"无效（谓词只放一个在飞）——这正是伸缩指标用"有活 bank 数"而非"总队列深度"的原因（§5.3，沿用 V6 的伸缩告诫）。

---

## 2. "Serverless" 在 ECS 形态下的语义边界（诚实声明）

三承诺拆解：①客户不管服务器 ✅ ②计算自动弹性 ✅（但见端到端时延预算 §8.3）③空闲归零 ⚠️ 部分达成——ECS 从镜像启动到引擎就绪需分钟级，因此 API 组 min 2（跨 AZ）、Worker 组 min 1（拉取式队列必须有人守着）、PgBouncer/TEI 常驻。**端到端扩容时延**（指标刷新 30s + 采集周期 30-60s + 告警持续 3min + 冷启动 2-3.5min）≈ **6-8min**——这才是 SLO 决定因素，min 基线与告警提前量据此设计（§8.3）。存储与平台服务费用常在。演进：L3 换 CCE Autopilot（秒级弹性/归零）时 **L1/L2/L4 与元数据模型完全不变**——计算形态是可替换的实现细节。

---

## 3. 元数据模型（Serverless 的使能数据）

### 3.1 两级元数据

**平台元数据（控制面，RDS 实例B）回答"谁、在哪、允许做什么、用哪个引擎、写过什么来源"；引擎元数据（数据面，每租户 schema 内）回答"这份记忆现在长什么样、任务执行到哪"。** 分工与 V2.0 相同（图略，见 §3.2 DDL 即全部真相），新增的 `spaces`/`sources` 是评审发现 2/6 的修正：没有它们，"平台持来源真相"与绑定模型都不成立。

### 3.2 平台元数据表设计（DDL 级，含评审修正）

```sql
-- RDS PG 实例B / 库 platform。新增/修正处标 ★
CREATE TABLE tenants (
  tenant_id UUID PRIMARY KEY, name TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'standard',        -- standard / dedicated(专属Cell)
  status TEXT NOT NULL CHECK (status IN ('provisioning','active','frozen','deleting','deleted')),
  cell_id TEXT NOT NULL, region TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE spaces (                            -- ★(评审6a) space 身份表，bindings 外键不再悬空
  space_id TEXT PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants,
  status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);                                               -- 一个 space 不跨引擎（文字契约，与 PK 一致）

CREATE TABLE tenant_schemas (
  tenant_id UUID NOT NULL REFERENCES tenants,
  schema_name TEXT NOT NULL UNIQUE,              -- 命名 t_<tenant_id 前12hex>；48bit 截断碰撞由
  rds_instance TEXT NOT NULL,                    --  UNIQUE 约束兜底，provision_job 幂等重试换名
  engine_version TEXT, status TEXT NOT NULL CHECK (status IN ('provisioning','active','draining','archived')),
  PRIMARY KEY (tenant_id, schema_name)
);

CREATE TABLE api_keys (                          -- ★(评审A1) 哈希与轮换硬化
  key_id UUID PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,                 -- HMAC-SHA256(服务端 pepper)(key)；key=32B CSPRNG
                                                 -- base62，key_id 前缀作查找键；不用 bcrypt(无法索引等值查找)
  tenant_id UUID NOT NULL REFERENCES tenants,
  scopes TEXT[] NOT NULL DEFAULT '{retain,recall,reflect,bank:read}',
  allowed_bank_ids TEXT[],
  status TEXT NOT NULL CHECK (status IN ('active','revoked','rotating')),
  prev_key_hash TEXT, grace_until TIMESTAMPTZ,   -- ★轮换双活窗口
  rotated_at TIMESTAMPTZ
);                                               -- 撤销生效 SLA = 引擎注册表缓存 TTL(5min, §3.5)

CREATE TABLE bank_registry (                     -- 引擎 banks 表的授权镜像（双真相，见同步机制）
  bank_id TEXT NOT NULL, tenant_id UUID NOT NULL REFERENCES tenants,
  display_name TEXT, deleted_at TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, bank_id)
);                                               -- ★同步：validator 挂 bank 生命周期钩子准实时回填
                                                 --  (引擎 delete_bank 触发 validate_bank_write，源码已证)
                                                 --  + 每日对账 + deny-on-missing

CREATE TABLE sources (                           -- ★★(评审2) 来源账本：重放协议的真相源
  space_id TEXT NOT NULL REFERENCES spaces,
  source_id TEXT NOT NULL, revision INT NOT NULL,
  bank_ref TEXT NOT NULL,
  content_ref TEXT NOT NULL,                     -- OBS 对象键（inline 文本也必须由产品层归档到 OBS）
  content_hash TEXT NOT NULL,                    -- 平台计算；进 SourceEnvelope，统一幂等命名(§3.3)
  status TEXT NOT NULL CHECK (status IN ('active','superseded','deleted')),
  correction_of TEXT,                            -- 用户纠正链
  occurred_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (space_id, source_id, revision)
);                                               -- 删除也记账（status=deleted）——重放时才能不复活已删数据

CREATE TABLE bindings (                          -- ★(评审6b) 补 tenant 归属与放置校验
  space_id TEXT PRIMARY KEY REFERENCES spaces,
  tenant_id UUID NOT NULL REFERENCES tenants,    -- 与 tenant_schemas 放置一致性由控制器校验
  engine_kind TEXT NOT NULL,                     -- 'hindsight' | 未来引擎
  engine_endpoint TEXT NOT NULL, binding_revision INT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','shadow','retired'))
);

CREATE TABLE quotas (tenant_id UUID, metric TEXT, limit_value BIGINT, window TEXT,
                     PRIMARY KEY(tenant_id, metric));
                 -- ★(评审A9) 增补指标：pending_operations / pending_bytes / chunks_per_minute
CREATE TABLE usage_events (
  event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id UUID, bank_id TEXT, operation TEXT, operation_ref TEXT,   -- ★按 operation_ref 幂等去重
  llm_input_tokens INT, llm_cached_input_tokens INT, processed_content_tokens INT,
  latency_ms INT, status TEXT, metered BOOL DEFAULT true,             -- ★shadow 引擎事件 metered=false
  created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX ON usage_events (tenant_id, created_at);

CREATE TABLE provision_jobs (
  job_id UUID PRIMARY KEY, kind TEXT NOT NULL,   -- schema_create|engine_upgrade|schema_drop|
  target TEXT NOT NULL,                          -- worker_recover|reconcile|orphan_sweep
  status TEXT NOT NULL, idempotency_key TEXT UNIQUE, payload JSONB,
  created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ, lease_expires TIMESTAMPTZ  -- ★卡死对账
);

CREATE TABLE engine_deployments (
  deployment_id TEXT PRIMARY KEY, engine_kind TEXT, version TEXT, image TEXT,
  schema_migration_sha TEXT,
  reversible BOOL NOT NULL DEFAULT false,        -- ★(评审4) 数据迁移 downgrade 是否可逆，发布时评定
  status TEXT CHECK (status IN ('released','canary','retired')), released_at TIMESTAMPTZ DEFAULT now()
);
```

### 3.3 映射表：每个 Serverless 机制依赖什么元数据

| Serverless 机制 | 依赖的元数据 | 存放 | 引擎侧实现（源码事实） |
|---|---|---|---|
| API 实例无状态可任意替换 | 任务=表、配置=表+env、原件/账本=OBS+平台库 | 引擎 RDS + OBS + 平台库 | `async_operations`；`banks.config`；S3 后端 |
| 请求路由 | tenant→schema→RDS 实例；space→引擎 | `tenant_schemas` + `bindings` | TenantExtension 每请求（扩展缓存 TTL 5min） |
| 认证 | key 哈希+scopes+bank ACL | `api_keys`/`bank_registry` | 断言 header + validator 钩子 |
| 弹性伸缩 | Cell 级队列/负载指标 | 采集器→CES/AS API | `public.schemas_with_pending_work()` 例程（V1 §1.2） |
| worker 故障恢复 | worker_id↔processing 行 | `async_operations.worker_id` | 恢复协议 + decommission（跨 schema 版本见 §5.5） |
| **引擎整体可替换** | **来源账本（含删除记录）+ bindings** | `sources` + `bindings` | 重放按 `(source_id, revision)` **幂等键**（统一命名：平台算 `content_hash` 进封套，WriteReceipt 回显 + `duplicate` 标志——不再混用引擎内部 content_hash 概念） |
| 计量计费 | usage 事件（经引擎 outbox） | `usage_events` | 事务性 webhook + `metered` 标记 |
| 租户生命周期 | 状态机 | `tenants`/`provision_jobs` | `run-db-migration --schema`；`DELETE /banks/{id}` |
| 版本升级回滚 | 版本目录+可逆性标记 | `engine_deployments` | 迁移幂等；**数据迁移 downgrade 多为故意 no-op（源码证据见 §9-F10）** |

### 3.4 引擎侧元数据

同 V2.0：每租户 schema 22 表按职责四类（bank 目录与配置 / 任务元数据 / 派生知识水位 / 审计），权威清单 `core/admin/cli.py:58-83`；配置三级解析链与凭据隔离见 V1 §1.4/§5.4。**审计的诚实定位（评审 A5）**：引擎 `audit_log`/`llm_requests` 是 fire-and-forget（`engine/audit.py:183-196`），进程死即丢——合规审计链应为 **APIG 访问日志 + usage_events（经 outbox，不丢）+ 平台操作审计** 三层，引擎审计表仅为补充。

### 3.5 引擎进程内允许的缓存（"无状态"的精确定义）

允许**可重建只读缓存**：租户→schema（TTL 5min——同时是 key 撤销生效 SLA 与平台库故障的可用性窗口，§9-F6）、bank 配置（30s，引擎原生）、tokenizer/连接池。判定标准：杀任意实例不丢已确认输入（202 后）、不产生错误输出、新实例凭元数据自举。不允许：本地队列、跨请求写锁、本地文件真相。

---

## 4. 客户数据存储与多租户

### 4.1 数据分类与放置（★ 含账本修正）

| 数据类别 | 内容 | 放置 | 说明 |
|---|---|---|---|
| 记忆主体（热） | documents/chunks/memory_units(向量)/entities/links | 引擎 RDS 实例A，tenant schema | per(bank×fact_type) 部分 HNSW |
| 派生知识 | observations/mental_models/knowledge_pages | 同上 | 可重建（重放 retain + **trigger_consolidation**，§6.2） |
| 任务与队列 | async_operations/维护队列/webhook outbox | 同上 | 与业务数据同库同事务 |
| **来源原件+账本（冷，真相）** | 上传文件、inline 文本归档、版本、删除记录、纠正链 | **OBS 原件桶（开版本控制）+ 平台库 `sources` 表** | 产品模式"先记账本再 retain"；**inline 文本也必须归档 OBS**（V2.0 漏洞：只归档上传文件） |
| 平台元数据 | §3.2 全部 | RDS 实例B | 控制面真相源 |

### 4.2 存储布局

同 V2.0（RDS-A：public 例程 + `t_<hex>` schema ×N；RDS-B：平台表；OBS：`hs-{cell}-originals/`、`hs-{cell}-archive/`，桶级 SSE-KMS 加密覆盖两个前缀——评审 V3）。

### 4.3 隔离层级（★ 评审 S1/S2/A7/A8 修正后）

| 层 | 机制 | 落地（含硬约束验收项） |
|---|---|---|
| **网络边界** | 引擎/TEI/ELB **无公网 IP**；APIG 唯一公网入口；**APIG 对断言 header 配置 override 语义**（不能 append——引擎只内置"重复头拒认"，无法区分平台注入与客户端伪造，`core/api/passthrough_headers.py:47-55`）；引擎安全组白名单 = ELB + L2 鉴权 SG + 控制器 | VPC+安全组+KMS；**§10-A 验收：公网直连引擎端口必须被拒** |
| 认证（HTTP） | 每请求断言 → 引擎 TenantExtension 二次验证 | 断言 header 放行 + validator |
| **认证（MCP）** | 平台扩展**必须实现 `authenticate_mcp`**；**`HINDSIGHT_API_MCP_AUTH_TOKEN` 严禁设置**（否则单令牌跳过租户验证落默认 schema——多租户旁路） | 配置档硬约束 |
| Schema 隔离 | 全限定表名 + SQL 守卫（V1 §6.2） | 引擎实现 |
| Bank 隔离 | WHERE bank_id + 复合外键 + validator ACL + bank_registry deny-on-missing | 平台 validator |
| 强隔离租户 | 专属 Cell（独立 RDS/引擎组/OBS 桶） | `plan='dedicated'` + 参数化模板 |
| **凭据收敛**（评审 A8） | ① 运行态引擎 DB 账号与迁移 Job 账号分离（运行态无 CREATE/DROP，迁移走直连 URL 顺势最小化）；② **per-Cell 独立 DB 账号 + per-Cell OBS 桶凭据**——把"单凭据可见全部 schema"收敛为"单凭据可见单 Cell" | 中间态强化，强隔离诉求的廉价缓解 |
| 传输加密（评审 A7） | 各跳边界显式声明：APIG↔ELB↔引擎（VPC 内）、引擎↔PgBouncer↔RDS（VPC 内明文的可接受边界需安全审批声明）、引擎↔TEI（同）、**引擎↔OBS 强制 HTTPS**（引擎 s3 后端允许 plain HTTP，是本地 MinIO 遗留，禁止用于 OBS） | 边界声明文档化 |
| 数据加密 | RDS/OBS 透明加密 + KMS；**备份导出物落归档桶同样 SSE-KMS** | 覆盖 backup/export 产物 |

不采用 RLS/每租户 DB 角色的论证、共享 Cell 残留风险见 V1 §6.3/§6.6。

### 4.4 配额与公平（★ 评审 S3/S4/A9 修正）

四层叠加，每层明确失败语义：

1. **APIG 限流**：粗粒度 QPS（防滥用）。
2. **平台配额（precheck，fail-close）**：`quotas` 表 + 引擎侧薄 HTTP 调用 L2 配额端点（§6.5）；**平台配额服务不可用时拒绝请求（fail-close），不用 fail-open**——V2.0 未定义此语义；**`content_length=None`（chunked 传输）一律保守处理：拒绝或强制 async+事后按实际字节追缴计量**（引擎 precheck 的 content_length 取自 Content-Length 头，chunked 时为 None，`operation_validator.py:111-114`——直接用会被绕过）。
3. **任务囤积防线（评审 A9）**：`pending_operations`/`pending_bytes` 指标双点检查（提交时 + worker 领取时）——precheck 只挡提交时余额，租户可囤积海量小异步任务（免费存储 + 后续 LLM 成本）。
4. **引擎内公平**：admission lanes（每进程）+ worker schema 轮转/bank 游标（V1 §6.4）；**可选 per-tenant worker 槽位上限（如 ≤2 slots）作为大租户阀门**；CONCURRENTLY 索引重建错峰调度（§8.4）。

---

## 5. 数据流

### 5.1 写路径：retain（异步，产品模式）

~~~mermaid
sequenceDiagram
  autonumber
  actor C as 客户端
  participant G as APIG 🟪
  participant P as 平台产品 API 🟧
  participant O as OBS 🟪
  participant S as 平台库(sources 账本) 🟧
  participant E as hindsight-api 🟦
  participant R as 引擎 RDS（tenant schema）
  participant W as hindsight-worker 🟦
  participant I as TEI/LLM 🟩
  C->>G: POST /v1/{space}/memories
  G->>P: 自定义认证(验 key)
  P->>P: 配额 precheck(fail-close·chunked 保守)
  P->>O: 归档原件(inline 文本也归档·开版本控制)
  P->>S: 账本行(source_id,revision,content_hash,status=active)
  P->>E: retain(SourceEnvelope 含 content_hash+幂等键)
  E->>E: TenantExtension(内部凭据+注册缓存→schema)
  E->>R: 事务：async_operations(+payload._schema)·bank 行锁去重
  E-->>C: 202 + operation_id(+binding_revision)
  W->>R: claim SKIP LOCKED(bank/文档串行化+折叠)
  loop 每 chunk 批
    W->>I: LLM 抽取(每chunk) + 批量 embedding
    W->>R: Phase1 实体(纯SQL)→Phase2 事务插入(content_hash 幂等)→Phase3 ANN
  end
  W->>R: 提交 consolidation(水位)+webhook outbox 同事务
  W->>R: consolidation→observations/mental_models→知识页刷新
  W-->>P: 完成事件(经事务性 outbox·HMAC·token 用量)
  P->>S: usage_events(按 operation_ref 幂等)
  W->>C: webhook 投递(at-least-once)
  C->>G: GET /operations/{id}(按 receipt 的 binding_revision 路由)
~~~

**直通模式差异**：跳过第 4-6 步（无账本），代价见 §1.3——此期间写入不在重放覆盖范围。

### 5.2 读路径：recall（★ 评审修正：读己之写）

请求路径同 V2.0（断言→schema→配置解析→查询 embedding→四路并行检索→RRF→rerank→返回，全程只读）。**新增读写一致性设计**：

- 源码事实：配置 `HINDSIGHT_API_READ_DATABASE_URL` 后，**recall 整个四路检索固定走只读副本**（`_get_read_backend()`，`memory_engine.py:8390`）；retain 写入与操作状态在主库；RDS PG 副本为异步复制、延迟无强 SLA。
- **Day-1 默认：不配置只读副本，recall 全走主库**——"存完即查"是 agent 最常见模式，副本延迟会直接表现为"刚写的记忆查不到"，记忆产品不可接受。pgvector 读压力靠 api 组扩容分摊。
- 只读副本保留给：分析/导出/备份校验/知识库导出等非交互负载。若后续引入副本分担 recall，三选一：请求带 freshness 语义（operation 终态后 N 秒内强制主库）/ 副本延迟阈值路由（`pg_last_wal_replay_lsn` 差值）/ 会话粘性——均需产品语义决策，不允许默认静默走副本。

### 5.3 弹性数据流（★ 评审修正：采集路径与指标）

~~~mermaid
flowchart LR
  R["引擎 RDS<br/>public.schemas_with_pending_work()<br/>(单例程跨schema聚合,非per-bank gauge)"]:::hs --> X["指标采集器 🟧 双活<br/>Cell级单值: 有活bank数·pending retain·lane占用率<br/>滞回(扩70%/缩40%)+冷却(扩后5-10min)"]:::platform
  X -->|"首选: 直调 AS API 设期望实例数"| AS["AS 🟪"]:::commercial
  X -->|"备选: CES 自定义指标→告警→SMN→控制器"| AS
  X -->|"断流自检: 采集器互检+CES 断流告警"| AL["告警(监控的监控)"]:::commercial
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
~~~

- **伸缩指标修正**：worker 组 = "有活 bank 数 + pending retain 数"（采集器直查跨租户例程聚合为 Cell 级单值，**不用**引擎 per-(tenant,bank) backlog gauge——万级 bank 会造成万级时间序列与每 30s 万次 COUNT 的基数爆炸）；**api 组 = admission lane in-flight 占用率 >70% 或 503 率 >1%（CPU 降为辅助）**——同步 retain/reflect 等 LLM 时 CPU 空闲，lane 满载 CPU 可能仅 10-20%，CPU 告警永不触发；且 admission 统计目前只写日志不进 metrics（`api/admission.py:163,237-244`）——**需引擎补丁暴露 lane gauges（§7.2-补丁2）**。
- 伸缩路径修正：采集器本就做聚合，**直调 AS API 设期望实例数**（自带滞回+冷却）比 CES→SMN→FunctionGraph 三跳更简单可控；CES 链路降为备选（SMN 可直接订阅平台控制器 HTTP，不必经 FunctionGraph）。AOM 告警不驱动 AS（与 CES 是独立体系）。

### 5.4 租户生命周期流（★ 评审修正：删除序列与冻结语义）

开通流同 V2.0（控制器→provision_job→`run-db-migration --schema` 直连+advisory lock→active→发 key）。修正两点：

- **冻结语义（评审 A4）**：冻结 = 停止受理（validator 全拒）+ 停止新领取（`list_tenants` 过滤）+ **in-flight 任务排空**（已被领取的 processing 任务会执行到完成——引擎无租约中断机制，排空 SLA 按最长任务类型估计，与 V1 §6.6-4 的钉死缺口同源，需向客户声明）。
- **删除序列（评审 A3）**：**第一步撤销 api_keys**（防删除中数据被重建）→ 逐 bank `DELETE /banks/{id}`（引擎对象清理是事务后 best-effort，`memory_engine.py:11307-11320`）→ provision_job(schema_drop：DROP SCHEMA + **OBS 前缀清理带重试与对账清单**，覆盖引擎前缀与平台账本两层) → sources 账本置 deleted（保留合规期）→ archived。**备份与删除的合规冲突单列**：PITR/归档桶中的备份导出不随删除流程清除，需"备份保留期 vs 租户删除"策略（到期清除或密钥销毁声明）。

### 5.5 worker 回收流（★ 评审修正：跨 schema 可操作性）

worker_id = ECS 实例 ID（cloud-init 注入）。三条路径：① AS 缩容走 SIGTERM → 引擎 30s drain + 释放自己的任务；② 实例异常终止：AS 生命周期钩子/实例事件 → 控制器回收；③ 兜底巡检。**修正（评审 R3-1）**：`hindsight-admin decommission-worker` 是**单 schema 命令**（`--schema` 默认 public，`admin/cli.py:1352-1356`），worker-status 同样单 schema——千级租户下不能逐 schema 调用。回收动作改为：**控制器批处理 Job 直连引擎 RDS，一条跨 schema 的 SQL 例程**（复用 public 例程模式：`UPDATE {schema}.async_operations ... WHERE worker_id=$1` 循环或 PL/pgSQL 化），或给引擎 CLI 加 `--all-tenants` 选项（§7.2-补丁3）。巡检阈值（"活着但卡死"检测，评审 R3-7）：retain >1.5h、consolidation >2.5h、其余类型按 P99×3——墙钟上限只覆盖 retain（1h 绝对）与 consolidation（2h 空闲），`file_convert_retain`/`refresh_mental_model`/图实体维护**无上限**（`worker/poller.py:137` "Other task types remain unbounded"）。

---

## 6. 引擎层可替换性：接口与接入合同

### 6.1 替换单元与部署解耦

同 V2.0：替换单元 = 引擎 Cell（L3 引擎组 + L4A 引擎 RDS + 运行配置）；不同 engine_kind 是不同 ECS 组/RDS 实例；路由由 bindings + L2 完成。**修正表述（评审 R5-5）**：替换成本 = "新引擎完成 §6.5 接入合同" + "按 §6.4 协议迁移数据" + "切 bindings"，不是"改一行"。

### 6.2 EngineProvider 接口（★ 补齐写生命周期）

```python
class EngineProvider(Protocol):
    async def capabilities(self) -> EngineCapabilities:
        """{sync_retain, async_retain, max_content_bytes, retrieval_kinds,
        reflect, knowledge_base, webhooks, bank_config_api, mcp_tools: [...],
        trigger_consolidation: bool, export_formats: [...]}"""

    # ---- 写生命周期（V2.0 缺失，评审 R5-1 补齐）----
    async def retain(self, env: SourceEnvelope,
                     idempotency_key: tuple[str, int]) -> WriteReceipt:
        """SourceEnvelope: {space_id, bank_ref, source_id, revision, content_ref,
        content_hash, inline_text|None, content_type, occurred_at, correction_of, metadata}
        WriteReceipt: {operation_ref(内嵌 binding_revision), accepted_at,
        visibility, content_hash, duplicate: bool}"""
    async def cancel_operation(self, ref: OperationRef) -> CancelResult:
        """{state: cancelled|already_terminal, coop: bool}  # 引擎为协作式取消"""
    async def retry_operation(self, ref: OperationRef) -> OperationStatus: ...
    async def delete_source(self, scope: DeletionScope) -> DeletionReceipt:
        """DeletionScope: {space_id, bank_ref, source_id, revision?}
        DeletionReceipt: {requested, deleted, derived_invalidated, pending_refresh, failures[]}"""
    async def erase_bank_data(self, scope: EraseScope) -> DeletionReceipt:
        """GDPR 式擦除：派生内容(observations/models)必须同步失效"""

    # ---- 读与状态 ----
    async def get_operation(self, ref: OperationRef) -> OperationStatus:
        """{state, progress, error?} — 按 receipt 内嵌的 binding_revision 路由到正确 Cell"""
    async def recall(self, q: RecallQuery) -> EvidenceBundle:
        """RecallQuery: {space_id, bank_ref, query, budget, filters, kinds}
        EvidenceBundle: {facts[], observations[], mental_models[], chunks[], graph[],
                         provenance[]}  # 每条目含 score + score_kind:
                         # engine_native|platform_normalized——跨引擎只用后者或 judge 盲评"""
    async def reflect(self, q: ReflectQuery) -> ReflectResult: ...

    # ---- 管理与派生 ----
    async def list_banks(self, space_id) -> list[BankSummary]: ...      # 评审 R5-8：对账必需
    async def create_bank / delete_bank / get_bank_config / set_bank_config(...) -> ...
    async def trigger_consolidation(self, bank_ref) -> OperationRef: ...  # 评审 R5-9：重放后派生知识可达 derived_ready
    async def export_knowledge(self, bank_ref, fmt) -> ExportHandle: ... # 评审 R5-10：OBS 引用
    async def register_webhook(self, wh: WebhookSpec) -> None: ...
    async def health(self) -> HealthReport: ...
```

单 bank 约束的诚实声明（评审 R5-6）：Hindsight recall 是单 bank 的，多 bank 聚合查询是平台职责（扇出 N 次 + 平台侧融合，质量不等价于引擎内融合）——RecallQuery 保持单 `bank_ref`，产品 API 文档显式声明此约束，不做假抽象。**一个 space 不跨引擎**（bindings PK 决定，写成文字契约）。

### 6.3 HindsightProvider 映射表（★ 勘误 + 未映射清单）

| 接口方法 | Hindsight 实现 | 
|---|---|
| `retain` | `POST /v1/default/banks/{id}/memories`（async=true；文件→file 上载→`file_convert_retain`） |
| `get_operation` / `cancel` / `retry` | `GET .../operations/{id}`；`DELETE .../operations/{id}`（协作式取消，`http.py:7947`）；`POST .../operations/{id}/retry`（`http.py:7984`） |
| `delete_source` / `erase_bank_data` | `DELETE .../documents/{id}`（级联 memory_units+links，`http.py:7818`）；`DELETE .../memories`（按 type 批量，`http.py:10027`） |
| `recall` / `reflect` | `POST .../memories/recall`；`POST .../reflect` |
| bank 管理 | **`PUT /v1/default/banks/{bank_id}`**（create_or_update upsert，`http.py:8118`——V2.0 误写 POST）；`DELETE`；`GET /v1/default/banks`（list，`http.py:6293`）；`PATCH .../config` |
| `trigger_consolidation` | `POST .../consolidate`（`http.py:9226`）；恢复 `POST .../consolidation/recover` |
| `export_knowledge` | `GET .../knowledge-base/export`（markdown，`http.py:7066`） |
| `register_webhook` | webhook API（HMAC v1/v2；**内置 SSRF 防护** url_guard：deny-by-default 私网/环回/元数据 IP + DNS 重绑定钉住 + 操作员 allowlist——allowlist 是唯一能重开 SSRF 面的开关，列为平台受控配置，评审 A6） |
| `health` / 客户端库 | `/health` + `/metrics`；`hindsight-clients/`（Py/TS/Rust/Go 生成 SDK） |

未映射的现有能力（接入时按需扩）：MCP 工具全量面（走 §6.6 平台网关）、directives/mental-models 单独 CRUD、graph/entities 只读 API、bank 克隆/导入导出、attachment 管理。

### 6.4 替换协议（★ 评审修正后的完整版）

~~~mermaid
flowchart LR
  A["① 新引擎 Cell 上线<br/>(完成§6.5接入合同)"]:::platform --> B["② 源重放<br/>按 sources 账本全序(source_id,revision)<br/>retain+delete_source 都重放<br/>幂等键=(source_id,revision)<br/>重放后 trigger_consolidation 追水位"]:::platform
  B --> C["③ 对照评测·量化门槛<br/>完整性=账本100%版本可检索·0缺失<br/>质量=judge胜率差≤3%或nDCG@10降幅≤2%<br/>延迟p95≤基线1.1×<br/>擦除验证=已删source不可检索<br/>二次重放零重复事实"]:::platform
  C --> D["④ 影子期<br/>写=active单写+shadow异步镜像(失败仅告警)<br/>读=active为真相+shadow 1%采样<br/>shadow引擎 metered=false"]:::platform
  D --> E["⑤ CAS 切换<br/>binding_revision+1·shadow→active·旧→retired<br/>在途操作按 receipt 内嵌版本路由"]:::platform
  E --> F["⑥ 反向影子观察期<br/>平台持续把新Cell已受理source<br/>回放至旧Cell(幂等键保证安全)<br/>回滚前置=反向回放追平(不丢切换后写入)<br/>追平后旧Cell冻结归档"]:::platform
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

相对 V2.0 的修正：② 重放必须包含**删除**（否则已删数据复活，GDPR 失效）且以**平台账本**为清单（含 inline 文本）；③ 评测门槛量化（不再缺省）；④ 影子期语义完整定义（V2.0 的"双写"未回答写谁/读谁/计量怎么算）；⑤⑥ 回滚从"切回旧 revision"（会静默丢切换后写入）改为**反向影子追平**后切换。**直通模式期间的写入不在账本内**——切产品模式前需对账补录（引擎 documents × sources 差集回填 OBS+账本），或接受 cutoff 并向客户声明。

### 6.5 引擎接入合同（★ 新增，评审 R5-5）

替换引擎需要完成的**完整**清单（不是只有 EngineProvider）：

| # | 合同项 | Hindsight 现状 | 通用要求 |
|---|---|---|---|
| C1 | EngineProvider 接口实现 | §6.3 映射表 | 语义验收（含幂等/擦除） |
| C2 | 租户解析 | TenantExtension（平台提供**通用参考扩展**，HTTP 调 L2 注册服务 + 本地缓存，不直连平台库） | 或平台网关模式（产品模式下引擎只见内部凭据） |
| C3 | 配额 precheck | 引擎侧薄 HTTP 调 L2 配额端点（fail-close；chunked 保守） | 同左 |
| C4 | 计量事件外发 | **复用引擎事务性 webhook outbox**（retain/consolidation 完成事件带 token 用量）→ L2 消费落 usage_events（按 operation_ref 幂等；shadow 打 metered=false） | 事件 schema 版本化 |
| C5 | metrics 暴露 | `/metrics` + OTLP（队列深度/延迟/lane 占用） | 采集器适配 |
| C6 | 健康报告 | `/health`（liveness/readiness） | 路由与伸缩决策 |
| C7 | 内部凭据验证 | 不验终端凭据，只验内部服务凭据 | — |
| C8 | 存储布局 | RDS schema-per-tenant + OBS 前缀约定 | 平台对账 Job 适配 |

**设计修正说明**：V2.0 让引擎 validator 钩子直写平台库——导致每个替换引擎重实现钩子、平台库成为数据面运行时依赖（也是 F6 爆炸半径的根因）。本版改为**计量事件化**（引擎已有事务性 outbox，复用零开发）+ **precheck 薄 HTTP 调用**（内网 <5ms）+ **通用参考扩展**（平台一次开发，所有 Hindsight Cell 复用；非 Hindsight 引擎在适配器内实现 C2-C4 等价物）。权衡：产品模式 precheck 多一跳；直通模式的配额由 APIG 自定义认证顺带判定（待 POC，H4a）。

### 6.6 MCP 通道（★ 新增，评审 R5-7）

直通模式：客户端连引擎 MCP（绑定 Hindsight 工具形状，§1.3 已标注不稳定）。产品模式：**平台 MCP 网关**——工具清单从 `capabilities().mcp_tools` 派生，网关把平台工具调用翻译为 EngineProvider 方法（retain/recall/reflect/list_banks/…）；客户端只连平台 MCP 端点。替换引擎时工具名/参数/返回由网关吸收，客户端不断裂。多 bank 工具按单 bank 约束扇出。

### 6.7 直通→产品的客户端迁移

平台提供**限期弃用的 Hindsight 兼容垫片**（EngineProvider 之上的旧形状 facade，明确 deprecation 时间表）；并存期两路径写同一 Cell 数据一致，但只有产品模式写进账本——迁移完成前以对账补录收口（§6.4-②）。

### 6.8 推理组件替换（Cell 内）

同 V2.0（env 级切换；**维度冻结**约束不变——换维度必须走 §6.4 新 Cell 重放）。

---

## 7. 华为云组件选型与产品假设清单（★ 评审 R1 重写；上线前逐项 POC）

**核查方法声明**：本环境在线核实通道不可用（WebFetch 对华为云域名及对照站均被网络策略阻断）。下表结论 = 引擎源码直接核验（可靠）+ 模型常识（不可作为上线依据）。所有 ⚠️ 项必须 POC。

| # | 组件/假设 | 结论 | POC 要点 | 不成立时降级 |
|---|---|---|---|---|
| **H1a** | **RDS PG 支持 pgvector ≥0.5.0**（硬依赖：部分 HNSW 索引，`USING hnsw ... WHERE`） | ⚠️ 成败点 | `CREATE EXTENSION vector` + 建一个 partial HNSW 索引；**默认配置下每 bank 创建即同步建 3 个部分 HNSW 索引（`per_bank_index_min_rows=0`）——pgvector 缺失/过旧时租户开通第一步即失败**，非运行后退化；运行时 CONCURRENTLY 重建走迁移直连 URL 也要测 | **自建 PG on ECS**（全扩展可用，自运维 HA）。~~GaussDB~~ 从降级路径移除（分布式架构 vs PG 深度惯用法，大概率不可行，仅在官方确认扩展兼容后独立评估） |
| **H1b** | RDS PG 支持 pg_trgm（**软依赖**：缺失时实体解析自动降级 "full" 查找——慢、不断服，迁移 `c1a2b3d4e5f6:39-50` + #626） | ⚠️ | `CREATE EXTENSION pg_trgm` | 接受降级（性能损失量化后决策） |
| H1c | **PG 15+ public schema CREATE 权限收紧**：引擎强制扩展装 public 并 pin search_path（`_pg_extensions.py:71-88`，#4118）——RDS PG 15+ 上迁移账号能否直接 CREATE EXTENSION、或须走 RDS 插件管理控制台预启用 | ⚠️ 新增 | 迁移账号实测 | 预启用插件/降 PG 版本 |
| H2 | RDS HA 主备+只读副本+PITR | ✅ 常识可靠 | 切换演练（**引擎 acquire 重试窗口仅 ~10s，桥不过 <2min 切换——切换期请求失败靠 SDK 重试 + 操作级重试兜底**）；主备切换后只读副本端点/角色变化的客户端可见行为；PgBouncer 对 stale 后端的检测参数（server_lifetime/query_timeout） | — |
| H3 | max_connections 随规格、通常不可自由上调 | ⚠️ | 按规格表核对 §8 预算（建议按 ≥256 规划）；**只读副本有独立 max_connections 需单独预算** | 升配/收紧池 |
| **H4a** | APIG 自定义认证：执行体疑为 **FunctionGraph 函数**（非直连 VPC 内 HTTP 服务）——若属实，直通模式每请求多一跳 + FG 冷启动，影响 recall SLO | ⚠️ 修正 | 认证后端类型；FG 预留实例；或改"APIG 后端直指 L2 兼做认证代理"拓扑 | L2 认证代理模式 |
| **H4b** | APIG 负载通道后端类型：疑支持 ECS 实例/私网 IP 列表，**对 ELB 直接支持待核**；若只支持直挂 ECS，AS 扩缩后需动态同步负载通道成员（与生命周期钩子联动） | ⚠️ 新增 | ELB vs ECS 列表两种形态各 POC；成员同步 API | 直挂 ECS + 成员同步 Job |
| H5 | AS 告警策略 + 生命周期钩子 | ⚠️ | 自定义指标能否直驱 AS 告警策略；**钩子→控制器通知语义（至多一次/至少一次，丢失时只靠巡检兜底）** | **首选路径已是采集器直调 AS API（§5.3），AS 告警策略降为备选**——此假设重要性下降 |
| H6 | CES 自定义指标 API 可用 | ✅ 常识可靠 | 上报批量窗口与丢失行为（影响伸缩灵敏度与 F12） | 采集器直调 AS API（已是首选） |
| **H7** | OBS S3 兼容——**验证对象必须是引擎本身（obstore/Rust object_store，非 boto3**，`engine/storage/s3.py:6-43`**）** | ⚠️ 修正 | 用引擎实测四项：① SigV4 签名作用域（region/service 取值宽容度）；② 寻址风格（引擎未暴露 path/virtual-hosted 配置，需实测 obstore 默认与 OBS 桶域名规则）；③ **预签名 URL**（下载链路 `obs.sign_async`，OBS 预签名兼容是常见故障点）；④ **批量删除**（ListObjects+DeleteObjects 每批 1000 key）；**端点强制 HTTPS** | 引擎原生 PG 文件存储（file_storage 表） |
| H8 | DCS 标准 Redis（可选组件） | ✅ | — | 不引入 |
| H9 | AOM OTLP 入口 | ⚠️ | 端点 URL/gRPC-or-HTTP/鉴权头/OTel exporter 协议版本兼容 | 自建 OTel Collector on ECS |
| H10 | ECS（含 GPU 规格） | ✅ | — | TEI 改用云上推理 API |
| H11 | LLM Provider 出网 | ✅ | 合规与延迟评估 | 专属 Cell 自托管（与弹性目标冲突，仅 dedicated 考虑） |
| H12 新增 | RDS PG 是否有内置数据库代理/连接池 | ⚠️ | 若有，可替代自建 PgBouncer 组——但引擎为 transaction pooler 做的适配（statement_cache_size=0、acquire 时批量 set_config 重放）须对新 pooler 全回归 | 自建 PgBouncer（本方案默认） |

### 7.2 引擎侧小补丁清单（累计 4 项，均为小改动）

1. **MaintenanceLoop 开关**（V1 §7.2-1，不变）：API 进程不跑维护循环。
2. **Admission stats 暴露为 gauges**（评审 R4-3）：各 lane in-flight/queued/rejected 率进 `/metrics`——api 组伸缩指标的数据源，当前只有日志。
3. **admin CLI `--all-tenants`**（评审 R3-1）：decommission-worker / worker-status 跨 schema 批量执行（或平台用跨 schema SQL 例程等价实现，二选一）。
4. **租户删除工具**（V1 §7.2-2，不变）：drop-schema + 对象清理。

---

## 8. 弹性与容量预算（★ 评审 R4 重写）

### 8.1 连接预算（五池分列，替代 V2.0 的单公式）

```
① 引擎库·PgBouncer 后端 = default_pool_size × pgbouncer实例数(2)
   [前提：default_pool_size 是 per-(user,database) 池——api 与 worker 若用不同 DB 账号需分别预算]
② 引擎库·直连 = 迁移Job(≤4) + CONCURRENTLY 维护(worker数×维护并发, 建议≤3, 分钟级长占)
              + 控制器巡检/回收 CLI(≤2)
③ 只读副本(若配) = api实例数 × READ_DB_POOL_MAX_SIZE(建议 20, 默认 100 会打爆副本)
④ 平台库 = 引擎实例数×事件消费与precheck连接(1-2) + L2服务实例数×pool
⑤ 约束: ①+② ≤ 引擎RDS max_conn × 0.8(预留≥20%余量, V2.0 示例仅4%余量不可接受)
        ③ ≤ 副本max_conn × 0.8；④ ≤ 平台RDS max_conn × 0.8
示例: ① 80×2 + ② 4+6+2 = 172 → 引擎 RDS max_conn ≥ 256 规划
```

**与扩容上限联动（评审 R3-F16）**：api 组 max=10 时需求 10×20+worker 6×10+直连 ≈ 264 后端 > 160 池——**扩容上限与连接预算互相击穿**。约束：api/worker 的 AS max 必须由 §8.1 反推（连接预算是硬上限），或扩容时联动调大 PgBouncer 池（配置变更走 IaC）。

### 8.2 伸缩参数

| 组 | min | max | 扩容指标（滞回 扩/缩） | 缩容保护 |
|---|---|---|---|---|
| hindsight-api | 2（跨AZ） | 由 §8.1 反推 | **lane in-flight 占用率 >70%（缩 40%）** 或 503 率 >1%；CPU 辅助 | 老化 ≥10min + ELB 摘除排空 |
| hindsight-worker | 1 | 由 §8.1 反推 | 有活 bank 数 > worker×slots×0.7（缩 40%） | 优雅退出路径（§5.5）；老化 ≥15min |
| PgBouncer / TEI | 2 / 1 | 2 / 按推理 QPS | TEI：CPU/GPU 利用率 | 常驻 |

### 8.3 时延预算（端到端，非仅冷启动）

| 项 | 预算 |
|---|---|
| 指标刷新（public 例程聚合） | 30-60s |
| 采集器滞回判定 + 冷却 | 即时（扩后冷却 5-10min 防抖） |
| 告警持续（CES 备选路径） | 3min |
| **ECS 冷启动**：实例启动 1-3min（建议自定义 ECS 镜像预拉引擎镜像压低端）+ 引擎 import ~6s + initialize（DB+TEI 探活）~5-10s | **2-3.5min** |
| **TEI 冷启动**（V2.0 遗漏）：ECS 启动 + 容器 + **模型加载**（官方镜像不含权重，现下载数百 MB~GB + CPU 数十秒/GPU 含 CUDA 初始化）——**必须用自定义镜像预烘焙权重**，否则 TEI 扩容永远追不上负载 | 预烘焙后 ~2-3min |
| **端到端扩容时延合计** | **~6-8min（SLO 决定因素）** |

### 8.4 RDS 容量（连接数之外）

- **HNSW 部分索引数 = 活跃 bank 数 × 3**（world/experience/observation）——万级活跃 bank = 3 万索引：relcache/共享缓冲碎片、autovacuum 压力、maintenance_work_mem 竞争。缓解（引擎已有）：索引按 size threshold 惰性创建 + reconcile（`per_bank_index_min_rows` 可调，`_vector_index.py:255-261`）；CONCURRENTLY 重建错峰。
- Cell 容量上限触发阈值：活跃 bank > N（按实测标定）→ 新租户放新 Cell。
- TEI 容量模型：请求 ≈ retain chunk 吞吐（批量 32-100）+ recall QPS×1 查询 embedding + recall QPS×rerank 候选对数（**cross-encoder 每对一次前向，算力远高于 embedding**）。注意引擎实例级 `embeddings_max_concurrent_requests` 默认 1（共享信号量串行化，`embeddings.py:162-165`）——多 slot worker 的 embedding 会被串行化，调优该参数与 TEI 容量同表。单 TEI QPS 基线待压测；embedding CPU 可满足，大候选集 rerank 需 GPU。

---

## 9. 高可靠设计（★ 评审 R3 重写：F1-F20 + 前提量化）

| # | 故障 | 影响 | 检测 | 恢复动作 | RTO 目标 |
|---|---|---|---|---|---|
| F1 | 单 api 实例死 | 无 | ELB 健康检查 | AS 补实例 | 秒级 |
| F2 | 单 worker 死 | processing 行钉住若干 bank | AS 钩子（H5）；**巡检周期 60-120s** + 卡死阈值（retain>1.5h/consolidation>2.5h/其余 P99×3） | **跨 schema 批量回收**（§5.5） | ≤5min（按巡检周期实测，非占位数） |
| F3 | worker 组整体故障 | 新任务积压（已受理不丢） | 队列深度 | AS 扩容 | 按积压 |
| F4 | RDS 主故障 | 读写中断 | RDS HA | 自动切换 + **切换期请求失败由 SDK 重试 + 操作级重试保 at-least-once**（引擎 acquire 重试 ~10s 桥不过切换，不能当恢复动作）；副本端点变化行为列 H2 | <2min（H2 实测） |
| F5 | PgBouncer 实例死 | 部分连接拒 | 健康检查 | 双实例互备（**暴露方式 ELB/DNS 与长连接再均衡行为待验证**） | 秒级 |
| F6 | 平台库故障 | **≤TTL(5min) 内已缓存租户可服务；超 TTL 后全部租户失败**（V2.0 低估） | CES | RDS HA；**配额 fail-close 已定（§4.4）；租户解析的 fail 策略=拒绝（安全优先），接受可用性损失并明示** | <2min |
| F7 | TEI 池故障 | recall 无查询向量；retain embedding 退避/延期（引擎 DeferOperation 原生） | 健康检查 | TEI AS 重建 | 分钟级 |
| F8 | LLM 限流 | 变慢 | 引擎指标 | 原生退避 | 随 Provider |
| F9 | OBS 故障 | 上传不可用；**上传失败即拒绝（受理不落，不留半态）** | OBS 健康 | — | 随云服务 |
| F10 | 误发布版本 | 新实例异常 | 健康检查+金丝雀 | **拆两段**：镜像回滚（随时可做）；schema 回退**仅限 `engine_deployments.reversible=true` 的版本**（104 个迁移的数据迁移 downgrade 多为故意 no-op——源码证据 `a1d3f5b7c9e2`/`b4c5d6e7f8a9` 注释）；发布规范采用 **expand-contract**（加列/索引先行，删旧列放 N+2 版）；金丝雀按 Cell/租户子集灰度 | 按发布流程 |
| **F11** | 平台控制器故障 | 开通/回收/升级停摆 | 双实例互检心跳 | 双实例 + 幂等 Job 抢占（状态全在平台库本就支持） | 分钟级 |
| **F12** | 指标采集器故障 | AS 伸缩失明（无指标无扩容）且无人发现 | **CES 指标断流告警（监控的监控）** + 采集器双活互检 | 双活采集 | 分钟级 |
| **F13** | provision_job 卡死 | 开通/升级悬挂 | running 超 lease_expires | 按 idempotency_key 幂等重跑 | 巡检周期 |
| **F14** | worker 存活但任务 hang | 钉死 serialization_key | F2 的卡死阈值 | 对该 worker 的行回收（**注明：decommission 活 worker 会导致另一 worker 重复执行——at-least-once 允许，LLM 费用重复由平台吸收，不转嫁租户**）；长期：引擎补丁扩展墙钟覆盖面（上游演进） | 巡检周期 |
| **F15** | RDS 磁盘满/WAL 膨胀 | 写路径全失败 | **CES 磁盘用量告警（须配置）** | 扩容+清理 | — |
| **F16** | 连接耗尽级联 | 排队延迟尖峰 | PgBouncer 池满指标 | §8.1 与 AS max 联动（互相击穿问题已收敛） | — |
| **F17** | PgBouncer 配置漂移/双实例不均 | 行为不一致/单边过载 | 配置 IaC 化 + 连接分布指标 | 重启均衡（行为待验证） | — |
| **F18** | OBS 桶配额/权限 | 上传失败 | OBS 告警 | 扩容/修权限 | — |
| **F19** | LLM 凭据过期/欠费 | 持续 401/403（重试无用） | 错误率告警 | 告警+人工介入 | 人工 |
| **F20** | webhook 持续投递失败 | 超 MAX_ATTEMPTS 置 permanently_failed **仅记引擎日志**（V2.0 无感知） | 平台侧死信告警 | **死信记录 + admin 重放命令（需开发）**；客户端轮询对账兜底（注意 `operation_retention_days` 若配置会限制轮询窗口） | — |

**数据层保障与对账（新增三个对账 Job，均入 provision_jobs 调度）**：
1. **跨实例一致性对账**：引擎 `list_tenants()` × 平台 `tenant_schemas` 逐 schema 比对（两库独立 PITR 时间线，恢复后可能不一致）→ 差异进修复队列；跨 Cell 迁移的 draining 态同理。
2. **OBS 孤儿对账**：原件桶前缀 × sources 账本 × 引擎 file_storage 三方比对（双写无跨系统事务：上传成功行插入失败→孤儿；删除是行先提交对象后 best-effort 删→孤儿）。
3. **bank_registry 对账**：引擎 `list_banks` × 平台镜像（deny-on-missing）。

**RPO（四行拆分）**：引擎库 = PITR 粒度（分钟级，随 H2 核实）；平台库 = 同；计费事件 = outbox 事务性 + 消费侧重放（丢失窗口≈0）；OBS = **版本控制开启**（误删原件可恢复——否则 §6.4 重放失去输入）。

**at-least-once 代价清单（明示）**：重复 LLM 事实抽取调用（重试/重放/decommission 各路径都会触发，直接费用）、重复 webhook 投递（租户端需幂等）。引擎幂等覆盖：chunks/memory_units 插入 ON CONFLICT content_hash、入队 bank 行锁去重、observation 去重仲裁；**不覆盖**：LLM 调用去重。平台计费按 operation_ref 幂等——引擎故障的重复执行成本由平台吸收，不转嫁租户。

---

## 10. 实施阶段与验收门槛（★ 评审后更新）

| 阶段 | 交付 | 验收门槛 |
|---|---|---|
| A：单 Cell 拆分 | H1a/H1b/H1c POC 通过；1×RDS PG + api/worker ECS + PgBouncer + TEI(预烘焙镜像) + 直通模式 APIG；V1 §7.1 配置档 + §7.2 补丁 1-2 | 双实例并发无串扰；**公网直连引擎端口被拒（S1）**；`MCP_AUTH_TOKEN` 未设置（S2）；PgBouncer transaction 全回归；H7 四项（obstore 实测）通过 |
| B：平台元数据+多租户 | §3.2 全部表；通用参考扩展；配额 fail-close + chunked 保守；开通控制器；worker_id 注入；**跨 schema 回收** | 双租户零串扰；开通不在请求路径；**F2/F14 演练：杀 worker 与 hang worker 按巡检周期实测端到端恢复**；**F6 演练：平台库切换期间已缓存租户可服务、超 TTL 拒绝语义符合声明** |
| C：弹性可观测 | 采集器直调 AS；lane gauges；AOM/LTS；断流告警 | 压测下按 §8.2 参数伸缩且 **AS max 不击穿连接预算**；F4 RDS 切换演练；F12 采集器双活切换；队列 at-least-once 边界内无丢失 |
| D：产品模式+替换就绪 | 产品 API + MCP 网关；sources 账本；绑定管理；三个对账 Job；死信重放 | **影子引擎按 §6.4 全流程**：账本重放（含删除不复活）→ 量化门槛 → 影子 → CAS → 反向回放 → 回滚演练；直通期数据对账补录 |
| E：规模与多 Cell | 多 Cell 放置；dedicated 模板；跨 Cell 迁移 runbook | 千级 schema：巡检/迁移/备份窗口实测；HNSW 索引数容量验证；dedicated Cell 独立故障域验证 |

---

## 11. 与 V1 的差异及计算形态演进

同 V2.0 表（略）——**设计不变量**更新为六条：元数据四类表在平台库；**来源账本（含删除）在平台+OBS**；引擎经接入合同（不只是 EngineProvider）接入；计量走事件不直写库；队列语义在引擎库；租户=schema、业务=bank、space 不跨引擎。

---

## 附录 A：评审记录（V2.0 → V2.1）

| 维度 | 严重 | 建议 | 关键采纳 |
|---|---|---|---|
| 产品契合度 | 3 | 5 | H7 改 obstore 实测；H1 拆硬/软依赖并定位爆炸点在开通路径；降级路径收敛（采集器直调 AS、去 GaussDB）；H4 拆 a/b；H1c PG15 权限；H12 RDS proxy 对比 |
| 多租户安全 | 4 | 9 | 网络硬约束清单（S1）；MCP 旁路禁用（S2）；chunked 绕过保守处理（S3）；配额 fail-close + 计量事件化（S4）；api_keys HMAC 硬化；删除序列先撤 key；冻结排空语义；TLS 边界；per-Cell 凭据；任务囤积配额 |
| 可靠性 | 7 | 6 | 跨 schema 回收（F2）；恢复机制自身 HA（F11-F13）；F4 重连语义修正；F10 拆镜像/schema 回退 + expand-contract；F6 影响修正；跨实例对账 Job；F14 hang 检测；F15-F20；RPO 四行；at-least-once 代价清单 |
| 弹性容量 | 5 | 4 | 连接预算五池公式 + 与 AS max 联动；读己之写 Day-1 全主库；lane 占用率指标 + 引擎补丁；采集走 public 例程防基数爆炸；TEI 预烘焙权重；端到端 6-8min 时延预算；HNSW 索引容量 |
| 接口解耦 | 7 | 7 | 写生命周期四方法；sources 账本（含删除/纠正/inline 归档）；幂等键契约化；影子期语义；接入合同八项；MCP 网关；评测门槛量化；反向影子回滚；spaces 表；映射表勘误（PUT） |

**评审方法限制**：五个评审均为源码+文档静态审查；产品能力在线核实通道不可用（网络策略阻断 WebFetch），所有华为云产品断言保留为待 POC（§7）。评审未运行任何系统——容量/延迟/SLO 数字均为预算目标。
