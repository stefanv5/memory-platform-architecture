# Hindsight vs Cognee 数据流深度对比（代码级）

> 方法：双路子代理并行走读两个仓库的当前 HEAD（Hindsight v0.10.1 / Cognee tag `v1.6.0-3-g663a2dc15`），逐阶段记录入口函数、file:line、数据形态、同步/异步边界、失败重试行为。本文所有断言均可回溯到代码位置。
> 规模：Hindsight 引擎 `hindsight_api/` ~156K 行（329 个 Python 文件，api-slim 测试 ~6,628 个）；Cognee 包 `cognee/` ~196K 行（2,443 个 .py 文件，~771 个测试文件）。
> 姊妹文档：[01 五方对比](01-hindsight-vs-competitors.md)（宏观）· [03 RSI 调研](03-rsi-memory-evolution-deep-dive.md)（进化机制）——本文专注**数据怎么流**。

---

## 1. 全景：两条主干先看懂

### 1.1 Hindsight：一条流式写入管线（LLM 嵌在写入里）

```
POST /banks/{id}/memories
  │ 准入控制 + 验证器 + memory defense 扫描（40+ 正则，redact/block）
  ▼
┌─ sync 路径：直接进 orchestrator ─────────────┐
└─ async 路径：async_operations 表（父/子操作行）─→ worker poller 认领
                                                    (SKIP LOCKED + serialization_key)
  ▼
orchestrator.retain_batch
  │ 文档身份解析 / append CAS / delta retain（哈希 diff）
  ▼
iter_chunks 形态感知分块（JSON轮次/JSONL行/句子递归/图片预算）
  │ chunk_id = bank_doc_index（转义防碰撞）
  ▼
producer ──每 chunk──→ LLM 抽取(五维事实+fact_type+因果序数) ──→ 生成嵌入 → asyncio.Queue(字节预算)
  ▼
consumer（3 阶段协议）
  P1 事务外：pg_trgm 实体候选 + 加权评分 + union-find + ANN
  P2 单事务：documents→chunks→memory_units(嵌入)→unit_entities→时间/因果链接
  P3 事务外：entity_cooccurrences + 全库语义 ANN 收尾
  ▼
retain 完成钩子（同请求内提交）
  ├→ submit_async_consolidation ──→ worker ──→ 巩固（observations+证据链+去重仲裁）
  │                                                └→ 级联触发 mental model 刷新
  ├→ graph_maintenance（补链/剪枝队列）
  └→ webhook_delivery（outbox = async_operations 表本身）
```

### 1.2 Cognee：一条解耦的 ETL 流水线（LLM 在 cognify 里）

```
add() ──哑存储──→ 文件存储 + SQLite Data/Dataset 行（无 LLM、无图、无向量）
  │  dataset 权限检查 / 内容哈希去重 / append-only（改内容必须 update()）
  ▼
cognify()（或 remember() 路由）
  │ TaskRunner 流水线（进程内 asyncio，信号量 20；无分布式执行器）
  ▼
classify_documents → extract_chunks（段落/句子，chunk_id = uuid5(content_hash)）
  ▼
asyncio.gather ──并行──┬→ extract_graph（LLM 结构化输出 KnowledgeGraph nodes/edges）
                       │   └→ ontology resolver（annotate/strict 模式，RDFLib）
                       └→ summarize_text（每 chunk 一个 TextSummary）
  ▼
add_data_points（图先于向量，绝不并发——自愈论证写在注释里）
  ├→ 图库：Ladybug（默认，Kuzu 后继）/Neo4j/…，确定性 id_for(name)
  ├→ 向量库：LanceDB（默认），集合按 {ClassName}_{field} 命名，含 Triplet_text
  └→ 关系库：nodes/edges 表 = 回滚台账（按 pipeline_run_id 清扫）
  ▼
（无自动巩固）──→ improve() 显式/去抖触发：lessons 蒸馏 + 反馈权重 + truth subspace
```

**一句话对比**：Hindsight 把智能压进**写路径**（retain 一次调用完成抽取→建图→入库，之后是永不停止的后台治理）；Cognee 把智能组织成**可编排的 ETL**（add 是哑存储，cognify 是流水线，进化靠显式 `improve()`）。

---

## 2. 摄入路径逐阶段对比

| 阶段 | Hindsight | Cognee | 差异本质 |
|---|---|---|---|
| **入口语义** | `retain` = 语义操作（"记住这个"），一次调用直达事实层 | `add` = 物理操作（存进去），`cognify` = 智能操作（理解它），两步解耦 | Hindsight 无"存了但没理解"的中间态；Cognee 允许存储与理解分别扩缩容、分批认知 |
| **队列** | `async_operations` 表 + worker poller：`FOR UPDATE SKIP LOCKED`、bank/document **serialization keys**（同文档操作跨 worker 串行）、slot 预留（巩固默认 2 槽）、wall-clock 上限（巩固按 idle 延长） | **没有独立 worker**：进程内 asyncio 后台任务（anchored tasks 防 gc），后台 run **串行一次一个**（避免 DB 写冲突）；无 Celery（`distributed/` 目录只是 Modal/Fly 等沙箱部署模板） | Hindsight 的吞吐是水平可扩的（多 worker 抢任务）；Cognee 的吞吐被单进程 asyncio 锁死 |
| **分块** | 形态感知：JSON 对话按轮次、JSONL 按行、文本按句子递归、图片按预算折算；`chunk_id=bank_doc_index`（~转义防注入，#4244） | 段落→句子驱动，`chunk_id=uuid5(content_hash)` 内容寻址（重复文本用出现计数区分）；CSV/JSON/Langchain 等多 chunker | Hindsight 的 id 是**位置稳定**的（delta retain 依赖它）；Cognee 的 id 是**内容稳定**的（增量重认知复用身份） |
| **LLM 抽取** | 每 chunk 一次：`FactExtractionResponse`——五维合一的 fact 文本、`fact_type`、发生时段、实体、**因果序数（只能引用更早事实）**；简洁/详述/逐字/自定义四种 prompt 族 | 每 chunk 一次：`KnowledgeGraph{nodes, edges}` 结构化输出；**本体解析器**（RDFLib OWL，annotate=只增强 / strict=丢弃无本体锚定的实体）；可换本地 GLiNER 实现无密钥抽取 | Hindsight 抽"受控事实"（schema 约束因果方向）；Cognee 抽"自由图"（本体提供约束选项） |
| **实体身份** | **动态评分归一**：pg_trgm 候选 → 名称相似 0.5 + 共现 0.3 + 时近 0.2，≥0.6 合并，union-find 聚簇 | **确定性 id**：`Entity.id_for(name)` = uuid5(tenant+user+dataset+data+slug)——同名自动同 id；不同写法是不同实体，靠 memify `consolidate_entities`（余弦≥0.85）**手动**合并 | Hindsight 能合并"Tigran"与"Tigran H."但有误合并风险（trigram 下限 0.3 防"Tigran→Iran"）；Cognee 简单可预测但同义异形不会自动聚合 |
| **图边的来源** | **写时派生**：时间链（24h 窗 LATERAL 探测）、语义链（kNN≥0.7，全库收尾一次 ANN）、因果链（抽取序数）——全部确定性计算 | **LLM 直接给出**：节点/边就是抽取产物；另有可选的 `detect_contradictions`（LLM 找矛盾→写 `contradicts` 边，默认关闭） | Hindsight 的图是检索索引（廉价、有界、可重建）；Cognee 的图是知识本体（丰富、但质量受 LLM 抽取波动影响） |
| **写入顺序与原子性** | 三阶段协议：解析在事务外 → facts+全部检索关键链路**单事务原子提交** → 统计事后写（`entity_cooccurrences` 写在事务内会永久死锁 Oracle——注释里的事故记录） | **图先于向量、绝不并发**（graph-ok/vector-fail 可自愈；反过来会永久服务孤儿内容——论证在 add_data_points.py 注释）；关系库 nodes/edges 表是**回滚台账**（按 pipeline_run_id 清扫），查询图在 Ladybug/LanceDB | 两家的"写入不变式"都很精彩且完全不同：Hindsight 保证检索一致性，Cognee 保证可清理性 |
| **幂等/恢复** | chunk 哈希短路重抽取 + `result_metadata` 检查点 + **delta retain**（只重抽变更 chunk，动机：全量替换会孤儿化 observations） | 内容寻址 id 天然幂等 + `Data.pipeline_status` 增量标记；**append-only**：内容变更的 re-add 直接抛 `DocumentUpdateRequiredError` | Hindsight 支持文档演进（update/append/delta）；Cognee 的演进语义弱（必须显式 update） |
| **安全** | memory defense（40+ 高置信正则，redact/block，fail-closed）+ operation validators + 准入控制 lane + 审计 | read/write/delete/share 权限检查在每个入口；**无内容级安全扫描** | Hindsight 防内容进库，Cognee 防人越权访问 |

---

## 3. 巩固/进化路径：自动化程度相反

**Hindsight（全自动后台）**：retain 完成钩子 → `submit_async_consolidation` → worker 认领 → 按 scope 分组取未巩固事实 → 每批 8 条 LLM 巩固（creates/updates/deletes 强制带 reason）→ 0.97 嵌入探针 + 逐对 LLM 仲裁去重 → observations 落库（`proof_count`/`source_memory_ids`/单调时间界）→ 事务内 stamp `consolidated_at` → 级联触发 mental model 刷新 → webhook outbox。**每 300s 对账例程兜底**（`banks_needing_consolidation()` 跨租户发现漏网事实）。用户零配置，数据自己"长出信念层"。

**Cognee（显式触发）**：没有自动巩固。进化发生在 `improve()`：九个阶段注册表（feedback_weights → persist_session_qa → persist_agent_traces → extract_agent_context → distill_sessions → update_user_preferences → build_truth_subspace → triplet_enrichment → global_context_index），由 `remember(self_improvement=True)` **去抖**触发或手动调用；每个阶段先门控（无会话/无 LLM 配置/锁被占 → 零 LLM 调用跳过）。memify 管线族（`consolidate_entities`、`global_context_index`、triplet 嵌入）也是手动。

**对照结论**：Hindsight 的进化是**基础设施式的**（后台常驻、对账兜底、webhook 通知）；Cognee 的进化是**应用式的**（调用方决定何时 improve）。与 [03 报告](03-rsi-memory-evolution-deep-dive.md)的结论互相印证：Hindsight 有 L2 无 L3，Cognee 的 L3-lite（反馈权重）就藏在这条 improve 管线里（`apply_feedback_weights` 用 `used_graph_element_ids` 对图元素做 alpha 混合加权）。

---

## 4. 查询路径逐阶段对比

| 阶段 | Hindsight recall | Cognee search/recall |
|---|---|---|
| **入口模型** | 单 bank、纯检索（不生成）+ `reflect`（agentic 只读工具循环：search_mental_models/search_observations/recall/expand/done，10 轮上限、墙钟超时） | 单接口多形态：**20 种 SearchType**（GRAPH_COMPLETION/HYBRID_COMPLETION/CYPHER/CHUNKS/TRIPLET_COMPLETION/CODE/TEMPORAL/FEELING_LUCKY…），按数据集并发 fan-out |
| **检索组织** | **固定四臂并行**：语义+BM25 合并单 SQL（per-fact-type partial HNSW）/ 图 CTE 扩展（实体共现+语义链+因果链，1 次往返）/ 时间臂（日期解析→窗口 ANN→8 桶覆盖） | **可插拔检索器注册表**：GraphCompletion = 把图引擎投影成内存图片段，再把向量库的距离映射到节点/边上排序（`triplet_distance_penalty=6.5`）；Hybrid = chunk/实体/三元组/摘要四车道合并 |
| **融合重排** | RRF k=60 + per-source caps + **cross-encoder 重排**（日期前缀注入文档）× recency×temporal×proof 乘法增益 | 距离惩罚排序 + `feedback_weight`（improve 的反馈权重在这里生效）；**无 cross-encoder** |
| **生成** | recall 不生成；reflect 由 agent 综合并返回 `based_on` 证据清单 | GRAPH_COMPLETION 等类型由 LLM 生成答案；**空上下文跳过 LLM**（防幻觉，#3728） |
| **会话** | 无内建会话缓存（调用方管理上下文） | **内建 session cache**（SQLite/redis，QA/Trace/Feedback/SkillRun 四类条目，token-overlap 关键词排序，TTL 900s 惰性清除）；`recall()` 按 scope 路由 session→graph |
| **预算/取消** | `thinking_budget`（固定 100/300/1000 或自适应比例）+ token 预算裁剪 + 逐阶段协作式取消（客户端断连即停） | 每项信号量 20；无检索预算体系 |
| **返回形态** | 结构化 `RecallResult`（scores 分臂透明、source_fact_ids、chunks、attachments） | `SearchResult`（按数据集分组）或 LLM 文本 |

**本质差异**：Hindsight 的查询是**数据库式**的（固定管线、可预算、可取消、分数透明、无生成）；Cognee 的查询是**应用式**的（检索器即产品形态，图补全直接出答案）。前者适合作为基础设施嵌入任何 agent；后者开箱即得"问图"体验但管线黑盒度更高。

---

## 5. 后台执行模型：worker 架构 vs 无 worker

| | Hindsight | Cognee |
|---|---|---|
| 任务持久化 | `async_operations` 表（操作即行：retain/consolidation/graph_maintenance/refresh_mental_model/**webhook_delivery**） | 无任务表；只有 `PipelineRun`/`TaskRun` 观测行 |
| 认领 | 多 worker `FOR UPDATE SKIP LOCKED` + 旋转公平 + serialization key 串行化 | 无认领概念；per-dataset asyncio 锁 + 后台任务串行 |
| 周期维护 | **leaderless maintenance loop（每进程 60s tick）**：300s 巩固对账、cron 发现跨全部租户 schema 的 mental models、graph maintenance 队列、保留清扫——正确性靠幂等入队 | **无任何周期任务**（唯一例外：web scraper 的可选 APScheduler）；缓存 TTL 靠写路径惰性清除 |
| 失败 | 重试预算 + `RetryTaskAt`/`DeferOperation` + 终态回执对账（启动时回收崩溃 worker 的行）+ 批次自适应二分 | tenacity（2 次**且** 240 秒——一次瞬态调用可空转 4 分钟）；失败 run 按台账回滚；cognify 默认**大声失败**（数据：57% 首次运行静默失败且从未被搜索过） |

---

## 6. 两侧"值得抄"的工程机制（对 contextDB）

**从 Hindsight 抄**：
1. 三阶段写入协议（解析出事务、写入原子化、统计后置）+ 规范化锁键排序消死锁——并发写入的正确性范本
2. async_operations 通用任务表（一个表承载所有异步语义：任务+重试+webhook outbox+幂等键）——比消息中间件轻两个数量级且事务一致
3. 位置稳定 chunk_id + delta retain——文档演进的最低成本方案
4. 巩固对账例程（`banks_needing_consolidation`）——"漏网之鱼"兜底是所有自动化管线的必需品
5. 阶段级协作取消（客户端断连即停全管线）——降本利器

**从 Cognee 抄**：
1. 图先于向量的写入不变式（含自愈论证）——多引擎一致性顺序的决策范本
2. 回滚台账（关系库存写入痕迹按 run_id 清扫）——ETL 失败恢复的最简实现
3. 内容寻址 chunk id + `pipeline_status` 增量标记——重跑幂等
4. `improve()` 九阶段注册表 + 门控（无输入零成本跳过）——L3 进化管线的组织方式
5. 反馈权重（`used_graph_element_ids` alpha 混合）——目前产品界最具体的"使用改变检索"实现
6. 大声失败的产品决策（57% 静默失败→默认 raise）——可观测性即产品
7. 本体 strict 模式（无锚定实体即丢弃）——企业知识图谱质量闸门

**互相矛盾的取舍（没有对错，只有场景）**：
- 位置稳定 id（支持 delta 演进）vs 内容寻址 id（天然幂等）——contextDB 可以两者结合：内容寻址 + 显式位置映射表
- 动态评分实体归一（智能但需调参）vs 确定性 id_for(name)（简单但同义异形失联）——contextDB 建议两级：确定性 id 做底座 + 评分合并作为后台治理
- 固定四臂检索（可预算可解释）vs 20 种检索器（灵活但黑盒）——contextDB 的答案取决于它想当基础设施还是当产品

---

## 7. 反直觉发现清单（两侧各 5 条，全部代码级）

**Hindsight**：
1. retain 的真实路由是 `POST /banks/{id}/memories`——文档里的 `/memories/retain` 在 slim API 中不存在
2. 语义链接从不按批创建——全库收尾后**一次** ANN（每批 ANN 因 O(bank) 扩展被移除）
3. `entity_cooccurrences` 写在事务内会永久死锁 Oracle——注释保留了这场事故
4. webhook 的 outbox 就是 `async_operations` 表本身（delivery 是一种 operation，与完成状态同事务插入）
5. 嵌入的字符串 ≠ 返回的文本：存的是原始 fact，嵌入的是 fact+日期+实体名（`embedding_processing.py`）

**Cognee**：
1. 宣称分布式，实际零分布式——`distributed/` 只是沙箱部署模板，并发全靠单进程 asyncio
2. 关系库的 nodes/edges 表不是查询图，是回滚台账（查询图在 Ladybug/LanceDB）
3. 默认图库已是 **Ladybug**（Kuzu 后继）——多数第三方资料还在写 Kuzu
4. LLM 重试 = "2 次**且** 240 秒"——瞬态故障最坏空转 4 分钟
5. cognify 默认抛异常——产品决策源自"57% 首次运行静默失败且从未被搜索"

---

## 附：关键代码锚点（file:line）

**Hindsight**（`hindsight-api-slim/hindsight_api/`）：`api/http.py:9598`（retain 入口）、`:5886`（recall）、`:6140`（reflect）；`engine/retain/orchestrator.py:1269`（retain_batch）、`:475/:3085/:3219`（三阶段）、`:3554`（delta）；`engine/retain/fact_extraction.py:761`（分块）、`:1959`（抽取）；`engine/entity_resolver.py:623`（归一）；`engine/retain/link_utils.py:455/:573/:904/:165`（三类链+锁序）；`engine/consolidation/consolidator.py:1542/:2369/:291`（巩固主链）；`engine/memory_engine.py:7894`（recall 管线）、`:21821`（异步提交）；`worker/poller.py:524/:670`（认领）；`engine/maintenance.py:164/:206`（维护循环）；`engine/chunk_ids.py:57`（chunk id）。

**Cognee**（`cognee/`）：`api/v1/add/add.py:35`、`api/v1/cognify/cognify.py:109/:558`、`api/v1/search/search.py:41`、`api/v1/remember/remember.py:867`、`api/v1/recall/recall.py:347`、`api/v1/improve/improve.py:101`；`tasks/ingestion/ingest_data.py:116`；`modules/pipelines/operations/run_tasks.py:39`、`run_tasks_base.py:262`（流式递归管线）；`tasks/graph/extract_graph_and_summarize.py:13`（抽取+摘要并行）；`tasks/storage/add_data_points.py:70/:317-334`（持久化与写序不变式）；`infrastructure/databases/graph/config.py:47`（Ladybug 默认）；`infrastructure/session/session_manager.py:41`（会话缓存）；`tasks/memify/apply_feedback_weights.py:367`（反馈权重）；`modules/ontology/get_default_ontology_resolver.py:21`（本体）。
