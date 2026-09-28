# Cognee 图需求全集与 AGE 改造工作包

基线 Cognee `663a2dc15`。本稿负责“Cognee 真实需要什么、改哪里、怎么验收”，不重复 AGE 内核/版本能力核验。源码只读；未运行数据库、未实施 adapter。所有新文件与工时均为设计/工程估算。

## 一、先固定交付含义

1. **最小跑通**：add/cognify→HYBRID/GRAPH_COMPLETION→删除的受控 PoC；不是全部 Cognee 功能已兼容。
2. **核心 adapter**：所有 abstract 方法有明确实现；业务 ID、CRUD、查询返回值、邻域、图筛选、统计、多 dataset 生命周期正确；`query()` 可明确拒绝开放 raw Cypher，并把 capability 设 false。abstract 需要 override，不等于必须支持任意输入方言。
3. **建议交付的完整业务范围**：核心 + 图来源/共享删除/run 回滚/增量 + feedback/truth/update_node + triplet 分页 +现有 Temporal 路径。这里的“完整”只针对列出的 Cognee 内置图业务，不包含任意用户自定义 Cypher 或 Neo4j 生态。
4. **选配**：开放 AGE raw Cypher、AGE NL、完整昂贵图统计、历史图无来源删除与迁移、用户 custom tasks。分别验收、分别估计；不能在正文笼统合成“AGE 全部支持/全部不支持”。

## 二、接口完整清单：47 方法，21 abstract + 26 非 abstract

已逐行读完整 `cognee/infrastructure/databases/graph/graph_db_interface.py:1–839`，通过扫描类内 def 和 abstractmethod 交叉计数。下表每个方法只计一次。括号为源码行号。

| 分组 | 方法全集 | 数量 | 契约地位 |
|---|---|---:|---|
| 生命周期 | is_empty(78), delete_graph(560) | 2 | abstract |
| 原始查询 | query(84) | 1 | abstract；是否开放 Cypher 由 capability 决定 |
| 节点 CRUD | add_node(97), add_nodes(116), delete_node(141), delete_nodes(156), get_node(484), get_nodes(496) | 6 | abstract |
| 边写/存在/邻接 | add_edge(508), add_edges(535), has_edge(639), has_edges(653), get_edges(666) | 5 | abstract |
| 全图/筛选/子图 | get_graph_data(567), get_neighbors(678), get_nodeset_subgraph(690), get_connections(705), get_neighborhood(719), get_filtered_graph_data(743) | 6 | abstract |
| 图统计 | get_graph_metrics(626) | 1 | abstract；include_optional 是统计深度参数，不是是否实现方法 |
| 来源附加/移除 | attach_node_source_refs(211), attach_edge_source_refs(235), remove_node_source_refs(259), remove_edge_source_refs(280) | 4 | 非abstract，默认抛 UnsupportedProvenanceCapability |
| 来源删除支持 | delete_edge_triples(301), get_node_delete_data(317), get_edge_delete_data(339) | 3 | 同上 |
| 来源索引查询 | find_nodes_by_source_ref(361), find_edges_by_source_ref(375), find_node_source_refs_by_dataset(389), find_edge_source_refs_by_dataset(408), find_node_source_refs_by_pipeline_run(427), find_edge_source_refs_by_pipeline_run(446) | 6 | 同上 |
| 图级标记 | set_graph_metadata(465), get_graph_metadata(479) | 2 | 同上 |
| 成员/增量位置 | remove_belongs_to_set_tags(169), update_chunk_index(190) | 2 | 前者默认 no-op；后者默认抛 UnsupportedGraphOperation |
| feedback | get_node_feedback_weights(757), set_node_feedback_weights(764), get_edge_feedback_weights(811), set_edge_feedback_weights(818) | 4 | 非abstract，默认 NotImplementedError |
| truth | get_node_truth_state(773), set_node_truth_state(780) | 2 | 非abstract，默认 NotImplementedError |
| 局部属性补丁 | update_node(789) | 1 | 非abstract，默认 NotImplementedError |
| triplet 分页 | get_triplets_batch(827) | 1 | 非abstract，默认 NotImplementedError |
| 可视化种子 | get_top_degree_node_ids(573) | 1 | 非abstract，有可工作但读全图的 Python fallback；近似结果符合契约 |
| **合计** | **abstract 21；非abstract 26** | **47** | **“非abstract”不表示开启对应功能时可忽略** |

3 个接口类 capability 是 supports_cypher_queries（默认 True）、supports_per_row_source_refs（默认 False）、supports_incremental_chunk_updates（默认 False），见 interface:58–75。新 adapter 应显式赋值，尤其不应继承 True 后无意开放 NL。improve 另按方法 override 或显式 flag 探测 feedback/truth，见 `modules/improve/capabilities.py:23–24,49–88`；只写返回成功的空壳 override 会让功能误判为可用。

## 三、真实调用与改造矩阵

文件缩写：A=新增 `infrastructure/databases/graph/age/adapter.py`；C=新增 `age/codec.py`；S=新增 `age/schema.py`；H=新增 AGE DatasetHandler。这些是建议文件，并非现有文件。W 编号对应末尾唯一计价工作包，表内不再重复加钱。

| 功能 | 真实调用/方法与证据 | 改造位置与性质 | 验收重点 | 归属 |
|---|---|---|---|---|
| DataPoint/Edge 存图 | `tasks/storage/add_data_points.py:250–346` 调 add_nodes/add_edges；源模型转换保持原样 | A/C 新增；get_graph_from_model、prepare_edges_for_storage、index_data_points 复用 | 同一 UUID 写两次不重复；batch 内去重；缺失端点跳过；保留 edge_object_id；node/edge tuple/dict 格式 | W2 |
| 幂等与字段所有权 | interface:35–49；DataPoint feedback 默认0.5、valid_to=None | A/S 新增状态规则；仅当上层无显式 reset 意图需条件修改数据契约 | 并发 MERGE 无重复；重新 cognify 不清空 feedback/truth/validity；删除的业务字段不残留；明示 reset 可用 | W2/W4/W5 各负责自己的字段，统一规则不重复计价 |
| HYBRID 关系补充 | `modules/retrieval/hybrid/entities.py:60` → get_neighborhood(depth=1) | A 新增，retriever 复用 | 无向扩展/原方向返回；节点集合与诱导边一致；不漏孤立 seed | W6 |
| GRAPH_COMPLETION/其 CoT 等变体 | `CogneeGraph.py:112,124–169,322` → NodeSet/filter/全图/ID-filtered/neighborhood；上层 Python 排序与LLM复用 | A 新增统一读方法与接口外 ID-filtered；默认无需改 retrievers | 不把 AGE graphid 返回为业务 ID；相关ID筛选避免无条件全图；多跳语义一致 | W6 |
| NodeSet/属性筛选/字面检索/代码图检索 | `lexical_retriever.py:56`、`code_retriever.py:863`、`coding_rule_associations.py:39` | A 新增 get_filtered_graph_data/get_nodeset_subgraph；上层复用 | NodeSet OR/AND、属性过滤、端点/边完整性；Code 检索功能不等于要求 Cypher 任意图算法 | W6 |
| 可视化/校验/RDF与导出 | `visualization/subgraph_data.py:137,233,246`；`api/v1/validate/validate.py:236`；`modules/graph/rdf/export.py:137` | A 新增 get_graph_data/neighborhood；建议override top-degree，避免全图fallback；上层复用 | 默认小图视图有界；孤立节点有效；全图导出准确；internal/metadata节点按现有规则处理 | W6 |
| 基础图统计/dataset计数 | `get_datasets_graph_counts.py:86`；`get_pipeline_run_metrics.py:63–80` | A 实现 get_graph_metrics；上层通常复用 | 返回所有约定键；节点/边数、均度、密度、连通分量正确；不把未算值伪装成0 | W6 |
| 来源/forget/run rollback | interface:211–480；`unified/provenance_delete_planner.py`；`cognify/rollback.py:158–193` | A/S 新增持久化；provenance状态转换及删除规划复用；必要时条件改能力检查 | 一个实体多来源删一份仍保留；run只移除本run新增归属；图对象与来源同事务；图与vector失败恢复 | W4 |
| chunk增量/NodeSet清理 | `api/v1/update/incremental.py:187,388`；`delete_data_nodes_and_edges.py:105` | A 新增 narrow update_chunk_index、remove_belongs_to_set_tags；验证连接返回shape再开flag | retained chunk只改位置；四类来源一致；NodeSet标签与边一致 | W4 |
| feedback四接口 | `tasks/memify/apply_feedback_weights.py:268–276` 以函数引用传入 | A 新增，应用公式复用；若要跨进程CAS需条件改 apply task/契约 | getter仅found；setter逐ID bool；edge按edge_object_id；缺省0.5；并发读算写不丢更新 | W5 |
| truth两接口 | `modules/truth_subspace/build.py:409`；`retrieval/hybrid/truth.py:60` | A 新增；epoch发布/检索门禁复用 | 坐标与epoch一起读写；部分失败与现有发布协议一致；无效类型/缺失对象；重建不擦状态 | W5 |
| update_node/事实关闭/偏好 | `tasks/storage/close_node.py:37`；`modules/user_preferences/store.py:107`（不支持会fallback full upsert） | A 新增patch；调用者复用 | 不存在返回False；未指定属性不动；偏好可更新；valid_to时间语义保持 | W5 |
| memify triplet索引 | `tasks/memify/get_triplet_datapoints.py:223` | A 新增 get_triplets_batch；task/indexer复用 | offset/limit稳定分页、格式一致、无遗漏/重复；当前hasattr不保证非stub，必须实测 | W6 |
| Temporal搜索 | `temporal_retriever.py:131–144` → collect_time_ids/collect_events | A **额外新增接口外两方法**；既有时间抽取与向量部分复用 | 现有Timestamp→Event方向、范围、缺失日期fallback一致；不是重新设计interval-overlap | W5 |
| dataset隔离/连接生命周期 | `get_graph_engine.py:344–354`；handler interface:11–76 | A/C/S/H 新增；use_graph_adapter和use_dataset_database_handler复用；若内置provider再修改工厂/config/依赖 | PG database与AGE graph名不混；owner+dataset绑定；cache关闭/连接重建；并发隔离；权限不越界 | W3，连接执行基础归W2 |
| raw AGE Cypher | `cypher_search_retriever.py:45,58` | A query与返回列协议新增；API能力门禁条件修改 | 仅承诺定义的AGE方言/返回协议；值参数化；超时/错误/实体解码 | O1 |
| AGE自然语言查询 | `natural_language_retriever.py:84–111,139,171`；Neo4j prompt:1,15,27 | 新AGE prompt；修改schema注入/方言选择；必要时raw/NL能力细分 | 真实node/edge schema进prompt；限制允许方言；不生成APOC/GDS；语料回归；未知语句明确失败 | O2 |
| 用户自定义Task/DataPoint | `modules/memify/memify.py:28–29,95–126`；Task:316–340可执行用户callable | 使用标准接口者复用；自行调用query者逐条移植；不能统一承诺 | 盘点项目自定义任务与Cypher清单；每条模板结果等价 | O5，按实际清单估 |

说明：update_node对偏好有fallback，但对事实有效期不是“随便缺失也无影响”。Graph completion的CoT/decomposition `self.get_triplets_batch(queries)` 是retriever自己的批处理方法，不能与 graph adapter 的 `get_triplets_batch(offset,limit)` 混数。

## 四、接口外需求：不能漏，也不能全部算默认必需

| 接口外方法/行为 | 调用者 | 范围判断 |
|---|---|---|
| get_id_filtered_graph_data | `CogneeGraph.py:144–156`，不存在则get_graph_data | 建议纳入核心查询优化；不实现会走全图fallback，功能可跑但大图代价不可忽略 |
| collect_time_ids / collect_events | `temporal_retriever.py:131–144` | 选择TEMPORAL就需要，非所有默认检索都需要 |
| get_document_subgraph / get_degree_one_nodes | `legacy_delete.py:72,111,117`；dataset delete:282–301先ledger/graph provenance再legacy | 只为无来源历史图legacy删除；全新AGE图强制provenance后可不纳入基础包。迁旧图时必须迁来源或实现兼容 |
| get_successors / get_predecessors / get_disconnected_nodes | `tasks/chunks/remove_disconnected_chunks.py:21–34` | 导出的可选Task，仓内搜索只定位定义与__init__导出，未找到默认pipeline调用。旧Task使用has_chunk/next_chunk与uuid shape，不能声称仅实现3方法就完成当前模型适配；用户选用时另验 |
| add_nodes_with_vectors / add_edges_with_vectors | `tasks/storage/add_data_points.py:330,341` hybrid分支 | 图向量统一后端专属分支；AGE+独立PGVector不需实现，不可误列为基础图接口 |
| push_to_s3 | `modules/pipelines/operations/run_tasks.py:265–267` | 完成run前按hasattr触发的后端持久化钩子；AGE无需声明不存在的文件上传方法，数据库提交仍须在写方法完成前落实 |
| query / engine私有访问的历史迁移 | Ladybug provenance migration:45–85；PG provenance migration:60–95；rekey_fork_document_ids:139–146 | 有provider/adapter guard，不能把所有Ladybug SHOW/ALTER语法都列成AGE必须兼容；新AGE需自身schema版本/迁移，跨后端数据迁移另计 |
| query健康探测 | `api/v1/health/health.py:125–128` | 先is_empty，只有无is_empty才fallback query；实现标准adapter不需要为了health开放任意Cypher |

本轮对 modules/tasks/api/unified 内 graph_engine/graph_db 等调用名做了定向搜索；用户未提交的任意项目插件无法凭Cognee仓库审计。自定义Task允许自行拿engine调用query，所以兼容报告必须把“任意扩展查询”保留为未知清单，而不是宣称不存在。

## 五、GDS/APOC的精确边界

**get_graph_metrics 是公共接口，GDS 是 Neo4j 的实现选择。** `neo4j_driver/adapter.py:2333–2348` 即使include_optional=False也先drop/project GDS图，再算wcc连通分量；不能写“GDS只在可选高级metrics触发”。dataset图计数真实调用 `get_graph_metrics(False)`。但这不意味着AGE必须提供gds.*：PG demo `adapter.py:879–904` 用节点/边读取和Python连通分量计算返回同一字典，已经证明上层依赖输出而非GDS调用协议。

基础字典应包括 num_nodes、num_edges、mean_degree、edge_density、num_connected_components、sizes_of_connected_components。optional键 num_selfloops、diameter、avg_shortest_path_length、avg_clustering 也应存在；include_optional=False可按现有-1约定。若不实现昂贵指标，需文档化-1/不支持语义，不能说“与Neo4j所有指标相同”。`get_pipeline_run_metrics.py:71–80`直接用下标读取所有键；本轮找到其定义与导出，未找到常规API路由直接调用，不能冒充它是默认每次cognify必经步骤。计数路径则是明确活动调用。

Neo4j graph_exists/project_entire_graph/drop_graph/get_node_labels/get_relationship_labels_string（adapter:2213–2313）不在47方法内，仓内外部业务调用搜索未找到直接使用；它们支撑Neo4j自身metrics，不必在AGE里复制这些方法名。GDS算法可按功能产出重写，任意GDS procedure兼容不属于Cognee迁移要求。

| Neo4j依赖 | 真正业务需求 | AGE设计替代（需目标版本回归验证） |
|---|---|---|
| apoc.coll.toSet（:384） | belongs_to_set去重并集 | Python批次去重 + 数据库端集合状态更新/受控事务；不要裸read→write丢并发更新 |
| apoc.create.addLabels（:392） | 保留类型可查 | 固定CogneeNode + type属性，或明确单标签映射；无需复制APOC动态标签过程 |
| apoc.merge.relationship（:1241） | 按source/target/name幂等建边，动态关系类型 | 按关系名分组、验证/转义标识符、固定MATCH+MERGE+SET模板；值走参数；端点缺失跳过 |
| gds.graph/wcc（:2228,2292,2346） | 统计连通性 | 原生查询+专用连通分量算法/受控应用计算；实现结果契约，不实现Neo4j procedure协议 |
| prompt中的apoc.algo/Neo4j 4.4 | 自然语言生成可执行图查询 | 独立AGE prompt/语法门禁，不修改默认graph检索算法来迁就Neo4j方言 |

以上“可重写”是架构方案，不是已经运行通过的SQL；AGE具体支持版本由版本能力表另证。

## 六、全Agent交付的范围与预算入口

本文方法清单与W1～W7归属保留，原人工人日估算撤下。项目采用Agent完成实现、局部测试、独立评审、端到端验证、修复及文档，详见[统一报告第6章](../graph-storage-postgres-analysis.md#agent-cost)。

基准为1协调/集成＋2实现＋1独立验证/评审Agent，最多4并发。基础范围初始预算3～6自然日、80～170 Agent占用小时；是按依赖和返工余量安排的未校准预算，不是传统人日除以提速倍数。W1真实执行后须以Agent占用、工具/数据库等待、token和返工记录重估。

W1验证参数与身份合同；W2连接/codec/CRUD；W3schema/index/handler；W4来源/回滚/增量；W5反馈/truth/局部更新/时间；W6图读/基础统计/分页；W7全链路与故障、代表性性能验证。三种APOC行为替代和基础连通分量计算均在对应基础包内，不重复计费。

O1公开raw Cypher、O2自然语言图查询和O3有界数据的完整昂贵统计另列Agent预算；旧图迁移、用户自定义过程、多版本维护、生产运维和AGE内核修改按实际范围另评估。静态源码与expected文件不替代目标环境运行证据。

## 阅读范围与证据强度

本轮完整精读GraphDBInterface（全部47方法）、get_pipeline_run_metrics、remove_disconnected_chunks、improve capabilities；精读Neo4j metrics/helpers 2213–2370、PG metrics 879–904、CogneeGraph 124–159、用户偏好80–135、health115–136、dataset delete275–301、triplet task194–267、Task316–340与PG provenance migration1–95。跨 modules/tasks/api/unified 搜索 graph对象方法、query、接口外方法及GDS调用；其他构建、feedback、truth、provenance核心函数复用前轮已核证行号。本轮未研究AGE内核，未宣称所有custom plugin均可用。
