# Ladybug 瓶颈调研与 Cognee vs Hindsight 最终裁决(1+1 审计版)

> 分析对象:LadybugDB 0.19.0(Kuzu 社区续作,cognee 默认图后端)+ cognee v1.6.0(锚定 663a2dc15)+ hindsight(与 [05 篇](05-code-architecture-cognee-vs-hindsight.md)同一基线)
> 分析日期:2026-09-29
> 分析方法:**3 个调研子代理并行**(Ladybug 性能 / 基准分数 / 架构对比)→ **2 个独立审计子代理 1+1 交叉核验**(一个复核全部网络数字与官方文档,一个逐条回查 16 项 file:line 代码主张)→ 主代理合并。审计推翻或修正之处在附录 B 全部列明。
> 证据等级:`[代码]`(子代理逐行验证)/ `[官方]`(官方文档/README)/ `[论文]`(arXiv)/ `[实测]`(第三方或本地实测)/ `[自报]`(厂商口径)/ `[推断]`(机制推断,无实测)
> 定位:05 篇已给出架构对比;本篇补上 Ladybug 规模红线、两家最新打榜数字的独立复核,并给出最终选型裁决。

---

## 0. 三句话结论

1. **Ladybug 的瓶颈是分层的**:最先撞上的不是引擎,而是 cognee 的部署默认值(磁盘/内存双 32GB)和集成层进程模型(同图查询实际串行);引擎本身的硬限制是"全库同一时刻只允许一个写事务"。**没有权威的规模实测数据**,但机制上可以画出红线(§1.3)。
2. **打榜分数:hindsight 全面领先,但两家从未在 LongMemEval 上同卷同榜对决**。同 harness 的直接对比在 AMB 榜的另外两个基准:领先 4.8 分(PersonaMem-32k)和 11.7 分(LoCoMo,cognee 只跑了 152 题小样本)。"94.6 vs 77.2 = 领先 17 分"的算法不成立。
3. **架构:hindsight 更先进但不碾压**。它是"读多写少的记忆数据库"的正确答案;cognee 在图分析与企业文档知识管理场景无可替代。选谁取决于要记忆还是知识(§3.3)。

---

## 1. Ladybug 会卡在哪(由浅入深)

### 1.1 它是什么

Ladybug 是 Kuzu 的社区续作:Kuzu 原公司 2025-06-30 停运(末版 v0.11.2),社区于 2025-10-07 以 MIT 协议 fork 出 LadybugDB 延续开发,自称 "formerly known as Kuzu" [官方]。cognee 将其作为默认图后端,pin 在 `ladybug==0.19.0`;macOS Ventura/Sonoma 另有 `>=0.17,<0.18` 的旧 pin(扩展二进制与存储格式匹配)[代码 pyproject.toml:91-92]。

### 1.2 三层瓶颈,一层比一层深

**第一层:cognee 替你做的部署决定(最先撞上,也最好改)**

- cognee 把 buffer pool 和数据库磁盘上限**都**钉死在 32GB(`1 << 35`,两处)[代码 adapter.py:70-71]。Kuzu 原生默认是磁盘上限 8TB(`1 << 43`)[官方 constants.h]、buffer pool 未设时自动取物理内存 ×0.8 [官方]。也就是说磁盘红线被砍了 256 倍。
- 磁盘那个是硬红线:图文件超过 32GB 直接报 "database is full" [官方]。两个都是构造参数,可调。

**第二层:cognee 的集成层进程模型(现实中最痛的一层)**

- 每个 dataset 一个 owner 子进程,该进程只开**一条**连接,请求**同步派发**(在事件循环内联执行 handler)[代码 harness.py:419-432]。官方文档确认 Ladybug 同进程可开多连接并发读,但 cognee 没有用上这个能力——结果是**同一个图上的所有查询实际排队串行**。
- 跨进程:一个图文件只允许一个 READ_WRITE 进程(OS 文件锁强制,第二个 RW 打开报锁错误,cognee 有专门的重试逻辑)[官方 docs.ladybugdb.com concurrency + 代码 kuzu_worker.py:35-38]。加 Uvicorn worker / 加 Pod 都不能给同一个图增加写入者。
- 任务队列默认 6 槽且仅进程内有效;多 API 进程 = 多套队列抢同一个文件锁 [代码 queue.py:83-86、shared/lru_cache.py:15]。

**第三层:引擎本身的硬限制**

- **全库同一时刻只有一个写事务**,写走 WAL + checkpoint;同进程多连接可以并发读 [官方 transactions 文档,2026-09-15 更新版]。
- **深遍历(VLE 变长路径)是已知弱项**:无界路径枚举工作量放大;官方 issue 里有 BFS extension 性能问题的记录(编号未二次核验)[官方]。cognee 自己的统计查询有真实翻车记录:5 节点玩具图、64MiB buffer 下递归统计直接报错 [实测,本地审计文档复现脚本,可重放]。

### 1.3 多大数据规模会出问题(分场景)

| 场景 | 红线 | 证据强度 |
|---|---|---|
| 单跳邻域查询(检索主路径) | 千万节点级机制上可用:引擎是磁盘式 out-of-core,buffer pool 不必装下全图 | `[推断]` **无权威公开实测;Kuzu 未参加 LDBC 审计** |
| 图文件大小 | cognee 默认 32GB 封顶,超限报错;调参可解 | `[代码]`+`[官方]` 确定 |
| 深遍历 / 全图统计 | 规模无关的病:小图也会翻车(无界枚举、cognee 统计查询三处缺陷);大图更糟 | `[代码]`+`[实测]` 确定 |
| 写入吞吐 | 全库单写事务 + 按文档逐批 MERGE(每批 2000 行,`_WRITE_CHUNK_SIZE`)[代码 adapter.py:67] → 大批量导入受制于单写者 | `[代码]` 确定 |
| 并发用户 | 同图查询串行,瓶颈是"排队"而非"算不动";中等规模先撞这堵墙 | `[代码]` 确定 |

> 官方自测数字(SNB Interactive SF10 约 4750 txn/s = **4.75× Neo4j**,2024-03 博客;SNB BI 查询 12.3× Neo4j、导入 5.2×/54×)[官方自报,博客已下线,部分日期未核]。第三方数字互相矛盾。**结论:百万节点以内、单跳为主、单写者可排队的中等规模,Ladybug+cognee 现实可用;要求高并发写、深遍历、图分析或超 32GB,按 [cognee 目录第 9 章](../cognee/graph-storage-postgres-analysis.md) 的路线 A/B/分布式评估换方案。**

---

## 2. 打榜分数对比(2026-09 最新,全部经独立复核)

### 2.1 分数表

| 基准+变体 | 跑分方 / harness | cognee | hindsight | 分差 |
|---|---|---|---|---|
| LongMemEval-S(500 题) | hindsight 官方 AMB 榜 | 未参加 | **94.6%**(473/500;answer=gemini-3.1-pro-preview,judge=gemini-2.5-flash-lite) | — |
| LongMemEval-S | hindsight 论文(arXiv 2512.12818,Table 3) | 未出现 | **91.4%**(Gemini-3 Pro 作答,OSS-120B 判卷)/ 89.0%(OSS-120B)/ 83.6%(OSS-20B) | — |
| LongMemEval-S | cognee 自家 harness(2025-10 博客,kuzu+lancedb) | **77.2%**(同期同 harness:Zep 71.2 / Mem0 68.4) | 未自跑 | — |
| PersonaMem-32k(589 题) | **AMB 同 harness** | 81.8% | **86.6%** | **+4.8** |
| LoCoMo10 | **AMB 同 harness** | 80.3%(**仅 152 题子集**) | **92.0%**(1540 题) | **+11.7**(题量不对等) |
| PrecisionMemBench(77 题) | AMB;cognee 数字为外部导入基线(source 标注 unverified) | 14.3% | 85.7%(hindsight-cloud) | 口径不同源,仅参考 |
| BEAM-100K | cognee 自评(REPORT.md L98:held-out 单会话 20 题×4 轮)vs AMB(n=400) | 0.79 | 86.2% | 不同 runner/协议,双方均声明不可比 |

OmniMemEval(程序性记忆基准):两家均未找到参跑记录。

### 2.2 分差到底多大——说人话

- **唯一干净的同卷对比是 AMB 榜**(同一模型答题、同一模型判卷,审到 run 文件级):hindsight 赢 **4.8~11.7 分**。这是目前最可信的差距量级:个位数到十个百分点。
- **LongMemEval 没有同榜对决**:hindsight 的 94.6% 在自己运营的榜单;cognee 的 77.2% 是自家 harness 自报(原博文已 404,但搜索快照、第三方索引 aimemos、以及 Zep 71.2/Mem0 68.4 两个锚点数字均可与 hindsight 论文和 Mem0 论文交叉印证)。
- **间接锚点**:Zep 在两家材料里都是 71.2%。各自口径下,cognee 比 Zep 高约 6 分,hindsight 高约 23 分——间接看差距可能比 4.8 分大,但没有同卷证据。

### 2.3 看分数前必须知道的事

1. **AMB 榜由 hindsight 方(vectorize)运营**——已核到其 manifesto 博文、harness 仓库与 analytics 域名署名。"运动员兼裁判"结构:hindsight 的数字本身可信(逐条核到 run 文件),但谁入场、怎么分题,话语权在它手里。
2. cognee 的 77.2% 发布后卷入 Zep/Mem0 的 judge 方法学争议;cognee 自己后续发文称"记忆基准都坏了"(博文现存链接均已 404)。
3. hindsight README 声称 Virginia Tech Sanghani Center 与 Washington Post "独立复现"其分数——**未检索到任何公开复现报告,仅为 README 自述** [自报]。

---

## 3. 架构对比(16 项代码主张,逐条审计后)

### 3.1 两句话各自的哲学

- **cognee**:知识图谱平台。三存储层(图/向量/关系)后端可换,LLM 在 cognify 时抽实体关系建图,检索靠图遍历 + LLM 生成答案。
- **hindsight**:记忆数据库。单 Postgres,图降级为写时派生的几张链表;LLM 的活全部前置到写入,**查询期零 LLM**,N 个 fact_type × 4 路并行检索(语义/BM25/图链接扩展/时间)+ RRF + cross-encoder 重排 [代码 memory_engine.py:7928-7935]。

### 3.2 六个维度(核心差异,全部 [代码])

| 维度 | 谁占优 | 一句话理由 |
|---|---|---|
| 数据模型 | hindsight | 五维事实→observation(带 `proof_count`+`source_memory_ids` 溯源,consolidator.py:9-10/413-414)→mental model 水位刷新(mental_model_refresh.py:166);cognee 是拓扑结构,没有"这条结论有几个证据"的概念 |
| 写入路径 | 平手 | 都靠写时 LLM;cognee 幂等简单(UUID upsert+advisory lock),hindsight 约束更密(内存预算 orchestrator.py:2566、chunk 幂等 :756-766、因果只能指向更早的事实 fact_extraction.py:241-252) |
| **查询路径** | **hindsight** | cognee 每答一次过一次 completion LLM(graph_completion_retriever.py:338-363/418),延迟不可预算;hindsight 纯 SQL,延迟=数据库时间 |
| 生命周期 | 各擅一头 | cognee 强在**删除正确性**(provenance 规划区分 unowned/surviving,planner.py:81-92;forget);hindsight 强在**信念演化**(0.97 阈值+LLM 仲裁去重 consolidator.py:318/353-359、refine-not-overwrite) |
| 部署运维 | hindsight | 一个 Postgres(104 个 alembic 迁移,schema-per-tenant env.py:119-136)vs 三个可换后端;cognee 换后端踩"47 方法里 7 个未实现"的坑 |
| 学习自改进 | hindsight | cognee improve 九阶段是编排器(registry.py:27-37,仅 persist_session_qa fatal);hindsight 巩固是带不变式的自治子系统(consolidator 约 3758 行) |

### 3.3 分场景裁决(不和稀泥)

| 场景 | 判给谁 | 为什么 |
|---|---|---|
| 对话型个人助手记忆 | **hindsight** | 时间/因果检索+巩固+查询零 LLM 低延迟;cognee 每轮多一次 LLM 往返 |
| 企业文档知识库问答 | **cognee** | 文档级溯源删除/重摄、chunk 级增量更新、本体/代码/多格式摄取是企业刚需;hindsight 的文档管理是二等公民 |
| 图分析(多跳、图算法) | **cognee** | 真图库有 VLE/Cypher/图统计;hindsight 的"图"是写时链表,一跳封顶,无算法能力 |
| 多租户 SaaS | **hindsight** | schema-per-tenant 一套 PG 搞定+审计;cognee 要求图/向量两个后端都支持隔离 |
| 单机内嵌零配置 | cognee 略胜 | keyless 本地模型零 API key 可跑 |

### 3.4 各自最痛的短板

- **cognee**:能力完整性成了隐藏的后端契约——换图后端,反馈权重/truth/局部更新静默降级,平台承诺被自己的接口矩阵掏空。
- **hindsight**:PG 锁死天花板,图永远只能做"派生链";外加 `memory_engine.py` 22,802 行的 god object,二次开发成本持续上升。

---

## 4. 综合裁决

**架构先进度:hindsight 胜**。三条硬理由:

1. 记忆系统读远多于写,"写时建链换查询零 LLM"的成本结构方向正确;cognee 查询期 LLM 使延迟与成本不可预算。
2. 证据/推论分离 + 机制强制的巩固层是记忆可靠性的一等解;cognee 的 improve 本质是编排器,缺少 proof_count 式可撤回的信念链。
3. 单 Postgres 把运维面压到最小——cognee 上需要做的整份"换 PG 适配评估"(见 cognee 目录),在这个架构里从源头就不需要。

**但不是无脑换**:同 harness 分数差 4.8~11.7 分是"领先"而非"碾压";若有真图分析需求或多格式文档知识管理,cognee(或 cognee + 服务型图库)仍是正解。

---

## 附录 A:关键分数来源

- hindsight AMB 榜与 run 文件:[benchmarks.hindsight.vectorize.io](https://benchmarks.hindsight.vectorize.io) / [agentmemorybenchmark.ai/api/results](https://agentmemorybenchmark.ai/api/results)(LongMemEval-S run:outputs/longmemeval/hindsight/rag/s.json,accuracy 0.946)
- hindsight 论文:[arXiv 2512.12818](https://arxiv.org/abs/2512.12818) Table 3
- cognee 77.2%:官方博文(现 404,[搜索快照标题](https://www.cognee.ai/blog/deep-dives/cognee-hits-77.2-on-longmemeval-beating-mem0-zep-and-openai-baselines) + [aimemos 索引](https://www.aimemos.com/submissions/cognee-memory));锚点:Zep 71.2 见 hindsight 论文引 Supermemory 报告,Mem0 68.4 见 [arXiv 2504.19413](https://arxiv.org/abs/2504.19413)
- cognee BEAM:cognee 仓库 `cognee/eval_framework/beam/REPORT.md`(main 与 663a2dc15 内容一致)
- Ladybug 并发模型:[docs.ladybugdb.com](https://docs.ladybugdb.com/cypher/transaction) 与 concurrency 页;Kuzu 自测:[blog.kuzudb.com "Kuzu is fast(ish)"](https://blog.kuzudb.com/post/kuzu-is-fast-ish/)(2024-03)

## 附录 B:1+1 审计推翻/修正/升级清单

**推翻/撤回**(原调研 → 审计结论):

| # | 原主张 | 审计结论 |
|---|---|---|
| 1 | cognee issue #910:400KB 文本后每文档 6→15 分钟 | **撤回**:#910 实为一个无关 PR;未找到该退化案例的原始 issue |
| 2 | Kuzu #5970:120M 节点 1.2GB 数据、4GB buffer 完成 COPY | **撤回**:#5970 是 UUID/int128 checkpoint 压缩 bug,与规模无关 |
| 3 | SNB 9.4× DuckDB | **查无出处**:原文只说对嵌入式系统 2.5-3× |
| 4 | LadybugDB 自称与 Kuzu 0.11.2 性能 parity | **降级**:README/docs 均无此表述,仅 "formerly known as Kuzu" |
| 5 | SNB BI 5.2×(查询) | **修正**:5.2× 是导入提速,查询是 12.3× vs Neo4j;发布日期未能核实 |
| 6 | Kuzu buffer pool 需手动设置 | **修正**:默认自动 = 物理内存 ×0.8;cognee 才是把它钉到 32GB 的一方 |

**修正/降级**:4.75× Neo4j 确认但日期修正为 2024-03(非 2024-06)。

**升级**:hindsight 94.6% 从"榜单页面"升级到**官方 run 文件级证据**(500 题、473 对、模型配置逐字段核对);架构对比 16 项代码主张 **13 项完全确认**,2 项内容成立但改出处后成立(因果方向约束的正确出处是 `fact_extraction.py:241-252`;`EntityType` 链不在 cognify.py 而在 `extract_graph_from_data.py:88` 与 models),其余 1 项行号微偏。基准分数表内所有数字(94.6/91.4/89.0/83.6/86.6/81.8/92.0/80.3/85.7/14.3/0.79/77.2)全部复核成立。
