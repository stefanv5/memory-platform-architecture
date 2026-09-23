# Cognee 组件化云架构 V3：来源标色、开源复用与可替换边界

> 主架构已由 [V4：Cognee / Hindsight 插件边界](../v4-engine-plugins/architecture.md) 修订。V3 的 MemoryBackend 更名并收敛为 MemoryProvider；规范图和细组件端口降为可选演进方向，不能作为所有引擎的准入要求。本文的组件源码证据仍可参考。

日期：2026-09-23。代码基线：663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e，Cognee 1.6.0。

**目标：让业务依赖平台自己的记忆服务契约，Cognee 是首个实现；后续既能逐组件替换，也能切换整个记忆后端。** 本文延续 V2 的输入、持久化、多租户和集群分析，重点补“谁来实现、哪里可替换、替换需要什么”。图中的平台组件是设计方案，未在本次分析中实施。

## 1. 配色与集成状态

| 颜色 | 含义 | 例子 |
|---|---|---|
| 🟦 蓝色 | 复用当前 Cognee 的开源代码 | Loader、Task、Retriever、Graph/Vector Adapter |
| 🟩 绿色 | 采用其他开源项目 | PostgreSQL、pgvector、Docling、Temporal、Qdrant |
| 🟧 橙色 | 平台拥有的业务契约、策略与集成代码 | 租户范围、ID 映射、Evidence、版本发布、适配层 |
| 🟪 紫色 | 可选商业组件；不计入纯开源方案 | Neo4j Enterprise、托管服务套餐 |

蓝色本身也是开源，仅按来源单独区分。橙色不表示从零写数据库、认证系统或工作流引擎，而表示这些通用组件之上的业务语义由平台掌握。

另外使用文字标记集成状态：**已有**＝本地源码存在接点；**需适配**＝需要接口实现/行为验证；**需迁移**＝涉及存量数据和索引版本。图中虚线表示候选或后续演进。颜色不表示性能高低、生产验收完成或所有候选都必须部署。

## 2. 总体架构：平台契约在上，Cognee 与替代实现并列

~~~mermaid
flowchart TB
  Client["业务应用 / SDK"] --> API["平台 API：租户范围、知识库、配额"]:::platform
  Identity["Keycloak / 企业身份系统"]:::oss --> API
  API --> Catalog["平台目录与业务 ID<br/>授权门面、资源绑定、模型配置"]:::platform
  Catalog --> SQL[("PostgreSQL<br/>平台元数据与产物目录")]:::oss
  API --> Port["MemoryBackend 中立契约<br/>提交、检索、删除、能力声明"]:::platform
  API --> Flow["Temporal：可选工作流执行"]:::oss
  Flow --> Port

  Port --> Bridge["CogneeBridge<br/>身份、类型、ID、上下文、错误映射"]:::platform
  Bridge --> Cognee["Cognee Runtime<br/>add / cognify / recall / improve"]:::cognee

  Port -.后续实现.-> Modular["组件化 MemoryBackend<br/>按端口组装解析、抽取、检索"]:::platform
  Modular -.选择.-> Parser["Docling 等开源解析器"]:::oss
  Modular -.选择.-> Search["Qdrant / Milvus / OpenSearch<br/>按职责选择，不要求全部安装"]:::oss
  Modular -.选择.-> Model["vLLM 等开源推理服务"]:::oss

  Cognee --> Adapters["Cognee Adapter + Dataset Handler"]:::cognee
  Adapters --> PG[("PostgreSQL + pgvector")]:::oss
  Adapters --> Graph["图后端：见图存储选项表"]:::platform

  API --> Assets["平台原件、版本、来源与修改记录"]:::platform
  Assets --> Blob[("Ceph RGW / 既有对象存储")]:::oss
  Bridge --> Assets
  Modular --> Assets

  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
~~~

这是目标逻辑图，非当前已部署拓扑。Identity 和对象存储的绿色指所列开源实现；选商业托管服务时按紫色理解。图后端单列是因为当前可用性与开源/商业版本边界需要区分。

关键边界是橙色的 MemoryBackend 与 CogneeBridge。业务不导入 Cognee 的 DataPoint、DocumentChunk、Dataset ORM、SearchType；公共知识库 ID、作业状态和证据格式由平台定义。适配层负责翻译，租户策略和业务编排仍放在平台服务内。

这种做法属于防腐层/端口适配器设计：将外部系统的模型与语义限制在转换边界内。它可以是进程内模块，不必为了“解耦”增加远程调用。[架构依据：Anti-Corruption Layer](https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer)

首版保持三个主要应用部署单元即可：API/查询、导入/维护 Worker、资源控制任务。解析、分块、抽取和 Adapter 先做代码模块；出现独立扩缩容、GPU、故障域或依赖冲突需求时再拆服务。

## 3. 哪些可以复用，哪些有开源替代

| 能力 | 可复用实现 | 外部开源选项 | 平台需要做的事与集成状态 |
|---|---|---|---|
| 身份认证 | 🟦 Cognee 现有用户和认证 | 🟩 Keycloak，或既有 OIDC 身份系统 | 🟧 subject 到内部 User 的稳定映射；外部登录不自动替换 Cognee ACL。**需适配** |
| Dataset 授权 | 🟦 Principal、Role、ACL、授权 resolver | 🟩 OpenFGA，复杂关系授权时评估 | 🟧 授权门面与单一权威，迁出时处理投影/撤权一致性。**已有 + 需迁移** |
| 长任务调度 | 🟦 Task 链、PipelineRun、补偿逻辑 | 🟩 Temporal | 🟧 Workflow/Activity、幂等、产物 checkpoint、Dataset 写协调。**需适配** |
| 文档解析 | 🟦 LoaderEngine、use_loader、DoclingLoader | 🟩 Docling | 🟧 ParsedDocument、页码/表格/坐标映射。Docling **已有集成**，中立结构输出**需适配** |
| 分块 | 🟦 TextChunker、SDK chunker 注入 | 可用外部分块算法实现同一契约 | 🟧 Chunk 身份、策略版本、Evidence。换算法通常**需重建** |
| 图抽取/摘要 | 🟦 Graph Task、graph_model、自定义 Task | 🟩 开源模型与推理服务提供计算；抽取策略仍需编排 | 🟧 本体、规范图、实体归并、证据和结果持久化。**需适配** |
| LLM / Embedding | 🟦 配置、阶段覆盖、兼容 endpoint | 🟩 vLLM 等推理服务 | 🟧 模型 profile、预算、结构化输出兼容；更换 embedding 空间**需重建** |
| 向量存储 | 🟦 LanceDB/PGVector Adapter、dataset handler | 🟩 PostgreSQL + pgvector；Qdrant / Milvus 候选 | 🟧 adapter、handler、能力测试与迁移。PGVector **已有**；其他候选不能视为已兼容 |
| 图存储 | 🟦 Graph Adapter/handler，现有 Ladybug、Neo4j 等接点 | 🟩 Apache AGE 为候选；🟪 Neo4j Enterprise 是商业选项 | 🟧 图能力、provenance、遍历和多租路由适配；见下节 |
| 检索算法 | 🟦 Retriever 与 use_retriever | 🟩 OpenSearch 可承担关键词/向量混合检索 | 🟧 Candidate/Evidence、分数约定、查询预算。OpenSearch **需适配** |
| 跨库融合/回答 | 🟦 可复用单库检索与 completion | 外部 reranker/推理实现可接入 | 🟧 跨 Dataset 去重、重排、统一回答与证据。**平台新增** |
| 原件/中间产物 | 🟦 文件存储适配，已有 S3 路径 | 🟩 Ceph RGW 的 S3 兼容 API | 🟧 平台对象目录、版本、保留/删除。兼容性要按所用 API 验证 |
| 会话/反馈 | 🟦 SessionManager、SQL/其他 cache backend | 🟩 PostgreSQL 可承载持久会话 | 🟧 tenant 范围、事件顺序、导出迁移；会话还可能关联 QA 向量 |

选型依据：Keycloak 提供标准认证协议；OpenFGA 提供细粒度关系授权；Temporal 提供工作流/Activity 执行；Docling 提供文档结构解析；vLLM 提供兼容模型 API。这些能力有官方实现，但不能据此推断当前 Cognee 已无缝接通所有能力。[Keycloak](https://www.keycloak.org/)、[OpenFGA](https://github.com/openfga/openfga)、[Temporal](https://docs.temporal.io/activities)、[Docling](https://github.com/docling-project/docling)、[vLLM API](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/)

**Docling 已经接入，优先利用现有接口。** supported_loaders 在依赖可导入时注册它；add 支持 preferred_loaders。当前 Loader 主要转换并保存纯文本；如果需要布局、表格和证据坐标，需要补结果映射，不能宣称换成该 Loader 后这些信息已经全部贯通。[注册位置](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/loaders/supported_loaders.py#L45)、[当前输出](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/loaders/external/docling_loader.py#L115)

**Qdrant、Milvus、OpenSearch 的状态不同。** 当前仓库 Qdrant catalog 指向外部社区包；Milvus 在工厂注释中被列为社区适配；本次未发现本地 OpenSearch 数据库适配实现。因此均需核查具体版本和多租户 handler；不能把社区名称当兼容性测试通过。[Qdrant 外仓引用](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/catalog/entries/packages/qdrant.yaml#L15)、[Vector 工厂](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/create_vector_engine.py#L146)

### 图存储的开源与商业边界

| 选项 | 状态 | 对本方案的意义 |
|---|---|---|
| 🟦 现有 Ladybug 接入 | 已有嵌入式图路径 | 可按 Dataset 固定单写 owner，设计受控执行和故障恢复；不能任意多 Pod 共享文件写入 |
| 🟩 Neo4j Community | 开源版本；单实例一个标准 database | 不能直接承接“一实例多个 Dataset database”的现有 Neo4j handler 方案 |
| 🟪 Neo4j Enterprise | 商业选项，具备多 database 能力 | 接近既有 handler 模型；仍需配置角色、资源限制、HA 与迁移 |
| 🟩 Apache AGE | 开源 PostgreSQL 图扩展候选 | 需要新适配并验证图查询/来源删除；不等于仓库的 postgres_demo adapter |
| 🟦 postgres_demo adapter | 当前源码明确标为 demo | 可作实现参考，不能仅因 PostgreSQL 成熟就推断此图实现已达到生产目标 |

依据：[Neo4j 版本与数据库数量](https://neo4j.com/docs/operations-manual/current/database-administration/)、[Apache AGE](https://age.apache.org/)、[Cognee postgres_demo 声明](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/postgres_demo/adapter.py#L1)。

本轮没有证明存在一个同时满足“完全开源、现有 Cognee 多租路由直接使用、已完成目标规模集群验收”的图后端组合。图存储保留为明确的适配/验证工作包，避免架构图掩盖工程量。

## 4. 组件替换位置：中立端口与具体实现分离

下图是演进目标。橙色端口由平台定义；蓝/绿实现通过适配进入端口。当前 Cognee 各模块尚未全部符合这些中立契约，建立转换层就是需要做的工作。

~~~mermaid
flowchart LR
  Parse["DocumentTransform<br/>输入原件，输出文档与块"]:::platform --> CL["Cognee Loader / Chunker"]:::cognee
  Parse -.结构化结果适配.-> DL["Docling"]:::oss

  Extract["KnowledgeExtractor<br/>输入块，输出规范图与证据"]:::platform --> CG["Cognee Graph / Summary Tasks"]:::cognee
  Extract -.后续策略.-> EG["平台抽取策略<br/>调用开源模型服务"]:::platform

  Embed["EmbeddingService<br/>固定模型空间"]:::platform --> CE["Cognee EmbeddingEngine"]:::cognee
  Embed -.兼容接口.-> VE["vLLM 对应模型服务"]:::oss

  Retrieve["Retrieval<br/>输入查询，输出候选与证据"]:::platform --> CR["Cognee Retriever"]:::cognee
  Retrieve -.混合检索策略.-> OS["OpenSearch"]:::oss

  VS["VectorIndex<br/>向量、过滤、ID 与 payload"]:::platform --> CP["Cognee PGVector Adapter"]:::cognee
  VS -.新适配及迁移.-> QD["Qdrant / Milvus"]:::oss

  GS["GraphStore<br/>节点、边、来源、子图能力"]:::platform --> CA["Cognee Graph Adapter"]:::cognee
  GS -.新适配及验证.-> AGE["Apache AGE"]:::oss
  GS -.商业选项.-> NE["Neo4j Enterprise"]:::commercial

  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
~~~

实现选路应由 Dataset 的处理 profile、数据版本、产品能力及资源目录决定。每个进程启动时注册插件，再按请求选择已注册实现。use_loader / use_retriever 修改模块级注册表，不适合在并发租户请求中反复覆盖同名实现。[Loader 注册](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/loaders/use_loader.py#L6)、[Retriever 注册](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/register_retriever.py#L6)

### 现有接点与必须补的适配

| 接点 | 已有代码 | 可复用程度与限制 |
|---|---|---|
| Loader | use_loader + LoaderInterface | 显式注册；返回 LoaderResult/派生文本位置，不能直接返回任意 DTO |
| Chunker | cognify 的 chunker 参数 | SDK 注入类；需要适配 Document、DocumentChunk、ID 和元数据 |
| Graph extraction | graph_model、内部 callback、自定义 Task | 可以扩展，但不是完全中立、稳定的 provider 插拔协议 |
| Retriever | use_retriever + BaseRetriever | 需匹配对象/context/completion、构造参数与可选 session/evidence 能力 |
| Graph / Vector | use_graph_adapter / use_vector_adapter | 可注册新后端，但实际数据契约不止方法签名 |
| Dataset provisioning | use_dataset_database_handler | 与 adapter 是独立注册；BAC=true 还会校验 handler/provider 匹配 |
| LLM / Embedding | provider/model/endpoint 配置及阶段配置 | 兼容 API 可接入；替换整个模型访问层仍需内部适配与缓存处理 |
| Session | CacheDBInterface + 具体工厂分支 | 是业务存储接口，不能以普通 get/set cache 替代全部语义 |

代码依据：[Chunker 注入与默认任务](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/cognify/cognify.py#L109)、[抽图回调](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/graph/extract_graph_from_data.py#L198)、[Retriever 工厂](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/search/methods/get_search_type_retriever_instance.py#L414)、[Graph 契约](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/graph_db_interface.py#L27)、[Vector 契约](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/vector_db_interface.py#L11)、[Handler 注册](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/dataset_database_handler/use_dataset_database_handler.py#L4)。

## 5. 平台需要拥有的数据：避免接口换了，数据仍被锁定

仅把 Cognee API 包一层 HTTP，只解决调用入口，尚未解决数据语义和迁移。应定义下列平台模型，并逐阶段保存可交换的产物。

| 平台数据 | 稳定含义 | 与 Cognee 的关系 |
|---|---|---|
| Tenant / Dataset | 安全范围、业务知识库 ID | 映射内部 UUID；不让物理库名成为业务 ID |
| Document / DocumentVersion | 业务文档身份、不可变内容版本 | 映射 SQL Data ID；重试与新版本明确区分 |
| SourceArtifact | 原件 URI、对象版本、checksum、MIME | 平台先保管；不能只引用 Worker 临时路径 |
| ParsedDocument / ChunkManifest | 文本、结构、坐标、顺序、解析与分块版本 | 通过适配层转换 Document/DocumentChunk |
| CanonicalGraphArtifact | 规范实体、关系、属性、本体及抽取版本 | 映射 DataPoint 图；保存抽取输出，避免迁移时必须再次调用 LLM |
| Evidence | 事实到文档版本/块/页码/字符或时间区间 | 显式保留来源与多来源归属 |
| EmbeddingProfile / VectorBatch | 模型版本、预处理、维度、归一化、metric、向量 | 固定向量空间，便于直接迁移同模型向量 |
| IndexRevision / Binding | 图、向量、来源与模型的一组可见版本 | 平台决定发布；物理索引作为实现细节 |
| Session / Feedback / GraphEdit | 用户交互、反馈、人工修订 | 属于需保留的业务记录，不能都当成可丢弃派生索引 |

ID 与内容 hash 各有职责。相同文本不代表同一文档、同一租户或同一次业务操作；实体名字相同也不能自动跨文档/租户归并。平台保存稳定 ID、处理版本和 implementation_id 映射，重建时保留来源链。

对象产物可用版本化 JSON/JSONL 加大对象引用保存；不把整个 Python 对象图放进队列。每份 manifest 记录 schema_version、checksum、producer/profile version、scope、输入与输出引用。平台模型保留扩展字段与原始产物，无法无损转换的能力必须记录限制。

**这不是现有 Cognee 的完整导出能力声明。** 首版至少保存原件、请求配置、操作和 ID 映射；按抽离阶段新增规范产物与 provenance exporter。未实现完整图/会话/人工修改导出前，不能承诺全量无损退出 Cognee。

### 当前最容易漏掉的五种耦合

1. DataPoint 的 Python 类型名和 index_fields 参与向量 collection 命名；CHUNKS 检索固定使用 DocumentChunk_text。因此自己的 MyChunk DTO，甚至改名的子类，都不自动兼容。
2. 默认 chunk ID 依赖 document_id、精确文本 hash 与相同内容出现次数。改文档 ID 或分块边界会影响引用和重跑语义。
3. 默认 chunk task 会按 document.id 更新 SQL Data.token_count；不能只有一个外部文档字符串而没有相应内部映射/记录。
4. 来源信息包括 DocumentChunk 私有属性和嵌套对象；普通 JSON/model_dump 不保证保留全部 provenance。
5. Vector 命中的 ID 要能定位图节点；摘要 payload 还会引用原 chunk。只搬向量数值会丢失完整检索能力。

证据：[DataPoint 与类型](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/engine/models/DataPoint.py#L94)、[索引命名](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/storage/index_data_points.py#L39)、[chunk ID](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/chunking/chunk_id.py#L15)、[SQL token_count 更新](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/documents/extract_chunks_from_documents.py#L44)、[私有来源属性](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/chunking/models/DocumentChunk.py#L60)、[provenance 捕获](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/provenance/edge_evidence/capture.py#L14)。

因此有两条合理路径：保留内部模型，编写经过验证的双向转换；或者把强耦合的一段整体替换成平台管线。不能用一个字段重命名器承诺任意 chunk、graph、retriever 随机组合。

## 6. 最小接口契约：规定行为，不只规定方法名

以下为目标端口，不是现有公共 API，也不要求每行部署成独立服务。

| 端口 | 输入 → 输出 | 必须约定的行为 |
|---|---|---|
| MemoryBackend | InputManifest / QuerySpec / DeleteSpec → operation_ref、答案或上下文、Evidence | 首版粗粒度封装；声明能力；区分 accepted、committed、failed |
| SourceStore | 原件或受控引用 → immutable ArtifactRef | 校验、不可变版本、幂等写入、按引用/保留策略回收 |
| DocumentTransform | ArtifactRef + parser/chunker profile → 文档与 ChunkManifest | 坐标单位、编码、顺序、稳定身份、处理版本；不隐式修改共享图 |
| KnowledgeExtractor | ChunkManifest + ontology/model profile → GraphArtifact + Evidence | 保存实际抽取结果；实体归并策略显式化；重试复用已完成产物 |
| EmbeddingService | 文本引用 + 固定 profile → VectorBatch | 返回实际模型、维度和预处理信息；同空间检查；计量未知状态可对账 |
| GraphStore | GraphArtifact / 子图请求 / 来源删除 → receipt 或子图 | 稳定节点边身份、方向、多来源归属、能力声明、幂等和写入协调 |
| VectorIndex | 向量、ID、payload / 查询与过滤 → receipt 或 candidates | 不可省略租户/Dataset 范围；明确距离方向、索引版本、按 ID 删除 |
| Retrieval | query + 授权 bindings + snapshot + budget → RankedEvidence / Context / Answer | 预算、总超时、来源、失败策略；context-only 与 answer 分开协商 |

身份/授权门面与 SessionStore 另作为平台服务边界，不强塞入某个检索器。业务入口只依赖这些平台类型；未来换另一个 RAG 框架，也只让它进入实现侧。

共同执行封套至少包含：actor_id、tenant_id、dataset_id、operation_id、attempt_id、权限范围、route_revision、目标数据版本、deadline。写操作增加 idempotency_key、request_digest 和期望版本；相同 key 不同内容应冲突，不能悄悄覆盖。

每个实现返回 Capability：支持的过滤、图查询、批量大小、来源删除、导出、版本读取、条件写/fencing 等。缺少必需能力应拒绝部署组合或显式降级；不能悄悄扫描全图来模拟高效邻域查询，再声称能力与性能等价。

“方法相同”并不意味着可替换。例如 Cognee Vector 协议约定分数为距离且越低越好，外部返回 similarity 时须转换并验证；不同 embedding 空间或不同检索器的原始分数也不宜直接混排。[Vector 返回契约](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/vector_db_interface.py#L19)

GraphStore 不应把任意 Cypher 当通用合同。按需要定义节点/边、邻域、投影、路径等能力，并保留供应商扩展；否则换数据库时上层查询语言依赖仍然存在。

## 7. 身份、权限和资源目录也要可替换，并保持单一权威

平台开始拥有自己的用户、租户和知识库模型后，不能让两个系统各自授权、各自建库，最后依赖双向同步碰运气。

| 信息 | 推荐过渡方式 | 后续完全解耦 |
|---|---|---|
| 外部身份、业务 Tenant/Dataset ID | 🟧 平台目录为权威，稳定映射到 🟦 Cognee 内部记录 | 替换 MemoryBackend 后业务 ID 不变 |
| Dataset ACL | 🟧 AuthorizationPort 首版代理 🟦 Cognee ACL，所有 grant/revoke 经过同一入口，Cognee 暂为唯一权限权威 | 切到 🟩 OpenFGA 或平台授权实现；Cognee 如仍需要 ACL 则作为版本化投影 |
| 数据库放置与开通 | 选择一个 provisioning 控制器；DatasetDatabase 保存其分配的绑定 | 🟧 BindingResolver 映射多个后端；不同 handler 消费同一资源目录 |
| Workflow 状态 | 采用 🟩 Temporal 时以其为调度权威，平台 Job 表仅业务视图 | 更换工作流引擎需要处理在途实例和重放，不是只换 URL |
| Session/Feedback/人工修订 | 首版可委托 🟦 Cognee 保存，但必须定义导出、范围和保留契约 | 🟧 平台业务记录权威，检索器只消费所需视图 |

采用平台授权权威后，平台入口必须实时按既定一致性策略检查；Cognee 不应保留可绕过平台的公网操作/授权入口。投影落后时不能继续使用旧授权放行撤权请求；定义权限版本与失败拒绝策略。授权从 Cognee 迁出需要修改其 ORM 读路径或维护受控投影，外部 IAM URL 本身不会替换这些逻辑。

同一账号不同租户请求应持有不可变 ExecutionScope，不能在每个请求中修改共享 User.tenant_id 或全局配置来模拟切换。涉及 Cognee 默认行为的地方可能需要小范围内部适配；包装一个 API 类不会自动改变这些语义。[当前活动租户持久化](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/tenants/methods/select_tenant.py#L32)、[当前上下文恢复](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L380)

### 不同存储实现下，租户隔离如何保持

🟧 DatasetBinding 使用平台逻辑身份，解析为后端命名空间：PG schema、Qdrant collection/受控 payload scope、Milvus collection/partition 等。命名空间策略可以不同，但所有查询、按 ID 读取、批量操作、统计、导出、删除均需执行同一 scope。

Qdrant 提供 payload 与自定义 shard 等组织方式，Milvus 提供多种租户划分层级；这些物理组织方式的权限能力不同，不能把 shard/partition 路由本身当成业务 ACL。[Qdrant 多租户](https://qdrant.tech/documentation/manage-data/multitenancy/)、[Milvus 多租户](https://milvus.io/docs/multi_tenancy.md)

共享 collection 实现必须强制注入 tenant_id、dataset_id 与 index_revision 范围，并限制客户端直接访问数据库；更强隔离套餐使用独立库、账号或实例。后端未完成多租户 handler 和行为测试前，不通过关闭 Cognee 的访问控制开关规避集成工作。

SourceStore 使用对象引用和版本，不向业务暴露永久数据库密码或可随意修改的路径。Ceph RGW 可作为开源 S3 兼容实现，但具体所用的 IAM、生命周期和复制能力要逐项匹配，不能把“兼容 S3”理解成全部云产品功能一致。[Ceph S3 API](https://docs.ceph.com/en/latest/radosgw/s3/)

Session 迁移还需处理语义索引：当前 session_embeddings 使用 SessionQAVector_text，scope tag 由 user_id/session_id 构成；只搬 SQL/Redis 会话记录不保证 QA 语义召回完整。平台应保存 QA ID、tenant 范围及索引状态，迁移时同步搬迁或重建。[会话向量实现](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/session/session_embeddings.py#L13)

## 8. 替换成本分级：哪里可以切策略，哪里必须搬数据

| 替换项 | 上层接口是否可保持 | 存量处理 | 必须验证 |
|---|---|---|---|
| 最终答案模型、prompt、reranker | 可以 | 一般不重建检索索引；处理答案缓存 | 质量、证据、结构化输出、成本与延迟 |
| 解析器 | 可以 | 输出变化时生成新解析版本，可能影响分块和下游索引 | 表格/OCR、坐标、文本覆盖、Evidence |
| 分块器 | 可以 | 新 ChunkVersion，通常重抽取/重索引 | ID、重复块、旧引用与删除 |
| 图抽取策略、本体、实体归并 | 可以 | 新 GraphArtifact，可能连带向量重建 | 实体/边语义、共享来源、人工修订 |
| Embedding 模型 | 可以 | 即使同维度也需新向量空间和索引版本 | query/index profile 一致、召回质量 |
| pgvector → Qdrant/Milvus | 可以 | 同模型时可迁移原始向量、ID、payload并重建 ANN；前提是导出/原始写入能力齐备 | 过滤、距离方向、引用完整性、删除、吞吐 |
| 图数据库 | 可以 | 保留 ID、边、属性与 provenance，回填目标图 | 邻域/投影/路径语义、来源删除、性能 |
| Cognee → 其他 MemoryBackend | 可以保持产品 API | 导出规范产物或从原件重建；会话/反馈/人工修改另迁移 | 产品能力差异、完整性、费用、行为兼容 |
| 工作流引擎 | 可以保持业务提交 API | 旧任务排空、分代运行或显式迁移 | 避免两个调度器重复执行同一操作 |

“可以保持接口”是完成本方案后的目标，不表示切配置即可完成所有替换。

当前 embedding 一致性检查主要比较维度；模型版本兼容性必须由平台补齐。保持相同模型、预处理和距离约定时，换向量数据库不必重做图抽取或重新付费计算所有 embedding；换模型则不能沿用旧语义空间。[维度检查](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/utils/ensure_embedding_model_matches.py#L56)

### 一个具体例子：只替换向量组件

~~~mermaid
flowchart LR
  Keep["平台 API / Tenant / Dataset / Document 保持"]:::platform --> Rev["创建新 index_revision<br/>冻结 profile，记录变更水位"]:::platform
  Old["pgvector 旧索引"]:::oss --> Export["向量 + ID + payload + 来源引用"]:::platform
  Export --> New["Qdrant 新索引"]:::oss
  Rev --> New
  New --> Check["租户过滤、引用、召回、删除与性能验收"]:::platform
  Check --> Switch["按 Dataset 切换 Binding<br/>维持旧后端追平以便回滚"]:::platform
  Switch --> Query["Cognee Retriever 可暂时保留<br/>需要新 Vector Adapter 与 Handler"]:::cognee
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

前提是保留 Cognee Retriever 所需的 collection/ID/payload/距离与过滤语义，并实现相应能力。若目标改为平台中立 Retrieval，则可摆脱这些命名约定，但工作量更大。

切换流程应包括快照/回填、增量追平或短暂停写、引用和删除对账、影子查询、路由切换、旧池失效与观察窗口。发布图和向量时绑定同一业务版本，避免新向量引用旧图或已删除对象。

影子查询必须禁止 session 追加、反馈、improve 等业务副作用；若旧入口无法做到，应走经过验证的纯检索路径。影子调用模型仍有成本，记录但不重复计入客户业务用量。

回滚只有在旧后端保持足够新的数据时成立。停止维护旧后端后，需要先重放变更再回切；删除 tombstone 必须传播到旧/新索引，不能通过旧快照复活已删除内容。

## 9. 开源工作流可以复用，但恢复语义仍需设计

🟩 Temporal 可以管理任务派发、重试和持久执行；🟦 Cognee 的 Task 链可以继续运行在 Activity 内。🟧 平台定义阶段产物、幂等键、Dataset 写入所有权、发布协议与错误分类。

把整个 cognify 包成一个 Activity，只能得到该 Activity 边界的重试，不会自动 checkpoint Cognee 内部每个 chunk、生成器或上下文。Activity 必须等待实际处理完成，不能把进程内后台任务提交成功当成数据已完成。只有显式持久化阶段输出，才可逐步拆成 Parse、Extract、Embed、Build 等可恢复 Activity。[Temporal Activity 与重试](https://docs.temporal.io/activities)

同 Dataset 更新、删除、improve 仍须统一写入协调。lease 到期并不证明旧进程已停止；使用存储端 fencing 或隔离的 attempt/revision 产物并安全发布，无法支持时需要确认旧执行环境已隔离再接管。[分布式锁的 fencing 依据](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)

工作流服务和 Adapter 不提供图、向量、对象、SQL 的共同事务。保留幂等、补偿和可见版本协议；不自建一个通用分布式数据库事务系统。

## 10. “性能更强”要通过同条件替换实验确认

本次推荐的是有相应能力的候选，不是已经证明优于 Cognee 的排名。Cognee 是编排与记忆运行时，Qdrant/Milvus 是向量服务，vLLM 是推理服务，不能将不同层产品的宣传吞吐直接比较。

| 瓶颈 | 优先替换/优化位置 | 候选与实验条件 |
|---|---|---|
| PDF/OCR 或复杂表格解析 | DocumentTransform | 🟩 Docling；与当前路径比较文档结构正确率、每页时间和资源消耗 |
| Embedding 吞吐/模型等待 | EmbeddingService | 🟩 vLLM 对应模型服务；同模型、tokenizer、精度和 batch 下比较吞吐、P95、成本 |
| 向量查询/存储容量 | VectorIndex | 🟩 pgvector、Qdrant、Milvus；固定向量、Recall@k、过滤选择性、并发与内存预算 |
| 关键词与语义混合召回 | Retrieval | 🟩 OpenSearch；比较 nDCG/Recall、引用准确性与总延迟；这是检索策略替换，不是图功能等价替换 |
| 大图检索内存或网络开销 | GraphStore + Retriever | 比较服务端邻域/过滤与客户端投影；更换数据库不自动消除 Python 侧图加载 |
| 跨库查询调用数/延迟 | 查询协调与融合 | 🟧 有界 fan-out、上下文合并、统一生成；记录每请求实际 LLM/Embedding 次数 |
| 任务失败重做和停机恢复 | Workflow / Artifact | 🟩 Temporal + 🟧 checkpoint；评测恢复时间和重复工作，不把调度换型当计算提速 |

OpenSearch 官方提供关键词/语义混合及分数或排名融合能力，适合在 RetrievalPort 下作为候选；它不自动替代 Cognee 的来源、会话和图记忆行为。[OpenSearch 混合检索](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/)

评测应同时设置正确性门槛：租户越界为零、删除有效、共享实体来源不误删、版本一致、Evidence 完整。然后在相同质量约束下比较 P50/P95/P99、吞吐、内存、单位文档/请求成本。缓存命中、热冷数据、图大小、Dataset 数和模型限额分别记录；不要通过关闭记忆能力让一个实现看起来更快。

## 11. 建议实施顺序与退出条件

| 阶段 | 具体工作 | 完成标志 |
|---|---|---|
| A：先隔离业务依赖 | 🟧 MemoryBackend、AuthorizationPort、CogneeBridge、业务 ID、原件目录；🟦 Cognee 粗粒度实现 | 业务模块不导入 Cognee 类型；接口、错误、权限与身份映射有契约测试 |
| B：补数据可携带性 | 🟧 版本化文档/块/图/Evidence 产物，Session/Feedback 导出，单一资源目录 | 指定 Dataset 可导出、重建并核对来源；明确仍无法迁移的功能 |
| C：替换收益最高的组件 | 🟩 解析/模型/向量/检索候选，按瓶颈逐个评测 | 新旧实现并存，至少完成一次真实的组件替换和回滚演练 |
| D：深度解耦 | 🟧 分阶段 Workflow、规范图与中立存储/检索；逐步减少 Cognee 内部模型依赖 | 选定产品能力可由非 Cognee MemoryBackend 完整提供 |
| E：规模化与产品等级 | 多存储组、专属池、迁移控制和租户计量 | 多租并发、故障接管、迁移/删除与容量验收通过 |

首版不必同时部署 Keycloak、OpenFGA、Temporal、Qdrant、Milvus、OpenSearch、AGE。沿用已有身份与存储设施；缺少企业登录时引入 Keycloak；长任务恢复需求明确时评估 Temporal；ACL 不复杂时先复用 Cognee；pgvector 已满足需求时不急于换库。

最有价值的第一批平台代码是橙色的接口、ID/Evidence 与版本目录、CogneeBridge、租户作用域和契约测试。它们使蓝色与绿色实现可以在业务不变的前提下演进；数据库、解析器和推理引擎的通用能力继续交给现成项目。

## 12. 本轮证据与交付范围

复用 V2 的数据流/租户/集群结论，新增定向检查 Loader、Chunker、DataPoint、provenance、Retriever、Graph/Vector/Handler 接口、session vectors 和社区适配引用；外部选项只使用官方文档/仓库。没有运行这些候选的集成或性能测试，没有修改业务代码。

原理和实现分别有边界：当前代码支持哪些插件接点由源码证明；候选组件提供哪些能力由官方资料证明；中立接口、迁移方式和推荐阶段属于本报告设计，仍需实现验收。新版颜色也不表示开源组件的商业版本、所有附加插件或模型权重使用同一种许可。

本轮源码阅读与接口细节记录：
- [计算组件与隐性耦合](../evidence/10-replaceable-compute.md)
- [存储组件、handler 与迁移约束](../evidence/10-replaceable-storage.md)
- [中立接口与迁移设计](../evidence/10-component-contracts.md)
- [V2：当前组件、数据流、多租户和集群架构](v2-multitenancy.md)
