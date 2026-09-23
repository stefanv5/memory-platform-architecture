# ContextDB 竞品深度分析与自研架构建议

> 目标场景：**团队/公司级 Agent 上下文数据库**——对标阿里云 RDS ContextDB 且性能更优；agent 介入后自动沉淀团队知识；后续会话高效召回关键历史；降 token；少描述即可从组织知识图谱命中关键信息；沉淀的是"关键且正确"的数据；具备 RSI，让数据活起来。
> 分析日期：2026-09-23
> 证据等级标注：`[代码]`=子代理在仓库中逐行验证；`[官方]`=竞品官方文档/README；`[论文]`=arXiv 论文摘要/全文；`[自报]`=厂商营销口径。
> 配套文档：基础竞品分析见本目录 [01-hindsight-vs-competitors.md](./01-hindsight-vs-competitors.md)。

---

## 1. 需求解构：把你的愿景拆成 7 项可评测能力

| # | 需求 | 技术含义 | 竞品评测维度 |
|---|---|---|---|
| R1 | 团队/公司级存储 | 组织→团队→agent→会话的层级对象模型；权限/隔离；多 agent 共享与个人视图并存 | 层级模型、RBAC、共享机制 |
| R2 | 自动知识沉淀 | agent 干完活，知识自动留下来且自动组织——无需人工整理 | 沉淀管线的自动化程度与组织结构 |
| R3 | 高效召回历史关键会话 | 跨会话检索、低延迟、高精度 | 检索架构、延迟数据 |
| R4 | 降低 token 使用 | 记忆命中的成本收益 | 量化 token 数据 |
| R5 | 少描述即可命中组织知识图谱 | 图导航/多跳/实体聚合，用户不用把问题问得很完整 | 图能力、图查询面 |
| R6 | 关键且正确 | 证据链、冲突消解、时效性、撤回——错误知识不沉淀、过时知识不残留 | 正确性机制 |
| R7 | RSI（数据活起来） | 递归自我改进：数据自我组织、自我修正、自我增值、（理想态）系统自我调参 | 进化机制的有无与深浅 |

**一个关键的行业事实先摆在前面**（影响价值主张）：MemTensor 的 OmniMemEval 横评（arXiv:2511.12419，14 个记忆产品）发现 **full-context 基线在多会话对话准确率上未被任何现成系统击败**。所以 contextDB 的价值主张不能是"比 full-context 更准"，而必须是：**成本（token/延迟）+ 团队复利（跨人跨 agent 的知识沉淀）+ 可解释（正确性）**——恰好是你 R2/R4/R6 要的东西。

---

## 2. 能力矩阵总览

●=一等公民/产品化　◐=部分支持/需扩展/披露不足　○=缺失

| 能力 | Hindsight | Zep/Graphiti | Mem0 | MemOS | Cognee | Letta |
|---|---|---|---|---|---|---|
| R1 团队/组织级 | ◐（可造但不产品化） | ●（group graphs） | ●（org→project→user） | ◐（cube 共享） | ●（org→tenant→workspace+四权限） | ○ |
| R2 自动沉淀 | ●（巩固+活文档） | ◐（episode→实体边） | ◐（事实加改删） | ●（四层自动分层） | ◐（session distillation） | ◐（自编辑记忆块） |
| R3 高效召回 | ●（四路+RRF+重排） | ●（混合+双时态） | ◐（向量+可选图） | ◐（混合检索） | ◐（三模式） | ◐（两层检索） |
| R4 降 token | ●（机制最多，无营销数字） | ◐（延迟-90%，token 未报） | ●（论文 -90% token） | ●（-35.24%/-72% 自报） | ○（无声明） | ◐（分层即省） |
| R5 图谱导航 | ◐（实体图+图可视化） | ●（图是一等公民） | ◐（可选 Mem0g） | ◐（Neo4j 图结构） | ●（图引擎为核心） | ○ |
| R6 关键且正确 | ●（全链路证据链+撤回） | ●（双时态失效） | ◐ | ◐（溯源元数据） | ◐（chunk 溯源） | ○ |
| R7 RSI | ◐（维护型自洽，无学习闭环 `[代码]`） | ◐（图自组织） | ○ | ●（self-evolving+技能结晶） | ◐（自改进管线） | ◐（agent 自编辑） |

**矩阵读法**：没有任何一家同时拥有 ● 于 R1+R2+R6+R7——这正是你的 contextDB 的空位。最接近的组合是 **Zep（R1/R3/R5/R6）+ Hindsight（R2/R3/R4/R6）+ MemOS（R7）**，但三家哲学互斥（图库/单库/多库），无法拼装，只能吸收设计。

---

## 3. 逐竞品深拆

### 3.1 Hindsight —— 沉淀与正确性最强，团队级是"可造未造"

**R1 团队级 `[代码]`**：
- 隔离边界 = Postgres schema-per-tenant；**bank 是"召回边界"不是存储单元**（官方决策规则："A retains 的记忆 B 能否 recall？是→同 bank，否→不同 bank"）；软分区 = tag（官方原话：**"a filter you can forget to pass is not isolation"**——这是整个赛道最重要的安全教训，你的 contextDB 应该内建它）。
- **没有 org 对象**：per-user/per-org/per-agent 全靠 bank_id 命名约定；directives/mental models/巩固策略全部 bank 作用域，**无跨 bank 继承**。
- **无 RBAC**：控制平面单访问密钥；数据平面身份只有 tenant/key 级。
- **无跨 bank 查询**：多 bank 合成是应用层 fan-out；**无 bank 间同步/订阅**（clone 是时点拷贝，merge 不去重）。
- **但扩展点恰好齐**：`OperationValidatorExtension.validate_recall` 支持 `accept_with(tags=..., tag_groups=...)` **服务端强制注入 tag 过滤**——RBAC 的钩子已存在，产品没做；`TenantContext` 只是一个 schema 字符串，N 个身份可映射到同一 schema（多 agent 共享存储不被引擎禁止，只是官方扩展拒绝）。
- 多 agent 共享已有实证：coding-agents 集成默认 bank id = `coding-agent::{gitProject}`（**刻意 harness 中立**——Claude Code/Codex/opencode 同 repo 已共享一个 bank），配 `retainTags` 盖来源戳 + recall 按 `project:{repo}` 过滤；`observation_scopes: "shared"` 保证两个 agent 的信念**合流而不是长成两套**。

**R2 沉淀 `[代码]`**：全赛道最强。四路触发巩固（retain 后/删除失效后/手动/每 300s 对账兜底）；8 条成文方针 + 机制强制；per-scope 巩固策略（按 tag-glob 认领作用域，整条覆盖 mission/上限/证据预算——可直接映射"per-team 治理策略"）；产物是带 proof_count 的 observations → 自动重写的 knowledge pages（watermark 陈旧度 + 撤回检测 + 块级 delta 刷新，不变式"文档只会变好或不变"）→ 可挂载为文件系统。

**R3 召回 `[代码]`**：四路并行（语义/BM25/图/时间）单 SQL 多臂、per-(bank, fact_type) partial HNSW、RRF+interleave 融合、cross-encoder 重排、阶段级 tracer 恒开。全赛道检索工程最深。

**R4 降 token `[代码]`**：机制最多的一家——**mental model 读取 = 纯数据库读，零 LLM**（团队共享上下文的成本杀器：一次合成，全员免费读）；recall `thinking_budget` LOW/MID/HIGH 映射 100/300/1000 或按 max_tokens 自适应比例；reflect 100k 上下文累计预算 + 超支强制综合；LLM cache 亲和（xAI/OpenAI/Anthropic/Gemini 四家缓存管理）；delta retain（哈希 diff 只重抽取变更 chunk）；`processed_content_tokens` 计量计费口径。**但没有对外营销的 token 节约百分比**——论文的角度是"20B 模型+记忆 39%→83.6% 打赢 GPT-4o full-context"（用小模型+记忆替代大模型裸跑，成本论）。

**R5 图谱 `[代码]`**：实体共现图 + 单 CTE 图扩展 + observation 传递扩展（同实体的 observation 互相激活）；control plane 有 constellation 图可视化 + `entities/graph` API。图是检索信号，不是给人导航的查询面（无 Cypher 类交互）。

**R6 正确性 `[代码]`**：全链路——fact 引用附件、observation 引用源事实（proof_count + source_memory_ids）、强制 reason 审计、撤回检测（区分"断链"与"真撤回"，专门的 "unsay" pass 只删被撤回证据支撑的论断）、失效单元归档不销毁。

**R7 RSI `[代码]`**：**只有"维护型自洽"，没有学习闭环**——这是本次分析最重要的代码级发现。已有的：撤回、staleness watermark、对账兜底、图自愈（链路补齐/实体剪枝的预算化队列）。没有的：**召回使用反馈不回流**（没有 recall 命中→巩固优先级加权；没有 reflect 采纳率→重要度评分；adaptive budget 是运维配置的比例，不是学出来的）。注释原话水平的一致性说明团队知道什么该自动化——但"什么知识有用"这个信号整个系统没有采集。

**对 contextDB 的结论**：沉淀/正确性/降 token 三个最难的子系统已被 MIT 开源且被基准验证；**缺的是 R1 产品化（org 对象/RBAC/联邦召回）、R7 学习闭环**——恰好是你该加的两层。

### 3.2 Zep / Graphiti —— 团队共享与图导航最强，代价是架构重

**R1 团队级 `[官方]`**：**group graphs 是赛道唯一的团队共享一等公民**（Community Edition v0.26.0+）：group 内含 episodes/entities，可按团队/项目/共享上下文建多 group；用户子图（社区）+ 之上的组织共享层；一个用户可属多 group；团队 agent 能看到全团队成员的会话。注意官方工程师口径：group 功能**不绑定多租户**（多租户在云/EE 门控）。
**R2 沉淀 `[论文]`**：查询时 LLM 从对话抽取实体/边入图，双时态标注（事件时间 vs 摄入时间）；**没有信念层**——沉淀止步于"图上的结构化事实"，无巩固学说、无合成文档。
**R3 召回 `[论文]`**：混合检索（语义+图+BM25）；DMR 94.8%（MemGPT 93.4%）；LongMemEval 准确率 +16~18.5%，**延迟比基线实现降 90%**。
**R4 降 token `[论文]`**：论文未报 token 数。
**R5 图谱 `[官方]`**：最强——图是一等公民，双时态边（事实失效不删除，标注有效期），支持社区检测。少描述命中组织信息的场景（"我们用的那个图数据库叫什么来着"）图导航天然占优。
**R6 正确性 `[论文]`**：双时态失效是时效正确性的最优解（新边使旧边失效而非覆盖）；溯源到 episode。
**R7 RSI `[官方]`**：Graphiti 的**动态社区检测**让图周期性自组织（实体聚类重组）——数据自我重排；无学习闭环。
**代价**：依赖 Neo4j/FalkorDB 独立图库 + 查询时 LLM 建图（写入延迟与成本在查询侧感受不到，但团队级高频摄入时账单在摄入侧爆发）；准确率口径自报（LongMemEval-S ~63.8%）。

**对 contextDB 的结论**：group graphs 的对象模型和双时态失效值得直接抄；**它的架构（外部图库+查询时建图）不值得抄**。

### 3.3 Mem0 —— 平台层级最完整，沉淀最浅

**R1 团队级 `[官方]`**：平台侧 **Organization → Project → User → Memory** 层级完整（org/project 双层 API key、owner/admin/member 角色、dashboard 管理）；API 三级作用域 `user_id` / `agent_id` / `run_id`；团队共享靠模式（共享 agent_id 或 project 级记忆 + 个人记忆组合 + 自定义 categories）。OSS 版无此层级。
**R2 沉淀 `[论文]`**：自动"抽取→巩固决策（ADD/UPDATE/DELETE）→检索"；**沉淀产物是离散事实**，无信念合成、无证据链——团队知识以扁平事实堆积，越积越难管。
**R3/R4 `[论文]`**：LoCoMo 上比 OpenAI Memory **相对提升 26%**；**p95 延迟比 full-context 低 91%、token 成本节约 >90%**（全赛道最响的成本数字，论文口径）；图变体 +2%。
**R5**：Mem0g 可选图记忆，非核心。
**R6**：update/delete 决策存在但不可审计到证据级；被覆盖的旧事实的去向不透明。
**R7**：○——加改删是记忆维护，不是进化。

**对 contextDB 的结论**：**平台的层级/密钥/角色产品化设计是 R1 的最佳参考**（你的 org→space→agent 对象模型照这个深度做）；它的沉淀深度是反面教材——你的用户会以"团队"规模把离散事实问题放大 N 倍。

### 3.4 MemOS —— RSI 最激进，工程披露最弱

**R1 团队级 `[官方]`**：memory cubes 可组合/受控共享/动态构成；README 列"multi-agent collaboration"为插件特性。对象模型年轻（2.0 Stardust 才引入），权限模型无披露。
**R2 沉淀 `[官方]`**：**四层自动分层 L1 traces → L2 policies → L3 world models → crystallized Skills**，与 Hindsight 的 facts→observations→mental models 独立收敛到同一结构（赛道趋同的证据）；MemScheduler 异步摄取。另原生多模态（文本/图像/tool traces/persona）。
**R3/R4 `[官方]`**：OmniMemEval 十数据集：LoCoMo 88.83 / LongMemEval 89.20 / BEAM-10M 56.75；token：**-35.24%**（README）、云插件 **-72%**；OpenClaw 任务完成率 36.63%→50.87%。
**R5**：Neo4j 图结构记忆（"inspectable and editable by design, not a black-box embedding store"）。
**R6**：MemCube 元数据含溯源/版本；自然语言记忆反馈（correct/supplement/replace）。
**R7 RSI `[官方]`**：**赛道最激进**——官方定位就是 "Self-evolving memory OS"；论文层（arXiv:2507.03724）主张记忆在明文/激活（KV-cache）/参数（LoRA）三形态间迁移融合，即**高频技能可结晶进模型参数**——这是"数据活起来"的终极形态，也是唯一把 RSI 推到参数层的竞品。工程落地深度无法从公开资料验证（README 无对应机制细节）。
**代价**：存储依赖 Neo4j+Qdrant 双外部系统；API 面披露稀疏（REST only）；权限/审计/企业面空白；工程透明度（测试/CI 无公开数据）。

**对 contextDB 的结论**：**"skills 结晶"的产品概念值得跟进**（团队级意义巨大：一个 agent 摸索出的 SRE playbook 结晶后全员可用），但参数化记忆（写进模型权重）对团队场景落地遥远（版本管理/回滚/权限全无解），建议作为 R7 的远期选项。

### 3.5 Cognee —— 权限模型最细，正确性口径最弱

**R1 团队级 `[官方]`**：**Organization → Tenants → Workspaces** + `SpecificPermission`（READ/WRITE/DELETE/SHARE）在 API 层强制——**赛道唯一的四类权限原语**；每个节点/边带 `user_id` 标签实现存储级隔离（向量库/图库/关系库三处都带）；权限路线图（issue #611："公司里不同人能看到图的不同部分"）。
**R2 沉淀 `[官方]`**：三条管线（ingestion/session learning/self-improvement）共享 Task/DataPoint 结构；session distillation 固化"被接受的教训"；**无信念层证据链披露**。
**R3/R5**：三检索模式（semantic/structural Cypher/hybrid）；图引擎为核心，少描述导航强。
**R4**：无 token 声明。
**R6**：chunk 溯源 + "deterministic memory" 倡导（类型安全本体/schema 校验）；**但基准口径混乱**（同一 LoCoMo 官方数字 0.83~0.925 随出处漂移）——"正确"这件事上言行差距最大的竞品。
**R7**：self-improvement 管线明确存在（架构文档三管线之一），深度披露少。
**商业边界**：生产级图存储是授权产品（开源单 Postgres 图模式官方标 demo）。

**对 contextDB 的结论**：**权限原语设计（READ/WRITE/DELETE/SHARE + 节点级 user_id）是 R6/R1 的最佳参考**；它的教训是：一个"正确性"为卖点的产品如果评测口径不严肃，信任资产会归零。

### 3.6 Letta（简评）

代理自管理记忆（MemGPT 分页），记忆=代理的内部状态而非共享基础设施。团队级无原生对象（R1 ○）。对 contextDB 的启示只有一条：**让 agent 自己能改记忆（自编辑工具）是 R7 的一条可选路径**，但对组织场景风险高（一个 agent 的错误编辑污染全团队知识——你的系统必须默认"agent 可写草稿层，巩固层独占晋升权"）。

---

## 4. 对标 RDS ContextDB：两条路线的差异与"性能更好"的含义

> 诚实声明：本次会话公开检索工具间歇不可用（Bing/DuckDuckGo 反爬、搜索降级），未能核实 RDS ContextDB 的官方规格细节。以下基于行业理解与用户参照；如你能提供产品文档链接，我可以补一节精确对标。

**RDS ContextDB 代表"数据库厂商路线"**：把 agent 的上下文（会话、记忆、知识）做成 RDS 内的原生能力——数据不离库、检索在引擎内完成、和现有数据库运维/安全/计费体系打通。它的性能优势来源可以推断为：①数据就近（无网络跳数）②SQL/存储引擎层做检索 ③云厂商的硬件与索引优化。

**你要"性能更好"，赢面不在照抄它，而在它做不到的三件事**：

| RDS ContextDB 的结构性局限 | 你的 contextDB 的打法 |
|---|---|
| 上下文=存储+检索，**没有知识加工层**（DB 厂商不做 LLM 编排） | 沉淀管线（巩固→信念→技能）是 LLM 编排层的活，数据库厂商天然缺位——这是 OSS 记忆系统整个品类的立身之本 |
| 通用索引，不懂记忆的形状 | 记忆专用索引：per-scope partial HNSW、时间桶覆盖选点、写时链接图、boot page 物化（见 §6.6） |
| 无使用反馈闭环 | RSI 闭环（§6.5）——数据越用越准，通用 DB 做不到 |

**延迟预算基线**（你的 SLO 设计目标，依据竞品机制推算）：recall p95 ≤ 150ms（4 臂 SQL ~30-60ms + RRF ~5ms + 重排：本地 cross-encoder ~30ms / passthrough ~5ms）；boot page 读取 ≤ 5ms（纯 DB read）；retain ACK ≤ 50ms（LLM 抽取全异步）。对比锚点：Mem0 论文口径 full-context 的 p95 是其 11 倍；Zep 比基线实现低 90%。

---

## 5. 跨竞品缺口分析 = 你的机会点

六家都没做到的六件事（按价值排序）：

1. **R7 学习闭环（所有竞品为零）**：没有一家把"召回命中/回答采纳/用户修正"回流到知识治理。Hindsight 代码级确认无此机制；MemOS 声称 self-evolving 但无公开机制细节。**做第一个闭环产品。**
2. **组织级继承（R1）**：org 全局 directive/knowledge 下发给 N 个 bank/space，Hindsight 明确缺失（bank 作用域死锁），Mem0 有层级无继承语义，Cognee 有层级有权限但无知识继承。
3. **服务端联邦召回（R1+R3）**：entitlement-aware 的跨空间合并排序召回端点。Hindsight 让客户自己 fan-out，Zep 靠 group 预定义。
4. **沉淀的权限化（R6×R1）**："谁能沉淀进团队知识层、谁能晋升 observation、谁只能写草稿"——Cognee 的四权限最接近但没有和沉淀层级绑定。
5. **正确性的可展示性（R6）**：每条答案附"这条知识来自哪三个会话、何时被何证据修正"——Hindsight 有数据（证据链）但反射到 UI 的深度一般，其他家连数据都没有。
6. **技能层（R7×R2）**：MemOS 的 L1-L4 中 Skills 是概念，AWM/Voyager（研究）证明了轨迹→可复用 playbook 的可行性——团队级"一个 agent 的经验全员复用"是 all 竞品都讲没做透的故事。

---

## 6. 自研 contextDB 架构蓝图

### 6.1 总体架构（三层平面）

```
┌─────────────────────────────────────────────────────────────┐
│  RSI 闭环层（自调参引擎）—— 全赛道空白，你的核心差异化          │
│  使用反馈 → 重要性加权 → 治理优先级 → 配置自进化 → 评估回归      │
├─────────────────────────────────────────────────────────────┤
│  信念平面（活数据层）                                         │
│  observations(证据链) → knowledge pages(活文档) → skills(技能) │
│  权限原语: READ/WRITE/CONTRIBUTE/GOVERN (Cognee×4 + 晋升语义)  │
├─────────────────────────────────────────────────────────────┤
│  证据平面（Postgres 单库一体化）                               │
│  sessions/messages → facts(+五维+因果序) → entities+links     │
│  检索: 4臂SQL(语义/BM25/图/时间) + RRF + 重排 + 预算           │
│  隔离: org→space→agent, 权限过滤内联在SQL (Hindsight教训)      │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 数据模型草案

```
org ── space(团队/项目) ── agent/user ── session
                  │
   trust_domain = space（硬边界 = schema 或 row-level scope_id 列）
   个人/子团队视图 = 服务端强制注入的 scope 过滤（永不依赖调用方传参）

memory_units(id, scope_id, text, fact_type, occurred_start/end,
             mentioned_at, embedding, text_signals, tags,
             proof_count, source_session_ids,  ← 证据链
             importance, use_count, last_used_at, ← RSI 字段
             valid_from, valid_to)             ← 双时态（抄 Zep）

memory_links(from, to, link_type[temporal/semantic/causes/...], weight)
beliefs(=observations: scope, content, proof_count, source_unit_ids,
        superseded_by, status[active/retracted/superseded])
pages(=活文档: standing_question, blocks, watermark, based_on→beliefs)
skills(=结晶: trigger条件, playbook步骤, 来源轨迹ids, 成功率统计)
permissions(space, principal, READ/WRITE/CONTRIBUTE/GOVERN)
feedback(recall_id, unit_id, signal[used/cited/corrected/discarded], ts)
```

### 6.3 沉淀管线（双速率 + 三层晋升）

- **实时层（ms~s）**：retain ACK 后异步抽取（五维事实+实体+因果序，抄 Hindsight schema）；会话结束触发轻量摘要。agent 感知不到延迟。
- **后台层（min）**：巩固 per-scope（Hindsight 已证可审计化：8 条方针+强制 reason+0.97 去重仲裁）；**晋升权独占**——只有巩固器能写 beliefs，agent 永远只写 facts（防 Letta 式错误污染）。
- **慢速层（h~d）**：pages delta 刷新（watermark+块操作不变式）；**skills 蒸馏**（新）：巩固时识别重复成功轨迹（AWM 思路）→ 生成 playbook 草稿 → 治理者/高置信度自动晋升 → 全 space 可读。
- 冲突闸门：新旧事实矛盾 → 保留双方 + LLM 仲裁标注 superseded（不物理覆盖，抄双时态精神）。

### 6.4 召回管线（性能核心：权限下沉到 SQL）

```
query → [boot-page 命中? → 直接返回 (≤5ms)]
      → 单SQL四臂检索（WHERE scope_id ∈ entitled scopes 内联）
        语义: per-(scope,fact_type) partial HNSW
        BM25: 原生tsvector起步(5后端可换)
        图:   单CTE实体共现+链接扩展
        时间: 日期解析+时间桶覆盖
      → RRF融合 → 重排(可配: 本地CE / passthrough)
      → token预算裁剪 → 附证据(来源会话+proof)
```

关键决策：**权限过滤必须是 SQL 谓词而不是服务层后过滤**（Hindsight 官方教训 "a filter you can forget to pass is not isolation"）——这也正是单库路线的性能红利：权限、检索、图遍历一个引擎一次往返。

### 6.5 RSI 闭环设计（数据活起来的六环）—— 核心差异化

1. **沉淀**：所有会话自动入证据平面（R2）。
2. **验证**：巩固时证据链强制 + LLM 仲裁 + 冲突消解（R6）。
3. **融合**：去重/合并/单调时间界。
4. **失效**：双时态 + 撤回检测 + 失效传播到 pages/skills（Hindsight 已证可行）。
5. **蒸馏**：observations → pages → skills 的价值晋升；**使用加权**（use_count/importance 字段——高频被引用的知识优先晋升与刷新）。
6. **自调参（全赛道空白，闭环的最后一米）**：
   - **feedback API**：客户端上报 recall 命中是否被引用/采纳/纠正（一条轻量端点+一张表，成本极低）；
   - 反馈回流：巩固优先级 = f(该 scope 的近期使用率)；budget 自适应 = f(命中率历史)（Hindsight 的 `adaptive` 预算是固定比例，改成滑动窗口学习）；页面刷新优先级 = f(读取频率×staleness)；
   - 质量回归：内置 judge 管线（Hindsight 的 hs_llm_core 模式已给出现成方法论）对治理动作做 A/B（新巩固策略 vs 旧策略跑同一评测集）——**系统的每次自我修改都被评测守护**，这是 RSI 不跑偏的安全绳；
   - 远期：高频 skills → 模型 adapter 结晶（MemOS 方向，版本化+可回滚后再考虑）。

### 6.6 性能工程清单（对标并超越 RDS ContextDB）

| 技术 | 来源/依据 | 预期收益 |
|---|---|---|
| 单 Postgres 一体化（关系+向量+BM25+图） | Hindsight `[代码]` 已验证 | 消除跨库往返；运维=一个 DB |
| per-(scope, fact_type) partial HNSW | 同上，迁移注释含规划器代价分析 | 万级 bank 下仍走索引 |
| 时间桶覆盖选点替代 recency 排序 | 同上（66 万行上从 30s+ 全扫修复为索引探测） | 时间查询 O(1) 探测 |
| 单 CTE 图扩展（写时建链） | 同上 | 图检索一次往返、零 LLM |
| boot pages 物化 | 同上（DB read，零 LLM） | 热点知识 ≤5ms，团队共享零边际成本 |
| 嵌入 float32 打包 + 缓存式格式化 | 同上（7.6× 内存缩减） | 写吞吐 |
| 规范化锁键排序消死锁 | 同上（并发写死锁构造性消除） | 团队高频并发 retain 不死锁 |
| LLM cache 亲和 + 按操作分模型 | 同上 | 巩固/反思用便宜模型，抽取 prompt 跨 bank 共享缓存 |
| delta retain（哈希 diff） | 同上 | 重复会话前缀零重抽取 |
| 异步 operations + webhook | 同上 | retain ACK ≤50ms |

### 6.7 基座建议：Fork Hindsight，而不是从零写

- **理由**：§6.3–6.6 里约 70% 的最难子系统（抽取 schema、巩固方针+机制、四臂检索+RRF+重排、mental models delta 刷新、证据链、扩展槽、预算体系、双方言迁移）在 Hindsight 里已 MIT 开源、有 ~1.1 万测试用例和基准背书。从零重写这些等于重走 18 个月的弯路。
- **Fork 后必做的四件事**（即 §5 缺口）：①org/space 对象模型 + 权限原语产品化（借 Mem0 层级 + Cognee 四权限，落在 OperationValidator 扩展点上）②服务端联邦召回端点 ③RSI 反馈闭环（新表+新端点+巩固调度改造）④拆 `memory_engine.py`（22.8K 行 god object，Fork 时按 retain/recall/govern 三包拆分，越早越便宜）。
- **风险**：上游活跃（日更），要决定"跟随上游 vs 硬分叉"；建议把自研层全部放在扩展槽+新表里，保持可 rebase。

---

## 7. 风险与 12 个月路线图

**风险**：①OmniMemEval 教训——记忆系统必须持续证明自己 vs full-context，你的指标要盯"每查询 token 成本×团队复利"而不是单点准确率；②LLM 沉淀成本（团队级写入量大，巩固账单要预算化+分级模型）；③RSI 闭环若无评测守护会自我强化错误（judge 回归是安全绳，不是可选项）；④评测口径纪律（Cognee 的教训：口径漂移毁信任）。

**路线图**：
- **P0（0-3 月）**：Fork + org/space/RBAC + 单库联邦召回 + 权限内联 SQL + boot pages。验收：内部 20 agent 团队跑起来，recall p95<150ms。
- **P1（3-8 月）**：skills 蒸馏（AWM 式 playbook）+ feedback API + 重要性加权 + 治理控制台（证据链可视化）。验收：token/查询对比 full-context 降 ≥80%（对齐 Mem0 论文口径并有官方 harness）。
- **P2（8-12 月）**：RSI 自调参全闭环（巩固优先级/预算/刷新策略学习）+ judge 守护的配置 A/B + 公开基准站（对标 AMB 透明度）。

---

## 8. 结论

1. **没有现成的你要的东西**：六家竞品在 R1+R2+R6+R7 上没有一家全绿；最接近的三家（Zep 团队图、Hindsight 沉淀与正确性、MemOS 自进化）哲学互斥不可拼装。
2. **该抄的抄**：Hindsight 的巩固学说+四臂检索+证据链（直接 Fork）、Zep 的 group graphs 对象模型与双时态、Mem0 的平台层级产品化、Cognee 的四权限原语、MemOS 的 skills 概念。
3. **该建的建**：RSI 反馈闭环（全赛道空白=唯一护城河）、org 级继承、服务端联邦召回、权限化沉淀。
4. **性能赢面**：不在和 RDS ContextDB 拼存储引擎，而在"记忆专用索引 + 语义预物化 + 使用反馈自进化"这三件通用数据库结构性不做的事。
5. **价值主张要对准**：full-context 未被击败（OmniMemEval）是赛道共同现实，你的故事必须是"团队复利 + 成本 + 可解释 + 越用越准"，而非"更准"。

---

## 附：信息来源

**代码级验证（子代理，file 级证据）**：Hindsight 仓库（tenancy/extensions/consolidation/search/transfer/webhooks/control-plane，含 `extensions/operation_validator.py` accept_with 机制、`engine/consolidation/consolidator.py` per-scope 策略、`engine/reflect/retractions.py` 撤回、无学习闭环的确认）
**官方文档/README**：[Zep group graphs](https://help.getzep.com/graph-groups) · [Mem0 平台层级](https://docs.mem0.ai/) · [Cognee 多租户](https://docs.cognee.ai/core-concepts/multi-tenancy) / [v0.2.300 发布](https://github.com/topoteretes/cognee/releases/tag/v0.2.300) · [MemOS README](https://github.com/MemTensor/MemOS)
**论文**：Hindsight arXiv:2512.12818 · Mem0 arXiv:2504.19413（-91% p95 / -90%+ token）· Zep arXiv:2501.13956（DMR 94.8% / -90% 延迟）· MemOS arXiv:2507.03724 · OmniMemEval arXiv:2511.12419（full-context 结论）· 研究锚点：AWM / Voyager / ExpeL / Mem-alpha（RSI 方法脉络）
**GitHub API**（2026-09-23）：hindsight 25,272 / graphiti 31,083 / mem0 65,853 / MemOS 11,542 / cognee 30,933
**限制声明**：阿里云 RDS ContextDB 官方规格未能在本次会话核实（搜索工具受限）；MemOS/Cognee 的 RSI 深度基于官方披露，未经代码验证。
