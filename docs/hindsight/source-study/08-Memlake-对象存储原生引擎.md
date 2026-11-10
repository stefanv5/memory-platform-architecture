# 08 · Memlake —— 对象存储原生的企业级记忆引擎

> 代码基线：`eebe3ab19`（2026-11-10，自 v1.2.0（`2c00b59ae`）以来 81 个提交（本窗口 73）；本篇正文素材落库于 1.0.0 轮，§10 的代码接缝按此快照逐条重核）；本篇随 1.0.0 增量更新轮新增（Memlake 于 2026-10-13 与 v1.0.0 同日发布，素材由 commit `610c135b6` 落库）。
> **消歧**：本篇分析的是 **Hindsight 官方的企业版存储引擎**（Hindsight Memlake），与同名开源项目 easy12138s/MemLake（团队级 MCP 记忆网关，见竞品分析文档 §3.7）无关。
> **证据等级声明（先读这段）**：Memlake 引擎是闭源 Rust 程序，**不在开源仓库里**（`grep -ril memlake` 在本快照（`eebe3ab19`）命中 **30** 个文件，全部是文档、架构图 JSON、博客 SVG 配图与 `hindsight-docs/src/data/featured-posts.json`；去掉 `versioned_docs/` 与 `skills/` 下的副本后 **22** 个——引擎代码零命中。上一基线 `33524bd33` 实测 31 / 23 个；这一轮少的那个是 `hindsight-docs/docusaurus.config.ts`——公告条从 Memlake 换成了 `Hindsight Models` 的 v1.2.0 博客（不再是 Memlake 的提及，故不计入），不是文档被删）。因此本篇论断分三级标注：`[blog]`=官方发布博客 `hindsight-docs/blog/2026-10-13-memlake.md`（含 11 条脚注）；`[文档]`=`hindsight-docs/docs/cloud-enterprise/`；`[图]`=6 张 Giotto 架构动画的 JSON 元数据与博客 SVG 内嵌文本（`hindsight-docs/figures/memlake-*.json`）；唯一的 `[代码]` 级内容是**开源仓库里的接缝**（memories store 抽象，§10）。博客是厂商叙述：机制描述与营销数字混排，本篇把每个数字的测量条件一并抄录，无法核实的内部实现一律写"未确认"。
> 研究方法：2 名研究员独立提取 + 2 名交叉审核员逐字回源核验（研究底稿见 `_memlake-research/`）。

---

## 1. 它是什么：一句话与三层定位

Memlake 是 Hindsight 的**企业版存储引擎**：把"记忆"从 Postgres 搬进对象存储（S3/GCS/认证过的 S3 兼容存储），桶是唯一真相源，服务器只是可丢弃的缓存；用 Rust 写成（为可预期的内存占用与"召回路径上没有 GC 停顿"）`[blog §Introducing]`。它通过 Hindsight 的数据库层接缝接入，"你的 agent、SDK 和 API 调用完全不变" `[blog §Introducing]`。

三层定位（`[文档]` overview.md 的部署对比表）：

| 形态 | 记忆存储 | 说明 |
|----|----|----|
| 自托管（开源，MIT） | PostgreSQL（或 Oracle） | 开源版只认关系库——**Memlake 不涉 Oracle** |
| Hindsight Cloud | Vectorize 托管 | "大多数组织已经在跑 Memlake" `[blog §Availability]` |
| Hindsight Enterprise | PostgreSQL **或 Memlake**（对象存储） | 专属云/BYOC/本地四种形态（§15） |

一句话的动机：开源 Hindsight 的引擎、巩固、检索工程全部照旧（01-07 篇的所有结论不变），**只有"记忆行放在哪"这一层被换掉**。

## 2. 为什么 Postgres 会疼：官方的测量（动机侧）

`[blog §Object storage is all you need]` + 脚注 pg（经 Hindsight API 端到端测量，dev 集群，2026-09；链接表占比一项来自托管集群）。五条痛点：

1. **recall 随 bank 变慢**：4-vCPU 主库，33k 记忆 7.0 queries/s → 164k 只剩 1.9；加并发只加排队。
2. **关键词搜索没为大队列设计**：PG 内置 FTS 给**每条匹配行**打分；210k 记忆的 bank 一条查询几乎命中全表——**即使 Hindsight 默认的"只保留 16 个最稀有查询词"上限已开**。换关键词引擎（vchord/pg_textsearch/pgroonga）能改善这一项，但救不了下面的写与链接成本 `[脚注 pg]`。
3. **写打爆 WAL**：每条记忆约 **190 KB WAL**（向量+链接+全文索引合计）；摄入封顶 **55–80 条/s**。
4. **多 bank 也疼**：某 1,000 个小 bank 的组织只撑到 ~2 recalls/s——每条查询都要在数千个 per-bank 索引上做计划。
5. **字节大头是链接**：托管集群里，记忆互链表占库 **65%**，其中 **3/4 是索引而不是数据**。

`[代码]` 佐证（这些痛点在开源代码里有原文级痕迹）：`engine/search/bm25_term_selection.py` 的模块 docstring 记录了整条链路——长查询被 tokenize 后 **OR-join** 成一个 tsquery，native 后端 `@@` 门命中大半 bank，`ts_rank_cd` 无 IDF 且无索引支撑，曾造成 **+60 秒的生产 BM25 挂起**；对策 `DEFAULT_BM25_MAX_QUERY_TERMS=16`（`config.py:1467`）+ `DEFAULT_BM25_SELECTIVE_TERMS=True`（按 pg_stats 文档频率保留最稀有词，`config.py:1472`，服务器级静态配置、不在 `_CONFIGURABLE_FIELDS`）。可选关键词引擎即 `VALID_TEXT_SEARCH_EXTENSIONS = ("native","vchord","pg_textsearch","pgroonga","pg_search")`（`config.py:1694`）。

官方的结论很克制：Postgres 没做错什么，是工作负载形状特殊（只增不减的小记录 × 每次召回四路搜索 × 写入即刻可见）；水平扩展 Postgres（Citus/Aurora Limitless/AlloyDB）买的是余量，不改设计——"每条链接仍是一行、每次召回仍是跨索引查询"。所以他们为这个数据形状造了一个专用存储 `[blog §Object storage]`。

## 3. 设计目标六条（写代码前立下、每次变更仍对照评审）

`[blog §Goals]`：

1. **对象存储是记忆的唯一真相源**——Memlake 自己没有数据库、没有共识集群、桶旁边没有第二个有状态系统；持久性/复制/可用性全部来自 S3/GCS；租户的每一字节记忆都在租户自己控制的桶里。
2. **确认前已持久，确认后即可见**（durable before acknowledged, visible once acknowledged）——Hindsight 确认一条记忆已保存时，它已经在对象存储里，且下一次 recall 从任何服务器都能找到它。
3. **一切增量**——写、索引、召回路径上没有任何一步会重处理整个 bank；retain 的成本与 bank 是一千条还是一亿条无关；recall 对对象存储的往返次数有界。
4. **一份拷贝、四种搜索**——每条记忆只存一份，同时服务向量/关键词/图/时间四路；没有需要保持同步的独立向量库+搜索引擎+图库。
5. **计算可丢弃**——服务器只持缓存；丢一台只损失延迟，绝不损失数据；扩容=加服务器。
6. **有界设计**——缓存与"未索引写"的读取都跑在固定内存预算上；写入快于索引时，Memlake 用**可重试错误**让写方减速，而不是放任内存增长。

## 4. 架构总览：四个活动部件，其中一个是你的桶

```mermaid
flowchart LR
    HS["Hindsight API<br/>(retain · recall · reflect)"] -->|"gRPC"| SA["Memlake Server A<br/>RAM cache + NVMe cache"]
    HS -->|"gRPC"| SB["Memlake Server B<br/>RAM cache + NVMe cache"]
    subgraph BUCKET["你的 bucket（每租户一个，bank 是其中前缀）"]
        LOG["Log<br/>一次写一个不可变对象<br/>wal/43"]
        SEGS["Segments<br/>一次写永不改的段索引<br/>seg-5 · seg-6 · seg-7"]
        MAN["Manifest + head pointer<br/>仅有的两个会变的对象"]
    end
    IDX["Indexer<br/>log → segments"] --> LOG
    IDX --> SEGS
    IDX -->|"manifest swap (if-match)"| MAN
    ARC["Archiver"] --> LOG
    ARC --> BK["备份 bucket<br/>最好在另一个账户、凭证不可删除"]
    SA --> LOG
    SA --> SEGS
    SA --> MAN
    SB --> SEGS
    SB --> MAN
```

读法 `[blog §Architecture; 图:memlake-architecture.json]`：

- **桶是唯一的状态**："Everything durable lives in your bucket, per tenant and bank: a log with one object per write, immutable segment indexes, and a manifest whose head is the only object that changes."
- **服务器是纯缓存**："Servers A and B each keep a RAM and an NVMe cache, and hold no durable state of their own"；"Any server can answer any request, so there is no primary"；"Memlake's servers never talk to each other"——**没有主从、没有互连**，协调全部发生在桶上的条件写里（§5）。
- **indexer 与 archiver 是后台部件**（§8）。gRPC 只存在于 Hindsight API ↔ Memlake servers 之间（引擎级压测也直走 gRPC，`[脚注 engine]`）。

**哪些数据进桶、哪些留在 Postgres** `[blog §Introducing]`：

| 进桶（记忆所在处） | 留在 Postgres（运维面） |
|----|----|
| memories、links、索引、documents 本体、mental models 与 knowledge pages（其文本入索引） | banks 与设置、后台任务队列（async_operations）、审计 log、Cloud 账户数据 |

"那部分数据小且天然关系型（有更新、有 join、有事务），留在 Postgres 让 Hindsight 能继续在普通表上做功能，也让每个系统做它最擅长的事。"

## 5. bucket 对象模型：三类对象

每个租户一个桶，每个 memory bank 是桶内的一个前缀，前缀下三类对象 `[blog §The bucket]`：

1. **Log（日志）**："Every write becomes one new, immutable file. The log holds recent memories until they're indexed." 一次写 = 一个新的不可变对象；图数据显示对象键形如 `wal/43`，并同屏出现三态标注：`wal/41 indexed`、`wal/42 committed`、`wal/43 claimed · durable` `[图:writes]`（是否线上真实键名未确认，图属示意）。log 被索引后的删除时机**素材未说明（未确认）**。
2. **Segments（段）**："Small, self-contained indexes over a slice of the bank, **written once and never modified**. Inside a segment, each memory type (facts, experiences, observations) has its own **vector clusters, full-text index, entity index, time index and links**." 段内按记忆类型（facts/experiences/observations）各配五套结构；图数据里段名 `seg-<N>`，状态从 "built · not yet published" 到进入 manifest 的 live 清单（`seg-5 · seg-6 · seg-7`）`[图:indexing]`。
3. **Manifest + head pointer**：**每 bank 前缀内**三类对象中仅这两个会变（多 bank 的桶里各 bank 有自己的 manifest/head 对）；manifest 列出 live segments，head 标记 log 已提交到哪。

关键不变式："Because nothing is overwritten in place, **a reader always sees a complete version of a bank, never something in between**." `[blog §Architecture]`

## 6. 条件写协议：全部协调机制就这一招

`[blog §Architecture]`："the two objects that change are only ever replaced with a **conditional write**: the store accepts the new version only if nobody changed it first. **That is the whole coordination story.** There's no database, lock service or leader beside the bucket."

四个使用点：

| # | 场景 | 条件写形态 | 语义 |
|---|------|-----------|------|
| 1 | retain 占 log 槽位 | `PUT wal/43 · if-none-match`（conditional create） | 两个写者永远抢不到同一槽位 `[blog §Writes]` |
| 2 | 推进 head pointer | `head → 43 · if-match` | 条目 durable 后 head 才前移 |
| 3 | 索引发布 | manifest swap（if-match） | 双 indexer 无锁竞争同一 bank：一个 swap 成功，另一个的文件"从不被引用、随后被 GC"——GC 判据："removes only what no manifest points to" `[blog §Indexers; 图:indexing]` |
| 4 | GC 决策本身 | 条件写 | "what garbage collection may delete comes down to a conditional write that the bucket either accepts or rejects" `[blog §Architecture]` |

**写入时序**（一次 Hindsight retain = 新事实 + 替换旧事实 + 新见实体，打包成**单条 log entry**，"lands entirely or not at all"）：

```mermaid
sequenceDiagram
    participant H as Hindsight API
    participant S as Memlake Server
    participant B as Bucket
    H->>S: retain（一次 retain 的全部产物）
    S->>B: PUT wal/43 · if-none-match（占槽，对象落桶即 durable）
    S->>B: head → 43 · if-match（推进 head）
    S-->>H: 此刻才 acknowledge
```

`[blog §Writes; 图:writes beats 2-4]`。commit 约 0.25 秒，被 Hindsight 的异步 retain 挡在 agent 主路径之外 `[blog §Performance]`。

## 7. 读写语义与 recall 全序

**三条保证的达成方式**：

- **durable before acknowledged**：ack 之前对象已经条件写进桶（占槽成功 = 对象已在 bucket）；持久性由对象存储背书（"Durability, replication and availability come from S3 or GCS"）。
- **visible once acknowledged / 写不等索引**："Writes don't wait for indexing: the next recall, from any server, reads the part of the log that hasn't been indexed yet."——图数据显示 recall 有专门一步"扫 log 未索引尾段"（`wal/43 scanned`），且"查 head"是热查询固定的一次请求（"the one request a warm query always makes"——图数据独有细节）`[blog §Writes; 图:recall beats 2-3]`。
- **读者总见完整版本**：见 §5 末不变式；索引发布同理——"Readers see the old version or the new one — never half." `[图:indexing]`

**recall 全序**（`[图:recall beats 1-6]` + `[blog §Servers]`）：

1. Hindsight 发 recall，**附带 ranking policy**（图标注三组成：fusion · rerank · budget）；
2. 服务器查 manifest/head（最新写入了什么）；
3. ranged reads 读 segments 里需要的块（主要命中缓存）+ 扫 log 未索引尾部；
4. 在**同一份数据上**并行跑四路搜索（§9）；
5. **在数据旁边**应用 Hindsight 送来的 ranking policy（"the server ranks next to the data"）；
6. 只返回达标的记忆："Only the memories that make the cut leave the server."

## 8. servers / indexers / archiver

**servers** `[blog §Servers; 图:architecture/failover]`：

- **粘性路由**：负载均衡把每个 bank 固定到同一台 server，内存 + 本地 NVMe 两级缓存常热；"since objects never change, that cache **never needs invalidating**"（段不可变 + head/manifest 条件写版本化）。
- **扩容 = 摊开 banks**："A bank's index lives on one server at a time"——而 agent 记忆天然按 agent/user/project 切分，所以水平扩展顺手。容量：每 server 约 **8–14 warm recalls/s/vCPU**，随服务器线性增长。
- **failover**：server 消失后另一台直接接手其 banks——"no handover, no replica to promote"，边服务边从桶热身；"Every acknowledged write is still there: it was never on the lost server." `[图:failover]`
- **冷启动代价（重要 tradeoff）** `[blog §The cold-start tradeoff]`：首次打开 bank 要从桶加载索引元数据——10M 记忆 bank **8.6s p95**（每 server 一次，非每查询）；数据块未缓存前查询 p99 约 1 秒；1M bank 靠流量自然变热：1.7s → 74ms p99。缓解：server 打开 bank/接手新索引时**后台预载**；bank 粘性路由使冷启动很少发生；还可以**提前发一枪 recall** 预热（"a user logging in, an agent starting a job"）。
- **反压**：写快于索引时，写方收到**可重试错误**而非无限内存增长 `[blog §Goals]`。

**indexers** `[blog §Indexers]`：

- 与应答查询的 servers 分离运行；读新 log 条目 → **折叠成新 segment，途中派生记忆间链接** → swap manifest 发布 → 后台 compaction 合并小段（触发条件/参数：未确认）。
- **增量性**："Adding a hundred memories to a bank of a hundred million **builds a hundred-memory segment**"——索引成本跟随新增而非存量；"the work that grows with the data happens here rather than at query time."
- **无锁双 indexer**：两个 indexer 同 bank 竞争不需要锁——赢者换 manifest，输者的文件无人引用、后被 GC（TLA 性质之一，§10）。

**archiver** `[blog §Archiver]`：

- **opt-in**（"When backups are on"）：持续跟随 log 复制 + 每日快照，到独立备份桶，"ideally in another account with credentials that can't delete"。
- **PITR**：bank 可恢复到任意时点；"every restore is verified before traffic moves to it"（验证内容未确认）。

## 9. 四路检索：一份拷贝，四种搜索

`[blog §The four searches]`。全部四路读同一份存储的记忆：

| 路 | 实现 | 要点 |
|----|------|------|
| 语义（by meaning） | IVF 簇索引，查询只扫最近的簇 | **每条向量存两份**：RaBitQ 压缩到约 **60 字节**（384 维 embedding 下，比原始小 **25×**）做快速第一遍 + **全精度副本**对最佳候选精确重排 |
| 关键词（by keyword） | BM25 over [tantivy](https://github.com/quickwit-oss/tantivy)，随 segment 存储 | **含中文分词**（word segmentation for Chinese） |
| 关系（through relationships） | 从最佳向量匹配**一步出边** | 共享实体 / 语义相近 / **caused or were caused by**（因果）/ 时序紧邻（right before or after） |
| 时间（through time） | 每 segment 一个时间索引 | 带时间窗的查询**只读该范围** |

`[代码]` 对照（开源侧语义一致性）：`memory_links` 表的 check 约束恰好是 `'temporal','semantic','entity','causes','caused_by','enables','prevents'` 七种链接类型（alembic `5a366d414dce_initial_schema.py:471`），与关系路描述吻合；图臂以语义命中为种子（`engine/memories/base.py:828` `GRAPH_SEED_LIMIT = 20`）。

**exact vs approximate 的口径**（这是评估任何 ANN 引擎的关键，官方给了明确边界）：

- **精确的部分**：跨 segment 分数可比——"merging each segment's reranked top results gives the same top results as reranking the same candidates in one place"（合并的是**重排后**结果，不是粗排分数）。
- **近似的部分**：segment **内**找候选是近似的（任何聚类索引皆然）。
- **质量检查手段**：测试中用"探遍所有簇"的搜索对 brute force，要求**完全一致**；BEIR 上检查压缩首过找出的 top 10 与 8-bit 向量的 top 10 一致。
- **成本模型**：设计预期段内搜索成本随段大小 **√N** 增长；实测（bank 扩 30×，recall 慢 ~6×，√30≈5.5）接近该曲线 `[blog §The four searches; 脚注 sweep]`。

## 10. 与开源引擎的接缝：本篇唯一的 [代码] 级内容

Memlake 引擎本体不在开源仓库，但"插进 Hindsight 数据库层"的接缝在开源代码里完整可见 `[代码]`：

1. **store 抽象**：`hindsight-api-slim/hindsight_api/engine/memories/base.py:1117` 定义 `MemoriesExtension(Extension, ABC)`（全文件 4571 行）。计数按本轮重数：`MemoriesExtension` 的直接方法 **186** 个，连同同文件 `EntityResolverHandle`(5)/`RetainSession`(3)/`WriteBatch`(3)/`FactRecord`(1)/`DeletePredicate`(1)，各类合计 **199** 个方法，另有 **9** 个模块级函数；计数方法：用 Python `ast` 解析模块体，逐 `ClassDef` 取 `body` 里的 `FunctionDef/AsyncFunctionDef`（不再用 `grep -c`，因为行内嵌套 def 会被误计）。`extensions/loader.py:178` 注册 `MEMORIES` 扩展槽；`engine/memories/__init__.py:49-63` 的工厂 `create_memories()` 先尝试 `load_extension("MEMORIES", ...)`（加载时打日志 `"[memories] store=%s (memory rows do not go to postgres)"`，`:58`），未配置则回落 `PostgresMemories`。两条路径都被 `time_store_calls(...)` 包一层（`hindsight_api/operation_runs.py`）：每次 store 调用的次数与耗时记进当前 operation run，计量点就在 store 接口上，所以对任何后端（含网络型）同样有效；store 内部对自身的再调用用 `_in_store` contextvar 去重，只记一次——`_in_store` 与去重逻辑在 `operation_runs.py:62` 与 `:188-195`，`memories/__init__.py:59` 与 `:63` 是两处包 `time_store_calls(...)` 的包装点（`:52` 只是 import，不是包装点）。企业版 Memlake 就是这个槽位的另一个实现（此句为接口契约推论，实现本身未确认）。
2. **表集合精确对应**：`engine/schema.py:16` 的 `STORE_TABLES = {memory_units, memory_links, unit_entities, documents, chunks, entities, entity_cooccurrences, invalidated_memory_units}`——**仍是 8 张**，本窗口没动。这就是 blog 所说"记忆所在处"的开源清单；`base.py` 模块 docstring 原文："Banks, operations and the other engine tables stay in Postgres either way"（bank/操作队列等运维表两种存储下都留 Postgres）。
3. **接口已为"网络型 store"备好形状**：
   - `store_owned`（base.py:1172）：store 拥有全部记忆行 + 文档/chunk 本体 + retain 的原子服务端提交（返回 `seq/unit_ids/new_entities`）；`store_owned_for(bank_id)`（:1174）支持**按 bank 路由不同后端**——docstring 明说支持"banks live in different backends"，这正是博客"迁移任务逐组织搬迁"的接缝。旁边那段注释还记了一次真实事故：曾经另有一个"有没有 retain session"的独立标志，某个只转发 `store_owned_for` 的路由没转发它，于是每个 bank 都答"没有 session"——**什么都不报错，retain 只是悄无声息地退回逐批写入、吞吐减半**，所以现在"一个能力一个标志"。
   - `assert_writable`（base.py:1399）+ `StoreWriteUnavailable`（:73-86，映射 503+Retry-After）：docstring 原文即 "migrating a bank between backends holds it for a few seconds"——**bank 在两后端间搬迁时冻结数秒**已代码化；`StoreWriteConflict`（:88）则是**条件写输掉竞争**的异常形状（"the state it was based on moved before it committed……the caller has to re-read and redo the work"）——与 Memlake 的条件写协议同构。（注：这是 OSS 存储契约的事实，不据此推断 Memlake 内部实现。）
   - `begin_retain` → `RetainSession`（流式收 part）；`allocate_unit_ids`（`base.py:1434` 的 docstring："The Postgres path never calls this — its ids come back from the INSERT's RETURNING clause"；即"不再依赖 INSERT 的 RETURNING 发 ID"）；`FactRecord` 内联携带 `entity_ids` + `causal_edges`（base.py:571 起；docstring 原文 `:574-576`："There is no row behind this — it is the *whole* record — so it carries every column recall returns, plus the edges that would otherwise have become `memory_links` and `unit_entities` rows."）——链接可以不落 SQL 行。
   - **recall 两级接缝**：`recall_unified`（base.py:1858，一次调用返回四臂结果，"a store that owns its index answers every arm from a single query with no per-arm round-trips"）与 `full_recall`（base.py:1898，store 整包接下融合/重排/新近度/预算——"A store that owns its index can do all of it where the data already is"）。引擎侧 `memory_engine.py:10227` 起是 "Store-answered recall" 分支：记录 store 上报的分阶段耗时与 `store_hop_overhead`（`:10251`），注释明写 "**our gRPC client**"——网络型 store 的观测位已就绪。
4. **一个 blog 表述最粗的地方（接口 docstring 甄别）**：blog 笼统说 mental models 与 knowledge pages "move to the bucket"；但接口 docstring（base.py:936 `KnowledgePageEntry`）明确：**页面的行（name/body/folder/trigger/history）仍留 Postgres 且是权威，store 只持有派生的索引副本**（"this is the derived half"）。两者可相容（索引进桶、行留 PG），但"整页搬进桶"是博客的粗化。
5. **边界执法**：运行时 `fq_table` 拒绝 store 表名（`engine/schema.py:16` 的 `STORE_TABLES`、`:34` 的 `_guard`）、store 专属 `fq_store_table`；AST 级测试 `tests/test_store_table_boundary.py` 禁止 store 表出现在 store 之外的 SQL 里（本窗口该测试与 `STORE_TABLES` 都一字未改）。v1.1.0（#5993）把 schema 访问器 `get_current_schema` 从 `memory_engine.py` 迁到独立的 `hindsight_api/schema_context.py`——目的是让 config、metrics、extensions 与引擎自己的模块都能读到租户/bank 上下文而**不必 import 引擎**（`schema_context.py:1-5` 的模块 docstring 原话），`schema.py:9` 改为直接 import 它。

6. **更早几个窗口攒下的契约（行号已按本轮快照 `85ab32ebe` 重取）**：
   - **写入锁序成为契约**：`lock_document_row`（base.py:3613）与 `lock_consolidation_write_set`（:3915）。retain、consolidation、delete 三个写者按同一顺序取锁，才不会互相死锁（#5433）。两者默认都是 no-op，但注释说的不是同一件事：`lock_document_row` 的是 "A no-op here: there is no row"（`:3614-3615`，store-owned 的 bank 在本库没有 document 行可锁），`lock_consolidation_write_set` 的是 "A no-op here: a store that keeps memories outside SQL has its own concurrency model"（`:3922`）——后者才是"自管并发"的契约句。对 Memlake 这类条件写 store，"同文档并发写"的不变量必须自己保证。
   - `memories_updated_between`（:2226）：按时间窗口列出记忆，供 delta knowledge page 刷新用决策模型判断"这批记忆会不会改页"；返回 `None` = 本 store 不支持，刷新退回检索（#5698）。
   - **导出改分页迭代**：`iter_entity_maintenance_queue`（:4332）与 `iter_archived_memories`（:4345）取代一次性的 `dump_entity_maintenance_queue`（#5737）——大 bank 导出不再把整表读进内存。
   - `ensure_bank_storage`（:1426）的契约改写为"**必须能在 bank 行存在之前调用**"（创建与导入都先建存储），创建失败留下的孤儿存储由下一次调用接管（#5763）。
   - `GRAPH_MAX_EDGES = 10_000`（:835）：每个方向的图视图答案最多 1 万条边。不设界时几千条 observation 能派生几十万条边，在事件循环里算数秒、回出几 MB JSON（#5703）。
   - `MemoriesExtension.mental_model_is_stale`（`base.py:2285`，v1.2.0 窗口加的）：把"单个 mental model 是否过期"整个收进 store。它由三次读组成——bank 写水位（`latest_memory_write_at`）、作用域内有没有更晚的写（`any_memory_updated_since`）、被引用的事实是否还活着（`live_memory_ids`）。默认实现在**同一连接上串行**做（docstring 原话："the only correct shape for a store whose reads share one connection"），并注明 reads 可并发时覆写它（#6261）。**取舍**：接缝只保证"问一次"，不保证"问得快"——默认实现故意不并发，因为对 Postgres（一次调用一条连接）并发是错的。
   - 三次读的**顺序**本身就是预算，但**短路的位置很容易记错**（本稿第一版就写错了）：① 先读水位；② **只有**水位说"此后有写入"时才问作用域（`watermark_may_postdate(...) and await any_memory_updated_since(...)` 写在同一行，前件假则后件不求值），作用域命中就直接 `return True`、**跳过第三次**；③ 被引用事实的存活检查**几乎总要跑**——水位说"没有任何写入"时它照样跑，因为**被删除的记忆不抬高水位**（`memory_engine.py:21434-21436` 的注释："a memory that was *removed* raises no watermark — the row is simply not there"），只查水位会把"引用的事实已经被删"误判成新鲜。于是两种常见答案的成本是：**"新鲜" = 第 1+3 次读**，"作用域已过期" = 第 1+2 次读；`cited_ids` 为空时最多两次读。
   - 引擎侧：引用清单 `_cited_fact_ids`（`memory_engine.py:21452`）在调用 store **之前**取（`memory_engine.py:21447`，store 调用在 `:21449`），所以完整读路径（行里已带 `reflect_response`）白拿。**别和批量路径混为一谈**：`compute_mental_models_are_stale`（`:21572`）在 `if not _may_need_refresh(scope.since, watermark)` 时直接 `answers[key] = False; continue`（`:21616-21621`）——那里水位说"没写入"就整题免答（连引用存活都不查），因为它服务于列表上的 `is_stale` 轮询、不逐模型取 `reflect_response`。
   - `watermark_may_postdate` 住在 `memories/base.py:737`（不是引擎）：默认实现要用它，而 store 不能反向 import 引擎；引擎侧改为 `from .memories.base import watermark_may_postdate as _may_need_refresh`（`memory_engine.py:694`）。**这是一次依赖方向的修正**，不是新语义。

7. **本窗口（`85ab32ebe`）接缝净增 3 个方法、改 4 处签名——仍然一个抽象方法都没加。**
   - **`has_pending_consolidation`（base.py:2348，带默认实现）**：默认就是"调 `consolidation_freshness` 再看 `pending`"，也就是一次**计数**整 bank 的聚合；PG 侧覆写成"遇到第一条就停"的存在性查询（`memories/pg/counts.py:62-82`，`ORDER BY created_at DESC LIMIT 1` 走 `idx_memory_units_unconsolidated`，注释解释了为什么**倒序**：consolidation 从小到大处理，永久失败的事实会堆在旧的一端，正序会被一条条踏过去）。动机是引擎侧一处"要不要排刷新"的探测（`memory_engine.py:21530`：consolidation 还没做完就别再排刷新，做完它会自己回来查）。**取舍**：接缝只承诺"问一次"，默认实现并不省成本——网络型 store 想要收益必须自己覆写。
   - **`existing_documents_tags`（base.py:3115，带默认实现）**：`document id -> tags`，**"存在但没有 tags 键"要返回 `None` 而不是 `[]`**。默认实现是逐 id 调 `current_document_tags`（N 次往返）；PG 覆写成一条 `documents_tags`（`memories/postgres.py:967-971`，注释：`documents` 行总有 tags，NULL 读作 `[]`，所以"存在但无 tags"在 PG 上不会发生）。它和已有的 `documents_tags` 分开，是因为后者**会漏掉**"存在但没有 tags 键"的记录——retain 的写权限检查会把它当成"新文档"放行（`memory_engine.py:3904` 的调用点，#6327）。这是本窗口最典型的一类接缝修复：**语义差一格，权限判断就差一档**。
   - **`EntityResolverHandle.forget_pending_mentions(entity_ids)`（base.py:1103）**：把已经排进"待累加 mention_count"队列、但随后被丢弃的事实对应的实体撤回来。消费方是 `_drop_committed_chunks`（`retain/orchestrator.py:818-863`）：一次 retain 的 chunk 若在写锁内发现已被别人提交（两个并发 retain 同一份内容），就要把 phase 1 已经排队的提及计数**撤回**，否则那些实体会被数两遍（#6337）。它挂在 `EntityResolverHandle` 协议上而不是 `MemoriesExtension` 上，因为 phase 1 拿到的是解析器句柄。
   - **签名加参数（四处，全部带默认值，既有实现零改动）**：
     - **`put_document(..., keep_chunk_text: bool = True)`**（store 侧方法：`memories/base.py:1513` 的 def、`:1527` 的参数、`:1533-1538` 的 docstring）：store-owned 的 bank 若 `store_document_text` 关着，chunk 文本**不能落盘**（#6301）。docstring 把边界写死了：store 仍要保存**由真实文本算出的 hash**（delta retain 拿它做差分），只是不保存文本，读回时每个 chunk 都是空串——这正是 SQL 路径的行为。调用方传的是 `config.store_document_text`（`retain/orchestrator.py:2454`/`:4643`）。（**别把它和 `_store_document_bodies` 弄混**：后者是引擎侧私有助手 `retain/orchestrator.py:2379`，负责「先复原读不到的 chunk 前缀、再整篇交给 store」；store 侧写正文的接口方法名只有 `put_document`，`MemoriesExtension` 上没有第二个。）
     - `current_document_tags(..., for_update: bool = False)`（base.py:3096）：文档 PATCH 的 `add_tags` / `remove_tags` 要"读—改—写"，所以要求 store 把该行持有到事务结束（PG 是 `SELECT ... FOR UPDATE`，`memories/pg/documents.py:356`）。默认实现是"读 store 记录"，**无法加锁、忽略该参数**——于是不支持部分改标签的 store 会被引擎用 400 拒绝（`memory_engine.py:11843-11849`）。这是"能力靠参数表达、而不是靠新方法"的一个例子。
     - `load_existing_chunks(..., chunk_indices: list[int] | None = None)`（base.py:3631）：只读指定下标的 chunk（PG 是 `AND chunk_index = ANY($3::integer[])`，`memories/pg/retain.py:65`）。动机是 append 的差分量：只需要重算变动的那些 chunk，不必把整篇的 chunk 列表读回来。
     - `DocumentChunkState.tags`（base.py:1038 的字段、`document_chunk_state` 在 :1041 填充）：文档"加载时"的标签，`None` = 读不到。引擎把它当 `previous_document_tags` 传给写入器（`retain/orchestrator.py:4072` → `:4157`/`:4193`），用来**保留标签组被删掉之后仍挂在记忆上的 label 标签**（叙述段 `retain/fact_storage.py:499-512`，真正的实现是 :537 的 `from .entity_labels import carried_label_tags` 与 :539-547 的 `_tags_for`，`carried_label_tags(existing, label_tag_keys, previous_document_tags)` 的调用在 :543；#6336）。store 若拿不到这列，delta retain 会把"本来该留"的标签洗掉——所以这是一个"默认能跑、但语义会退化"的字段。
   - **没有破的边界**：`@abstractmethod` 29→**29**（新旧相同），`STORE_TABLES` 仍 8 张，`tests/test_store_table_boundary.py` 未改。所以既有 store 实现**一行都不用改**就能继续工作；本窗口扩的全是"带默认实现的可覆写点"或"带默认值的参数"，与上一窗口同一条路线。
   - 另有两处**引擎侧**（不是 store 接口）的行为，store 作者需要知道：`attachments_for_memories` 的 `read_uncarried` 参数（`memory_engine.py:8852` 定义、`:8860` 签名）——knowledge page 的证据是"存下来的 memory id"，store-owned 的 bank 因此要把没随行带回来的 memory 的附件引用**回 store 读一次**（`http.py:6324` 传 `read_uncarried=True`）；默认关闭是为了不让每次 recall 多一次 store 往返（#6262）。以及 recall 的决策去重改成经接缝取向量（`base.py:3817` 定义 `unit_embeddings`，`memory_engine.py:10018` 消费）。

8. **上一窗口的另外两处形状变更本轮未再变，行号重取如下**（第三处「recall 决策去重改为经接缝取向量」已并入第 7 条末句）：`flush_pending_stats(conn=...)`（`base.py:1105` 的 `EntityResolverHandle` 协议）、`expand_memories` 多返回 `occurred_start`/`occurred_end`/`mentioned_at` 三列（`base.py:4187` 定义、三列在 `:4203-4205`，而 `:4200-4202` 是 `fact_type`/`context`/`tags`）。

## 11. 性能与成本台账（每个数字都带测量条件）

> 全部 recall 数字**不含 cross-encoder 重排器**（开着时重排主导 recall 时间，受重排器 GPU 容量限制而非 store）`[脚注 nocross]`。所有数字为 `[blog]`/`[图:SVG]` 级自报数据，本系列无法独立复测。

**Postgres 痛点基线**：见 §2。

**Head-to-head**（同集群同数据同 driver 无 LLM，2026-09-15；Postgres 为 4-vCPU 主库且与开发租户共享——**非同硬件**）`[脚注 presets]`。**吞吐口径：单 bank = 16 并发稳态；多 bank = 最高稳态速率**（图注原文，两类口径不同）。定性结论：小 bank 上 Memlake 快 3–5 倍、扛 10 倍流量；大 bank 上差距两个数量级 `[blog §Performance]`。封面图的第三条参照线（单 bank 从 4k 增长到 3M 的外推）：PG recall p99 0.5s → 930ms@50k → 5.3s@210k、月成本 $7.4k@210k → 外推 **$73.3k@3M**；Memlake 平坦在 ~90ms p99 / ~$1.7k/月：

| 工作负载 | Postgres | Memlake | 单查 p99 PG | 单查 p99 ML |
|----|----|----|----|----|
| 1 bank · 4k 条 | 19/s | 207/s | 513 ms | 106 ms |
| 1 bank · ~150–210k 条 | 0.45/s | 106/s | 5,300 ms（主因关键词搜索 `[脚注 fts]`） | 113 ms |
| 100 banks · 共 422k | 4/s | 113/s | — | — |
| 1,000 小 bank | 2/s | 113/s | — | — |

两点诚实限定：① PG 大 bank 延迟主要是关键词搜索（`ts_rank_cd` 对几乎全表的匹配行逐行打分）——"视为 Hindsight 今日 Postgres 配置的成本，不是 Postgres 最优形态的成本" `[脚注 fts]`；② **113/s 是该测试环境的上限而非引擎上限**——Memlake 服务器远未饱和，瓶颈疑似在 store 之外（"most likely per-request billing on the organization's account row"）`[脚注 cap]`。

**引擎级扩展性**（gRPC 直连、无 Hindsight API；`[脚注 sweep/engine]`）：

- bank 100k→3M（30×）重载下 recall 慢 **~6×** ≈ √N（2026-08，6-vCPU，本地 S3 兼容存储，真实语料，16 并发，非全热：0.1–0.3 次对象往返/召回）。
- **1 亿条记忆 / 20 banks / 4 台 6-vCPU**（≈1500 员工一年的 agent 量）：1→200 recalls/s 延迟平坦，p50 40–43ms，p99 73–80ms（该 fleet 与 10M 单 bank 用了**另一朵云**的对象存储，fleet 为**合成数据**）。
- 单 3M bank：p50 71ms / p99 90ms（**空载、更晚的另一版引擎跑在 GCS、单请求串行**——与 √N sweep 非同一引擎/存储）。
- 最大单 bank：**1000 万条**（≈自主 agent 不间断两年）。

**写**：单 bank **1,200–1,500 条/s** 随 bank 规模平坦（Postgres 的 15–27×）；commit ~0.25s。写很少成为瓶颈——每个 retain 先过 LLM，速度受 LLM 约束而非 store `[blog §Performance]`。

**冷启动**：见 §8。

**检索质量（BEIR）** `[脚注 beir]`：SciFact + NFCorpus，nDCG@10，两系统同用 bge-small-en-v1.5，**2026-07 引擎、官方注明正在用当前引擎重跑**；BEIR 无实体无时间戳——只测"存储引擎不损失质量"，不测图/时间检索的增益。结果：与一个成熟向量库对比，Memlake **从未落后超过 0.3%**（最大落后 0.27%，SciFact hybrid 0.7345→0.7325），两处对比反而领先（SVG 内嵌精确值——SciFact：向量库 0.7127/0.683/0.7345 vs Memlake 0.7127/0.6907/0.7325，即 BM25 +1.1%；NFCorpus：0.3436/0.3236/0.3626 vs 0.3429/0.3244/0.3638，即 hybrid +0.33%）；加图检索 NFCorpus hybrid 升至 **0.3645**。

**成本模型** `[脚注 cost]`——**Modelled, not billed**（内部 sizing calculator：整个 GKE 部署、GCP list prices、12 个月增长后、按 recall p99 ≤1s@峰值定规格；排除 LLM 与 embedding/reranker GPU——$1.1–2.3k/月两边相同；Hindsight 其余组件 $0.6–2k/月两边相同；Postgres 单次 recall 已超 1s 的大 bank 加硬件无解——单 primary——只按流量定规格并如实报告慢；Memlake 的 0.35s 是"默认缓存设置的预算值"而非实测；自治 bank ~5M 超 API 实测最大 151k、PG 超 ~210k 均为外推）：

- 存储单价：桶内 **4–5 KB/条** + 文档本体；员工 agent 一年 ≈ **350 MB**，S3 上**每月不到 1 美分**。
- 四场景（一年后的月度集群总成本，去 LLM/GPU）：

| 场景 | 记忆量 | Memlake | Postgres | p99 Memlake | p99 Postgres |
|----|----|----|----|----|----|
| 1,000 员工 | 68M | $3.2k | $4.6k | 0.35s | 2.4s |
| 10,000 员工 | 680M | $15.1k | $58.4k | 0.35s | 9.1s |
| 100 自治 agent | 490M | $11.1k | $146k | 0.35s | 5.2s |
| 1,000 自治 agent | 4.9B | $94k | $2.46M | 0.35s | 11.1s |

- 核心结论：**总量不是 Postgres 贵的原因，bank 的大小和数量才是**。小 bank 两边接近（小部署 Postgres 可能更便宜）；官方给的阈值："banks stay under ~50k memories each → Postgres is great, and it's not going anywhere"。
- hosted fleet 实际形状：存储层 Memlake 便宜 2.2×，整部署 1.5×。
- 写入量模型 `[脚注 usage]`：员工 agent ~68k 条/年（每工作日 10 次对话 × 实测 ~27 条/次）；自主 agent ~5M/年（500 任务/天）；万人公司 + 千个自治 agent ~5.5B 条/年。

## 12. TLA+：协议小到可以证明

`[blog §Verified, not just tested]`——因为正确性归结于少数几个条件写，"the protocol is small enough to prove"：

- 写、索引、GC 三协议用 **TLA+** 规约；模型检查器**穷举所有交错**：多 writer/indexer 竞争、server 任意步崩溃、响应丢失、进程任意长停顿。
- **五条被证明的性质**：① 已 ack 的写永不丢失，log 槽位永不复用；② 任何查询从任何 server 都能看到每条已 ack 的写；③ 读者总见完整索引，从不见半个更新；④ GC 永不删除仍在使用的东西；⑤ 双 indexer 无锁竞争同一 bank 仍安全。
- **防实现漂移四件套**：CI 里每次协议变更重跑规约；对照**故意写错的变体**验证检查器必须能抓出来；**真实引擎的执行记录（trace checks）对照规约**；注入存储故障的 property-based 测试 harness。
- 诚实限定 `[脚注 tla]`：模型检查是**有界范围内**的穷举（少量 writer/indexer/log 位置——"which is where interleaving bugs live"）；它验证的是**规约本身**；规约与代码的纽带是 trace checks + 故障注入 harness；细节在白皮书（`vectorize.io/memlake/whitepaper`，仓库外，未读未确认）。

## 13. blog 与 enterprise.md 的差异（同日落库，无先后可考）

| 项 | blog | enterprise.md |
|----|------|---------------|
| 存储后端清单 | S3、GCS、"**certified** S3-compatible"；on-prem 需条件写认证 | S3、GCS、**Azure Blob Storage**、on-prem S3 兼容（列了 Azure、未提 certified） |
| bucket 粒度措辞 | per-**tenant** | per-**organization**（同义异词） |
| 备份 | opt-in（"When backups are on"） | 无条件句式（"continuously backed up"）——未写前提 |
| 可用形态 | 四种（含 Cloud） | 仅 private cloud / BYOC / on-premises（文档视角）；另提 air-gapped、PrivateLink/PSC/Azure Private Link |
| 私有网络 | 未提 | PrivateLink / PSC / Azure Private Link |
| 性能数据 | 全套 | 除"100M <100ms p99 @4 servers"一句外无细节 |

## 14. 未能确认清单（本篇诚实边界）

1. 引擎内部实现（Rust 代码、gRPC schema、缓存替换策略、compaction 触发条件与合并参数）——闭源。
2. log 对象被索引后的删除时机与 GC 细则（GC 判据只有一句："removes only what no manifest points to"）。
3. indexer/archiver 进程自身是否有内部状态（图上未画）。
4. 恢复验证的具体内容；迁移任务的具体机制（blog 只说"keeps every memory ID, and verifies the copy before switching"）。
5. `wal/`、`seg-` 命名是否线上真实对象键（图为示意）。
6. 白皮书与 docs.hindsight.vectorize.io/enterprise/memlake 四个页面的内容（仓库外未读）。
7. 全部性能/成本数字的独立复测（本系列无该环境）。
8. **Memlake 对开源 store 接缝的跟随程度**：§10 已按本轮快照 `85ab32ebe` 逐条重核（`STORE_TABLES` 8 张、`MemoriesExtension` 直接方法 **186**、各类合计 **199**、抽象方法 **29** 不变、模块级函数 9 个不变）。本窗口净增的 3 个方法（`has_pending_consolidation`、`existing_documents_tags`、`forget_pending_mentions`）与 4 处加参数（`put_document(keep_chunk_text)`、`current_document_tags(for_update)`、`load_existing_chunks(chunk_indices)`、`DocumentChunkState.tags`）Memlake 是否实现、或是否走 `recall_unified`/`full_recall` 整包路径从而根本不经过它们，闭源无法确认。可推断的只有一点：**它们全都"默认能跑"，但其中三条会静默退化**——`has_pending_consolidation` 退化成一次整 bank 计数、`existing_documents_tags` 退化成 N 次往返、`DocumentChunkState.tags` 为 `None` 时 delta retain 会洗掉该保留的 label 标签（不报错，只是数据变差），而 `current_document_tags(for_update=True)` 不支持时引擎会明确回 400（有信号）。

9. **本篇行号的维护口径**：共 **54** 处 `路径:行`（按文件：`base.py` 系 22（含 `memories/base.py`、`engine/memories/base.py` 与一处全路径）、`memory_engine.py` 10、`retain/orchestrator.py` 4、`config.py` 3、`schema.py` 系 3、`memories/__init__.py` 2，其余 10 个文件各 1），其中 **46** 处指向本轮位移过的文件，**8** 处指向未改动的文件（`schema.py` ×3、`memories/__init__.py` ×2、`operation_runs.py`、`schema_context.py`、初始迁移 `5a366d414dce`）。做法：先用 `difflib.SequenceMatcher` 对新旧两棵树做逐行相等块映射，把旧行号机械换算（映射不到即该处代码被改写过，列出待手工）；再对换算结果逐条要求"目标行内容 = 旧行内容"，并把指向空行的一律当作差一错重取。§10 因整节重写，其锚点是手工逐条取的（每个符号都用 `grep -n` 找 `def`/`class` 行）。

## 15. 可用性四形态与迁移 `[blog §Availability]`

1. **Hindsight Cloud**："Most organizations already run on Memlake"，无需设置。
2. **专属云实例**：Vectorize 代管的单租户——独立 cloud project/cluster/bucket、自选 region、自己的域名、按你排期升级。
3. **你的云（BYOC）**：你自己的 AWS 或 GCP 账户，S3/GCS，用你 bucket 的加密与密钥（enterprise.md 口径含 Azure）。
4. **本地机房**：你自己的 S3 兼容存储，**需通过 Memlake 依赖的条件写认证**；官方与存储硬件 design partner 合作建设（一家用自家 flash 跑全栈含模型，blog 未点名厂商）。

**从 Postgres 迁移**：迁移任务逐组织搬迁，**保留每条 memory ID**，**验证副本后才切换流量**——切换窗口对应开源接口的 `StoreWriteUnavailable`（503+Retry-After，见 §10）。

## 16. 本篇与全系列的关系（收束）

Memlake 不改变 01–07 篇的任何结论：开源 Hindsight 1.0 的引擎、巩固、四臂检索、reflect、配置体系原样运行在 Postgres 上（"the API is a promise"）。它示范的其实是三件对理解这个系统更重要的事：

1. **存储层是被刻意抽象掉的**——05/06 篇讲过的 `MemoriesExtension` 接口（§10）就是为这一天准备的：`store_owned`/`store_owned_for`/`recall_unified`/`full_recall`/`StoreWriteConflict` 这些接口形状，与 Memlake 的架构需求（按 bank 路由、单查询四臂、条件写冲突、搬迁冻结）一一对应。读接口就知道闭源引擎长什么样的大致轮廓。（接入方式：`HINDSIGHT_API_MEMORIES_EXTENSION=module:Class` 环境变量，其余 `HINDSIGHT_API_MEMORIES_*` 变成该实现自己的配置——base.py 模块 docstring 的契约。）
2. **协调机制的极简主义**——不用数据库、不用锁服务、不用 leader，全部协调归结为对象存储上的条件写（if-none-match 占槽 / if-match 推进或发布），并用 TLA+ 把"协议小到可以证明"变成工程事实。
3. **官方对数字的口径纪律**——几乎每个性能数字的脚注都主动交代对己不利的条件（非同硬件、测试环境瓶颈、外推段、modelled not billed、BEIR 正在重跑）。引用这些数字时，请连同脚注一起引用。

> 2.0 展望（`[blog §What's next]`，路线图非 Memlake 本体）：围绕 **decision models** 重建 retain/recall/consolidation/reflect/knowledge pages（[代码] 侧已有落点：consolidation triage #5664、三个 `*_use_decision_model` 配置）；知识与经验合一（文档/wiki/runbook 并入同一系统）；对通用与编码 agent 的更深集成与专用基准。
