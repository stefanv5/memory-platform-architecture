**本报告解释 Cognee 哪些模块依赖图、图里存什么、如何交互、为什么 PG 能替换基础图后端，以及替换边界。**

基线：Cognee 1.6.0，源码 commit `663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`；核对日期 2026-09-24。静态代码分析与官方文档交叉核验，未连接数据库或调用 LLM。示例中的实体、业务边名和描述为示意，真实抽取依赖模型、prompt、ontology。

**1. 先区分存储职责**

| 层 | 典型对象 | 职责 |
|---|---|---|
| 原文件存储 | 上传文件、原文位置 | 保存文件内容，图中 Document 可引用路径 |
| 关系存储 | data、datasets、users、ACL、运行信息 | 文件登记、归属、权限与任务管理；部分 provenance 也在这里 |
| 向量存储 | DocumentChunk_text、Entity_name 等 collection | 按相似度召回文本、实体、关系描述 |
| 图存储 | 文档、块、摘要、实体、类型与有向关系 | 表达结构、读取关联子图、管理知识来源和生命周期 |
| 会话缓存 | 会话问答、trace 等 | 会话记忆，不是每条会话都即时写永久图 |

[Data.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/data/models/Data.py#L11) 保存 raw_data_location、owner_id、dataset_id、content_hash、pipeline_status 等。`remember(session_id=...)` 先写 session cache，按配置再桥接永久图；永久记忆路径才是 add→cognify→可选 improve。见 [remember.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/remember/remember.py#L1801) 与 [永久记忆路径](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/remember/remember.py#L1886)。

[官方架构](https://docs.cognee.ai/core-concepts/architecture)也区分关系、向量和图的角色，并说明内容可能交叉存储。因此，关系数据库的 nodes/edges 来源记录，不应与 PG 图后端的 graph_node/graph_edge 混同；更不能把设置 DB_PROVIDER=postgres 当作已把图后端切到 PG。

**2. 哪些模块为什么依赖图**

| 模块 | 写入/读取的数据与操作 | 依赖图的原因 | PG 可替换程度 |
|---|---|---|---|
| api/v1/cognify + tasks/graph | 抽取实体、类型、实体关系，关联文档块 | 将孤立文本组织成可连接的知识 | 通过后续公共存储接口可写 PG |
| modules/graph/utils + tasks/storage | DataPoint 递归转换为节点/边；add_nodes、add_edges | 保留对象引用、有向关系和来源 | PG 已实现基础持久化 |
| modules/retrieval/hybrid* | Entity 向量命中 ID → 一跳邻域 | 给原文召回补充实体关联事实 | PG 支持 |
| graph_completion_retriever + CogneeGraph | 子图投影、三元组排序、生成上下文 | 综合节点、边语义选出关联知识 | PG 支持基础路径；规模需压测 |
| modules/improve | 图节点/边反馈权重、truth state、事实维护 | 让知识随反馈变化 | 当前 PG 部分接口缺失 |
| modules/graph/methods + unified/provenance_delete_planner | 查来源归属、去来源引用、删无主节点/边 | 删文档时保留其他文档仍支撑的共享知识 | PG 有 provenance 实现 |
| tasks/code_graph + CodeRetriever | 模块、函数、调用、导入、路径 | 精确依赖分析、路径和影响范围分析 | 相关路径使用共同图操作，需验证性能 |
| Cypher/NaturalLanguage/Temporal retrievers | 原始查询或时间专用接口 | 提供特定图查询能力 | 当前 PG 存在明确缺口 |

图的意义不仅是“实体之间画一条线”。它还把事实与原文、类型、集合、来源生命周期联系起来。向量距离能回答“哪些内容相似”，节点与边还能表达“谁与谁有什么关系、由哪些文档支撑”。

**3. 实际图里保存什么**

以输入文档“张三负责项目 X。项目 X 使用系统 Y。”为例：

```mermaid
flowchart LR
  S["TextSummary：摘要"] -->|made_from| C["DocumentChunk：原文片段"]
  C -->|is_part_of| D["Document 子类：project.txt"]
  C -->|contains| A["Entity：张三"]
  C -->|contains| P["Entity：项目 X"]
  C -->|contains| Y["Entity：系统 Y"]
  A -->|is_a| T1["EntityType：person"]
  P -->|is_a| T2["EntityType：project"]
  Y -->|is_a| T3["EntityType：system"]
  A -->|"responsible_for（示意）"| P
  P -->|"uses（示意）"| Y
```

`made_from/is_part_of/contains/is_a` 是模型与转换代码确认的关系；业务边名称和实体划分只是示意。没有断言这句话实际执行后必定得到这张图。

| 模型 | 重要节点属性 | 与其他节点的关系 |
|---|---|---|
| Document 具体子类 | name、raw_data_location、mime_type、external_metadata | 文档块指向它 |
| DocumentChunk | text、chunk_index、chunk_size、content_hash、document_id/name | is_part_of→文档；contains→实体 |
| TextSummary | text、source_chunk_id、importance_weight | made_from→文档块；可选 summarized_in |
| Entity | name、description、importance 及可选状态字段 | is_a→EntityType；业务关系→其他实体 |
| EntityType | name、description | 作为实体的类别 |
| NodeSet | 分组信息 | belongs_to_set，需实际 NodeSet/DataPoint 引用才产生边 |

模型证据：[Document.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/data/processing/document_types/Document.py#L9)、[DocumentChunk.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/chunking/models/DocumentChunk.py#L32)、[TextSummary](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/summarization/models.py#L22)、[Entity.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/engine/models/Entity.py#L5)。

这里 Entity 的模型类型是 `Entity`，人的语义类别由 `is_a→EntityType(person)` 表达。不要把所有实体节点的模型 type 都写成 Person。自定义 Person(DataPoint) 是另一种建模方式。

**4. 文档如何变成图：对象模型是关键中间层**

永久记忆写入的主流程：

```text
remember()
  → add()：登记/加载原始数据
  → cognify()：分类、切块、抽图和摘要
  → extract_graph_from_data()：抽取逻辑实体与关系
  → expand_with_nodes_and_edges()：转 Entity/EntityType/Edge 对象
  → TextSummary.made_from 指向原来的 DocumentChunk
  → add_data_points()：递归展开对象
  → 图 adapter：写节点和边
  → 向量 adapter：索引指定字段和关系文本
  → 写入/补充来源信息
```

[TextChunker.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/chunking/TextChunker.py#L40) 创建 DocumentChunk，保存 text，设置 is_part_of，初始化 contains。LLM 的抽取结构先是带局部 ID 的 nodes/edges；[expand_with_nodes_and_edges.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/graph/utils/expand_with_nodes_and_edges.py#L19) 将它们转成正式 Entity/EntityType，并将边端点解析成最终节点 ID。

摘要任务生成 TextSummary，其 made_from 指向原 chunk。[summarize_text.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/summarization/summarize_text.py#L60)因此保证从摘要对象仍能访问文档块及其知识结构。

[模型转换器](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/graph/utils/get_graph_from_model.py#L87)按字段展开：

- 普通字符串、数字、字典等 → 节点属性。
- 引用其他 DataPoint 的字段 → 对方节点与一条边。
- DataPoint 列表 → 多条边。
- 显式 `Edge(relationship_type=...)` → 用指定关系名；不会把所有业务关系统称为 relations。
- 根据节点 ID、边身份去重，并避免循环对象重复展开。

例如 `summary.made_from=chunk` 生成 made_from 边；`chunk.is_part_of=document` 生成 is_part_of 边；`entity.relations=[(Edge(relationship_type="uses"), target)]` 生成 uses 边。这与[官方自定义模型说明](https://docs.cognee.ai/guides/custom-data-models)一致。

**为什么它支持替换：业务知识在应用层已转成节点、边和属性。数据库 adapter 不需要重新理解文档或重新调用 LLM，只需要持久化并查询这些统一结构。**

**5. 图和向量如何连接**

通常同一个 DataPoint 在图和向量里保留同一 ID。向量索引复制节点对象时不重新生成 ID，因此命中的实体 UUID 可直接用于图查询。见 [index_data_points.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/storage/index_data_points.py#L53)、[PGVector 索引写入](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py#L489)。官网也解释了[DataPoint 的共享 ID 与索引字段](https://docs.cognee.ai/core-concepts/building-blocks/datapoints)。

| 向量 collection / PG 表 | 内容 | 关联方式 |
|---|---|---|
| DocumentChunk_text | 文档块可嵌入文本、正文及来源 payload | id 对应 chunk 节点 |
| TextSummary_text | 摘要 | source_chunk_id 对回原文块 |
| Entity_name | 实体可嵌入内容 | id 对应实体节点 |
| EntityType_name | 实体类别 | id 对应类型节点 |
| EdgeType_relationship_name | 关系检索文本 | 由文本生成索引 ID，不能直接当图边实例 ID |

索引字段由模型 metadata 指定；collection 名由类型和字段组成，具体 embedding 文本由模型的 get_embeddable_data 生成，不能仅凭表名断言只嵌入该字段的原始字符串。

**关系向量有不同规则。** [index_graph_edges.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/storage/index_graph_edges.py#L39)按 edge_text（缺省时关系名）聚合成 EdgeType 索引对象。具体图边身份则是源、目标、关系名及派生的 edge_object_id。关系文本相同的多条图边可能关联同一关系文本索引点。因此“所有向量 ID 都是图节点 ID”不成立。

ID 的稳定性也不等于完美消歧：Entity 默认按名字构造确定 ID，但同一抽取图中重名实体另有区分逻辑；不能据此保证不同文档中的现实同名人物都会正确分开。

**6. 默认检索怎样与图交互**

当前 search 默认 HYBRID_COMPLETION；recall 还有会话及路由逻辑，并非每次都读永久图。

普通 HYBRID 查询流程：

```mermaid
sequenceDiagram
  participant U as 用户
  participant R as HybridRetriever
  participant V as 向量库
  participant G as 图适配器
  participant L as LLM
  U->>R: 项目 X 由谁负责？
  R->>V: 查询向量：原文、摘要、实体、关系文本
  V-->>R: 命中内容、ID、距离与来源
  R->>G: get_neighborhood(实体IDs, depth=1)
  G-->>R: 节点列表、边列表
  R->>R: 排序原文与关系，拼接上下文
  R->>L: 原文片段 + 实体关联事实 + 问题
  L-->>U: 回答
```

准确调用点：[hybrid_retriever.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/hybrid_retriever.py#L136)、[entities.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/hybrid/entities.py#L43)。

- chunk lane 查 DocumentChunk_text 和 TextSummary_text，摘要可帮助找回和重排原文。
- entity/fact lane 查 Entity_name 和 EdgeType_relationship_name。
- Entity 命中 IDs 是图邻域查询起点；实际调用 depth=1。
- 图返回关联节点与边，例如 项目X←responsible_for—张三、项目X→uses—系统Y。
- 关系文本命中用于排序和补充事实；它不直接取代图中的端点关系。
- [context.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/hybrid/context.py#L8)将原文 passages、实体关系 bullets、facts 和可选 global context 拼接给 LLM。当前 formatter 不直接渲染 chunk_summaries；摘要在此主要辅助召回与排名。

邻域读取失败会降级为实体无边，所以“接口有回答”不代表图关系确实参与成功。上线验证应检查 retrieved_objects/context 和日志。

GRAPH_COMPLETION 则更侧重三元组：向量候选→读取子图→应用内 CogneeGraph→综合源节点/边/目标节点距离与权重选 top-k→转文本→LLM。证据：[CogneeGraph.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/graph/cognee_graph/CogneeGraph.py#L480)。这部分排序在 Python，基础路径不要求数据库执行 PageRank、Cypher 或图神经网络。

**7. 为什么 PG 可以接替：同一操作、同一返回结构**

上层取得图对象的入口是 get_graph_engine。工厂根据全局或当前 dataset 配置返回不同 adapter；业务层使用共同方法。

```text
构建/检索/删除模块
     ↓ add_nodes / add_edges / get_neighborhood / delete_nodes
get_graph_engine() + GraphDBInterface/共同adapter协议
     ├─ LadybugAdapter → 图查询
     ├─ Neo4jAdapter  → Cypher
     └─ PostgresDemoAdapter → SQL
     ↑ 统一 Python tuple/dict
```

[GraphDBInterface.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/graph_db_interface.py#L16)约定返回形状：

```python
Node = (node_id, properties)
EdgeData = (source_id, target_id, relationship_name, properties)
```

接口说明还规定字符串 ID、幂等 upsert、删除关联边、批量来源信息等契约。get_id_filtered_graph_data 虽是多个 adapter 都实现的共同方法，但当前抽象类未显式声明它；不能把所有公共调用都说成抽象接口强制定义。

以示意 ID（实际为 UUID）说明 PG 的物理映射：

| 表 | 示例行 |
|---|---|
| graph_node | id=A，name=张三，type=Entity，properties={description: ...} |
| graph_node | id=P，name=项目X，type=Entity，properties={...} |
| graph_node | id=T，name=person，type=EntityType，properties={...} |
| graph_edge | source_id=A，target_id=P，relationship_name=responsible_for |
| graph_edge | source_id=A，target_id=T，relationship_name=is_a |

[PG 表定义](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/postgres_demo/tables.py#L27)保留方向、类型和属性；源/目标列是外键，边身份是三列复合主键。节点写入用 ON CONFLICT 按 ID 更新，边按其复合身份更新。

实体向量命中 P 后，PG adapter 的一跳边查询为：

```sql
SELECT source_id, target_id, relationship_name, properties
FROM graph_edge
WHERE source_id = ANY(:ids) OR target_id = ANY(:ids);
```

这里 :ids 是 SQLAlchemy 绑定参数，值为命中的节点 ID 集合。再收集返回边的端点，按 ID 取 graph_node，组装上述 Node/EdgeData。证据：[adapter.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/postgres_demo/adapter.py#L663)。

检索器收到的仍是“哪些节点通过哪些边连接”，后续排序和回答逻辑继续复用。多跳时 PG adapter 在 Python 做 BFS，每跳执行有端点过滤的 SQL；Neo4j/Ladybug 可以在图查询中展开路径。语义可相近，执行代价不同。[官方 Adapter 指南](https://docs.cognee.ai/guides/graph-engine-adapters)也介绍了这个工厂和适配器分层。

**8. 来源与删除为什么同样需要图协议**

结构来源链是 Summary→Chunk→Document；另有精细来源引用，记录节点/边由哪些文档、块、运行产生。

例如文档 A、B 都说明“项目 X 使用系统 Y”。删除 A 时，如果直接按可达节点删除，会把 B 仍需使用的实体和关系一起删掉。正确流程是：

1. 查该文档支持过哪些节点与边。
2. 对仍由其他来源支持的对象，只移除 A 的来源引用。
3. 对失去全部来源的对象，删除其向量记录及图结构。
4. 保留文档 B 的事实与引用。

代码通过统一 provenance 删除计划完成：[provenance_delete_planner.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/unified/provenance_delete_planner.py#L90)。PG 用来源数组和相关查询/更新方法支撑这些操作。这里不能用“删几个向量”替代图及来源管理。

同一 PG 实例也不自动保证所有步骤在同一个事务。add_data_points 分步骤调用图写、向量索引、来源捕获，各适配器有自己的提交边界；本报告未审计全部故障补偿，不能宣称跨存储完全原子。

**9. 独立代码图：更接近“依赖图”的用途**

如果关注代码仓库依赖，相关模块是 tasks/code_graph：

- 模型包括 CodeRepository、CodeModule、CodeSymbol、ApiEndpoint、StorageResource、ExternalDependency；属性有 file_path、line、repo、fact_properties。
- enola 提取事实后转 DataPoint，动态 calls/imports 等关系通过 add_edges 写入。
- CodeRetriever 读取按代码类型过滤的图快照，在应用层执行 traverse、find_path、impact_analysis 等。
- 该路径默认不需要向量索引，index_vectors 是 opt-in；其 completion 返回结构结果，不调用 LLM 生成答案。

证据：[代码图模型](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/code_graph/models.py#L29)、[任务列表](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/code_graph/extract_code_graph.py#L976)、[代码检索](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/code_retriever.py#L857)。这又说明部分图算法在应用层，后端提供结构即可；但快照加载的规模代价必须验证。

**10. 哪些模块不能直接等价替换**

| 模块/能力 | 当前 PG 状态 | 原因 |
|---|---|---|
| 核心图构建、邻域、子图、来源删除 | 已实现基础接口 | 普通 SQL 可表达且 adapter 已实现 |
| CYPHER / NATURAL_LANGUAGE（NL→Cypher） | 不支持 | supports_cypher_queries=False；query 未实现 |
| TEMPORAL 的时间过滤路径 | 有明确缺口 | 直接调用 collect_time_ids/collect_events，PG 无这些方法 |
| improve 的图反馈权重、truth state | 跳过相应阶段 | PG 未实现扩展方法，能力探测返回不支持 |
| close_node 写 valid_to | 不支持该更新路径 | PG 没有通用局部 update_node |
| 全部边界行为完全一致 | 不能保证 | 例如缺少端点时 PG 外键行为与某些 adapter 跳过行为不同，未实测 |

时间检索代码证据：[temporal_retriever.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/temporal_retriever.py#L124)。它只有未抽取到时间或时间查询为空时才转三元组；缺方法并没有在此捕获降级。因此 PG 支持 timestamp 列，并不能证明 Cognee TEMPORAL 已适配。

[官方 Graph Stores](https://docs.cognee.ai/setup-configuration/graph-stores)及本地 README 均将开源 PG 图后端标为 demo，生产 PG 图适配器另行授权。官网通用 Adapter 指南部分文字提到 PG raw query 可执行 SQL，但与本版本 query() 实际未实现不一致；本报告以代码和专门能力声明为准。

性能也不随接口统一而相同：PG k-hop 有多次 SQL 往返；PG 图写固定 advisory lock 会让同库 schema 写入竞争；PGVector create_vector_index 实际只建表，未自动创建 ANN 索引。可替换应分别验收功能语义、数据保真、性能与生命周期。

**11. 落到华为云，判断变成具体能力映射**

| Cognee 使用方式 | 所需 PG 能力 | 华为云结论 |
|---|---|---|
| 节点/边属性存储 | 普通表、JSONB、数组、外键、upsert | RDS PG 基础能力适配路线成立 |
| 子图、一跳/多跳 | 按端点 ID 过滤、索引、事务 | 能承载该实现；吞吐延迟需实测 |
| 向量召回 | vector 扩展、余弦距离、维度匹配 | 官方提供 pgvector，核实实例小版本 |
| dataset 按 schema 隔离 | CREATE schema/table/index 等权限 | 可用当前 shared handlers，需授权 |
| 高级缺失接口 | 需要 Cognee adapter 代码实现 | RDS 本身不会自动补齐 |

[华为云 pgvector 文档](https://support.huaweicloud.com/usermanual-rds-pg/rds_09_0062.html)可确认扩展支持；[插件安装说明](https://support.huaweicloud.com/usermanual-rds-pg/rds_09_0043.html)说明按目标业务库安装及管理员要求。图部分不依赖 Apache AGE。

替换已有系统还需要数据迁移。改 provider 只改变目标 adapter，不会自动搬运旧图或改完已有 dataset 的注册配置。当前有 COGX 导出/导入，可将图导出后在目标后端恢复，并核验向量索引、dataset 权限和来源。见 [export.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/migration/export.py#L237)、[cogx_archive.py](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/migration/sources/cogx_archive.py#L22)。它不是整个数据库物理备份，本次未执行跨后端迁移。

建议将验收按实际模块列出：默认 HYBRID 问答、GRAPH 三元组、文档删除与共享事实、代码图、时间检索、反馈学习分别验证。华为云 PG 可以承载基础图模型；是否够用最终取决于业务会调用哪些 Cognee 模块，而不是只检查能否连上 PostgreSQL。
