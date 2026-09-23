# Cognee 多租户与集群架构：组件、数据流、执行上下文与存储隔离

> 演进架构以 [V4：Cognee / Hindsight 插件边界](../v4-engine-plugins/architecture.md) 为准：整引擎插件与内部组件替换分开，明确 MemoryProvider、业务 Workflow 与原生 Worker 的职责。本文保留 Cognee 当前实现及集群风险证据。

> 后续细化：参见 [V3 彩色组件化架构](v3-components.md)，区分 Cognee 复用、其他开源组件、平台自建边界和商业选项，并明确接口契约、数据归属与逐组件替换方式。

分析日期：2026-09-23。代码基线：663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e，版本 1.6.0。

本文针对“当前有哪些组件、数据如何流转、在哪里做集群和多租户、上下层如何配合”重新组织分析。**建议以现有 Dataset 隔离和存储 handler 为基础，保留 Cognee 作为计算运行时，补租户控制面和分布式执行层。** 下面明确区分现有实现与目标设计；目标图中的 Worker、资源控制器和请求级租户上下文尚未在本次分析中实现。

研究沿用 repo-analyzer Skill??????????，结合官方文档和输入、查询、授权、存储、执行链的定向源码审阅。没有修改业务代码，没有运行云集群、调用付费模型或完成性能验收。旧版报告保留作故障与恢复专题参考；理解整体架构优先阅读本篇。

## 1. 官网多用户模型应当如何理解

官方说明明确区分：Tenant 组织用户及其 Dataset 权限；Dataset 决定图和向量数据路由；关系数据库不随 Dataset 拆开。[官方 Multi-user overview](https://docs.cognee.ai/core-concepts/multi-user-mode/multi-user-mode-overview)

对应当前代码，有五个不同概念：

| 概念 | 当前作用 | 商业化映射 |
|---|---|---|
| User / actor | 谁发起这次操作 | 登录用户、服务账号、受控代理身份 |
| Tenant | 组织成员与权限；限定当前可见的数据集范围 | 企业、工作空间 |
| Dataset | 拥有 owner_id、tenant_id，是 ACL 对象，也是图/向量路由单位 | 知识库、部门资料库、项目记忆空间 |
| DatasetDatabase | 以 dataset_id 为主键，记录图和向量的连接配置 | 知识库资源目录的现有基础 |
| Handler / Adapter | 前者创建和解析存储位置，后者实际执行数据操作 | 资源开通插件、运行时数据访问插件 |

源码：[Dataset](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/data/models/Dataset.py#L12)、[DatasetDatabase](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/models/DatasetDatabase.py#L11)、[权限集合计算](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/permissions/methods/get_all_user_permission_datasets.py#L21)。

因此，需要把官网的“每 Dataset 一个后端”理解成**独立的存储路由与命名空间**，不能一概解释为独占机器或数据库实例。默认后端使用独立文件；Neo4j handler 使用独立 database；pgvector_shared 使用同一 PostgreSQL database 内的独立 schema。后两者可让多个 Dataset 共享数据库服务资源。[官方 Handler 用法](https://docs.cognee.ai/core-concepts/multi-user-mode/dataset-database-handlers/dataset-database-handlers-how-to-use-them)、[PGVectorShared 实现](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/pgvector/PGVectorSharedDatasetDatabaseHandler.py#L39)

两个文档简化点需要用当前代码修正：

- 关闭 ENABLE_BACKEND_ACCESS_CONTROL 仍可通过 REQUIRE_AUTHENTICATION=true 要求认证；认证成功不代表共享后端重新获得 Dataset 存储隔离。
- 当前 checkout 在隔离开关未设置或为 true 时，会校验 handler/provider；不兼容会报错，不是自动回退到共享后端。关闭后，上层仍可能校验 Dataset 参数和 ACL，但不能依靠它实现所有检索路径的数据库隔离。

依据：[存储模式判定](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L48)、[HTTP 认证策略](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/methods/get_authenticated_user.py#L33)、[共享检索分支](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/search.py#L452)。

## 2. 当前 Cognee 由哪些组件组成

**现有后端主要是模块化的 Python 运行时。** 下表中的 pipeline、retriever、LLMGateway、engine 多数是同一进程内的函数/对象，不能直接按名称理解成已经存在的独立微服务。API、SDK、CLI、MCP 是调用入口；数据库和模型服务可位于远程。

| 组件 | 输入 → 输出 | 当前职责与状态 |
|---|---|---|
| 接入层 | HTTP/SDK/CLI/MCP 请求 → Python API 调用 | API 注册路由；MCP 可直接调用库或通过 API 模式连接后端 |
| Memory API | remember / recall / improve / forget → 底层操作组合 | 区分长期记忆、会话记忆与维护 |
| 身份、权限与目录 | actor、活动租户、Dataset UUID → 授权 Dataset | SQL 中的 User、Tenant、Role、ACL、Dataset |
| Ingestion / Loader | 文本、文件、地址 → 原件位置、抽取文本、Data 元数据 | add 主要完成输入落地与登记 |
| Pipeline / Task runtime | Dataset、Data、Task 链 → 加工结果与运行状态 | 调度分类、分块、抽图、摘要、索引；并发和部分队列是进程内状态 |
| Retrieval runtime | query、SearchType、授权 Dataset → 对象、上下文、答案、证据 | 组织向量检索、图检索、排序和生成 |
| LLMGateway / EmbeddingEngine | prompt 或文本 → 结构化结果、答案或向量 | 模型访问抽象；两者用途不同 |
| DatabaseContextManager | Dataset → 本次执行的数据库/模型配置 | 解析真实 owner 和数据库注册表，设置 ContextVar |
| DatasetDatabaseHandler | Dataset 与配置 → 创建/解析/删除后端资源 | 现有多租户存储路由扩展点 |
| Graph / Vector Adapter | 节点边、向量、查询 → 后端读写 | provider 适配及进程内连接池 |
| Session / History / Feedback | 问答、trace、上下文 → 会话记忆和反馈 | 读取也可能写状态，并触发后续改善 |
| 持久化服务 | 文件、元数据、图、向量、会话 | 生命周期和一致性边界不同，默认含多个本地后端 |

代表源码：[API 注册](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/client.py)、[Pipeline](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/run_tasks.py#L62)、[Retriever 工厂](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/get_search_type_retriever_instance.py#L113)、[LLMGateway](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/llm/LLMGateway.py#L106)、[UnifiedStoreEngine](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/unified/get_unified_engine.py#L65)。

~~~mermaid
flowchart TB
  Clients["SDK / CLI / UI / MCP"] --> Entry["Memory API / 低层 API"]
  subgraph Runtime["当前 Cognee Python 运行时：逻辑模块"]
    Entry --> ACL["身份、Dataset ACL 与目录"]
    ACL --> Ingest["add：Loader 与输入落地"]
    ACL --> Pipe["cognify / improve：Pipeline 与 Tasks"]
    ACL --> Query["recall / search：Retriever"]
    Entry --> Session["会话记忆与反馈"]
    Pipe --> Model["LLM / Embedding 调用层"]
    Query --> Model
    Pipe --> Route["Dataset Context + Handler + Adapter"]
    Query --> Route
    Ingest --> File["文件存储适配"]
  end
  ACL --> SQL[("关系元数据")]
  Pipe --> SQL
  File --> Objects[("原件与抽取文本")]
  Route --> Graph[("图")]
  Route --> Vector[("向量")]
  Session --> Cache[("会话存储")]
  Model --> Provider["模型 API / 推理服务"]
~~~

图中 SQL、Graph、Vector 是逻辑存储职责，既可使用不同服务，也可落在某些共同基础设施上。即便共用 PostgreSQL 服务，也不自动获得跨 adapter 的共同事务。

## 3. 用两个企业解释租户、共享和存储

假设：

- 企业 A：Alice、Bob 两个用户；产品资料 D1、财务资料 D2。
- 企业 B：Carol；客户资料 D3。
- Alice 在 A 中创建 D1、D2，向 Tenant A 授予 D1 的 read，D2 只授权财务角色；Bob 没有财务角色。

现有实现可以表达这个场景：

| 请求 | 权限计算 | 最终访问 |
|---|---|---|
| Alice 查询 D1 | 创建者拥有权限，且当前租户为 A | D1 对应的图 G_D1 和向量 V_D1 |
| Bob 查询 D1 | 继承 Tenant A 的 D1 read | **同一个** G_D1 / V_D1 |
| Bob 查询 D2 | 无 read | 拒绝 |
| Carol 在 B 查询 D3 | B 范围内的授权 | G_D3 / V_D3 |
| Bob 在 A 显式查询 D3 | 不属于当前租户可访问范围 | 拒绝 |

**Bob 的身份决定能不能访问，D1 的存储归属决定连接哪一组数据库。** 被授权用户不会得到一份复制数据库。DatasetDatabase 只有一条 D1 路由；上下文最终解析的是 Dataset.owner_id。[owner 解析与路由](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L238)

加入 Tenant 不会自动拥有其全部 Dataset。必须对 User、Tenant 或 Role 授予 Dataset 权限；现有权限是多来源授权集合，不能把个人较少的授权当作对 Tenant 广泛授权的撤销。[创建者初始授权](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/data/methods/create_authorized_dataset.py#L33)、[授权合并](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/permissions/methods/get_all_user_permission_datasets.py#L21)

企业产品应使用 Dataset UUID 调用共享知识库。现有按名字解析限定当前用户拥有、当前租户范围，同名字符串不能代替企业级共享知识库身份。[名字解析](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/data/methods/get_dataset_ids.py#L23)

活动 Tenant 目前保存在 User.tenant_id；成员验证后 select_tenant 修改该行。多标签页和后台作业需要的是执行期间固定的租户上下文，因此云端应把这个字段保留为默认工作空间，将真正的请求租户显式固定。[select_tenant](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/tenants/methods/select_tenant.py#L32)、[官方租户作用域](https://docs.cognee.ai/core-concepts/multi-user-mode/permissions-system/tenants)

## 4. 写入流程：输入如何变成可检索记忆

下面是普通文档、默认抽图任务链；代码/DLT/其他 extractor 分支不必经过相同 LLM 步骤。

~~~mermaid
flowchart TB
  A["remember：长期资料"] --> B["add：解析用户及 write 权限"]
  B --> C["保存原件或引用已有位置"]
  C --> D["Loader 提取文本，保存文本对象"]
  D --> E[("SQL：Data / Dataset 关系、URI、hash、状态")]
  E --> F["cognify：绑定 Dataset 上下文"]
  F --> G["分类 Document / 切分 DocumentChunk"]
  G --> H["LLM 抽取实体关系与摘要"]
  H --> I["DataPoint 对象图"]
  I --> J["add_data_points：展开、去重、来源关联"]
  J --> K[("Graph：节点、边、来源")]
  K --> L["按可索引字段调用 Embedding"]
  L --> M[("Vector：向量、ID、payload")]
  M --> N["完成状态 / 可选 improve"]
~~~

关键持久化位置：

| 阶段 | 具体保存什么 | 不能误解为什么 |
|---|---|---|
| 原件落地 | 上传内容，或者保留已有文件/S3 位置 | 接受一个 URI 不代表平台已经复制并永久保管原件 |
| 文本落地 | Loader 抽出的文本及其位置 | 不是向量本身 |
| SQL Data | owner、tenant、Dataset 关联、原件/文本 URI、hash 等 | 不是唯一的全部业务内容仓库 |
| 图写入 | 文档/块/摘要/实体/关系等 DataPoint 的节点边及来源 | 不只有实体名称 |
| 向量写入 | 被索引的字段、向量、关联 ID 和 payload | 不只有原始 document chunk；实体名、摘要等也可被索引 |
| 运行记录 | PipelineRun、状态、补偿/来源信息 | 有运行日志不等于具备跨进程作业接管 |

依据：[add 的 Task 组合](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/add/add.py#L263)、[ingest_data 元数据](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/ingestion/ingest_data.py#L484)、[原件位置处理](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/ingestion/save_data_item_to_storage.py#L49)、[默认 cognify Tasks](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/cognify/cognify.py#L581)、[图向量写入](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/storage/add_data_points.py#L317)。

SQL、对象、图、向量在不同阶段提交，当前分离后端没有包裹全部写入的共同 ACID 事务。已有来源追踪和补偿逻辑可复用；云端还要定义“原件已接收”“处理中”“索引可查询”的产品状态，避免返回接收成功却让用户误认为完整索引已经发布。

建议首版明确异步一致性：提交输入后返回 document_id/job_id；状态显示接收、处理、可用或失败。同一 Dataset 的更新、删除、improve 进入统一写入协调；进一步需要平滑重建时再引入索引 generation 和完整版本切换。跨服务提交可使用持久 Job 与 outbox，跨存储失败通过幂等重试/补偿收敛。[AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)

## 5. 查询流程：向量、图和 LLM 如何配合

普通长期记忆查询的实际调用顺序：

1. recall/search 接收 query、SearchType、Dataset UUID 等。
2. authorized_search 计算当前用户在当前租户下有 read 的 Dataset 集合。
3. 对每个 Dataset 建立独立异步查询，绑定该 Dataset 的存储配置。
4. 工厂选择 Retriever；Retriever 调用向量/图 adapter 取得候选和上下文。
5. 需要回答时，LLMGateway 根据上下文生成结果；可能记录会话、历史和反馈。
6. 收集每个 Dataset 的 SearchResultPayload。

源码：[授权入口](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/search.py#L243)、[每 Dataset 上下文](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/search.py#L327)、[并发和结果收集](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/search.py#L500)。

以 GRAPH_COMPLETION 为例：

~~~mermaid
flowchart LR
  Q["query"] --> Emb["按该 Dataset 的模型计算 query embedding"]
  Emb --> V["向量库：块、摘要、实体等集合候选"]
  V --> IDs["候选节点 ID / 距离"]
  IDs --> G["图库：投影或邻域展开"]
  G --> Rank["关系三元组评分与选择"]
  Rank --> C["文本上下文 + 来源"]
  C --> LLM["prompt / 会话上下文 / LLM"]
  LLM --> R["该 Dataset 的回答与证据"]
~~~

图检索并非直接把自然语言问题交给图数据库。当前算法会先找向量候选，再通过节点 ID 关联图结构；参数不同可走不同范围的投影/邻域展开。向量 top_k 小也不必然意味着图端只读取同样数量的节点。[向量候选](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/utils/node_edge_vector_search.py#L166)、[图检索与排序](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/utils/brute_force_triplet_search.py#L49)

| 模式 | 查询算法 | 是否生成答案 |
|---|---|---|
| CHUNKS | 主要检索 DocumentChunk 向量 payload | 不必调用生成 LLM |
| RAG_COMPLETION | 以文本块为主要上下文 | 通常调用 LLM |
| GRAPH_COMPLETION | 向量候选 + 图结构 + 三元组上下文 | 通常调用 LLM |
| only_context | 执行所选 Retriever 的上下文构造 | 跳过最终 completion |
| Session 范围 | 搜索会话记忆，可命中后直接返回 | 不一定查询图或生成 |

算法不使用图遍历，不代表当前公共查询链完全不会初始化或检查图；外层仍存在 graph.is_empty 探测。[CHUNKS](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/chunks_retriever.py#L48)、[only_context](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/session_aware_completion.py#L431)、[外层图检查](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/search.py#L333)

**当前多 Dataset 查询分别生成结果，再返回结果列表；没有统一的跨 Dataset 最终综合回答步骤。** 如果商业产品要求“同时查询产品库和财务库，给一个带来源的答案”，建议新增上层协调器：

~~~text
授权后的 Dataset 集合
  → 每 Dataset 只检索对象/上下文，保留 dataset_id 与来源
  → 限制 fan-out、每库预算和总超时
  → 文本级去重与统一重排
  → 一次最终答案生成
~~~

这是新能力，不应把现有 asyncio.gather 当成已经完成全局融合。不同 Dataset 若使用不同 embedding 模型，原始距离不可直接当同一尺度排序，可用文本级 reranker 统一比较。跨 Dataset 答案融合也不等于跨图数据库自动建立实体关系。

## 6. 会话记忆是另一条持久化链

带 session_id 的 remember 可先通过 SessionManager 写入 QA、trace、guidance 等会话内容，再按选项触发异步 improve；它不等同于每轮立即走完整文档分块、抽图和向量索引。[会话 remember](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/remember/remember.py#L1786)、[官方 Data flows](https://docs.cognee.ai/core-concepts/data-flows)

这影响商业部署：

- 图和向量按 Dataset 隔离，不会自动替会话存储增加 tenant 范围。当前会话主键/定位主要使用 user_id 与 session_id；目标应明确 tenant_id + user_id + session_id，并设计旧数据迁移。
- Query runtime 不能假定完全只读：问答记录、反馈、历史和后续改善都可能写状态。
- 共享部署需要网络可访问的会话后端；不能让每个 API 副本保有互不一致的本地 SQLite 会话。
- 查询导致的图维护应进入统一写入调度；读取服务保留会话能力，不能通过关闭 CACHING 来规避状态设计。
- forget/prune 必须审核 tenant/session/Dataset 范围。现有全局 prune 分支需要在对外 SaaS API 上收窄，不能直接作为普通租户的“清空我的记忆”。

具体默认后端、删除和全局清理证据见 [原报告](v1-analysis.md) 及 [持久化草稿](../evidence/06-module-persistence.md)；本节不把这些问题泛化为已证明所有请求会越权。

## 7. 上层引擎应该如何处理租户

“上层引擎”要拆成执行、检索、模型调用和数据库访问四类职责。

### 7.1 执行引擎：每次请求或 Job 建立固定上下文

建议新增明确的执行上下文，最少携带：

~~~text
actor_id                 已认证操作主体
tenant_id                本次执行的组织范围
dataset_id / dataset_ids  已授权的知识库 UUID
operation                read / write / delete / improve 等
request_id / job_id       追踪、幂等和计量
route_revision           本次固定的资源绑定版本
model_profile_version    LLM 阶段配置与密钥引用
embedding_profile        模型、版本、维度、索引版本
~~~

tenant_id 来自请求选择，但服务端必须校验成员资格；Dataset 必须校验租户归属和操作权限。队列里的字段不是无需校验的信任凭据。Worker 执行前根据已定义的撤权语义重新授权，加载真实 Dataset，而不是信任调用者提供的 owner 或数据库 URL。

当前 ContextVar 可承载异步调用配置，却不是跨进程传输协议；Worker 每次任务都须重建。现有上下文退出只恢复 LLM、Embedding 和当前 Dataset ID，图/向量/文件配置刻意保留在该 task，因此应改成完整作用域恢复或明确不可复用的绑定对象。[上下文退出行为](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L380)

### 7.2 检索引擎：先授权，再解析资源，再执行算法

保留现有授权 Dataset fan-out 机制；增加 tenant 查询配额、Dataset 数量限制、总超时、每库候选预算。需要全局回答时增加上一节的融合层。权限撤销后缓存也要失效；候选、答案和会话缓存必须带足够的租户、数据版本与权限范围，不能仅以 query 文本为键。

### 7.3 模型引擎：共享服务，绑定配置；Embedding 随索引固定

现有 LLMConfig/EmbeddingConfig 支持调用级 ContextVar 覆盖，LLM 又有 extraction、summarization、query 阶段覆盖。这可以复用，但代码主链没有自动按 tenant 加载一个完整的模型配置控制面。[阶段配置](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/llm/pipeline_stage.py#L12)

建议：

- 控制面保存租户模型 profile、版本和 secret 引用；运行时解析后传入，不将 API key 放入普通 Job 消息或日志。
- 不在多租户请求中调用修改全局配置/环境变量的设置方法。当前 save_llm_config 有这种全局修改行为。[全局设置实现](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/settings/save_llm_config.py#L14)
- 同一个模型服务可以被多个租户复用，配额、费用和会话上下文分别管理；有专属推理要求的租户路由到专属 endpoint。
- 每 Dataset 的 embedding 模型及版本固定。改变模型应重建索引并发布新版本；维度相同不代表向量语义空间兼容。
- 当前模型检查主要比较维度；Vector adapter 缓存按数据库连接配置，构建时持有 embedder，因此仅改 ContextVar 不能保证已缓存 adapter 切换模型。[维度检查](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/utils/ensure_embedding_model_matches.py#L56)、[Vector factory 缓存](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/create_vector_engine.py#L100)

### 7.4 存储引擎：接受已经确定的绑定，不替代业务授权

运行时解析链应为：

~~~text
已验证 actor + tenant
 → Dataset ACL
 → DatasetPlacement
 → 固定 DatasetBinding
 → graph/vector Resource
 → adapter / connection pool
~~~

现有 ContextManager 只有传入 permission_type 时才会再次检查权限；低层 engine factory 只解决连接和操作，不能作为对外接口绕过授权。[上下文权限边界](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L245)

池的 key 至少区分实际 endpoint、database、schema/namespace 绑定方式、角色和凭证版本；不要只用 tenant_id，也不要假设每个 Pod 只有一个连接池。当前 shared PG adapter 仍会为不同 Dataset 建立 engine，需要用活跃 Dataset 数量估算总连接并设置有界缓存。[PGVector engine](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py#L134)

## 8. 目标整体架构：控制面、计算面、数据面

以下为**建议目标架构**。三个“面”是职责分层，不要求首版都独立成微服务。

~~~mermaid
flowchart TB
  User["UI / SDK / MCP / 企业应用"] --> API["API 副本：认证、固定租户、ACL、配额"]
  subgraph Control["控制面"]
    Catalog["租户、角色、Dataset 目录、模型配置"]
    SQL[("共享 PostgreSQL：ACL、元数据、Job、资源目录")]
    Provision["资源控制器：创建、迁移、回收"]
    Catalog --> SQL
    Provision --> SQL
  end
  API --> Catalog
  API --> Jobs["持久 Job / 队列"]
  Jobs --> Workers["Ingest / Improve Worker 池\n内嵌 Cognee Pipeline Runtime"]
  API --> Query["Query Runtime\n首版与 API 同部署，后续独立扩容"]
  Workers --> Resolve["授权后的 Dataset Binding 解析\n固定路由与模型版本"]
  Query --> Resolve
  Resolve --> SQL
  subgraph Data["数据面：网络可访问的共享或专属服务"]
    Blob[("对象存储：原件与抽取文本")]
    G1[("Graph 存储组 G1\n多个 Dataset database")]
    G2[("Graph 存储组 G2\n专属租户或容量分片")]
    V1[("Vector 存储组 V1\nDataset schema / namespace")]
    V2[("Vector 存储组 V2")]
    Session[("共享会话存储")]
  end
  Workers --> Blob
  Resolve --> G1
  Resolve --> G2
  Resolve --> V1
  Resolve --> V2
  Query --> Session
  Workers --> Session
  Workers --> Model["模型网关 / LLM 与 Embedding 服务"]
  Query --> Model
  Provision -.资源生命周期.-> Data
~~~

首版建议的实际部署单元：

| 部署单元 | 内容 | 扩容依据 |
|---|---|---|
| API + Query Deployment | 接入、授权、同步 recall/search | 并发请求、延迟、模型等待、查询 CPU |
| Ingest Worker Deployment | add 后续加工、cognify、improve、更新删除协调 | 作业积压、抽取/embedding 吞吐、租户预算 |
| 控制任务入口 | schema 迁移、资源开通与修复 | 单独协调，不依赖每个数据请求临时完成 |
| 网络存储服务 | SQL、对象、图、向量、会话 | 由各数据库容量与可用性要求决定 |

无需一开始把 Loader、分块、抽图、Embedding、图写入拆成五个微服务。它们可以在 Worker 内复用现有 Task 链；有明确 CPU/GPU/调用限额瓶颈后再拆专门 worker 池。

如果以后 Query Runtime 独立部署，应同时迁出检索上下文构造和模型调用，API 只负责身份、配额和协议；会话写入仍走共享存储。API 与 Worker 可以使用同一版本代码和不同启动入口。

## 9. 下层存储怎样隔离

| 存储类别 | 推荐首版隔离 | 平台必须补齐的约束 | 更强隔离选项 |
|---|---|---|---|
| 关系元数据 | 共享 PostgreSQL，按 tenant/Dataset/actor 明确作用域 | 对适用业务表补完整归属、复合约束与授权；审计全局删除；全局身份/成员表不能机械套同一过滤 | 部分业务表 RLS、独立 database/实例 |
| 原件与文本 | 对象 key 使用稳定 tenant/Dataset/document/version 前缀 | 前缀不是权限；签名 URL 与 IAM 校验，记录真实 URI，保留/删除策略 | 独立 bucket、账号、区域、密钥 |
| 图 | 每 Dataset database，多个 database 可在一个图服务组 | 数据面账号限权；资源开通账号与查询写入账号分离；验证 adapter 所需能力 | tenant 专属图实例/集群 |
| 向量 | 当前可用 pgvector_shared 的 Dataset schema | schema 权限、连接绑定、embedding 版本；不能只靠 search_path 防越权 | 独立 database/实例或已适配的远程向量服务 |
| 会话/反馈 | 共享网络后端，tenant + user + session 定位 | 所有读写/清理和锁使用同一范围；保留期限与租户删除 | 专属会话库 |
| 查询缓存 | 租户/权限范围、Dataset 版本、模型 profile 参与 key | 撤权与重建后失效；禁止只按 query 做跨租户命中 | 专属缓存 |
| Job / 配额 / 用量 | 按 tenant 归属、按 Dataset 协调写 | 防一个租户独占 worker；用量事件可重试去重 | 专属队列和 worker 池 |

Schema 解决命名空间，数据库角色权限决定能访问什么。PostgreSQL 官方明确，连接用户拥有权限时可以跨 schema 访问；因此共用高权限账号不会因 schema 名不同自动形成强安全边界。[PostgreSQL schemas 与权限](https://www.postgresql.org/docs/current/ddl-schemas.html)

AWS 的 SaaS 架构也区分身份认证/功能授权与租户资源隔离。目标方案把租户上下文一直约束到实际存储和资源权限，正是为了防止上层有 ACL、下层仍能随意路由的断层。[AWS Tenant isolation](https://docs.aws.amazon.com/whitepapers/latest/saas-architecture-fundamentals/tenant-isolation.html)

可选参考组合：共享 PostgreSQL 元数据与 PGVector、支持多 database 的 Neo4j Enterprise 图服务、对象存储、网络会话后端。它与现有 adapter 接点接近，但需按负载、数据库数量限制、成本和运维要求验证，不能从源码直接推定容量。Neo4j Community 单实例只有一个标准数据库，不能直接承接相同的多 database 部署方案。[Neo4j 数据库管理](https://neo4j.com/docs/operations-manual/current/database-administration/)

不能把“图和向量都放 PostgreSQL”直接作为已成熟的生产默认：仓库 PostgreSQL 图 adapter 明确标为 demo，且存在固定 advisory lock 与应用层图查询等实现，需要专门验证并加固。[Postgres 图 adapter 声明](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/postgres_demo/adapter.py#L1)

## 10. 如何按 Dataset 分片，同时支持企业专属部署

建议在现有 DatasetDatabase 基础上增加资源目录，最小表达三类记录：

| 记录 | 必要字段 | 职责 |
|---|---|---|
| DatasetPlacement | dataset_id、固定 tenant_id、状态、mode、binding_id、route_revision | 逻辑 Dataset 当前在哪里 |
| DatasetBinding | graph resource + database、vector resource + namespace、object prefix、generation | 一次执行固定使用的完整绑定 |
| Resource | provider、endpoint、region、credential_ref/version、能力 | 一个实际数据库服务组或资源 |

这样同样的企业例子可以落成：

| Tenant / Dataset | 图路由 | 向量路由 | 计算池 |
|---|---|---|---|
| A / D1 产品 | G1 的 g_d1 | V1 的 ds_d1 | 公共池 |
| A / D2 财务 | G1 的 g_d2 | V1 的 ds_d2 | 公共池 |
| B / D3 客户 | G2 的 g_d3 | V2 的 ds_d3 | B 专属池或公共池，依套餐 |
| C / D4 项目 | G1 的 g_d4 | V1 的 ds_d4 | 公共池 |

这是一种示例 placement；B 不必天然单独占一个存储组，是否独占由产品隔离等级决定。A 的多个 Dataset 也可因容量分别放在不同组。

现有 handler 是合适接点，但当前 shared PG handler 默认从统一配置取连接，并不是已经实现了任意存储组的自动调度。新增 resolver/handler 应接受资源目录指定的资源，控制器负责开通；读写 runtime 仅使用已就绪的绑定。[现有开通顺序](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/utils/get_or_create_dataset_database.py#L97)

三个不同的集群问题必须分别验收：

1. **应用副本集群**：更多 API / Worker 处理更多请求或不同 Dataset。
2. **Dataset 分片**：将完整 Dataset 放到不同图/向量存储组，扩大总数据容量。
3. **数据库内部集群**：单个存储组的复制、HA、读扩展或内部分片，由后端提供。

增加 Worker 不会让同一个大图自动分片。首版用整个 Dataset 作为放置和写入协调单位；一个超大 Dataset 需要单独评估数据库原生能力，或重新设计知识库拆分和跨库检索。现有 fan-out 不是跨库图遍历协议。

企业专属模式仍可复用同一个控制面，但查询、导入、数据账号、存储和网络边界都应按承诺划分；只分配专属 Pod、仍共用不受限管理员账号，不等于完整专属数据隔离。

## 11. 集群化应具体改在哪里

| 改造接点 | 现有实现 | 建议改造 |
|---|---|---|
| API / SDK 包装层 | HTTP 识别 User，SDK 可默认用户 | 统一可信 actor、显式请求 tenant、Dataset UUID；MCP 每次传递调用者身份 |
| 租户选择与授权 | User.tenant_id + Principal/ACL | 请求快照、同租户 grant 规则、owner 转移/离职策略 |
| ContextManager | 解析 owner，配置 ContextVar | 完整作用域恢复；绑定固定的路由、模型版本 |
| DatasetDatabase + handlers | 首次访问可同步创建图/向量后端 | 资源目录、placement、独立幂等开通与状态协调 |
| pipeline 执行入口 | 进程内 task / background | 持久 Job 和独立 Worker，复用 dataset-aware pipeline |
| Dataset 锁和队列 | asyncio/local registry，进程内限流 | Dataset 写入所有权跨节点协调；tenant 公平调度 |
| PipelineRun / 恢复 | 运行审计、已有失败补偿 | 加 job claim、lease/heartbeat、重试及恢复所有权；不能仅按开始时间判断其他 Pod 任务死亡 |
| 查询协调 | 多 Dataset 分别检索/生成 | 有界 fan-out、可选统一重排和综合生成、总预算 |
| 模型配置与缓存 | 调用覆盖 + 部分全局配置/缓存 | tenant profile、secret 引用、embedding 索引版本和 cache 生命周期 |
| 文件/会话存储 | 可使用本地默认后端 | 网络存储、稳定 URI、tenant 范围读写与删除 |
| 部署生命周期 | 每进程连接池、启动/退出清理 | 统一认证 secret、受控迁移、worker drain、池容量限制 |

主要执行源码：[本地 Dataset 锁](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/locks/dataset_lock.py#L18)、[dataset-aware pipeline](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/pipeline.py#L96)、[任务并发](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/run_tasks.py#L182)。

跨节点提交与消费方式可以选数据库作业队列或已有消息系统，不需要先承诺某个中间件。必要语义是：提交可持久化、重复消费幂等、执行者可检测失效、同 Dataset 的变更有协调、旧执行者不能继续发布结果。仅增加 Redis 锁不能替代完整的失效和提交协议。

**第二阶段若暂不采用隔离的索引 generation，必须在实际存储写入边界拒绝过期写入者，或在接管前确认旧执行环境已被隔离/终止。** 现有 pipeline 直接修改图与向量，仅阻止旧 worker 更新 Job 状态仍可能让迟到写入影响查询。单纯获得新 lease 不足以保证安全；后端不支持有效 fencing 时，应采用可确认的单写执行环境隔离，而不能承诺同 Dataset 自动安全接管。采用 generation 后也要约束最终发布，防止旧执行者把可见路由切回旧版本。[分布式锁与 fencing 的故障模型](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)

FastAPI 官方对重型后台任务也建议考虑可运行在多个进程/服务器的任务系统；当前同事件循环的后台任务不提供这个部署边界。[FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)

## 12. 落地顺序与验收

| 阶段 | 交付边界 | 有意义的验收 |
|---|---|---|
| 第一阶段：多租户语义固定 | actor/tenant 上下文、UUID 知识库、ACL/模型 profile、文件与会话作用域 | A/B 使用同一用户账号和相同 session 名并发操作仍隔离；A 用户共享同一 D1 路由；D2 未授权不可见 |
| 第二阶段：云端多副本闭环 | 网络存储、API 副本、持久 Job、独立导入 Worker、跨节点 Dataset 写协调 | 任意 API/Worker 重启后能继续操作同一知识库；重复 Job 不重复发布；删除/更新/improve 互不破坏 |
| 第三阶段：容量与产品等级 | Dataset placement、多存储组、专属池、全局检索融合、租户计量 | Dataset 迁移后按新路由读写；旧写者不能提交；热点租户不拖垮其他租户；融合答案保留每条证据来源 |

在第二阶段只对“API 副本数翻倍”压测还不够。必须测不同 Dataset 并发、同一 Dataset 竞争、同账号多 Tenant、模型缓存命中、部分存储失败，以及会话/删除范围。容量应分别记录 API 并发、活跃 Dataset 数、总数据库连接、图规模、检索 fan-out、LLM/Embedding 限额和租户费用。

本次完成的是架构研究与源码证据整理；以上验收尚未执行。建议将第一阶段的租户上下文和存储绑定契约作为实施起点，再落地持久作业和 Worker，最后引入跨存储组迁移与专属部署。

## 13. 深入源码阅读材料

- [认证、权限、Tenant、Dataset 与路由时序](../evidence/09-tenancy-boundaries.md)：调用图、共享实例时序、配置真值表。
- [上层引擎与读写双链路](../evidence/09-engine-flow.md)：SearchType 差异、LLM/Embedding 覆盖和缓存细节。
- [集群组件与资源目录](../evidence/09-cluster-topology.md)：三种集群、runtime 部署、资源绑定和专属模式。
- [原始商业化分析报告](v1-analysis.md)：输入、持久化、恢复、风险和迁移的更多代码证据。
