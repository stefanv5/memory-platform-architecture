# Hindsight 多租户 Serverless 架构设计（华为云·V3.1，经第二轮四维评审修订）

**V3 重写说明**：V2 及之前版本按"评审发现"组织，信息分散在各节。本版按**关注点**重组为单一自包含文档：每章讲完一件事（总览 / 架构决策 / 数据与映射 / 无状态 / 数据流 / 多租户 / 扩展），不需要跨章跳读。**V3.1 修订**（第二轮评审：清晰度新读者测试 / 架构决策对比 / 数据模型与寻址 / 无状态与扩展，15 严重项全部采纳）：ADR 重写为五方案对比（A-1/A-2/A-3/B'/B）并修正了 A2 事实错误；异步任务寻址机制更正为"认领时注入"两段式；数据清单补墓碑表/变更时间线/bank 画像/任务载荷快照并限定"可重建"范围；新增 §3.5 来源账本双存关系与 §3.4 非 PG 数据清单；修正 MCP 默认 stateful 陷阱与"零丢失"表述；连接预算拆为 RDS/PgBouncer 两侧。评审记录、扩展代码骨架、引擎源码事实详见文末"关联文档"。基线：Hindsight `12f2d54`，`core/` = `hindsight-api-slim/hindsight_api/`。华为云产品能力均为待 POC 假设（§10）。

---

## 0. 术语表（先读这一节，全文只用这些词）

本文的"Serverless"指：**AS 弹性伸缩组自动增减 ECS 实例 + 最低实例数保底**——客户不管理服务器、计算按负载自动扩缩；不承诺计算空闲归零（存储与平台服务费用常在，§2）。

| 术语 | 是什么 | 不是什么 |
|---|---|---|
| **Hindsight 引擎** | 一个软件包（`hindsight-api-slim`）。**同一个容器镜像、同一份代码**，按启动参数分成两种进程角色（官方文档口径为"三个服务"，第三个是调试用 UI，见 §0.1） | 不是一个"总控节点"，不是一套微服务 |
| **引擎·请求角色**（`hindsight-api`） | 引擎的进程角色之一：接 HTTP/MCP 请求，执行 recall / reflect / 同步 retain / 受理异步任务。**ELB 对接的就是它——它就是引擎本身** | 不是独立于引擎的"API 网关"或"API 服务" |
| **引擎·任务角色**（`hindsight-worker`） | 引擎的进程角色之二：跑异步任务（retain 的 LLM 流水线、consolidation、知识页刷新、webhook 投递） | 不是独立产品，与请求角色同镜像 |
| **Cell** | 一套独立部署 = 一组引擎 ECS + **一个** RDS PG 实例（引擎库）+ 一套运行配置（引擎版本、embedding 模型与维度）。租户被放进某个 Cell——**数据+计算的放置单位** | 不是负载均衡组；绑定后不可漂移，跨 Cell 迁移需搬数据 |
| **引擎库** | Cell 里那个 RDS PG 实例。存放租户的记忆数据。**schema = 租户**（一租户一个 schema，每个 schema 约 22 张表），表内 `bank_id` 列再分业务区 | 不是"每个租户一个 PG 实例" |
| **平台库** | 另一个独立 RDS PG 实例，存平台自己的元数据（租户注册表、API key、配额、计量、绑定） | 引擎不直连它（经 L2 的 HTTP） |
| **注册表** | 平台库里的 `tenants` / `tenant_schemas` 表，回答"租户→哪个 Cell、哪个 schema" | 不是引擎的组件，引擎只经缓存读它 |
| **bank** | 租户 schema 内的业务分区（一个项目/一个 agent 的记忆集合），就是表里 `WHERE bank_id=...` 的那个值 | 不是租户，不是 schema |

### 0.1 两个引擎角色的官方定义（引自 `hindsight-docs/docs/developer/services.md`，非本文杜撰）

官方文档把 Hindsight 描述为**三个服务**：API、Worker、Control Plane。前两个就是本文说的"引擎"（同一软件包、同一镜像、不同启动入口）；第三个是面向开发调试的 Web UI。

| 服务 | 官方定义（译自原文） | 入口与端口 | 健康端点（官方） |
|---|---|---|---|
| **API 服务** | "核心记忆引擎，处理全部记忆操作：**Retain**（摄取内容、抽取事实、构建知识图）、**Recall**（跨记忆的语义检索）、**Reflect**（带 disposition 特质的回答生成）。**API 服务是无状态的，可在负载均衡器后水平扩展；全部状态存于 PostgreSQL。**默认 API 也在内部处理后台任务（心智模型整合）；高吞吐部署可禁用内部 worker 改跑专职 worker" | `hindsight-api`，默认端口 8888 | 与 worker 相同的三端点＋指标（出处：官方 monitoring.md）：`/health/live`（不查库）、`/health` 与 `/health/ready`（readiness，查库）、`/metrics` |
| **Worker 服务** | "专职后台任务处理器。**与 API 服务使用同一个软件包和 Docker 镜像，只是入口不同**。worker 以 PostgreSQL 为任务 broker，轮询领取待处理任务；多个 worker 可同时运行而不冲突" | `hindsight-worker`，默认指标端口 8889 | `/health/live`（liveness，不查库）、`/health` 与 `/health/ready`（readiness，查库）、`/metrics` |
| **Control Plane** | "管理/浏览记忆库的 Web UI：浏览 bank、查看实体与关系、摄取历史与操作、交互测试 recall。连接 API 服务，面向开发与调试" | 裸机可 `npx` 独立运行 | — |

与本文架构的对应关系：**API 服务 = 引擎·请求角色**（ECS 伸缩组 + ELB 后水平扩展——官方原文就是 "stateless and can be horizontally scaled behind a load balancer"）；**Worker 服务 = 引擎·任务角色**（官方原文 "uses PostgreSQL as a task broker"——与本文 §5 描述的轮询领取机制完全一致）；**Control Plane 不部署**——它是单租户调试工具（数据面地址硬编码单实例，V1 调研已确认），多租户产品由平台自己的控制台替代。官方还给出 worker 缩容守则："缩容或移除 worker 前，用 `hindsight-admin decommission-worker <worker-id>` 释放其任务"——正常缩容由优雅排空覆盖该守则（§4.3），实例异常死亡由回收控制器（§8-F2）自动执行 decommission 等价操作。

---

## 1. 架构总览

### 1.1 主架构图

图中**编号①-⑧是数据流向**（§5 逐步展开）；引擎实例框内的"无本地状态"标注是第 4 章的主题；虚线是扩展路径（第 7 章）。

~~~mermaid
flowchart TB
  C["客户端<br/>SDK / MCP / coding-agents<br/>（请求携带：① API key ② bank 名）"]:::external

  APIG["APIG（华为云 API 网关）<br/>TLS · 限流 · 调 L2 做认证"]:::commercial

  subgraph L2["平台层（自研，ECS 双实例）——产品的大脑"]
    AUTH["鉴权/路由服务<br/>输入：API key 哈希 → 查平台库<br/>输出：租户ID / Cell / schema 名（仅供路由，不进断言）/ 允许的 bank<br/>＋ 生成签名断言（≤60s）注入 header"]:::platform
    CTRL["控制器：租户开通/worker 回收/版本升级/对账"]:::platform
    MEV["计量接收 → 平台库"]:::platform
  end

  subgraph CELLA["Cell-A（一套引擎部署 + 一个引擎库 PG）"]
    direction TB
    ELB["ELB（仅 VPC 内）<br/>加权轮询·无会话保持·健康检查 /health"]:::commercial

    subgraph ENG["Hindsight 引擎（同一镜像，两种角色）"]
      direction LR
      subgraph ROLE1["请求角色组 hindsight-api ×N（AS 弹性，min 2）"]
        A1["实例①<br/>━━━━━━━━━━<br/>无本地状态：<br/>·请求上下文=请求级<br/>·租户/配置=只读缓存<br/>·任务/数据=不在实例上"]:::hs
        A2["实例② …"]:::hs
      end
      subgraph ROLE2["任务角色组 hindsight-worker ×M（AS 弹性，min 1）"]
        W1["实例①<br/>━━━━━━━━━━<br/>无本地持久状态：<br/>·任务状态在 DB 行上<br/>·worker_id 只是记账标记"]:::hs
        W2["实例② …"]:::hs
      end
    end

    PB["PgBouncer ×2（transaction 模式）<br/>引擎库连接的唯一入口"]:::oss
    TEI["TEI 推理池<br/>embedding + rerank"]:::oss
  end

  subgraph DCUST["客户数据存储：引擎库 RDS PG 实例（每 Cell 一个）——只存客户记忆数据"]
    RDSE[("引擎库（Cell-A 绑定）<br/>public：跨租户例程<br/>t_3f2a… schema＝租户甲<br/>t_9b7d… schema＝租户乙<br/>（每 schema 同样 22 张表）")]:::commercial
  end
  subgraph DPLAT["平台元数据存储：平台库 RDS PG 实例——独立实例、独立凭据、独立迁移"]
    RDSP[("平台库<br/>注册表 tenants/tenant_schemas<br/>api_keys / 配额 / 计量 / 来源账本")]:::commercial
  end
  subgraph DOBJ["对象存储 OBS（独立桶）"]
    OBS[("OBS<br/>文档原件对象（来源账本行存平台库）<br/>（引擎文件后端走 S3 兼容端点）")]:::commercial
  end

  LLM["LLM API<br/>（引擎原生支持 deepseek/智谱/火山/…）"]:::external
  SCALE["指标采集器（双活）<br/>→ 直调 AS API 扩缩引擎两组"]:::platform

  C -->|"① 请求"| APIG
  APIG -->|"② 认证+路由到 Cell"| AUTH
  AUTH -->|"③ 带断言 header 转发"| ELB
  ELB --> A1
  A1 -->|"④ SQL：全限定表名+WHERE bank_id（经 PgBouncer）"| RDSE
  A1 -->|"④' 受理异步任务：INSERT 任务行"| RDSE
  W1 -->|"⑤ 每 500ms 轮询领取（SKIP LOCKED）"| RDSE
  W1 -->|"⑥ 执行：LLM 抽取/embedding/写入记忆"| RDSE
  A1 & W1 --> TEI
  A1 & W1 --> LLM
  W1 -->|"⑦ 完成事件（计量）"| MEV
  W1 -->|"⑧ webhook 通知客户端"| C
  AUTH -.->|"产品受理时归档原件（账本行写平台库）"| OBS
  A1 -.->|"引擎文件后端（S3 兼容端点）"| OBS
  AUTH & CTRL & MEV --> RDSP
  CTRL -.->|"admin CLI 直连（开通/回收/迁移）"| RDSE
  SCALE -.->|"扩容=加引擎实例；扩容=加 Cell"| ROLE1 & ROLE2

  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

### 1.2 整体故事（一段话）

客户端请求经 APIG 到平台鉴权服务：验 API key、查注册表得知"这个租户在 Cell-A"，把请求转发到 Cell-A 的 ELB 并注入签名断言。ELB 把请求交给 Cell-A 里**任意一台**引擎请求角色实例（实例无状态，谁闲谁接）。实例验证断言、从缓存拿到租户的 schema 名，然后对引擎库执行全限定表名的 SQL（`t_3f2a….memory_units WHERE bank_id=…`）。写路径上，耗时任务被受理成引擎库里的任务行；任务角色实例自主轮询领取执行（执行中调 LLM/TEI，结果写回引擎库），完成后发计量事件与 webhook。**引擎的两个角色之间没有任何直接连接——它们只通过引擎库的任务表协作。**

### 1.3 组件与职责

| 组件 | 是谁的 | 职责 | 为什么需要它 |
|---|---|---|---|
| APIG | 华为云 | TLS、限流、API 发布、触发认证 | 不想让引擎直接暴露公网 |
| 平台鉴权/路由 | 自研 | 验 key、租户→Cell 路由、注入签名断言、bank ACL | 多租户的准入控制（引擎不做终端用户认证） |
| 平台校验器（validator） | 自研（引擎扩展槽加载，与引擎同进程） | 在断言之外执行 bank ACL、租户状态（冻结）校验、配额检查 | 断言证明"是谁"，validator 执行"能做什么"的细粒度规则 |
| 引擎（两角色） | Hindsight | 全部记忆能力：retain/recall/reflect/consolidation | 核心引擎 |
| ELB | 华为云 | Cell 内流量分发、健康检查 | 多实例无状态分发的标准做法 |
| PgBouncer | 开源 | 引擎库连接收敛 | RDS 连接数有限，实例弹性会导致连接爆炸 |
| TEI 池 | 开源 | embedding + rerank 推理 | 引擎默认的本地模型不适合弹性部署（§4.1） |
| 引擎库 RDS | 华为云 | 租户记忆数据（schema-per-tenant） | 持久层 |
| 平台库 RDS | 华为云 | 注册表/配额/计量 | 控制面真相源，与数据面隔离 |
| OBS | 华为云 | 文档原件 + 来源账本 | 引擎可替换的前提（平台持原件） |
| 指标采集器 | 自研 | 聚合队列/负载指标，驱动 AS 扩缩 | 华为云 AS 不认识引擎的队列语义 |

---

## 2. 关键架构决策：引擎如何找到租户的 PG

### 2.1 候选方案（"引擎怎么找到租户的 PG"的四种答案）

| 方案 | 一句话 | 引擎侧改动 |
|---|---|---|
| **A-1 引擎多池直连** | 所有引擎实例组成一个无差别大池；中心节点告知"这个租户在哪个 PG"，引擎为每个 PG 动态建连接池 | 最大 |
| **A-2 代理层路由** | 引擎仍只连一个静态端点（代理）；代理读 SQL 里的 schema 前缀，把语句转发到对应 PG | 中（代理选型+poller 重构） |
| **A-3 SDK 直连** | 客户端从中心节点拿到引擎端点直连对应 Cell | 无（= B 的客户端侧变体；但与"引擎不暴露公网"冲突，弃） |
| **B' 引擎多库选池** | 一套引擎部署组配置多个数据库地址，按注册表给 schema 选池 | 中（数周） |
| **B Cell 静态绑定（本方案）** | 每套引擎部署（Cell）启动配置写死自己绑定的 PG；租户开通时放进某 Cell；引擎再用注册表缓存查"租户→schema" | **零** |

注意：**所有方案都有"中心节点 + 缓存"**——引擎都要从注册表拿租户信息。分歧只在：注册表要不要回答"哪个 PG"，引擎要不要动态面对多个 PG。

### 2.2 引擎源码约束（已逐条核实）与各形态的真实代价

| # | 约束 | 源码证据 |
|---|---|---|
| C1 | 租户解析合同**只能返回 schema 名**，没有返回数据库端点的位置 | `TenantContext` 唯一字段 `schema_name`（`core/extensions/tenant.py:24-34`） |
| C2 | 数据库地址是**进程级静态配置**（主/读/迁移最多 3 个 URL），启动时对主、读两个地址建池，**无按租户动态选池** | `config.py:153-157`；`engine/db/postgresql.py:305` 的 `create_pool` |
| C3 | SQL 是**单段式全限定表名**（`"schema".table`），没有"先选库再查表" | `fq_table()`（`core/engine/schema.py:16-27`）+ 运行时守卫 |
| C4 | admin 工具（迁移/备份/回收）全部面向单一数据库地址 | `admin/cli.py:314-326`（单 URL 直连） |
| C5 | **跨租户发现是"单库内枚举全部 schema"的语义**：`public.schemas_with_pending_work()` 等例程 + worker 的 schema 扫描都假设一个库 | `engine/schema.py:30-44`（`fq_routine`）；`worker/poller.py:404-464` |
| C6 | 引擎运行时**禁用 advisory lock**（在连接池代理后不可靠）；迁移锁只在直连上有效 | `engine/db/ops_postgresql.py`（索引锁注释）；`migrations.py:432-458` |

各形态撞上的墙：

- **A-1**：C1-C4 全破——引擎要变成"多数据源管理器"（动态池生命周期、跨库迁移路由、每租户池隔离），数月级改造，改完实质是新引擎。
- **A-2**：C1/C2/C3 **不破**（引擎仍连单一静态端点；全限定表名恰好把路由键写进了每条 SQL——这是 A-2 的巧妙之处）。真实障碍是：① C5——poller 的跨租户发现必须重构成逐库轮询；② PG 生态**没有经过验证的生产级"按 schema 前缀路由"池化器**（pgcat 一类形态存在，但与 pgvector 的 DDL、事务、COPY 语义兼容性未验证，列为待 POC）；③ 连接矩阵没有消失，只是从引擎挪进代理层。
- **B'**：C2 破但**有现成缝隙**——`DatabaseBackend` 抽象 + `_get_read_backend()` 已证明"按调用点选池"的先例（`memory_engine.py:5421`），acquire 调用汇聚于少数入口，改造约数周。但 C5 同样要改（poller 逐库），且**连接预算从"每 Cell 有界"变成 K×每池**——直接削弱 B 的核心卖点之一。

### 2.3 对比（诚实版：双方优缺点都列全）

| 维度 | A（动态路由，含代理形态） | B（Cell 静态绑定，本方案） |
|---|---|---|
| 引擎改动 | A-1 数月 / A-2 数周+代理验证 | **零** |
| 计算实例 | 完全无差别：单一伸缩组/启动模板/升级流程（运维同质是真实优点） | 按 Cell 分组；Cell 数增长后有版本碎片管理成本 |
| 引擎怎么找到 PG | 每请求（或缓存）问中心节点 | 不找；部署时绑定，L2 把请求路由到 Cell |
| 一个 PG 故障的影响 | 该 PG 租户失败；计算组可整体漂移继续服务其他 PG（优点） | 只影响该 Cell 租户；该 Cell 引擎组闲置需人工重绑 |
| 新 PG 上线/退役 | 对引擎透明 | 全套部署组+ELB+伸缩组+配置（运营动作） |
| 连接管理 | 矩阵在引擎（A-1）或代理（A-2）层，不消失 | 每 Cell 一套池，预算有界可算（§7.3） |
| 租户容量上限 | 全局 PG 数，无单 Cell 地板 | 受单 Cell 阈值；大租户超限须迁移（有机制，§6.2） |
| 引擎版本 / embedding 维度隔离 | 无处安放 | 天然：一个 Cell = 一套版本 + 一个维度（维度冻结是引擎硬约束） |
| 租户放置与迁移 | 中心调度自动；迁移只改一条记录 | 开通时运营决策；不均衡时 export/import 搬数据（排空窗口） |
| 跨 Cell/库的联合分析 | 可单点 SQL | 逐 Cell 拉取聚合 |
| 落地周期 | 数周-数月 | 平台层开发数周（L2 是主要工作量） |

### 2.4 结论

在"不改 Hindsight 引擎核心"的前提下，**只有 B 可落地**（A-2/B' 都可行但都要改引擎且各有一个未验证依赖）。B 以 L2 平台层的开发为代价，换来：故障域隔离、版本/维度隔离、有界连接预算三样 A 给不了的东西；B 的代价（租户放置粒度粗、迁移搬数据、每 Cell 最低成本、跨 Cell 聚合麻烦）如上表如实列出。

**演进路径**：将来若自研引擎原生支持动态数据源（A 形态反超），本架构**保留件**：APIG、平台鉴权/注册表、平台库（含账本）、OBS；**重造件**：路由职责（从"路由到 Cell"变为"路由到 PG"）、每 Cell 一套 ELB/PgBouncer 的池化部署形态。

---

## 3. 客户数据：有哪些、怎么存、怎么映射到 PG

### 3.1 数据全景（一个客户在系统里的全部数据，8 类）

| # | 数据类别 | 具体内容 | 存在哪 | 为什么存这 |
|---|---|---|---|---|
| 1 | 身份与访问 | 租户记录、API key（哈希）、bank 目录、ACL | **平台库**（tenants / api_keys / bank_registry / spaces） | 控制面真相源；引擎不认终端用户 |
| 2 | 来源原件（冷） | 上传的文件、inline 文本归档、版本、删除记录、用户纠正 | **OBS** + 平台库 `sources` 账本行 | 引擎可替换的前提：平台持原件，才能重放（§9） |
| 3 | 记忆主体（热） | 文档行（含全文列）、分块、**记忆单元（事实 + 向量列 + 用户 metadata/context 列 + 时间字段）**、实体、实体-记忆关联、记忆间链接（图）、**失效记忆归档（墓碑表，无向量，recall 跳过——删除/GDPR 在引擎侧的落点）** | **引擎库·租户 schema** | 检索热路径；向量是行内列（pgvector），图是关系表——**没有独立向量库/图库** |
| 4 | 派生知识与用户策展 | 心智模型（含向量）及其**变更历史时间线**、观察历史、知识页、行为准则 | 引擎库·租户 schema | consolidation 派生的部分可重建；**用户创建/固定的部分（directives、pinned 心智模型、非托管知识页、webhook 订阅）重放不可恢复**——迁移走 export-bank，替换时需账本补记（§9） |
| 5 | 任务与操作 | 异步任务行（状态/worker/重试 + **受理内容完整快照，默认永久保留——删除合规须评估保留期策略**）、图维护/实体维护两条队列 | 引擎库·租户 schema | 队列即表——任务与数据同事务（受理即持久） |
| 6 | 观测 | 审计日志、LLM 调用记录 | 引擎库·租户 schema | best-effort（进程死即丢），精确计量走第 7 类 |
| 7 | 计量 | token 用量事件、配额 | 平台库 usage_events / quotas | 计费真相（经引擎完成钩子事件化上报） |
| 8 | bank 配置与人格画像 | 每个 bank 的 disposition（怀疑度/字面度/共情等特质）、mission、三级配置覆盖 | 引擎库 `banks` 表（运行时真相）；目录与 ACL 真相在平台库 `bank_registry`（每日对账 + 变更钩子同步） | disposition 影响 reflect 行为——这是客户数据不是系统配置 |

### 3.2 租户数据在引擎库里的形态

**先回答"schema 是谁的概念"**：PostgreSQL 的 schema 是数据库原生的命名空间（一个库内的表分组），不是本文发明的。**Hindsight 引擎原生使用它做租户隔离**——引擎的租户扩展合同就是"认证后返回一个 schema 名"（`core/extensions/tenant.py` 的 `TenantContext.schema_name`），所有 SQL 由 `fq_table()` 自动拼接 schema 前缀，迁移工具按 schema 逐个执行，admin CLI 有 `--schema` 参数。但"**一个租户 = 一个 schema**"这条映射规则是**部署方通过扩展自定义**的：官方 Supabase 扩展用 `user_<uuid>` 命名，static-keys 扩展用 `user_<id>`，本平台用 `t_<租户ID前12位hex>`。一句话：**机制（schema 隔离）是 Hindsight 引擎原生的；命名规则与租户粒度是平台自定义的。**

租户甲（`tenant_id=3f2a91c0…`）开通后，引擎库（Cell-A 的 PG）里出现 schema `t_3f2a91c0d4e2`，含约 22 张表（权威清单 = `core/admin/cli.py:58-83`）：

```
t_3f2a91c0d4e2（租户甲的 schema）
├── banks              租户的 bank 目录（bank_id 主键 + config JSONB 三级配置）
├── documents          原始文档行（id + bank_id 复合主键）
├── chunks             文档分块（PK = bank_文档_序号 拼接）
├── memory_units       ★核心表：每行=一条事实
│     · text           事实文本
│     · embedding      pgvector 向量列（HNSW 部分索引 × bank × fact_type）
│     · fact_type      world / experience / observation
│     · bank_id        ← bank 分区就在这一列
│     · event_date…    时间字段（时间检索用）
├── entities / unit_entities / entity_cooccurrences / memory_links   实体与图
├── mental_models / mental_model_history / observation_history<br/>knowledge_pages / directives   派生知识与用户策展（含变更时间线）
├── invalidated_memory_units   失效记忆墓碑（无向量列，recall/consolidation 跳过）
├── async_operations / graph_maintenance_queue / entity_maintenance_queue   任务与两条维护队列
├── webhooks   webhook 订阅配置（待发件是 async_operations 里的任务行）
├── audit_log / llm_requests   观测
└── attachments / file_storage / banks（disposition/mission/config）…
```

**具体一行**：租户甲在 bank `team-proj` 里 retain 了一条"用户偏好函数式编程"→ `t_3f2a91c0d4e2.memory_units` 多一行：`text='用户偏好…'`、`fact_type='world'`、`bank_id='team-proj'`、`embedding=[0.12, -0.53, …]`（维度=Cell 配置，默认 384）、时间字段（`occurred_start/end` 事实发生区间、`mentioned_at` 提及时间）。租户乙的同类数据在它自己的 schema 里，物理上同库不同 schema。

**向量索引**：每个 bank × fact_type 一枚部分向量索引（bank 创建时即建）+ 一枚全局向量索引（租户开通耗时的主体）；索引类型随 Cell 配置（默认 pgvector/HNSW，可选 diskann/vchord 等）。**索引数量约束**：活跃 bank × 3 + 全局——万级活跃 bank 即 3 万枚索引，是加 Cell 的触发阈值之一（§7.2）。

### 3.3 引擎寻址：从请求到数据的完整链路（三步）

先看一张请求信息的流转图——**请求带了什么、每一步凭它换到了什么**：

~~~mermaid
flowchart LR
  subgraph WHAT["客户端请求里实际携带的信息"]
    K["① Authorization: Bearer {API key}"]:::external
    B["② URL 路径里的 bank 名<br/>/v1/{space}/memories/recall<br/>（产品 API 形态，转发时改写为引擎路由）"]:::external
  end
  K --> P1["平台鉴权服务<br/>用 key 哈希查平台库 api_keys<br/>→ 得到：tenant_id ＋ 允许的 bank 列表"]:::platform
  P1 -->|"用 tenant_id 查"| P2["查注册表 tenant_schemas<br/>→ 得到：cell_id ＋ schema_name<br/>（schema 名仅供路由，不进断言）"]:::platform
  P1 & P2 & B --> AS["生成签名断言（HMAC，≤60 秒有效）<br/>载荷：tenant_id / key_id / 允许的 bank<br/>（bank 名在此对照允许列表做 ACL 校验）"]:::platform
  AS -->|"转发：断言放放行 header；<br/>Authorization 换成平台内部凭据"| ENG["引擎实例（L2 依 cell_id 转发到<br/>Cell-A 的 ELB，轮询落本实例）"]:::hs
  ENG --> V["引擎验断言签名 → 查本地缓存<br/>（miss 时问平台）→ schema 名进请求上下文"]:::hs
  V --> SQL["SQL 生成：<br/>SELECT … FROM #quot;t_3f2a…#quot;.memory_units<br/>WHERE bank_id = 'team-proj'"]:::hs
  SQL --> PGDB[("引擎库 PG<br/>（部署时静态绑定——见下）")]:::commercial
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

要点：**用户的 API key 不进引擎**——L2 转发时把 Authorization 替换为平台内部凭据，身份断言经放行 header 传入（引擎收到的 `RequestContext.api_key` 是内部凭据，不是用户 key；引擎另支持用户 key 直连模式，本方案关闭）；**bank 名是唯一进入 SQL 寻址谓词（`WHERE bank_id`）的客户信息**；schema 名是引擎收到请求后自己换来的（缓存或问平台）；PG 地址谁都不用查——部署时绑定。三步展开：

```text
第一步：找到 PG     —— 不找。引擎部署（Cell）启动配置写死 DATABASE_URL，
                      绑定本 Cell 的引擎库。租户→Cell 由平台在开通时决定，
                      请求由 L2 路由到对应 Cell。
第二步：找到 schema —— 请求到达引擎后：验证签名断言 → 查注册表缓存
                      （miss 时 HTTP 查 L2，TTL 5 分钟）→ 得到 schema 名
                      → 放入请求级上下文（contextvar）
第三步：找到行     —— 所有 SQL 自动拼接全限定表名 + WHERE bank_id：
                      SELECT … FROM "t_3f2a91c0d4e2".memory_units
                      WHERE bank_id = $1 ORDER BY embedding <=> $2 …
```

**同一个连接池服务所有租户**——不切连接、不切数据源，只是 SQL 文本里的 schema 前缀不同。这对连接池代理（PgBouncer transaction 模式）友好的根本原因是引擎的四个专门适配：① 禁用预备语句缓存（`statement_cache_size=0`）；② 每次从池里取连接都用一条批量 `SELECT set_config(...)` 重放会话参数（不依赖连接的会话残留）；③ `application_name` 每次取连接重设；④ 运行时禁用 advisory lock（在代理后不可靠——这也是迁移必须直连的原因，与下表第 4 条路径互为印证）。

四条执行路径都走通这三步：

| 路径 | 谁执行 | 寻址方式 |
|---|---|---|
| 同步请求（recall/reflect/小 retain、知识库导出、reflect 内部的检索） | 请求角色 | L2 路由到 Cell → 断言 → 注册表缓存 → schema → SQL |
| 异步任务执行 | 任务角色 | **两段式**：发现（租户扩展枚举 + `public.schemas_with_pending_work()` 例程扫描本库有活的 schema）→ 认领时 poller 把 schema 写进任务的 `_schema` 字段 → 执行时恢复上下文。新租户被 worker 发现最多滞后 60 秒（枚举缓存） |
| 后台维护（consolidation 对账、知识页 cron、清理） | 任务角色 | **两段式**：先由 public 例程从库里枚举候选 schema（如 `banks_needing_consolidation()`）→ 再用 `list_tenants()`（注册表缓存）过滤出 worker 会轮询的 schema → 生成任务行 |
| 控制面操作（开通/迁移/回收/备份） | 平台控制器 | 直接读写平台库；对引擎库用 admin CLI **直连**（绕过 PgBouncer，advisory lock 需要）。注意：`import-bank` 会在 CLI 进程里拉起完整引擎（含迁移），不是纯直连 |

### 3.4 PG 之外的数据清单与处理（调研结论，全部有代码位置）

"数据都在 PG 里"只对了一半——引擎还有一批**存在 PG 之外**的数据，Serverless 部署必须逐项处理：

| 数据 | 实际存哪 | 代码位置 | 处理方式 |
|---|---|---|---|
| 附件/上传文件 blob | **默认（native）就在 PG**：`file_storage` 表 BYTEA 列；s3/gcs/azure 模式则存对象存储 | `engine/storage/postgresql.py:61-131`；后端工厂 `storage/__init__.py:44-122` | 默认模式天然兼容 RDS；文件量大时切 `HINDSIGHT_API_FILE_STORAGE_TYPE=s3` 指向 OBS（键自带 bank 前缀） |
| 文档/整库导出档案（transfer.zip） | file_storage（native 模式=PG 表） | 存入 `{bank前缀}exports/{uuid}/`，`memory_engine.py:2990-2996` | **无 TTL、只在删 bank 时清扫**——长期运行无限增长；切对象存储 + 生命周期规则 |
| OAuth 凭据（codex/nous/xai 的 LLM 登录态） | **本地磁盘 JSON**：`~/.codex/auth.json` 等 + `.lock` 文件 | `providers/codex_auth.py:87-90`、`nous_auth.py:94-95`、`xai_oauth_auth.py:235-244`；跨进程锁 `oauth_store_lock.py:63-121` | **Serverless 档禁用这三类 Provider**（改 API key 型：deepseek/智谱/火山）。原因：刷新令牌是 rotating 且须写回本地文件；Nous 的刷新令牌单次有效且有服务端重用检测——**两个实例用同一旧令牌刷新会触发盗用信号、吊销整个会话**（`nous_auth.py:22-30`） |
| 本地模型权重（embedding/rerank/llama.cpp） | HF 缓存目录、`~/.hindsight/models`（llama.cpp 约 3.5GB） | `embeddings.py:355-360`、`cross_encoder.py:147-174`、`llamacpp_llm.py:83-99` | 消除：全远程推理（§4.4）；官方 Docker 镜像本就排除 llama.cpp |
| **pg0 嵌入式 PG 数据目录** | **实例本地磁盘** | 默认 `DATABASE_URL="pg0"`（`config.py:1084`），引擎静默起本地 PG（`memory_engine.py:5010-5028`） | **必须显式配置 RDS 地址**——否则所有"PG 数据"实际落在实例临时盘，实例回收即全部丢失，多实例各持一份互相不一致 |
| MCP 会话（stateful 模式） | **进程内存** | `api/mcp.py:395-439`；默认 stateful（`config.py:1506`） | 显式 `HINDSIGHT_API_MCP_STATELESS=true`（§4.1 已列入验收） |
| admin CLI 的 backup / export-bank 产物 | **操作者本地磁盘** zip | `admin/cli.py:375-393, 1220`；`import-bank` 在 CLI 进程拉起完整引擎（含迁移，`cli.py:1261-1283`） | 运维工具保留在运维跳板机（常驻或按需拉起的一台 ECS，有本地盘）上跑；运行时不用它 |
| 可安全丢弃的临时数据 | 临时目录（markitdown 解析产物自清理、CLI provider 工作目录）、daemon 日志、多 worker 指标快照 | `parsers/markitdown.py:205-228` 等 | 无需处理（随进程生死） |

**一句话**：native 文件模式和审计/追踪/webhook 队列本来就落在 PG（随 RDS 高可靠）；**必须消除的是 pg0 默认值、本地模型、OAuth 文件型 Provider、stateful MCP 四项**（前三项已在 Serverless 配置档中强制，第四项在 §4.1）；导出档案和 OBS 化文件是容量治理项。

### 3.5 来源账本与引擎侧原件的双存关系（替换协议的基础）

引擎库里其实有**第二份原件**：`documents.original_text` 存全文、attachments 的 blob（native 文件模式下存表内、s3 模式下存 OBS）。平台侧另有 sources 账本（平台库）+ OBS 原件对象。三者的分工与写入规则：

| 问题 | 答案 |
|---|---|
| 谁写账本 | **平台产品 API 在受理时**：先归档原件到 OBS（inline 文本也归档）→ 写 `sources` 账本行（含 content_hash 与删除标记）→ 才调引擎 retain |
| 直通模式的窗口期 | 直通模式绕过产品 API，**没人写账本**——这段数据不在重放覆盖范围；切产品模式前用"引擎 documents × 账本差集"对账补录，或向客户声明截止点 |
| 删除如何传播 | 账本行置 `status=deleted`；引擎侧 `delete_source` 把记忆行清掉、归档进 `invalidated_memory_units` 墓碑表——重放时两处都会被账本的删除标记挡住（防复活） |
| 双存的一致性 | 引擎侧原件是**缓存性质**（可从 OBS 重建）；账本+OBS 是真相。周期对账任务（平台控制器定时发起）比对三方（OBS 对象 × 账本 × 引擎 file_storage 引用），孤儿对象清理 |

---

## 4. 引擎无状态的实现

### 4.1 实例解剖：什么在实例上、什么不在

~~~mermaid
flowchart TB
  subgraph INST["一台请求角色实例（hindsight-api 进程）"]
    direction LR
    CV["请求上下文<br/>（contextvar）<br/>当前 schema / bank / 租户<br/>——随每个请求创建、随响应消亡"]:::hs
    CA["只读缓存<br/>租户→schema（TTL 5min）<br/>bank 配置（TTL 30s）<br/>——丢了自动重建"]:::hs
    PO["DB 连接池<br/>连本 Cell 的 PgBouncer<br/>——断线重连即可"]:::hs
    SE["进程内并发闸<br/>限制单实例同时处理的请求数<br/>——总并发=单实例上限×实例数×每实例进程数"]:::hs
    BG["后台循环与内置任务处理<br/>——本方案以配置关闭请求角色的两者：<br/>内置 worker 开关 WORKER_ENABLED=false<br/>（否则请求角色会抢领任务！）<br/>＋维护任务间隔设 0（原生开关）<br/>只在任务角色运行；幂等可续跑"]:::hs
  end
  OUT["✗ 不在实例上的东西（全在持久层）：<br/>异步任务 → 引擎库 async_operations 表<br/>记忆数据 → 引擎库租户 schema<br/>原件与账本 → OBS + 平台库<br/>配置真相 → env + banks.config 表<br/>会话状态 → 无（**注意：引擎默认开启 MCP 有状态会话，<br/>Serverless 配置档必须显式设 HINDSIGHT_API_MCP_STATELESS=true**，<br/>否则与 ELB 轮询不兼容）"]:::oss
  INST ==>|"进程被杀 = 框内全部蒸发，零数据损失"| REB["新实例自举：新请求自带上下文<br/>缓存 miss 查 L2 重建<br/>池重连"]:::platform
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

任务角色实例的差别只有一处：它**执行中**的任务在引擎库的行上标记 `status=processing, worker_id=本实例ID`——这是**记账**不是持有：任何实例都能执行任何任务（只要行被重置为 pending）。详见 §4.3。

### 4.2 请求生命周期（无会话亲和的证明）

~~~mermaid
sequenceDiagram
  autonumber
  participant U as 用户甲
  participant E as ELB
  participant X1 as 引擎实例①（请求角色）
  participant X2 as 引擎实例②（请求角色）
  participant R as 引擎库
  U->>E: 请求 1（recall）
  E->>X1: 轮询分给实例①
  X1->>X1: 断言→schema 进请求上下文（缓存命中）
  X1->>R: SQL（全限定表名）
  X1-->>U: 结果（上下文随响应销毁）
  U->>E: 请求 2（同一用户，紧跟其后）
  E->>X2: 轮询分给实例② ← 换了实例！
  X2->>X2: 同样流程：断言→缓存 miss 则查 L2→自举
  X2->>R: 同样的 SQL
  X2-->>U: 同样正确的结果
  Note over X1,X2: 两次请求落不同实例都能正确工作——因为实例上没有任何跨请求状态。<br/>这就是"无状态"：不是把状态共享，而是实例只持有可丢弃的派生物。
~~~

### 4.3 "杀实例"演练（无状态的最终检验）

| 杀掉谁 | 立刻发生什么 | 丢失什么 | 恢复方式 |
|---|---|---|---|
| 一台请求角色实例 | ELB 健康检查摘除，流量切到其他实例 | **业务与任务数据零丢失**（在途请求失败由客户端重试；已受理的异步任务在表里）；在途的审计/LLM 追踪行会丢（best-effort 设计，§3.1 第 6 类——不要拿它做合规承诺） | AS 自动补一台，自举后入 ELB |
| 一台任务角色实例（优雅终止） | 进程收到 SIGTERM → 30 秒排空 → 把自己的 processing 行释放回 pending | 零 | 行被其他实例下一次轮询领取 |
| 一台任务角色实例（异常死亡） | 它的 processing 行留在原地（钉住相关 bank） | 零数据（at-least-once） | 回收控制器跨 schema 重置行 → 其他实例领取（§8-F2） |
| 全部请求角色实例 | Cell 不可服务 | 已受理任务不受影响 | AS 从镜像重建（2-3.5 分钟） |

### 4.4 为什么默认本地模型不能进这个架构

引擎默认用进程内 SentenceTransformers 模型做 embedding/rerank（数百 MB 权重、分钟级加载、GPU/内存占用）。弹性部署下每个新实例都要加载一遍 = 冷启动不可用。因此 Serverless 配置档强制改为远程推理（TEI 池或云 API）——引擎原生支持（`tei`/`openai`/`cohere`… Provider，V1 §1.3），改环境变量即可。这是"无状态"的必要条件之一：**实例镜像里没有任何模型权重**。

---

## 5. 数据流

### 5.1 写路径（retain 端到端；图内自动编号即执行顺序）

~~~mermaid
sequenceDiagram
  autonumber
  actor C as 客户端
  participant G as APIG
  participant P as 平台鉴权/路由（L2）
  participant E as 引擎·请求角色（任意实例）
  participant R as 引擎库（租户 schema）
  participant W as 引擎·任务角色（某实例）
  participant I as TEI / LLM
  participant O as OBS（原件 + 账本对象）
  C->>G: POST /v1/{space}/memories（大内容；space 即 bank 在产品 API 的名称）
  G->>P: 触发认证
  P->>P: 验 API key → 查注册表：租户在 Cell-A、schema=t_3f2a…
  P->>P: 配额检查（fail-close；超限直接拒）
  P->>O: 归档原件（inline 文本也归档）+ 写 sources 账本行（产品模式；直通模式无此步，见 §3.5）
  P->>E: 转发到 Cell-A 的 ELB + 注入签名断言 header
  E->>E: 验断言 → 注册表缓存取 schema → 请求上下文
  E->>R: 受理：与业务写同一事务 INSERT 任务行（payload 不含租户信息；认领时由 poller 注入 schema，§3.3）
  E-->>C: 202 + operation_id（任务已持久化，此后任何实例死了都不影响）
  Note over W: 请求角色与任务角色之间没有任何调用——从这行开始换演员
  W->>R: 每 500ms 轮询：SELECT … FOR UPDATE SKIP LOCKED（bank/文档串行化谓词）
  loop 每个 chunk 批次（流式，逐批提交）
    W->>I: 事实抽取（每 chunk 一次 LLM 调用）+ 批量 embedding
    W->>R: 实体解析（纯 SQL 算法）→ 事务写入 facts/entities/links（幂等：content_hash）
  end
  W->>R: 提交 consolidation 任务（水位标记）+ webhook 待发行（同事务）
  W->>R: consolidation 执行 → 生成观察/心智模型 → 刷新知识页
  W-->>P: 完成事件（token 用量，经引擎完成钩子事件化上报；at-least-once，平台按事件 ID 幂等去重）
  W-->>C: webhook 通知（at-least-once）；C 也可轮询 operation 状态
~~~

### 5.2 读路径（recall，全程只读）

请求路径与写路径相同的前 5 步（APIG→L2→ELB→引擎实例），然后：查询 embedding（TEI）→ **四路并行检索**（语义 HNSW / 全文 BM25 / 图 memory_links / 时间字段，全部 = 同一 schema 的表 + `WHERE bank_id`）→ RRF 融合 → rerank（TEI，可按 bank 配置关闭）→ 返回。**全程零持久写**——这就是读路径可以无限横向扩展、任意实例可服务的原因。

读写一致性（重要决策）：**Day-1 全部读主库，不配只读副本**。原因：引擎一旦配置了副本地址，recall 整个检索固定走副本（`memory_engine.py:8390`），而副本是异步复制——"retain 完立即 recall"会查不到刚写的记忆。副本只用于分析/导出负载。

### 5.3 后台闭环

consolidation 完成后按配置触发知识页（mental model）刷新；刷新也可由维护循环的 cron 扫描补发（防漏）。两条路径都有去重（同 bank/同模型只留一个 pending）。webhook 待发行在投递失败时按退避重试，超限进死信（平台告警 + 重放工具）。

---

## 6. 多租户隔离

### 6.1 四层防线

~~~mermaid
flowchart LR
  REQ["请求"]:::external --> L1["第 1 层·网络<br/>APIG 唯一公网入口<br/>引擎端口仅收 ELB 流量<br/>（断言由平台鉴权服务生成注入；<br/>转发链路对客户端自带的同名 header 覆盖清除）"]:::platform
  L1 --> L2["第 2 层·认证与路由<br/>平台验 API key（哈希比对）<br/>签名断言（HMAC，≤60s）<br/>bank ACL 在断言与 validator 双查"]:::platform
  L2 --> L3["第 3 层·Schema 隔离（租户）<br/>一租户一 schema<br/>SQL 全限定表名 + 运行时守卫<br/>引擎集中强制，无旁路"]:::hs
  L3 --> L4["第 4 层·Bank 隔离（业务区）<br/>每查询 WHERE bank_id<br/>复合外键结构性防护<br/>validator bank ACL"]:::hs
  classDef hs fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

强隔离租户（合规/大客户）：**专属 Cell**——独立引擎部署 + 独立 PG + 独立 OBS 桶，物理隔离（`tenants.plan='dedicated'`）。中间态强化：每 Cell 独立 DB 账号 + OBS 桶凭据，把"单凭据可见全部 schema"收敛为"单凭据可见单 Cell"。

### 6.2 租户生命周期

```text
开通：控制器在平台库建档 → 控制器（平台层 ECS 上）以 admin CLI 直连引擎库跑迁移（CREATE SCHEMA + 22 表
      + 向量/全文索引，advisory lock 防并发）→ 状态 active → 发 API key
冻结：validator 全拒 + list_tenants 过滤（worker 停止领取该 schema）
      —— 已在执行的任务会跑完（无租约中断，需向客户声明排空窗口）
删除：先撤销 API key → 逐 bank 调引擎删除 API → drop schema + OBS 前缀
      清理（带重试对账）→ 账本置 deleted（留合规期）
迁移（跨 Cell）：export-bank / import-bank 搬数据 → 注册表改指向 → 旧侧排空
```

---

## 7. 横向扩展

### 7.1 引擎怎么扩（两种角色各自的机制）

| | 请求角色（hindsight-api） | 任务角色（hindsight-worker） |
|---|---|---|
| 加一台实例时发生什么 | AS 按启动模板建 ECS → 容器就绪（镜像无模型权重，秒级）→ 健康检查过 → **自动注册进 ELB** → 立即接流量（缓存 miss 自举，无预热） | AS 建实例 → poller 启动轮询 → **没有任何人需要知道它来了**——下一次 `SKIP LOCKED` 领取自然分到它 |
| 扩容触发指标 | 引擎内请求并发占用率 >70%（**该指标当前引擎不导出，需引擎补丁暴露，或采集器直查任务表 SQL**；不用 CPU——LLM 等待期 CPU 低会漏报） | "有活 bank 数"（采集器查 public 例程聚合；不是总队列深度） |
| 有效并行度 | lane 并发 × 实例数，上限 = 连接预算 | slots × 实例数；**同 bank 串行化谓词 = 单 bank 不因加实例变快**，并行度来自 bank/文档维度 |
| 减实例 | ELB 摘除排空，零损失 | 优雅退出释放自己的行；异常死亡由回收控制器兜底 |

**启动模板的三个必要配置**（纯 ECS 部署的机制细节，两角色各自一份模板）：
① **user-data（cloud-init）**：安装容器运行时，按角色启动参数拉起容器——两角色同一镜像，请求角色起 `hindsight-api`，任务角色起 `hindsight-worker`，并把 **worker_id 用 ECS 实例 ID 写入环境变量**（从实例元数据服务获取，替代默认的 hostname）；
② **关机信号链**：systemd/docker 的停止超时设 ≥35 秒，保证 AS 缩容的关机信号能传到容器、完成引擎的 30 秒排空（不配置则缩容等于异常死亡，走回收控制器兜底）；
③ **健康探针选型**：ELB 对请求角色用 readiness（`/health`，8888）；任务角色如需探针用 8889 的 `/health/live`（不查库——避免数据库故障时把 worker 误杀）。注意 `/health` 是查库的：引擎库故障期间请求角色会被 ELB 全量摘除（503 由 ELB 返回，客户端 SDK 重试仍然适用，§8-F4）。

### 7.2 存储怎么扩（两个方向）

**方向一：Cell 内扩容（纵向 + 读扩展）**——升配 RDS 规格（连接数/IO/内存）；加只读副本（只服务分析负载，见 §5.2 的一致性约束）。适用于"这个 Cell 的租户变多了但还没到分片阈值"。

**方向二：加 Cell（水平分片）**——触发阈值不只看活跃 bank 数，还要看**向量索引总数（活跃 bank × 3 + 全局，§3.2）**与 schema 数（万级索引带来内存碎片与清理压力）；开新 Cell 后新租户放入，存量租户用迁移流程搬。Cell 内缓解参数：索引按行数阈值惰性创建。**Cell 是本架构的分片单位**：每个 Cell 有独立的故障域、版本、embedding 维度、连接预算。这就是方案 B 的扩展模型（§2）。另注意**同 bank 串行化只作用于 retain/consolidation 类任务（recall 不串行）**——单一大 bank 是任务角色的扩展上限，疏导手段是把大 bank 拆成多个。

### 7.3 扩展的硬约束：连接预算（算不准就互相击穿）

```text
两条预算线（分开算，任何一条不满足都会翻车）：
① RDS 侧（服务端连接数）：
   PgBouncer 后端池 (default_pool_size × 2 台)
   + 直连（并发迁移进程 ≤4 + 索引维护 ≤3 + 控制器 ≤2）
   ≤ RDS max_connections × 0.8        例：80×2 + 9 = 169 → RDS 按 ≥256 规划
② PgBouncer 客户端侧（引擎连代理的连接数）：
   请求角色实例数 × 每实例引擎池上限（引擎默认 100，必须显式调低，如 20）
   + 任务角色实例数 × 每实例连接（约 10-15）
   ≤ PgBouncer max_client_conn（默认仅 100，必须调大）
推论：两个角色的 AS max 实例数由 ①② 反推；容器内多进程时再乘进程数
（只读副本与平台库各有独立预算，公式同构）
另两个吞吐注意：每次取连接的会话参数重放在代理模式下是额外服务端事务
（高频时有开销）；代理的 query_timeout 须 ≥ 引擎语句超时（600 秒）
```

---

## 8. 可靠性（故障模式总表）

| # | 故障 | 影响 | 恢复 | RTO |
|---|---|---|---|---|
| F1 | 请求角色实例死 | 无 | ELB 摘除 + AS 补 | 秒级 |
| F2 | 任务角色实例死 | 其 processing 行钉住相关 bank | AS 钩子/巡检（60-120s）→ 控制器跨 schema 重置行 | ≤5min |
| F3 | 任务角色 hang（活着但卡死） | 同上但更隐蔽 | 卡死阈值检测（retain>1.5h 等）→ 同 F2 | 巡检周期 |
| F4 | 引擎库主库故障 | Cell 读写中断 | RDS 自动主备切换；切换期请求失败由 SDK 重试 + 任务级重试兜底（引擎重连窗口仅 ~10s，桥不过切换） | <2min |
| F5 | PgBouncer 实例死 | 部分连接拒 | 双实例互备 | 秒级 |
| F6 | 平台库故障 | ≤缓存 TTL(5min) 内已缓存租户可服务；超 TTL 后认证失败（拒绝，不放行） | RDS HA | <2min |
| F7 | TEI 池故障 | recall 不可用；retain 退避延期 | 重建 | 分钟级 |
| F8 | LLM 限流/凭据失效 | 变慢/持续失败 | 退避重试 / 告警人工 | 随因 |
| F9 | OBS 故障 | 文件上传失败即拒绝（不留半态） | — | 随云 |
| F10 | 误发版本 | 新实例异常 | 镜像回滚随时可做；schema 回退仅限可逆版本（数据迁移的 downgrade 多为故意空操作）——发布用 expand-contract 契约 | 按流程 |
| F11 | 平台控制器故障 | 开通/回收停摆 | 双实例 + 操作幂等可重跑 | 分钟级 |
| F12 | 指标采集器故障 | 伸缩失明且无人发现 | 双活 + **指标断流告警**（监控的监控） | 分钟级 |
| F13 | 开通流程卡死 | 悬挂 | lease 超时幂等重跑 | 巡检周期 |
| F14 | RDS 磁盘满 | 写全失败 | CES 磁盘告警（须配置）→ 扩容 | — |
| F15 | webhook 持续失败 | 超限进死信仅引擎日志 | 平台死信告警 + 重放工具 | — |

数据保障：引擎库/平台库各自 HA + PITR；**三个对账任务（平台控制器定时发起）**（注册表×引擎 schema、OBS 孤儿、bank 镜像）；队列 at-least-once + content_hash 幂等；**at-least-once 的代价明示**：重复 LLM 调用的费用由平台吸收不转嫁租户（计量按事件幂等去重）。

---

## 9. 引擎可替换（保留 V2 结论，摘要）

- **替换单元 = Cell**。新引擎（比 Hindsight 更强的组件）以新 Cell 类型接入，平台产品 API 只依赖 **EngineProvider 接口**（retain/recall/reflect/操作状态/取消/删除/擦除/consolidation 触发/导出/健康）+ **引擎接入合同**（租户解析/配额钩子/计量事件/metrics——参考实现见扩展骨架文档）。
- **替换协议**：从 OBS 原件+账本重放（含删除，防复活）→ 量化门槛评测 → 影子期（active 单写 + shadow 镜像，计量不双记）→ CAS 切绑定 → 反向影子追平后才可回滚。
- Cell 内组件替换：LLM/embedding/rerank 换 env 即可；**embedding 维度冻结**（pgvector 列宽）——换维度必须开新 Cell 重放。

## 10. 华为云产品假设（上线前逐项 POC）

关键项摘要（完整清单与 POC 要点见 V2 评审版 git 历史 / 扩展骨架文档）：
**H1a RDS PG 支持 pgvector ≥0.5.0（成败点**，租户开通第一步就建 HNSW 索引；不成立则自建 PG on ECS）；H1b pg_trgm（软依赖可降级）；H1c PG15 public 权限；H2 RDS HA/副本/PITR 切换演练；H3 连接数规格；H4 APIG 自定义认证机制与负载通道形态；H5/H6 AS 与 CES；**H7 OBS S3 兼容必须用引擎实测（obstore，非 boto）**：SigV4 作用域/寻址风格/预签名 URL/批量删除；H9 AOM OTLP；H12 RDS 内置代理对比。

## 11. 实施阶段

A 单 Cell 拆分（纯配置即可——含关闭请求角色的内置任务处理（`HINDSIGHT_API_WORKER_ENABLED=false`）与维护循环（间隔=0）；少量引擎侧改进：伸缩指标导出/跨 schema 回收 CLI/drop-schema 工具）→ B 平台多租户（注册表/扩展/配额/开通）→ C 弹性可观测（采集器/AS/告警）→ D 产品模式与替换就绪（账本/绑定/影子演练）→ E 多 Cell 规模验证。每阶段验收门槛见 V2 评审版；新增：**阶段 A 增"杀实例演练"三项（§4.3 表全过）**。

---

## 关联文档

- [`serverless-multi-tenant.md`](serverless-multi-tenant.md)：V1——引擎源码事实与缝隙的完整论证（file:line 级）
- [`platform-reference-extension.md`](platform-reference-extension.md)：平台参考扩展代码骨架（租户解析/配额/计量的实现）
- V2.1 评审版（git 历史 `e27e38d`）：五维评审全记录、H1-H12 完整 POC 要点、F 表原始版
