# V6：共享 Cognee 集群如何实现 Serverless

**默认路线修订为：多个租户共享计算池，数据按 Space 隔离；计算与持久存储分离；查询按请求扩缩容，摄取按持久作业扩缩容。一个客户不需要常驻一整套 Cognee。** V5 的固定 owner 是嵌入式图文件的访问约束，不应成为云上产品的默认计算分配方式。

本文基于 Cognee `663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`；Hindsight 对照基于 `12f2d54f643baddacb98cd547c89b1a50c5c3dcc`。源码事实、设计建议、待测条件分别说明。未部署集群，未执行故障注入或性能测试；这里给出可落地的设计和验证门槛，不声称现有项目已经实现全部能力。

颜色：🟦 Cognee 已有开源组件；🟩 其他开源组件；🟧 需开发的平台逻辑/适配；🟪 商业组件；灰色为客户或未限定供应商的服务。颜色表示来源，不表示已集成、免费或通过验收。

## 1. 先解开“一套 Cognee”的歧义

| 对象 | 是否每租户一个 | 实际分配依据 |
|---|---|---|
| 企业账户、授权、账单 | 是，逻辑独立 | Tenant；用户组管理 Space 权限 |
| Cognee Python 进程/容器 | **不需要** | 活跃请求与作业数、CPU/内存、模型与连接预算 |
| 数据库客户端 engine/driver | 当前常按活跃 Dataset 缓存 | 进程内连接对象；不是启动新的数据库服务器 |
| 图 database、向量 schema、对象目录 | 通常每 Dataset/版本一份 | Space 的索引绑定与权限边界；一个租户可有多个 Space |
| PostgreSQL / 图数据库服务 | 可由多租户共享 | Cell 的容量、隔离等级、地域和故障边界 |
| 专属 Cell / 独立存储集群 | 只给有需要的客户 | 强隔离、容量或定制运行配置套餐 |

注册 1,000 个租户不意味着启动 1,000 个 Cognee 容器。若当前只有少量空间活跃，就只需要覆盖这部分并发的计算容量；实际副本数要通过负载测试确定，不能从租户数量直接推算。

这里的 **Cell 是一组兼容运行配置和存储端点的资源池**，可容纳多个租户和多台机器。不能每个请求修改同一 Python 进程的全局环境来切换客户配置；需要不同全局框架/地域/信任等级时才建立另一个池。不要把任意客户偏好都变成新 Cell，否则又会产生资源碎片。

“Serverless”还应分清客户不用管理服务器、计算自动扩缩容、计算空闲归零三个承诺。前两者可以保留热副本；数据库、控制器和持久数据通常仍有成本。自己运行 Knative/KEDA 时，平台团队仍管理底层集群。

## 2. 推荐拓扑：共享计算池，独立持久数据面

~~~mermaid
flowchart TB
  T["多个企业 / 用户组 / Agent<br/>共用产品 API 与 SDK"]:::external --> E["共享入口与身份认证"]:::oss
  E --> A["平台 API<br/>Tenant / Group / Space 授权、配额"]:::platform
  A --> D[("平台 PostgreSQL<br/>Space绑定、权限、Job、attempt、发布版本")]:::oss
  A --> QG["查询路由与准入<br/>解析固定 binding / profile"]:::platform
  A --> J["持久作业提交与公平调度<br/>原件引用、幂等键、租户预算"]:::platform
  J --> D
  subgraph C["共享 Cell：多个租户；跨多个计算节点"]
    Q["查询容器池：0..N 或保留热副本<br/>ScopeGuard / 会话协调 / 输出检查"]:::platform
    CQ["Cognee Query Runtime<br/>retriever、图与向量检索、生成、session"]:::cognee
    W["摄取 Job / Worker 池：0..M<br/>领取、临时写所有权、终态校验"]:::platform
    CW["Cognee Ingest Runtime<br/>add、cognify、解析、分块、图抽取、索引"]:::cognee
    Q --> CQ
    W --> CW
    CQ --> G[("远程 Neo4j Enterprise<br/>Dataset databases；图库服务共享")]:::commercial
    CW --> G
    CQ --> P[("PostgreSQL + pgvector<br/>Cognee 元数据、Dataset schemas")]:::oss
    CW --> P
    CQ --> S[("持久 session / history<br/>独立会话库及权限")]:::oss
    CW --> O[("对象存储<br/>原件、版本、重放清单")]:::external
  end
  QG --> Q
  J --> W
  K["Knative KPA / Activator<br/>HTTP 唤醒与查询扩缩容"]:::oss -.-> Q
  KD["KEDA / Kubernetes Jobs<br/>基于可运行作业数创建计算"]:::oss -.-> W
  KD -.读取指标.-> D
  PC["平台控制器<br/>开通、迁移、修复、配额、版本发布"]:::platform --> D
  PC --> G
  PC --> P
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

**Cognee 并没有被缩成一个小工具。** 每个计算副本中仍加载它完整的记忆处理能力；图中将其分成 Query/Ingest 两种执行角色，以便分别伸缩，不代表已经将 Cognee 内部各任务拆成独立微服务。平台承担客户身份、资源治理与可靠执行，Cognee 承担资料转记忆、检索和生成。

主路线选择远程图库，是为了解除本地图文件对计算位置的绑定。当前 Cognee 的 Neo4j Dataset 隔离需要多 database 能力，应采用具备该能力的 Enterprise/Aura 方案并核实服务规格；**不能把 Neo4j Community 直接替换到图中并声称隔离等价**。全开源嵌入式路线见第 8 节，Hindsight 替换见第 9 节。

图中的 PostgreSQL 可以物理复用服务，但必须明确库、账号与连接预算。当前 `pgvector_shared` handler 将向量 schema 放在 Cognee 关系库所在的 database，并复用其凭据；画成逻辑分层不代表已支持任意独立 vector endpoint。若需独立端点或细粒度账号，需修改 handler/凭据解析。详见[存储证据](../evidence/13-serverless-storage.md)。

多租户隔离仍落在五处：入口验证 tenant membership；Group 对 Space 授权；Provider 接受可信 binding 而不是客户传来的数据库地址；存储按 Dataset 分区并配置访问凭据；session、trace、下载和答案输出再次校验范围。共享进程意味着共享受信计算域，不能代替数据库最小权限或专属 Cell 的强隔离等级。

## 3. 为什么能集群化：已有基础和缺口

| 要求 | 当前源码事实 | 集群化必须补齐什么 |
|---|---|---|
| 不同租户复用进程 | ContextVar 保存 dataset、graph/vector、session user 等；scope 按 Dataset owner 解析存储 | 外层 ScopeGuard 完整绑定/恢复上下文，包括异常、取消和初始化失败；禁止请求修改全局 settings/env |
| 多副本读取同一份数据 | Neo4j adapter 使用网络 driver 与指定 database；PGVector 使用远程 PG 的 schema | binding 固定，读准入与版本可见性受控；模型/索引版本一致 |
| 多 Worker 修改数据 | 当前 Dataset 锁只在单进程内有效 | 跨进程 Space 写协调、幂等、故障所有权和删除屏障 |
| 进程销毁后任务还在 | 当前一部分后台执行只是事件循环 create_task | 原件、完整 Job manifest 和 attempt 持久化；工作进程等待真实终态 |
| 查询可换副本 | 数据可外置，但 recall 会保存 session/history，可能触发反馈 | 会话顺序、重试幂等和持久反馈；不能靠关闭全部 CACHING 来宣称无状态 |
| 冷启动安全 | API lifespan 含迁移、资源创建与过期恢复；adapter 初始化也可能 DDL | 由 provisioner/recovery controller 协调；查询实例冷启动仅做必要初始化和版本检查 |

事实定位：[ContextVar 与退出行为](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L341-L400)、[Neo4j 网络连接与初始化](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/neo4j_driver/adapter.py#L192-L226)、[进程内 Dataset 锁](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/locks/dataset_lock.py#L18-L34)、[内存后台任务](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/layers/pipeline_execution_mode.py#L104-L127)。

理论依据是**可替换计算副本 + 外部持久状态 + 明确的并发与恢复协议**。任意兼容 Worker 获得同一个已授权 binding、相同输入和配置后，就能访问同一数据；不需要依靠上一个容器的本地内存。数据库支持网络访问只是前提，不能证明整个 pipeline 已具备事务或安全重试。

要区分三层扩展：①增加 Cognee 副本，提高多请求/多 Space 总吞吐；②将 Space 放到多个 Cell/存储资源组，扩大总容量；③数据库本身的复制、HA、分片，由对应存储产品和适配决定。**增加 Cognee Pod 不会自动把一个大图拆成分布式图，也不保证同一 Space 多 Writer 更快。**

## 4. 临时写所有权：同 Space 串行，不等于一 Space 一台机器

建议首版按 Space 串行执行会修改共享索引的操作；不同 Space 并行。授权域保持 V5 设计：相同读取权限的资料进入同一 Space。Space 写所有权只在修改期间存在，Worker 完成后可以服务其他客户。

~~~mermaid
flowchart LR
  A["租户甲：Space A 的多个作业"]:::platform --> J["共享持久作业队列<br/>按租户公平分配和 Space 协调"]:::platform
  B["租户甲：Space B 的作业"]:::platform --> J
  C["租户乙：Space C 的作业"]:::platform --> J
  J --> W1["Worker 1<br/>当前处理 A；完成后可处理 C"]:::cognee
  J --> W2["Worker 2<br/>当前处理 B"]:::cognee
  J --> W3["Worker 3<br/>当前处理 C；空闲后退出"]:::cognee
  W1 --> SA[("A 的远程存储绑定")]:::external
  W2 --> SB[("B 的远程存储绑定")]:::external
  W3 --> SC[("C 的远程存储绑定")]:::external
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

一个 Worker 可依次处理多个租户；完成并发上下文验收后也可同时处理少量不同 Space。首版每 Worker 同时一个摄取 Job 更容易控制资源和故障范围，这也不等于每客户常驻一个 Worker。

持久 Job 至少记录 `tenant_id / actor_ref / space_id / binding_revision / source_revision / provider_version / model_profile / idempotency_key`。attempt 另记录执行者、心跳、状态和发布 epoch。Worker 领取后重新校验授权、取消/删除状态、固定 binding，不能只相信几小时前的入队权限。

**租约过期不是安全接管的充分条件。** 旧 Worker 可能恢复网络后继续向图库写入。两种可选择的正确性路径：

1. **就地更新**：修改前关闭该 Space 的读准入并排空读者；所有修改路径受协调，包括更新、删除、improve 和会改图的反馈。任务结果未知时阻塞后续写，确认旧计算及远程在途写已停止后对账/修复；修复成功才开放查询。可用性代价明确，但首版协议较小。
2. **隔离版本构建**：每个 attempt 使用独立原生 Dataset 及成套图/向量资源，重建 Space 的完整有效来源集合，完整校验后以绑定版本 CAS 原子切换可见指针。不能只向空版本写入新增资料后替换整个 Space；此路线通常有全量重建 O(N) 成本。失败 attempt 不污染已发布版本。要覆盖 Cognee 元数据及其他共享副作用，不能仅给图文件加版本号就宣称所有写入隔离；旧版本还需等待读者结束才能回收。

若选择下游 fencing，每一个实际写入点必须验证 fencing token；在 PostgreSQL 保存一个 epoch 却不让 Neo4j/向量写路径执行校验，不构成 fencing。远程数据库各自成功也不是跨图库、向量库和对象存储的原子提交。以上沿用[V5 的发布、删除与修复合同](../v5-product-saas/02-data-flows.md)。

## 5. Serverless 怎样工作：HTTP 和长任务分开伸缩

| 执行角色 | 建议方式 | 从零唤醒者 | 归零条件及边界 |
|---|---|---|---|
| 产品 API / Query | Knative Serving 的 KPA，或等价托管容器服务 | 共享入口/Activator 接收 HTTP 并触发副本启动 | 无在途请求和未持久化副作用；低时延套餐保留热副本；HTTP 冷启动有超时预算 |
| Ingest / maintenance | KEDA ScaledJob + 容器 Job；短且可安全中断的任务也可用 Worker Deployment | 外部 scaler 读取可运行任务指标 | Job 等待真实终态后退出；不能在内存启动 background task 后立刻退出 |
| Provision / recovery | 受控控制器，按事件或定期执行 | 调度/事件服务 | 即使工作容器归零，仍必须存在启动它的触发设施和持久状态 |
| 数据库 / 原件 / 索引 | 独立持久服务，可选相应托管方案 | 不跟随 Cognee 工作副本销毁 | 存储费用和必要服务容量仍存在；是否可暂停要单独验证数据库产品 |

按 Cell/运行 profile 创建少量服务和伸缩对象，**不为每个租户创建一个 Knative Service 或一个 KEDA ScaledJob**。租户级配额在共享调度器执行。Query 与 Ingest 的并发、超时和成本不同，不宜共用一个副本数指标。

[Knative 请求路径](https://knative.dev/docs/serving/request-flow/)解释共享 Activator 如何在容量不足时等待应用 Pod；它不是持久业务队列。[KPA 的缩零能力](https://knative.dev/docs/serving/autoscaling/scale-to-zero/)解决 HTTP 计算空闲回收。对接受后必须完成的长摄取请求，仍要先落盘再返回 `202 + operation_id`。

### 摄取从零启动的完整路径

~~~mermaid
sequenceDiagram
  actor Client as 客户
  participant API as 平台 API
  participant DB as 持久目录与作业库
  participant Scale as KEDA / Job 控制器
  participant Worker as 临时 Worker + Cognee
  participant Store as 图 / 向量 / 对象存储
  Client->>API: 上传完成后提交摄取请求和幂等键
  API->>Store: 校验原件版本已持久保存
  API->>DB: 事务保存来源引用与 Job
  API-->>Client: 202 + operation_id
  Note over Scale,Worker: 此时可没有任何摄取 Worker
  Scale->>DB: 查询满足执行条件的工作量
  Scale->>Worker: 创建 Job 容器
  Worker->>DB: 原子 claim；校验身份、binding、Space 写所有权
  Worker->>Store: await Cognee pipeline；保留任务心跳
  Store-->>Worker: 各阶段结果
  Worker->>Worker: 检查真实终态与结果完整性
  Worker->>DB: 按发布协议记录结果并完成 Job
  Worker-->>Scale: 完成并退出
  Client->>API: 查询 operation 状态
  API->>DB: 读取持久终态
  API-->>Client: 返回完成、失败或修复状态
~~~

如果另加 broker，Job 提交与消息发布之间需 Outbox/等价一致性协议；首版可直接从 SQL 领取，少引入一个消息系统。[KEDA PostgreSQL scaler](https://keda.sh/docs/2.20/scalers/postgresql/)可查询单个数值指标，但只负责伸缩，不负责 claim、公平调度、授权或幂等。SQL 领取与 Space 排他状态需原子协调，避免两个 Job 同时认领同一写域。

**伸缩指标应是可运行工作量，不是所有排队记录数。** 同一 Space 排队 100 个串行更新，不应该启动 100 个 Writer 等锁。应排除未到执行时间、等待其他写者、被取消及无预算的任务；还需考虑已经启动但尚未完成 claim 的 Job，防止反复过量创建。单个长 Job 正在运行时，不应因“待领取数变零”便被杀死。

指标的口径必须与所选 `scalingStrategy`、target 及运行中 Job 的计算方式配套，不能把一个 SQL count 直接接默认参数就交付。特别要避免指标已排除在途工作、伸缩计算又重复扣减这部分容量。验收应包含：一个长 Job 正在运行、另一个独立 Space 出现可运行 Job 时，仍会增加所需计算；尚未 claim 的启动中容器不会导致持续重复扩容。

[KEDA ScaledJob](https://keda.sh/docs/2.20/concepts/scaling-jobs/)适合处理后退出的任务；[官方长执行说明](https://keda.sh/docs/2.20/concepts/scaling-deployments/#long-running-executions)也指出 Deployment 缩容会打断长任务。采用 Job 仍可能遇到节点故障、驱逐和平台超时，因此不能省略恢复协议。

### 查询路径与安全缩容

查询先授权 → 解析 binding/已发布版本 → 取得读准入 → 任一兼容查询副本检索和生成 → 持久保存承诺的会话状态/反馈操作 → 输出检查 → 完成响应。跨副本的同 session 请求需要明确 turn 顺序；流式输出、撤权和删除要沿用 V5 的在途读者合同。不要在回答已返回后依赖未跟踪内存任务完成用户承诺。

缩容时停止接新请求和领取任务，保持在途所有权，等待完成；若必须取消，等待子任务退出并记录可恢复状态。超时强杀按故障恢复处理，不能先释放 Space 锁再让旧任务继续运行。还需确认远程在途数据库写是否结束，杀掉 Python 进程本身不是对所有远程副作用的证明。

当前 Cognee 有局部 drain 基础，但低层 pipeline 私有后台集合没有完全纳入统一 drain；错误路径还可能 yield `PipelineRunErrored` 而不抛异常。因此 Consumer 必须检查 pipeline 终态，不能将 `await` 正常返回视为成功。[执行证据及代码定位](../evidence/13-serverless-execution.md)。

## 6. 资源是否友好：关注四个预算

**计算预算。** Query 按同时在途请求和实测安全并发定副本数；Ingest 按可并行 Space、作业 CPU/内存及模型预算伸缩。外部模型配额已满时，再加 Worker 可能只会增加限流、等待与费用。每租户限制同时作业数、查询数、队列积压和模型消耗；用公平调度避免大客户抢占整个共享池。

**连接预算。** 当前 PGVector 每活跃 Dataset engine 有自己的连接池，访问控制路径默认 `pool_size=2`、`max_overflow=20`。潜在向量连接上界约为 `Σ(各副本活跃 engine 数 × 每 engine 连接上限)`，另加元数据、session、管理连接；不是默认立即创建所有这些连接。LRU 默认数量也不是全局硬上限，活跃项 pin 时可超出。伸缩器的最大副本数必须受数据库连接预算约束；冷启动批量建连接要限速。可研究按端点/凭据共享 driver，但不能把这项优化描述为现有实现。[源码依据](../evidence/13-serverless-storage.md#4-资源随namespace增长不必随租户启动进程但也不是零成本)。

**命名空间与数据预算。** Dataset database/schema/索引数量大致随 Space×保留版本增长，跟 Pod 数不是同一个量。Neo4j 多 database 的维护、内存与备份成本需要按目标规模实测；上万 Space 是否合适没有本次证据支持。若成本不可接受，应评估存储适配或引擎替换。把所有租户混到一张图再加 tenant 标签，是另一项涉及遍历、摘要、删除、授权的设计，不能当作无损配置优化。

**冷启动与平台底座预算。** 查询容器需要 import、初始化连接和可能的模型加载；大模型不一定适合随每个 Job 冷启动。可固定外部模型服务并缓存兼容客户端，低时延等级保留最小热容量。Knative/KEDA 的控制面、入口和持久库依然需要运行；Pod 缩零不等于 Kubernetes 节点缩零，更不等于整个系统零账单。选择托管容器平台可把节点运维交给供应商，但仍有存储及平台费用。

## 7. 当前仓库不是“完全没有 Serverless”，但模板不等于完整方案

已有 [Modal ASGI 部署模板](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/distributed/deploy/modal_app.py#L22-L58)，配置挂载卷、请求超时、空闲回收和容器并发；[Fly 配置](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/distributed/deploy/fly.toml#L23-L29)也有自动启停与最小实例数 0。这证明项目考虑了按需托管，不能由此推导多副本写、可靠作业和安全接管已经完成。

Modal 模板把数据目录指向挂载卷，安装的是未固定版本的发布包；本轮未运行模板，未核实当前 SDK 参数兼容性。[Modal Volumes 文档](https://modal.com/docs/guide/volumes)描述 commit/reload 可见性，并要求避免多个容器同时修改同一文件。共享挂载不能替代嵌入式数据库的并发协议；仅把 SQLite 换 PostgreSQL、LanceDB 换 PGVector，也没有自动把 Ladybug 图文件变成远程数据库。

因此可以复用这些部署方向，但产品交付仍需实现第 3–5 节的运行合同。参见[模板与官方伸缩机制证据](../evidence/13-deployment-and-autoscaling.md)。

## 8. 若坚持 Ladybug：共享 owner 集群与按需启停

这条路线仍可形成多节点集群：Space 路由到持有其图文件的 owner；**一个 owner 可服务多个租户的多个 Space**；多个 owner 分担总体负载。所有触及该可变图的读写都走同一受控 owner。它与远程数据库路线的区别在于数据访问位置受约束，不是整个平台只能有一台机器。

空闲 Space 的按需启停需新增完整协议：停止准入 → 排空读写 → checkpoint/关闭数据库 → 保存经校验的快照和一致版本清单 → 确认安全停止 → 回收资源；唤醒时唯一领取 → 下载/挂载 → 校验恢复 → 打开 → 注册路由。图与向量、SQL 元数据的版本关系必须可恢复，不能只备份一份图文件。失联旧 owner 要可靠隔离；超时租约和新 owner 成功打开文件都不能证明旧 owner 不再写。

当前 [Ladybug adapter 有 checkpoint 和 S3 上传/下载入口](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/ladybug/adapter.py#L573-L592)，没有因此自动具备完整冻结/唤醒协议。多个临时 Pod 下载同一快照各自修改也不是共享图库。

这一方案可以对客户呈现 Serverless，但冷启动受数据大小、卷接管和恢复耗时影响；单个热 Space 的访问仍受 owner 容量限制。不可变只读快照副本是另一条可研究路线，需要发布、刷新与回收协议。**如果首要目标是计算自由伸缩，优先远程存储；如果首要目标是沿用嵌入式开源栈，接受 owner 路由并单独实现按需生命周期。**

## 9. Hindsight 能否成为更适合此拓扑的插件

可以保留上层产品 API、Tenant/Group/Space、持久来源、operation 合同与配额，将某些 Cell 的 Provider 换成 Hindsight。Hindsight 的核心 PostgreSQL backend 通过 asyncpg pool 服务带 schema 的表，事实、关系、向量和全文集中于 SQL；从存储拓扑看，它减少了单独图库服务和图/向量跨引擎协调，值得作为共享弹性服务候选。[backend 实现](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/db/postgresql.py#L222-L276)、[schema 解析](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/schema.py#L16-L27)。

这不证明其性能一定更高、认证天然满足 SaaS，或将 Worker 缩零就能正确恢复。其 worker 身份/持久 operation 恢复、schema/bank 授权也需适配；引擎语义仍需回归。平台只定义可持久追踪的操作结果，Provider 声明同步执行还是使用原生持久操作，避免两层调度对同一任务重复恢复。

这里的 MemoryProvider/MemoryBackend 指的是 **remember/recall/improve/forget 的引擎能力边界**，不是把图库、向量库简单拼成存储接口。可选外部工作流负责跨步骤任务恢复与编排，不会自动保存 Cognee 内部任意阶段的内存进度。首版 SQL Job 足够时不必引入额外工作流系统；需要跨服务长事务再增加，且保持 Job 与 Provider operation 的稳定映射。替换需重放来源、对照评测和切换绑定，不能直接互读双方私有图/事实表。完整语义边界见[V4 引擎插件设计](../v4-engine-plugins/architecture.md)及[Hindsight 运行证据](../evidence/11-hindsight-runtime.md)。

## 10. 实施顺序和验收

| 阶段 | 具体交付 | 必须通过的门槛 |
|---|---|---|
| A：远程共享运行池 | 固定 Cell/profile；ScopeGuard；可信 binding；分开 Query/Ingest 入口；资源预开通 | 两租户顺序及并发复用实例无数据/session/凭据串用；异常/取消后上下文干净；新查询实例无需管理级 DDL |
| B：常驻多副本集群 | 持久 Job/attempt；Space 写协调；发布/读屏障；会话顺序；受控恢复 | 不同 Worker 修改不同 Space；同 Space 无并发冲突；任意 Worker 故障不丢 Job、不查询半成品、不使已删除资料复活 |
| C：安全弹性 | 查询和摄取独立伸缩；公平调度；连接/模型预算；drain | 运行中增减副本、节点失联、响应丢失重试；无陈旧写者覆盖，无长期饥饿；终态正确识别 |
| D：空闲归零 | HTTP 唤醒、按事件创建 Job、冷启动无全局修复副作用 | 计算为零时提交请求可启动；测冷启动 p95/p99、成本、连接峰值；另一长任务运行时新实例启动不误修复它 |
| E：规模与替换 | 多 Cell placement；存储容量验证；Hindsight Provider 对照 | 目标 Space/索引规模可运营；切换可回滚；评测满足产品质量与授权合同 |

阶段 B 必须注入“图库成功、向量失败”“提交成功但应答丢失”“旧 Worker 失联后恢复”；阶段 C/D 必须记录重复模型调用成本、其他租户等待时间和修复时长。没有这些数据，不承诺容量数值、冷启动 SLA 或端到端 exactly-once。

本版仅新增分析文档。验证范围是固定 SHA 源码定位、官方基础设施文档、Markdown 链接和 Mermaid 渲染；没有把建议实施的系统作为现有功能。专项阅读边界见[运行上下文](../evidence/13-serverless-runtime.md)、[持久存储](../evidence/13-serverless-storage.md)、[执行生命周期](../evidence/13-serverless-execution.md)、[部署与伸缩](../evidence/13-deployment-and-autoscaling.md)。
