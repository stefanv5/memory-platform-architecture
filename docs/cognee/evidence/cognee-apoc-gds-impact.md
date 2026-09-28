# Cognee 内置 APOC / GDS 的具体影响与替代工作

固定 Cognee SHA：`663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`。只读源码审计，没有连接 Neo4j/AGE 或执行迁移。本文假定已经补上 PG 驱动与 AGE query 桥接，再讨论“把现有 Neo4j Cypher 原样交给不提供 APOC/GDS 的后端”会在哪里失败；实际直接替换连接串还会先遇到 Bolt/SQL 协议、初始化 Neo4j CREATE CONSTRAINT 等障碍（`neo4j_driver/adapter.py:213–218,245–280`）。

## 1. 全仓搜索结果：实际只有这些内置依赖

在当前 checkout 根目录执行 `rg -l -i '\bapoc\.|\bgds\.' .`，按 rg 默认规则覆盖仓内可搜索文本（未搜索.git和被忽略的生成物），命中四个文件：

1. `cognee/infrastructure/databases/graph/neo4j_driver/adapter.py`：3种APOC调用、3种GDS graph管理调用。
2. 同目录 `neo4j_metrics_utils.py`：4种GDS算法调用。
3. `cognee/infrastructure/llm/prompts/natural_language_retriever_system.txt`：建议LLM使用 `apoc.algo.*`，不是固定执行某个APOC算法。
4. `neptune_driver/adapter.py:1324–1329`：仅占位docstring写GDS projection不支持，没有执行调用，不能算一个额外运行时依赖。

因此当前内置执行路径是 **3种APOC函数/过程，7种GDS过程**。APOC文本执行点共5处：toSet/addLabels各2处（单节点与批量），merge.relationship一处。GDS过程名称固定写在query文本；动态插入的是 graph_name 等参数，不是从配置任意拼选GDS算法。本结论不覆盖用户仓外插件、自定义Task或用户输入的任意Cypher。

## 2. APOC：影响默认入图，不只是高级算法

默认调用链：`remember` 的持久化路径 → `cognify` → 默认Task `add_data_points`（`api/v1/cognify/cognify.py:607`）→ `_write_nodes_grouped`（`tasks/storage/add_data_points.py:250–263`）→ Neo4jAdapter.add_nodes；随后 `_write_edges_grouped` → add_edges（`:340–346`）。单节点方法也是公开图接口，但该默认批量写链路主要走add_nodes/add_edges。

| 具体依赖 | 源码执行点与实际用途 | 原样迁移失败及用户结果 | 替代实现与成本 | 上层改动 |
|---|---|---|---|---|
| `apoc.coll.toSet` | adapter:328–331（add_node）、384–387（add_nodes）；将旧+新belongs_to_set列表去重，避免共享节点丢标签 | 写节点statement无法成功执行；普通cognify在图持久化阶段失败，知识没有完成入库。不是只丢一个可有可无的展示标签 | 用集合并集替代。应用内批次去重对K个输入标签平均O(K)时间/空间；数据库已有值的合并还需事务/锁或数据库端原子表达式，不能拆成无保护read→write。读大列表和同节点争用是成本 | 保持add_nodes与属性合同则cognify无需改；adapter必须重写 |
| `apoc.create.addLabels` | adapter:335、392；给公共 `__Node__` 节点追加模型类型标签，如Entity、TextDocument | 同一节点写statement失败，而不是节点成功仅少标签；如果简单删去该调用而仍沿用后端查询，按`:Entity`等类型查询又会漏数据 | 选择固定公共label＋type属性，或按类型固定单label；按类型分组构造安全模板，不要求动态APOC过程。成本是类型/查询映射维护与按组批次，不能机械保留Neo4j多标签假设 | adapter内部所有依赖label的读同步改；标准图API调用者可复用。现有raw Cypher/NL若写死`:Entity`需改 |
| `apoc.merge.relationship` | adapter:1238–1255（add_edges）；两个端点已MATCH，关系名由每条输入指定，以source_node_id/target_node_id身份MERGE；随后SET在create/match都更新属性 | 节点/节点向量可能已经写完，边阶段失败，关系图不完整；不能说整个cognify跨库自动回滚为无痕 | 按relationship_name分组，用固定类型的MATCH+MERGE+SET；端点UUID和属性走参数，关系名经过验证/转义。E条边、R种关系约O(E)分组，查询批次数按R及batch大小增长；真实lookup/写代价取决索引与并发，不仅是语句长度 | 保持三元组身份和EdgeData返回合同，上层add_data_points无需改；原始查询兼容另议 |

一个重要证据：**单边** add_edge已经使用普通 `MATCH ... MERGE ... ON CREATE SET ... ON MATCH SET`，没有APOC（adapter:1153–1171）。这说明apoc.merge.relationship主要承担批量输入里关系类型动态选择与merge封装，不是Cognee独有算法只能靠APOC实现。AGE是否支持这些确切语法由版本专项核验；不可因此把单边Neo4j模板不加验证就当通用AGE模板。

Neo4j add_nodes还在Python端按node ID合并批内重复输入及tags（adapter:396–431）；数据库上的toSet负责与**已存**状态合并。两层都要保留，单纯应用端对本批去重不能替代数据库当前状态并集。

**标签替代的合同边界：**普通DataPoint初始化会把type强制设为Python类名，但允许显式指定id（`infrastructure/engine/models/DataPoint.py:94–106`）。默认Neo4j写先按公共`__Node__`+id合并，再用addLabels添加当前type，写路径没有移除旧类型标签。如果同一显式id先后以不同模型type写入，当前type属性可能已改变，而旧Neo4j标签仍保留；不能把这层多标签积累语义无成本等价为一个type字符串。固定AGE label方案应选择：明确禁止业务ID跨类型复用，或维护独立labels集合并在adapter查询中解释它；选择后写入验收范围。普通get_filtered/get_nodeset等接口可以重写查询保持约定结果；用户原始`MATCH (:Entity)`、多label谓词和labels(n)返回不能自动保持语法/结果兼容，须迁移查询或列为不支持。这里不是要求普通默认文档一定会跨type重写，而是指出当前接口并未禁止显式ID输入，不能假定永不发生。

发生上述异常时，Neo4j query记录后重新抛出（adapter:270–280；add_edges:1273–1282）。pipeline错误路径会尝试调用rollback_handler、记录错误、yield PipelineRunErrored（`modules/pipelines/operations/run_tasks.py:292–350`）。rollback本身失败只记日志，因此不能承诺失败后绝无残留。HTTP后台模式可能先返回已受理，最终job失败；同步/流式调用如何暴露错误依执行模式，不应一概写为HTTP500。

`add_data_points.py:317–346`明确图先于对应向量：节点写失败则不会进入该批节点向量写；边写失败时该批节点和节点向量可能已完成。这解释为什么要保留来源和补偿，而不能把APOC缺失当成一次无副作用的查询错误。

## 3. GDS：7个固定过程分别影响什么

`Neo4jAdapter.get_graph_metrics(include_optional=False)` 是公共接口的实现。它在False时也无条件执行 `get_model_independent_graph_data → drop_graph → project_entire_graph`（adapter:2333–2336），并计算WCC（:2341–2349）。因此不能把GDS全部归为“只有高级指标打开才需要”。

| GDS过程 | 执行点/调用顺序 | 缺少时原样执行的影响 | AGE adapter真正需要替代的功能 |
|---|---|---|---|
| `gds.graph.list` | adapter:2228；graph_exists，供drop_graph(:2311)及project_entire_graph(:2283)调用 | metrics会在drop_graph检查阶段先失败，即使include_optional=False | 不必复制GDS投影注册目录；新metrics实现若直接计算就不需要该操作 |
| `gds.graph.drop` | adapter:2312；投影存在才调用 | 原metrics无法清理旧投影、无法继续完成统计 | 不必复制该过程；只需管理自己的临时算法状态生命周期 |
| `gds.graph.project` | adapter:2292–2296；按节点label与关系类型建立UNDIRECTED投影，GraphMetadata label被排除(:2286–2289) | 无法创建后续算法所需投影 | 用图拓扑读取/邻接结构等新路径；定义业务节点/边集合，避免metadata计入业务统计；不需新AGE图复制一份持久数据 |
| `gds.wcc.stats` | metrics_utils:61–66；adapter:2346 | 无法得到num_connected_components | BFS/DFS或并查集计算弱连通分量 |
| `gds.wcc.stream` | metrics_utils:93–100；adapter:2347–2349 | 无法得到sizes_of_connected_components | 同一次分量计算同时导出数量与size降序列表，不必重复算两遍 |
| `gds.allShortestPaths.stream` | metrics_utils:153–159；仅include_optional=True，adapter:2353–2359 | 无法得到diameter、avg_shortest_path_length；函数先取此数据，其失败也阻止本次其他optional指标返回 | 按所选无权/无向图口径计算所有源最短距离并聚合；需要定义不可达点、自环、多重边处理并与参考结果比对 |
| `gds.localClusteringCoefficient.stats` | metrics_utils:185–191；仅include_optional=True，adapter:2360 | 无法得到avg_clustering | 三角形/邻居连边计数与局部聚类系数聚合，按参考图口径验收 |

count_self_loops（metrics_utils:122–127）和edge_density（:26–37）使用普通MATCH/count，**不依赖GDS**。但它们嵌在get_graph_metrics总体顺序中，前面GDS失败时整次metrics方法仍不返回字典；不能因此把自环count也写成必须有GDS算法。

include_optional=False时，返回num_selfloops/diameter/avg_shortest_path_length/avg_clustering的-1占位（adapter:2362–2368）。False也包含节点/边数、mean_degree、edge_density和两个连通分量字段。完整字典消费者 `modules/metrics/operations/get_pipeline_run_metrics.py:63–80` 直接按键读取，因此替代方法要保留键和约定值。

## 4. 用户到底看到什么：必须区分3条读取路径

### 4.1 图摘要API不是一遇到GDS错误就整体失败

活动调用链1：`GET /v1/datasets/graph-summary`（router:700）→ `get_datasets_graph_counts`（`api/v1/datasets/routers/get_datasets_router.py:759`）→ 冷cache时 `_count_and_cache` → get_graph_metrics(False)。

活动调用链2：`GET /v1/visualize/brains-summary`（`api/v1/users/routers/get_visualize_router.py:430`）→ visualization的聚合读取 → get_datasets_graph_counts（`api/v1/visualize/visualize.py:460–462`），最终node_count来自计数（:471）。

**实际异常处理**：`get_datasets_graph_counts.py:83–91` 捕获graph/metrics异常，记录warning并返回 `DatasetGraphCounts(pipeline_run_id=...)`；其默认值为num_nodes=0、num_edges=0、computed_at=None（:30–45）。该分支在缓存写入前return，不会把错误的0写成成功缓存，后续请求可再次重试。单元测试 `tests/unit/modules/data/test_get_datasets_graph_counts.py:163–191` 专门覆盖一个图不可读时该dataset降级0，其他dataset保留正常结果。本轮未执行该测试。

因此GDS缺失的用户症状可能是：图摘要返回成功，但这个dataset显示0节点/0边、computed_at为空；brains-summary显示node_count=0。不能写成“所有查询失败”或“图数据被删了”。若命中已有计数缓存（get_datasets_graph_counts:182–190），不再调用图metrics，用户可能暂时仍看到已缓存计数；此时不能据界面正常证明GDS兼容。

图摘要router外层也有409处理（router:760–774），但graph metrics错误已经在单dataset层捕捉，该错误通常不会触发这层409；关系库读取等其他异常另当别论。

### 4.2 直接完整metrics调用会失败

`get_pipeline_run_metrics.py:63`直接await get_graph_metrics，没有包住该调用的异常降级；故调用者遇到GDS缺失会收到异常，不返回完整统计。本轮全仓搜索找到此函数定义、导出与测试，没有定位到正常API路由对它的直接调用，不能宣称它是“每次cognify必经步骤”。

### 4.3 普通检索不是GDS算法调用者

HYBRID/GRAPH_COMPLETION等通过get_neighborhood、get_filtered_graph_data、get_graph_data等公共读接口取图；当前内置调用搜索没有把GDS放入它们的召回/排序链。已有数据且这些读方法正确适配时，没有GDS并不天然阻断回答。默认入图却仍需要解决APOC写入依赖，两者不能混为一个结论。

## 5. 不用GDS如何实现：算法成本必须单独承担

下述复杂度是建议替代算法的理论上界/平均口径，不是本轮测得耗时，不是在断言GDS内部执行复杂度。V/E为参与算法的业务图节点/边数。

| 目标产出 | 可选替代算法 | 成本与实施限制 | 能否不改业务上层 |
|---|---|---|---|
| 节点数/边数、均度、密度、自环数 | 图上count；按公式派生统计 | exact count可能扫描；公式本身O(1)；不能因为公式简单就认为取数无成本 | A.get_graph_metrics保持字典可复用上层 |
| 弱连通分量数＋size | 一次构建无向邻接，再BFS/DFS | O(V+E)时间与邻接空间；size排序O(C log C)，C为分量数；也可并查集近线性并减少部分邻接存储。全图取到Python会增加网络与对象内存 | 可不改上层；PG demo已有Python _component_sizes范例（adapter:124–147、879–904），是功能参考不是规模保证 |
| diameter/平均最短路 | 在无权图上逐源BFS，边计算边聚合距离 | 简单实现O(V(V+E))时间，单次BFS辅助O(V+E)；若保存所有距离则O(V²)级结果。现Neo4j helper把所有distance收成Python list(:158–159)，迁移可避免这种全量结果，但需保持统计口径 | 可以保留method输出，调用者不改；若要近似/采样必须显式改变功能合同，不能冒充精确值 |
| avg_clustering | 邻接集合内枚举邻居对并检查相连、聚合局部系数 | 朴素实现约O(Σd(v)²)候选检查、O(V+E)邻接空间；高阶节点昂贵。可优化为邻接交集等算法，但不能宣称免费/自动由VLE解决 | 输出合同不变则不改；精确的孤立节点、多边/投影口径必须回归 |
| 仅UI摘要希望很便宜 | 给summary路径新增专用轻量计数入口，绕过完整metrics | 可避免“只要两个数却做WCC”；这是上层优化，需要修改counts调用/接口，不是只换底层就自动获得 | **需要条件性改动**，与完整metrics保留并行存在；不能把get_graph_metrics(False)的WCC悄悄删掉却仍声称完整等价 |

GDS graph list/drop/project只是Neo4j算法投影管理。AGE适配层如果自行读取拓扑计算，不需要模仿这些过程或建立可查询的GDS兼容目录。真正成本在取图、存邻接、执行WCC/全源距离/三角形统计。基础metrics必须有结果，昂贵optional指标可以按当前合同不计算并返回-1；如果用户要求完整optional统计，就计入算法与性能验收。

## 6. NL与原始Cypher：第三种影响，不是默认GDS依赖

prompt `natural_language_retriever_system.txt:1,15,27`要求Neo4j 4.4+，并建议shortestPath()/apoc.algo.*。这个建议可能让LLM生成目标后端没有的调用，**不表示每个自然语言问题必调用APOC**。自然语言检索 `natural_language_retriever.py:127–162` 对生成/执行错误捕捉并重试，全部失败返回[]；schema读取发生在循环外(:123)，其失败不走这个重试分支。不能笼统写“APOC缺失直接导致NL HTTP500”。

用户显式CYPHER检索直接执行其query（`cypher_search_retriever.py:58`）；异常被转换为CypherSearchError（:61–65）。两条路径都先检查supports_cypher_queries（Cypher:45；NL:171）。默认graph retrieval无需这些Neo4j prompt。AGE方案可以先明确关闭公开query capability，或增加AGE方言prompt、schema及受限query协议，保持标准图API业务可用。

任意用户提供的APOC/GDS语句不能用上述10种内置依赖清单兜底。自定义Task可执行任意callable（Task.execute_coroutine/execute_function:316–340），项目外查询必须另列清单评估。

## 7. 唯一性是业务数据合同，与“有没有APOC过程”分开验收

GraphDBInterface:35–46定义：节点ID为str(DataPoint.id)；边唯一身份是 `(source_id,target_id,relationship_name)`；重复upsert不增加节点/边；缺失端点应跳过；输出业务tuple/dict。edge_object_id是派生的反馈寻址属性，不是允许同三元组出现无限条边的第四维。

| 当前实现证据 | 它证明什么 | 它不能证明什么 |
|---|---|---|
| Neo4j initialize:217–218：公共label的id UNIQUE约束 | 节点业务ID不仅靠应用if不存在判断；factory initialize见get_graph_engine:179 | 不证明AGE也有同一DDL，或不同label任意重复ID可接受 |
| Neo4j add_nodes:396–431批内去重 | 同一batch重复node ID不会作为两次独立状态重写丢tags | 不单独证明跨进程并发唯一；数据库约束/事务仍需正确 |
| Neo4j add_edges:1239–1253 | 按端点+动态关系类型及端点身份属性merge，更新create/match两路径 | 没有据此审计出“任意并发条件下边唯一”的完整数据库证明；AGE方案应自己设置并发防线 |
| PG demo tables.py:30；:42–56 | node PK(id)；edge复合PK(source_id,target_id,relationship_name)，端点FK+CASCADE | 不是以AGE graphid替换业务ID的理由 |
| PG demo adapter.py:46–94；:362–368、484–495 | Python批内按业务身份去重；ON CONFLICT以业务主键更新 | 不能把某个顺序MERGE回归等同数据库并发唯一约束 |
| PG demo adapter.py:104–117 | 当前demo以事务advisory lock串行图写，且仍有PK约束 | 不是必须照搬到AGE的性能方案，也不是只有加全局锁才能实现唯一性 |

特别校准：接口规定缺端点跳过，但PG demo add_edges当前直接INSERT，表有端点FK，没有在:484–497看到跳过过滤；不能用该实现证明缺端点合同已经满足。此处只引用其业务主键与ON CONFLICT作为身份设计证据。

适配验收必须至少覆盖：同UUID两并发事务、同边三元组重复批次、源/目标相同但关系名不同、方向反转、missing endpoint、重试/死锁、删除与upsert并发。唯一性冲突应产生明确重试/业务处理，不可悄悄复制节点再让向量命中同UUID的多个图实体。AGE侧采用何种表达式索引/登记表/锁，由AGE专项设计并实测；本文不从Cognee源码替它背书。

## 8. 结论与工作量归属

当前内置业务中，**必须替代**的是APOC承载的节点标签/集合和批量边写，以及基础metrics的统计产出；不是必须实现APOC/GDS兼容库。**选配替代**的是include_optional=True的全源最短路/聚类统计，以及公开NL/raw Cypher方言。**无法统一承诺**的是用户任意APOC/GDS算法与外部Task。

这些工作已分别在上一稿W2（CRUD/codec）、W4（来源和字段归属）、W6（基本metrics）计价，不能因这次把过程名展开而再次整体加钱。工作量现按[统一报告第6章](../graph-storage-postgres-analysis.md#agent-cost)的全Agent口径计量，旧人工人日不再适用。O3（完整昂贵统计）的Agent预算只适用于固定有界数据、明确统计口径的实现与回归，不包含目标大图SLA优化；O1/O2负责公开查询。若用户需要新增轻量summary计数接口，才新增小范围上层改造并在PoC后重估；数据库扩展本身缺失属于部署阻断，不由adapter Agent预算覆盖。

实际精读范围：Neo4j adapter 213–280、306–440、1125–1171、1205–1282、2213–2370；neo4j_metrics_utils全部191行；来源/存储add_data_points 250–346；run_tasks错误292–350；dataset counts完整文件（前轮与本轮核对）、summary router750–786及visualize455–477；对应counts失败单测163–191；NL121–185与Cypher检索异常段；PG tables27–61、prepare46–117、upsert348–380/471–511；DataPoint60–125。未宣称全仓逐文件阅读；全仓文本搜索仅用于排查内置APOC/GDS调用落点。
