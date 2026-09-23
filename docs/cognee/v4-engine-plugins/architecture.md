# V4：可持续演进的记忆平台——Cognee 与 Hindsight 的真实插件边界

日期：2026-09-23。本文修正 V3 的主架构，不撤销 V2 对 Cognee 输入、持久化、隔离和集群风险的源码分析。

**结论：平台可以支持整引擎插件，但上一版将“整引擎替换”和“内部组件替换”混在一起，抽象过早。应先采用薄平台核心 + MemoryProvider 接口 + 完整 Cognee 数据面，再用真正的 HindsightProvider 验证边界。不能据此承诺旧数据无损迁移、内部组件任意互换或性能自然提升。**

## 1. 分析基线与需要撤回的设计假设

- Cognee：本地 SHA 663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e，1.6.0。
- Hindsight：另行下载源码，SHA 12f2d54f643baddacb98cd547c89b1a50c5c3dcc。当前主分支快照，不代表特定稳定发布版。
- 已做：代码定向追踪、官网对照、架构反审。未做：实现适配器、运行服务、调用模型、迁移数据、故障注入及性能测试。
- 方法沿用 repo-analyzer Skill??????????，按其要求分别核查语义、存储执行和演进设计；三个专项已完成并交叉汇总。

| V3 的问题 | 为什么妨碍演进 | V4 修正 |
|---|---|---|
| Cognee 在图中只有一格，未来平台组件很多 | 误导实施比例；实际上首版大部分记忆计算仍由 Cognee 完成 | 展开 Cognee 的完整数据面；平台只保留产品责任 |
| 将 CanonicalGraph、ChunkManifest、GraphStore、VectorIndex 全部设为跨引擎边界 | Hindsight 的记忆事实及 SQL 图关系不遵循 Cognee DataPoint 模型 | 这些降为组件改造的可选产物，不作为整引擎准入条件 |
| MemoryBackend 定义含混 | 容易被理解成另一个引擎、数据库或必部署服务 | 更名 MemoryProvider：接口及适配器代码 |
| “可选工作流”未区分业务流程和引擎内部任务 | 可能出现两套调度器反复重试同一次写入 | 外层管业务阶段；原生 Worker 管引擎内部任务 |
| 用中立接口暗示随时切换 | 旧索引、ACL、会话和派生记忆并不自动迁移 | 分开验证新空间选择、接口兼容、存量迁移、效果与性能 |

可持续性的核心不是接口数量，而是**产品模型稳定、引擎模型局部化、数据可重建、运行状态可追踪、能力差异显式表达**。插件模式支持按配置绑定实现，防腐层负责外部语义转换；两者都不保证数据或行为天然等价。[Plugin 模式](https://martinfowler.com/eaaCatalog/plugin.html)、[Anti-Corruption Layer](https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer)

## 2. 修订架构：Cognee 仍是首版主要计算主体

配色：🟦 Cognee 开源组件；🟩 其他开源项目，包含 Hindsight；🟧 平台需要掌握的业务语义与适配代码；🟪 可选商业服务。虚线表示可选或待实现，不表示已经接通。蓝色也属于开源；颜色不代表性能或成熟度。

~~~mermaid
flowchart TB
  App["应用 / Agent / SDK"] --> Core["平台薄核心<br/>企业与成员、空间 ACL、来源版本<br/>引擎绑定、操作目录、配额"]:::platform
  Core --> Sources["平台原始资料与业务事件<br/>文档版本、纠正、删除记录"]:::platform
  Sources --> Obj[("既有对象存储 / Ceph RGW")]:::oss
  Core --> Catalog[("PostgreSQL：平台目录")]:::oss
  Core --> Port["MemoryProvider 接口<br/>代码契约：写入、证据检索、可选回答、删除"]:::platform
  Core -.跨系统业务需要时.-> WF["可选业务 Workflow<br/>Temporal 等"]:::oss
  WF --> Port

  subgraph C["首版主要记忆数据面：Cognee"]
    CP["CogneeProvider 适配器"]:::platform
    CWrite["add / cognify / remember<br/>文件接入与元数据"]:::cognee
    CBuild["Loader → 分块<br/>知识图抽取、摘要、Embedding"]:::cognee
    CIndex["DataPoint / 来源关联<br/>图与向量写入"]:::cognee
    CRead["search / recall<br/>检索器、上下文、回答"]:::cognee
    CM["session / feedback<br/>improve / memify"]:::cognee
    CA["Graph / Vector Adapter<br/>Dataset Database Handler"]:::cognee
    CS[("Cognee 内部 SQL、图、向量、会话存储<br/>资源布局按 V2 选型与隔离")]:::cognee
    CP --> CWrite --> CBuild --> CIndex --> CA --> CS
    CP --> CRead --> CA
    CP --> CM --> CA
    CM --> CS
    CWrite --> CS
  end

  subgraph H["可选整引擎插件：Hindsight，适配器尚待实现"]
    HP["HindsightProvider 适配器"]:::platform
    HA["retain / recall / reflect API"]:::oss
    HW["原生 Worker 与 async_operations<br/>retain、consolidation、派生内容刷新"]:::oss
    HD[("Hindsight PostgreSQL<br/>documents / chunks / memory_units<br/>entities / memory_links / 向量与全文")]:::oss
    HP --> HA --> HD
    HA --> HW --> HD
  end

  Port --> CP
  Port -.按空间选择.-> HP
  Port -.后续能力验收.-> Other["其他 Provider<br/>Mem0 / Graphiti 等"]:::oss
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
~~~

这是逻辑职责图，不是每个方框都部署成微服务，也不是一次请求同时调用两个引擎。Cognee 适配器可嵌入现有 Python API/Worker；Hindsight 可按其原生 API + 独立 Worker 部署。平台目录和 Provider 内部数据库具有不同数据所有权，可在初期共用 PostgreSQL 集群，但应分开逻辑库/角色及迁移权限。

Cognee 中会话路径与普通文档路径并非完全相同；图展示职责，不能解释为所有 remember 都经过同一条分块抽图链。依据：[add Task 组合](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/add/add.py#L263)、[默认 cognify Tasks](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/cognify/cognify.py#L581)、[图向量写入](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/storage/add_data_points.py#L317)、[会话 remember 分支](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/remember/remember.py#L1786)。

**平台掌握来源，并不要求接管每一种解析算法。** 平台持久保存原始字节或业务消息、版本、来源和用户纠正；Provider 可以自行解析并保留内部文本。已有可信解析文本可作为可选输入，但不能要求所有引擎使用同一种分块或图模型。

## 3. MemoryBackend 到底是什么：收敛为 MemoryProvider

它是平台代码中的**接口 + 每种引擎一个适配器**。不承担知识抽取、索引或推理，不要求新建独立服务。其职责是让业务层不引用 Cognee Dataset ORM、DataPoint、SearchType 或 Hindsight bank/fact 返回模型。

建议基础契约按实际产品需求选择，下表是设计合同，不是当前仓库已实现 API：

| 接口 | 稳定输入输出 | 适配器责任 |
|---|---|---|
| describe_capabilities | 契约版本、输入类型、结果类型、来源精度、隔离/删除/异步能力 | 真实声明，不支持的必需能力拒绝接入 |
| ingest | SourceEnvelope → WriteReceipt | 文档和版本身份转换，提交原生写入，返回 operation 与可见性 |
| retrieve | 授权范围、query、预算 → EvidenceBundle | 区分原文片段、抽取事实、图关系、派生观察；保留来源 |
| answer（可选） | query、回答要求 → AnswerEnvelope | 使用原生回答或指定的上层回答策略；必须标注策略版本 |
| remove_sources | source IDs/revisions → DeletionReceipt | 映射删除范围，报告来源不可读与派生清理状态 |
| get_operation / cancel（按执行能力） | ProviderOperationRef → 状态/取消效果 | 映射原生状态；不把超时当失败、不把取消请求当回滚 |

平台维护一张绑定目录：MemorySpace → provider_kind、provider_instance、native_scope、provider_config_version、binding_revision。空间是产品资源，scope 是 Provider 内部资源。一次请求及其后台任务固定绑定版本；不得中途因默认配置改变而向另一引擎写入。

最小数据封套：

- **SourceEnvelope**：tenant/space、平台 source_id、revision、受控 content/artifact_ref、业务时间、来源、内容类型和 metadata。只有需要时加解析文本；无需统一 chunk IDs。
- **WriteReceipt**：平台操作 ID、原生操作 ID、绑定版本、source revision，以及 accepted / searchable / derived_ready 等已验证阶段。不是所有 Provider 都承诺最后一阶段。
- **EvidenceBundle**：证据类型、文本、平台 source refs、原生 fact/chunk refs、来源粒度、检索策略。分数带 score_kind，跨引擎不可直接排序比较。
- **DeletionReceipt**：请求范围、不可查询阶段、派生清理进度与失败项；物理清除和备份保留另有明确政策。

适配器只做协议与语义映射。企业 ACL、计费和跨系统业务状态机不塞进适配器；原生算法也不被抽出来重新实现。

**操作身份在提交前建立。** 平台先持久化 operation_id、request_digest、source revision 和 binding_revision，再调用 Provider；同操作 ID 对不同摘要应拒绝。对同一来源的并发版本，首版采用来源级有序写入或明确的版本比较规则，拒绝过期 revision 覆盖新版本。若 Provider 原生不支持条件写，平台需串行协调并处理在途旧写；不能只在提交前检查一次版本就宣称消除了竞争。这些规则也适用于迁移追平。

### 能力画像比“最低共同接口”更重要

可以定义 basic-memory-v1、grounded-answer-v1、temporal-facts-v1 等产品画像，但每个画像的来源、更新和删除要求必须可测试。某引擎达不到画像时，拒绝该空间选择，或让产品显式选择另一种行为，不静默降级。

Cognee 自定义图/图遍历、Hindsight mental models/directives、Graphiti 时态关系可通过版本化的原生扩展保留。依赖这些扩展的功能必须标明可用 Provider。不要用一个无类型 options 字典隐藏所有差异，再宣传完全可替换。

会话另设可选 session-memory 能力画像：明确 tenant、user、space、session、turn 身份，轮次排序、历史读取、反馈写入及保留/删除规则。支持 answer 不代表具有 Cognee 同等会话行为；Hindsight 是否满足这一画像需要独立适配和验收。若产品依赖会话，画像就是该产品的必需能力，不能在换引擎时静默去掉。

CanonicalGraph、ChunkManifest 可以作为需要组件级改造时的可选产物格式；**不再是所有 Provider 的强制数据模型**。

## 4. Hindsight 能否接入：逐能力核查，而非按函数名对齐

**可以设计并实现 HindsightProvider；当前证据支持实现可行性，尚未证明适配器与产品语义已经通过验收。** 特别要避免下面两种错误：把 Hindsight recall 直接对应 Cognee 高层 recall；把 Hindsight reflect 对应 Cognee improve。

| 产品能力 | Cognee 路径 | Hindsight 路径 | 替换结论 |
|---|---|---|---|
| 导入长期资料 | add + cognify；普通 remember 组合这些步骤 | retain：分块、抽取事实、存储并建联系 | 可由 ingest 适配，无须要求 Hindsight 暴露 add/cognify 两阶段 |
| 找到相关证据 | search 的具体 Retriever；返回类型依模式而变 | recall 返回事实，可附 chunks/source facts | 可统一证据封套；事实不等于原文段落 |
| 基于记忆回答 | completion 检索器/高层 recall，可能带 session/feedback | reflect 通过工具查询记忆并生成答案 | 可以实现 answer，但算法、引用和副作用不同 |
| 维护与增强记忆 | improve / memify、自定义任务 | consolidation 形成 observations；mental model 独立刷新 | 不能设一个同名 improve 就宣称等价 |
| 更新文档 | 需适配 source 身份及处理流程 | document_id + replace/append；支持 delta retain | 业务 revision、原生 document_id、操作幂等身份分别管理 |
| 删除文档 | 图、向量和来源关联协调 | 删除来源事实，失效相关 observations，安排维护/刷新 | 删除状态必须覆盖派生内容，不能只映射 HTTP 成功 |
| 自定义领域图 | DataPoint、graph_model、图检索及存储接点 | 自有 facts/entities/memory_links 模型 | 不属于通用等价能力；依赖 Cognee 图结构的业务需要另做转换 |
| 原生数据导出 | 依具体数据与存储实现 | transfer 支持原生文档/银行状态迁移 | 不等于 Cognee→Hindsight 无损迁移 |

源码：[Hindsight Recall 返回模型](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/response_models.py#L364)、[Reflect HTTP 输出](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/api/http.py#L1691)、[文档更新与 delta retain](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/retain/orchestrator.py#L1269)、[删除及后续维护](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L10152)。

Hindsight 当前事实模型区分 world 和 experience；observations 是派生归纳，mental models 是可保存、可刷新的主题内容。这些应作为原生能力暴露。直接 reflect 是基于记忆回答，不等于持久化一次“自我改进”；持久 mental model 刷新另有路径。其 recall 和 reflect 的区分也见[官方 Recall](https://hindsight.vectorize.io/developer/api/recall)、[官方 Reflect](https://hindsight.vectorize.io/developer/api/reflect)。

**原文不能靠宣传概述推断。** 当前代码默认保存 document text 与 chunks；关闭 store_document_text 后行为变化。不能声称 Hindsight 永远只存事实、没有原文；也不能由文本保存推断所有原始 PDF/附件字节都已保留。平台自己保存受控原件仍有迁移价值。[原文条件持久化](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/retain/fact_storage.py#L364)、[chunk 条件持久化](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/retain/chunk_storage.py#L250)。

### 两种回答策略必须分清

1. **原生回答**：Cognee completion 或 Hindsight reflect。保留引擎完整检索/推理优势，上层统一请求和返回封套；切换后回答行为可能改变。
2. **上层统一回答**：Provider.retrieve 返回证据，由平台固定生成流程回答。便于控制格式、引用和多源融合，但不等同于 Hindsight 原生 reflect 的行为。

不建议第一天同时实现两套。根据产品先确定一种，并将 answer_strategy/version 纳入评测和配置；需要 Hindsight 原生推理时，不应为了“中立”强制丢掉它。跨 Provider 融合也属于新增产品流程，不是插件接口自然附送。

## 5. 可选 Workflow 的职责与不需要它的场景

这里原先混淆了三个东西：

| 名称 | 负责什么 | 是否必须部署独立产品 |
|---|---|---|
| 业务流程 | 同步企业数据源→提交引擎→等待可用→通知；或迁移→评测→切换 | 必须有正确状态处理，不一定需要专用引擎 |
| Cognee Task pipeline / Hindsight 内部任务 | 分块、抽取、索引、consolidation 等算法步骤 | 复用所选 Provider |
| Workflow 执行产品 | 持久业务等待、重试、补偿、跨服务协调，如 Temporal | 按复杂度引入，可选 |

一次短查询可以直接调用 Provider。单次 Hindsight 异步 retain 可以提交后记录原生 operation 并查询状态，原生 Worker 执行即可。跨多个外部系统的长流程、人工审批、复杂补偿才更值得引入外部 Workflow。

**Cognee 库调用模式**：平台持久作业 Worker 调用 Cognee 并等待真正完成。不能只排一个进程内后台任务就标记业务成功；外层任务引擎也不会自动保存 Cognee 每个 chunk 的执行位置。原有进程内 Dataset 队列仍不是集群锁，V2 的写所有权和恢复改造仍需要做。

**Hindsight 原生异步模式**：

~~~mermaid
sequenceDiagram
  participant P as 平台业务协调
  participant D as 平台操作目录
  participant A as Hindsight API
  participant Q as Hindsight 操作表
  participant W as 原生 Worker
  P->>D: 保存请求身份、空间绑定、来源版本
  P->>A: retain async + 稳定 operation_id
  A->>Q: 持久操作及待执行数据
  A-->>P: 原生 operation 引用
  P->>D: 记录原生 ID，状态为 accepted
  W->>Q: 事务领取任务
  W->>Q: 原生处理、重试、更新结果
  loop 等待原生操作
    P->>A: 查询相同 operation
    A-->>P: pending / processing / completed / failed
    P->>D: 更新业务阶段
  end
~~~

此时平台不再消费 Hindsight 内部 async_operations，也不再次拆解 retain 的算法步骤。Hindsight 当前 HTTP 支持调用者提供 UUID operation_id 进行重复提交对账；适配器应固定该身份。文档 upsert 的 document_id 与操作幂等 ID 不同，不能互相替代。[请求幂等字段](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/api/http.py#L1289)。

关键规则：

- 提交响应丢失：按原 operation_id 对账/重试协议，不能生成新 ID 重做整次写入。
- 外层网络重试与内层任务重试分别负责自己的层级。
- 原生 retain completed 只表示该操作完成；不能据此宣称所有 observations/mental model 刷新都完成。
- 取消可能已有部分结果提交；平台显示实际影响，不承诺回滚。
- 若未来采用其他 Provider 且无请求幂等/查询能力，必须显式暴露“结果未知”及修复策略，不能伪造统一 exactly-once。

Hindsight 已有独立 Worker、PostgreSQL 持久任务、事务内 SKIP LOCKED 领取和重试，因此 Temporal 不是其基础异步处理的先决条件。[官方 Services](https://hindsight.vectorize.io/developer/services)、[领取实现](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/db/ops_postgresql.py#L1813)。

它有进度 heartbeat，但本次核查未发现凭 heartbeat 到期进行租约/fencing 自动接管的机制；确认的是同 worker_id 重启恢复和 admin decommission。部署需稳定且唯一的 Worker 身份，永久缩容需回收遗留任务。仓库 Helm 使用 StatefulSet 与稳定 ID，符合这一恢复模型；不能因有 Worker 就宣称已完成全部集群故障转移。[重启恢复](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/worker/poller.py#L1295)、[进度 heartbeat](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L4457)、[Worker StatefulSet](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/helm/hindsight/templates/worker-statefulset.yaml#L3)。

## 6. 更换引擎后，多租户和存储如何落地

| 层次 | 平台稳定定义 | Cognee 映射 | Hindsight 映射 |
|---|---|---|---|
| 企业 Tenant | 企业身份、成员、套餐、边界 | 当前 Tenant/Principal/ACL 组织关系；需稳定身份映射 | 推荐映射 tenant schema；内置扩展的默认用户映射需按企业共享需求调整 |
| MemorySpace | 知识空间、授权策略、引擎绑定 | Dataset；共享者访问同一 Dataset 底层资源 | schema 内 bank；bank 名本身不是凭据 |
| 原始资料 | source_id + revision | Data/Document/内部 chunk refs | document_id/chunk_id/fact refs |
| 派生存储 | Provider 自主管理，平台记录资源绑定 | Dataset handler 决定图/向量资源；关系元数据共享 | schema 中的 SQL facts/entities/links + 向量/全文 |
| 后台隔离 | 可信租户上下文、配额、撤权策略 | 作业显式恢复 Dataset/用户上下文，仍需集群写协调 | Worker 从任务恢复 schema；扩展 list_tenants 负责发现租户 |
| 高隔离等级 | 独立资源、凭据、网络策略与运维成本 | 独立部署或资源组，具体后端受支持矩阵限制 | 独立数据库/实例/部署需资源路由设计，不由 bank 自动提供 |

Hindsight 以 PostgreSQL 关系表保存 memory_units、entities、memory_links，同时包含向量及全文索引；其“图”不要求 Neo4j。当前代码有 PostgreSQL 和 Oracle 后端分支，但并不是任意 GraphStore/VectorIndex 可替换的接口。换成 Qdrant、Milvus 或 Neo4j 将涉及数据访问与检索实现，不能只改连接串。[原生表结构](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/alembic/versions/5a366d414dce_initial_schema.py#L245)、[后端工厂](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/db/__init__.py#L29)。

认证必须明确：默认 DefaultTenantExtension 不验凭据；内置单 API key 模式把合法请求送到同一 schema，不提供企业内每 bank ACL。仓库确有 StaticKeys/Supabase 租户扩展，但需额外打包与配置；企业多成员共享 schema 及 bank 权限仍需平台或扩展实现。[默认与 API key 认证](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/extensions/builtin/tenant.py#L29)、[租户 ContextVar](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L2855)、[官方扩展说明](https://hindsight.vectorize.io/developer/extensions)。

schema 路由是应用隔离，不能自动等同于独立数据库角色/RLS 强制隔离。API 和 Worker 必须使用相同租户发现配置；用户撤权后已投递任务是否继续执行也要有产品策略。

平台权限应有**单一权威**。迁移早期可以让权限门面继续代理 Cognee ACL；切到 Hindsight 前必须完成授权目录迁移或经过验证的映射。不能让两套系统分别接受 grant/revoke，再依赖“最终会同步”维持数据安全。

## 7. 插件式替换分两个轴，不能承诺任意排列组合

~~~mermaid
flowchart LR
  Product["稳定产品接口"]:::platform --> Engine["整引擎插件轴"]:::platform
  Engine --> C["CogneeProvider"]:::cognee
  Engine --> H["HindsightProvider"]:::oss
  Engine -.准入验证.-> O["Mem0 / Graphiti Provider"]:::oss
  C --> CI["Cognee 内部扩展轴<br/>Loader / Chunker / Task<br/>Retriever / DB Adapter + Handler"]:::cognee
  H --> HI["Hindsight 原生扩展轴<br/>模型配置、Tenant/Operation 扩展<br/>原生数据库与记忆策略"]:::oss
  CI -.有收益才抽离.-> Custom["可选自建组合引擎<br/>自行承担图、向量、来源与一致性"]:::platform
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

**轴一：整引擎替换。** 实现另一 Provider，让产品继续使用平台契约。Hindsight 带自己的存储与内部执行方式进入系统，无须先把 Cognee 拆成八个服务。

**轴二：引擎内部组件替换。** Cognee 可通过已有扩展点替换解析器、任务、Retriever 等；图/向量适配还要满足 Dataset handler 与来源删除语义。Hindsight 有自己的接点，不能把 Cognee Task 或数据库 Adapter 原封不动插入。只有明确瓶颈值得投入时，才承担抽离组件、维护分支和重建索引的成本。

如果最终业务确实需要自行组合解析、图抽取、向量和检索，可建设独立的 ComposableProvider；这实质上是平台开始拥有一种记忆引擎，不是“写几个接口”就得到的免费能力。

### 其他类似项目应如何进入设计

| 候选 | 可用于哪种产品需求 | 作为插件时重点验证 | 本轮证据等级 |
|---|---|---|---|
| 🟦 Cognee | 文档/知识图构建、检索、回答及记忆维护 | Dataset ACL、写协调、图与向量一致性、会话副作用 | 本地源码深入核查，见 V2/V3 |
| 🟩 Hindsight | 事实记忆、时序/关系检索、原生 reflect 与派生记忆 | recall/reflect 分离、bank/schema 权限、操作状态、派生删除 | 固定 SHA 定向源码核查，未运行 |
| 🟩 Mem0 | 用户/Agent 长期记忆管理候选 | add/search/update/delete 语义、源文档证据、隔离与所需能力画像 | 仅官方资料初筛，不声称已完成适配 |
| 🟩 Graphiti | 时态实体关系、变化事实和来源追踪 | episode/edge 向证据映射；是否另需回答层与用户管理 | 仅官方资料初筛，不声称已完成适配 |
| 🟪 托管产品 | 希望购买运维与服务能力的情形 | API、数据导出、隔离、预算和服务承诺 | 必须与其同名开源框架分开评估 |

[Mem0 开源说明](https://docs.mem0.ai/open-source/overview)、[Graphiti 官方仓库](https://github.com/getzep/graphiti)。Graphiti 是开源框架；Zep 托管产品的专有存储及服务能力不能算成 Graphiti 开源版自带能力。

这不是性能排名。不同项目的评测任务、输入、模型、质量门槛和成本不同；“更强”必须在产品自己的数据上验证。引入第二个真实 Provider 比提前堆更多接口更能发现设计耦合。

## 8. 从 Cognee 迁移到 Hindsight 的实际步骤

| 步骤 | 具体动作 | 完成条件 |
|---|---|---|
| 1. 准入 | 实现 HindsightProvider；声明能力和版本；跑相同产品契约用例 | 必需能力、隔离、来源与删除符合要求 |
| 2. 新空间试用 | 为新 MemorySpace 绑定 Hindsight 实例/schema/bank | 新用户流程可用，尚不触碰旧索引 |
| 3. 存量建清单 | 盘点原件、版本、纠正、会话、人工图修改、删除记录及权限 | 明确哪些可重放、哪些要转换、哪些不能迁移 |
| 4. 新建目标 | 为旧空间创建独立目标 bank/绑定 revision | 旧服务不受重建写入影响 |
| 5. 重放与追平 | 按源版本导入，保存 source→native document 映射，应用纠正及 tombstone | 对账已纳入的来源版本；未恢复已删除资料 |
| 6. 影子评测 | 同一查询对照证据、答案、时效、成本；关闭影子路径的会话反馈写入 | 产品可接受差异、能力损失明确 |
| 7. 切换 | 短停写或可靠增量追平，排空/隔离旧绑定在途作业，再原子切绑定版本 | 新请求和新任务使用一致的新绑定 |
| 8. 回退窗口 | 旧端保持追平，或明确停写恢复策略；演练故障与删除同步 | 回切不丢新数据、不复活删除内容 |
| 9. 退役 | 满足保留策略后回收旧派生资源 | 可审计且不影响平台来源与当前运行 |

不应默认同步双写两个引擎：跨引擎没有原子事务，部分失败会产生漂移。若确有双写需求，应从平台持久事件/出站队列重放，分别记录每端进度并对账；仍需定义切换窗口，而非把双写当一致性保证。

Hindsight 自带 transfer 很有价值，但它是 Hindsight 原生模型的迁移格式。当前 whole-bank import 要求目标 bank 不存在，重嵌入并重建内部身份/联系；支持 observations、mental models 等可选原生状态，不是 Cognee 图数据库直接导入器。[导出 schema](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/transfer/schema.py#L26)、[整 bank 导入约束](https://github.com/vectorize-io/hindsight/blob/12f2d54f643baddacb98cd547c89b1a50c5c3dcc/hindsight-api-slim/hindsight_api/engine/transfer/importer.py#L943)。

从原件重建能恢复可重新计算的记忆，不保证恢复所有反馈学习效果、人工图编辑与原生派生状态。必须把人工修改保存为业务事件，或明确转换/保留方案；不能把旧引擎推断出的结论伪装成用户提供的原始事实。

“只改配置”只适用于**已经实现并验收适配器的新空间选择**。对既有空间，更换配置指针是迁移的最后一步，不是迁移本身。

## 9. 怎样证明这一版具备持续演进能力

第一阶段保持薄平台和完整 Cognee。平台新增的必要责任只有稳定空间/来源身份、授权门面、绑定目录、操作与删除状态；不要为了架构图齐全新增八个独立微服务。Cognee 云化仍按 V2 处理原件存储、任务持久性、嵌入式数据库单写所有权、隔离和资源限额。

第二阶段实现一个真实的 HindsightProvider，先供新空间使用。若为接入第二种引擎，需要修改每个业务路由、泄露 bank_id 到产品 API、或要求所有事实转换成 Cognee DataPoint，说明边界没有成立，应先修正契约。

第三阶段选择小范围旧空间迁移，完成对账、效果评测与回切演练。第四阶段才依据测得的瓶颈决定更换 Cognee 内部组件、扩大 Hindsight 使用范围或构建其他 Provider。

| 验收方向 | 必须拿到的证据 |
|---|---|
| 代码依赖 | 产品域不 import Provider SDK 类型；新引擎主要增加 adapter、能力描述、部署与迁移实现 |
| 真实互换 | 同一写入/检索/删除测试集运行在两个真实 Provider 上，不以 mock 代替 |
| 多租户 | 两租户同名文档/空间不串读；无权 bank/dataset 拒绝；后台任务范围不受外部参数伪造 |
| 操作可靠性 | 提交响应丢失、进程重启、取消部分执行、旧 Worker 恢复均有可解释结果 |
| 来源与删除 | 引用可回到平台资料；更新/删除后事实、派生内容、会话及原件按合同处理 |
| 迁移 | 版本、tombstone、人工纠正、权限一致；不能迁移的原生状态有清单 |
| 升级与回退 | Provider API/SDK 版本固定；在途 operation 保留原绑定；滚动升级不误路由 |
| 效果与性能 | 相同输入、模型预算、质量/隔离要求下比较延迟分布、吞吐、模型成本与恢复时间 |

**当前能下的结论：**V4 给出可落地的插件边界和验证路径；源码已表明 Cognee 与 Hindsight 存在可映射的产品能力，也存在必须显式处理的差异。尚不能下“系统已经可以热插拔 Hindsight”或“替换后性能更好”的结论。

## 10. 关联材料与阅读范围

- [V2：Cognee 数据流、多租户与集群风险](../history/v2-multitenancy.md)
- [V3：组件接点与开源/商业选项，主架构由 V4 修订](../history/v3-components.md)
- [专项：V3 演进反审](../evidence/11-evolution-review.md)
- [专项：Hindsight 接口、原文、删除与迁移语义](../evidence/11-hindsight-semantics.md)
- [专项：Hindsight 存储、租户、Worker 与恢复](../evidence/11-hindsight-runtime.md)

专项文件列出具体已读代码区间和未覆盖范围；本报告不等于全仓安全审计或生产性能认证。本轮只新增/更新分析文档，未改 Cognee 业务代码。
