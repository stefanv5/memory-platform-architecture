# Recall 流程庖丁解牛 v3:四臂为什么变两臂

> 生成:2026-10-10(v3) · 方法:本人先读源码与计划成稿 → 5 个核查 subagent 交叉审查(版本差/12点差异/三案例/四臂机制/四查询走查)→ 人工裁决 1 处矛盾。行号基于 agentstratum HEAD `abea5ed`;vendor=v0.10.2(`eb021da`);doc-06=上游 v0.10.3。
> 论断标注:`[一手]` 亲读 · `[复核]` 独立核查证实 · `[推断]` PG 标准行为按文档推演未实测 · `UNKNOWN` 运行时未知。

---

## 0. 口径对齐

| 对象 | 版本/基线 | 说明 |
|---|---|---|
| doc-06 | 上游 v0.10.3,基线 `6d8b09678` | 原始 md:`hindsight-源码庖丁解牛/06-api-层与服务形态.md` |
| 产品内核 | vendor 锁 `eb021da` = **v0.10.2** | 引用 vendor 一律按 0.10.2 实测行号 |
| 关键陷阱 | vendor 不是纯上游 | 含产品 seam(`activate_product_kernel` + fail-closed 守卫) |

---

## 1. 全景图:两条召回路径

### 上游:单体进程,最多四臂

```
Client ── HTTP :8888 ──► 单体 FastAPI(REST+MCP+poller 同进程,99 条路由)
 │ 别名重写(失败静默吞) → API key → 租户 schema ContextVar
 │ → precheck(计费 8 路由) → admission lane(16/核,排队≤30s → 503/499)
 │ → api_recall:>500 token → 400;引擎入口截断;sanitize;bank 404
 │ → 四臂并行[semantic | BM25 | graph | temporal](bank 级三开关可关,默认全开)
 │     graph 臂不是独立召回:从 semantic 高分命中(≥0.3,前20)拿种子,做一轮三路扩展
 │     temporal 臂没有时间窗就整个跳过
 │ → cap(默认关) → RRF(k=60) → trim → 全量取回正文 → 重排(cross_encoder 默认可关)
 ▼ → 开放响应(entities 默认回取;audit_log 异步落表;on_recall_complete 计量回调)
```

### 产品:五角色分离,两臂 + 双重资格复核

```
Client ── cookie/bearer ──► G 网关(仅 2 条路由 · 无 Engine/PG/密钥)
 │ 10s 逻辑总预算 → Session RPC(每请求) → 四元组 Grant → governor(4 层桶+队列)
 │ → 闭 parse(query≤8192B · types⊆{world,experience} · 未知键→404)
 │ → cookie 死线收紧(≤15s) → HMAC 信封(jti 一次性) → H 验封+授权重查
 │ → Authority:Meta 快照+ModelBinding(recall+embed+rerank 恰一)+PRE_FACTORY_PG 预检
 │ → host 白名单 [QP01,QP03,QP07,recall,done] → L.embed → QP01 两臂(semantic+keyword)
 │     keyword 臂 plainto AND + english 三重钉死;semantic 臂无相似度地板
 │ → QP03 带见证全量水合 → 证据组登记 → cap/RRF/trim(vendor 原码)
 │ → CrossEncoder(L rerank,源已改写)——发送前逐组 QP07
 │ → 打分(时间 boost 恒中性) → token 预算 → final_check(输出前再跑 QP07)
 ▼ → 闭投影(null 字段) → G 末帧死线复核 → audit 逐帧 fsync;无 499,传输后一律 UNKNOWN
```

一句话:上游是一台开架自助机,产品是一条带三道安检的流水线。

---

## 2. 专章:四臂为什么变两臂

### 2.1 先认识四个臂:每个臂是干什么的

用同一个故事贯穿:仓库里存了四条事实——

- **事实A**:"青桥项目QB-9于10月8日完成围挡施工"(10-08,含编号)
- **事实B**:"青桥项目的监理单位是华北监理公司"(与 A 共享实体"青桥项目",但和大多数问题的用词、向量都不像)
- **事实C**:"青桥项目10月1日进场"(10-01)
- **事实D**:"海上项目QZ-1完成沉桩"(无关项目)

**臂1|semantic(向量)**:把问题变成向量,在 pgvector 里找距离最近的。问"围挡干完没",它靠"意思相近"找到 A。两家都实现,这是唯一两边都指望得上的臂。
锚点:上游 `sql/postgresql.py:306-335`,相似度地板 0.3(`config.py:1309`);产品 `native_query_sql.py:264-269`,**无地板**(`query.py:636` semantic_min=-1)。

**臂2|BM25/keyword(关键词)**:把问题切成词,在全文索引里找含这些词的。问"QB-9",它靠"字面一样"找到 A——编号、人名这类向量不敏感的词,靠它。
两家实现但**语义不同**:上游把词用 `|`(OR)拼进 tsquery(`sql/postgresql.py:463-464`),命中任意一个词就算;产品用 `plainto_tsquery`(**AND**,`native_query_sql.py:261`),所有词都得出现。合同自己声明"不是BM25,也不声称上游OR-token等价"(`INTERFACE.md:31`)。

**臂3|graph(图扩展)**:**它不是独立召回**。上游的做法:先看 semantic 臂的高分命中(相似度≥0.3 的前 20 个当种子,`link_expansion_retrieval.py:46`、`postgres.py:120-130`),从每个种子出发做**一轮三路扩展**——①实体路:在 `unit_entities` 表里找"和种子共享同一个实体"的其他事实(B 因为也提到"青桥项目"被拉进来,`ops_postgresql.py:941-981`);②语义路:沿 retain 时预计算的 kNN 相似边(`memory_links` link_type='semantic');③因果路:沿 caused_by/enables 等因果边。大白话:**"它和已找到的事说了同一件事(同一实体)/很像/有因果"的事实,也能进候选**。

**臂4|temporal(时间)**:有"10月第一周"这类时间约束才跑(`postgres.py:155`);窗口来自调用方参数或 CPU 日期抽取(`retrieval.py:854-874`);过滤条件是 occurred_start/occurred_end 区间重叠或 mentioned_at 落窗(`:575-584`)。注意两点核查修正:窗口过滤后**排序仍按向量距离**(`:589`),不是按时间;窗口只管本臂的入选,**不约束其他臂的融合结果**——所以上游也不是"时间过滤得很干净"。

### 2.2 产品现在:两臂,还各带一处简化

| | 上游 | 产品 |
|---|---|---|
| semantic | 地板 0.3 | **无地板**(候选更全也更脏,全交给重排兜底) |
| keyword | OR 拼接,bm25_language **可配置**(默认 english) | plainto **AND**,english **三重钉死**(请求侧 `query.py:637`+服务端校验 `native_query_sql.py:216-220`+生成列 `native_catalog.py:105-110`) |
| graph | 种子扩展(实体/语义/因果三路) | **无**——QP01 例程不读 entities/memory_links(`native_query_sql.py:107-145`) |
| temporal | 有窗才跑,窗口可传可抽 | **无**——temporal_window/question_date 在拒绝名单(`query.py:622-625`),候选层零日期谓词;日期只在回体里透出给"近因加分",且时间加分恒中性(`reranking.py:209-210`) |

上游本来就能用 bank 级开关降级成两臂(`config.py:1929-1931` 默认全开、可按 bank 关)——**产品的两臂≈上游"关掉 graph/temporal 开关"的配置态**,但有三处不等价:keyword OR→AND、语料收窄到 world/experience 的 base facts(上游含 observation 等全部类型)、能力面整体拒绝。

### 2.3 为什么会变成这样:四个原因(按证据强度排序)

**原因1(最硬):图臂的"米"根本没种——写侧数据不支撑。** 图臂三路信号里,实体路要 `unit_entities` 表、语义路要 retain 时预计算的 kNN 边。而产品 retain 投影只写 documents/chunks/memory_units/memory_links 四张表(`native_projection.py:112`);实体解析被合同**整体关闭**——`native_retain.py:269-270` 原话 "Entity resolution and user entities are closed";现有边只有 entity_id=None 的 caused_by 因果边。结果:entities/unit_entities 在产品库里是**空表**(表和索引建了,`native_catalog.py:167-301`),连上游图臂依赖的 entity_id 优先索引都没建(`native_sql.py:600` 只有 memory_links 唯一索引)。**就算把图臂代码接上,它也只能走因果单路。**

**原因2:时间臂缺"窗口来源"这块上游零件。** 时间列(occurred_start/occurred_end/mentioned_at/event_date)在产品 QP01 例程里现成可用(`native_sql.py:427` 带出,`native_query_sql.py:101-102` 投影)——**数据不缺**,缺的是:上游窗口要么调用方传、要么 CPU 日期抽取件(`retrieval.py:854-874`),产品两者皆无,还在边界直接拒收窗口参数。

**原因3:治理成本把"加臂"从开关问题变成重注册问题。** 上游关/开臂是 bank 级布尔;产品每动一个 SQL 例程=重写 `baseline_sha256`(`native_sql.py:683`)+catalog seal+`execution_contract_digest`+**新 pair 注册与激活**(`INTERFACE.md:39-41`),旧 READY 不许 patch。加一臂的合同清单:例程+compile_plan、baseline、DTO/execution_contract_digest、catalog_seal、新 pair 注册激活、能力封闭面解封(`INTERFACE.md:33`)。

**原因4(设计意图):base 链先行, breadth 后补。** 首发计划把"Retain→base Recall"定位为**中间检查点**(`NEXT-STAGE-PLAN:13`),第一层只要"真实写入可被 Recall"(`:36-38`);图/时间/原文入口明确排到 D8/T11(`:47`)。两臂是把第一版验收面压到最小的主动裁剪。

### 2.4 定性:设计分期 + 明示边界,不是实现 bug

- 对**当前已交付的 base Query 合同**而言:两臂是合同明示的范围(`NATIVE-SEAM.md:45`"当前路径仅…semantic/native ts_rank_cd keyword、QP01/03/07"),拒绝是 fail-closed 设计,不是缺陷。
- 对 **S1 首发全量设计**而言:MP-03 明文要求"向量、关键词、**图关系和时间**相关检索"(`STAGED-CAPABILITIES §3.1`),`FIRST-RELEASE-SCOPE.json` 未把图/时间后置——**所以两臂态是未完成的中间态,不是终态**,承接包是 D8/T11。
- 两处**有意识的简化**(不是 bug,但后果要认账):keyword OR→AND;semantic 去掉 0.3 地板。
- 一处**继承+收紧**:keyword english 是上游默认值,但上游可换 regconfig/换后端(pgroonga bigram 等对中文有效,`sql/postgresql.py:381-398`),产品把可配置面锁死了。
- 一个**计划结构观察**(基于代码事实的推断,非文档原义):图臂的数据前提在 **retain 写侧**(实体解析解封+边生产),而 D8 只覆盖读侧入口——从当前计划文本看,这条写侧前置没有显式承载包。

### 2.5 两臂会导致什么:四个案例

**案例1|编号查询 "QB-9"——AND 和 OR 的差别在这显形。**
上游:Python 预分词把 "QB-9" 切成 qb、9,OR 拼接(`retrieval.py:34-40`;短查询不过 IDF 截断,`bm25_term_selection.py:98-99`)。产品:plainto AND。关键在文档侧分词:`to_tsvector('english','青桥项目QB-9…')` 在无空格时会把"青桥项目qb"**粘成一个词元**(PG 默认 parser 按空白/标点切,`[推断]`),只有 "9" 独立。于是:上游 OR 靠 "9" 命中;产品 AND 要求 qb 和 9 都在——"qb" 粘死了,**可能整条漏掉**。写事实时给编号两边留空格("…项目 QB-9 …")就能两边都命中。`[推断,待实跑]`

**案例2|关联事实 "青桥项目的相关方有哪些"——graph 臂是唯一通道,但漏召是规模效应。**
上游:semantic 先命中 A/C(相似度≥0.3 当种子),图臂沿共享实体"青桥项目"把 B 拉进候选。产品:没有图臂——但小库(4 条事实)下 semantic 无地板,B 必进 top-k,只是排尾;**真正漏召发生在大库**:候选被更相似的事实填满、B 挤不进 per_arm_limit 时。所以准确说法是:**产品把"找关联事实"从检索问题变成了重排问题,库越大越吃亏**。`[代码可证;实体抽取成功与否 UNKNOWN]`

**案例3|时间查询 "10月第一周的施工进展"——两边都不干净,但产品连通道都没有。**
上游:中文规则里**没有"N月第M周"**,最可能解析成整个 10 月(`chinese_temporal_periods.py` 静态核对);窗口只管 temporal 臂自己的入选,排序按向量距离——D 仍可能从其他臂混进最终结果。产品:候选层**零日期谓词**,时间约束无法传入;C 靠文本相似进候选,D 混入是真实风险,且"近因加分"的时间项恒中性。一句话:**丢时间臂 = 排序变差 + 无过滤;但别指望上游的时间臂本来就精确。**

**案例4|纯中文 "围挡施工完成了吗"——两边都只剩 semantic。**
纯中文无空格,在 english 配置下两边都切成一个整串词元,keyword 臂都失配(`[推断]`)。默认部署下"中文检索弱是继承上游"成立;但上游 english 是**可改的默认**(换 regconfig 或 pgroonga 后端),产品是**协议级锁死**。多出来的 graph/temporal 臂对纯中文也帮不上(前者依赖实体抽取,后者依赖日期解析)。

**后果清单(汇总)**:

| # | 后果 | 案例 |
|---|---|---|
| 1 | 共享实体的关联事实:大库漏召风险(小库只是排尾) | 案例2 |
| 2 | 时间约束:排序变差+无过滤,还叠加"第一周"这类解析本身就不精确 | 案例3 |
| 3 | keyword 缩水:OR→AND,多词查询要求全命中;中文+编号粘连场景会漏 | 案例1 |
| 4 | 候选更脏更全(无地板):大库下 top-k 竞争更烈,重排压力变大 | 案例2/3 叠加 |
| 5 | 纯中文只剩单臂,且产品失去上游的换配置/换后端退路 | 案例4 |
| 6 | **正面收益**:一次例程调用、候选面最小、治理成本最低——这正是"先两臂"的合理性 | §2.3 原因3/4 |
| 7 | S1 验收:MP-03 的图/时间/中文样例过不了,两臂态只能当 T6 中间检查点 | §2.4 |

### 2.6 补回两臂要付出什么

**时间臂(较近,读侧为主)**:①窗口来源件——移植上游日期抽取或定义产品自己的窗口合同;②解封 `temporal_window/question_date`(能力封闭面);③走一遍合同全家桶:例程+baseline+DTO digest+catalog seal+新 pair 注册激活。数据列现成,不用动 retain。

**图臂(较远,写侧前置)**:①先解 retain 实体解析合同(`native_retain.py:269-270`),写 entities/unit_entities;②补语义 kNN 边生产;③补 unit_entities 的 entity_id 优先索引(上游图臂依赖它,`link_expansion_retrieval.py:315-317`);④然后才是读侧扩展例程+合同全家桶。**这超出了 D8 的读侧范围,需要在计划里显式补一个写侧前置包。**

**keyword 语言(如果要支持中文词元)**:改 regconfig=生成列 schema 变更(baseline 连锁);或引入对 CJK 有效的 keyword 后端。属合同变更,不是开关。

---

## 3. 逐阶段对照大表(S1—S20)

> 与 v2 相同,保留作参照。改造程度:**保留**=经 seam 沿用 vendor 原码(git 证实零改动)· **适配**=沿用构造换绑定 · **替换**=新机制取代 · **改写**=vendor 源内修改 · **新增**=上游没有 · **关闭**=能力移除。汇总:替换 10 · 新增 4 · 保留 2 · 适配 1 · 改写 1 · 关闭 1。

| # | 阶段 | 上游 | 产品 | 改造程度 |
|---|---|---|---|---|
| S1 | 入口路由 | 99 条路由(`http.py:5275`);别名路由层重写 | 仅 2 条;bank_id 原样提取 | **替换(收窄)** |
| S2 | 认证 | API key(`http.py:5281`) | 会话(MetaPG)+HMAC 信封(jti 一次性,`envelope.py:65-100,129-196`) | **替换** |
| S3 | 租户定位 | schema ContextVar(`memory_engine.py:3249-3300`) | Meta 快照+四元组 Grant(`snapshots.py:318-326`) | **替换** |
| S4 | 授权校验 | validate_recall 1 个调用点;precheck 8 计费路由 | 验封重查+begin 重查+SQL 内联守卫,三层编排 | **新增** |
| S5 | 准入 | lane 16/核,30s→503/499 | governor 4 层桶+队列+状态槽,并入 10s 总账,无 499 | **替换** |
| S6 | 请求解析 | Pydantic 开放模型;types 默认含 observation(`:6040`) | 闭式 6 键;types 强制 {world,experience} | **替换** |
| S7 | 超长查询 | HTTP 400(500 token)/引擎截断 | 65536B→413、8192B→400、引擎断言,无截断 | **替换** |
| S8 | 转发通道 | 进程内直调 | HMAC 信封+禁重试+闭式校验 | **新增** |
| S9 | 引擎构造 | 进程单例 | QueryFacade 每实例一套+Closed ports;**无生产装配点** | **适配** |
| S10 | 模型授权 | 配置选 provider | ModelBinding 五元组+L 端口白名单 | **替换** |
| S11 | 向量编码 | 多 provider 栈 | provider 栈绕过,ScopedNativeModels 直发 L | **替换/适配** |
| S12 | 候选检索 | 四臂(`postgres.py:140-190`) | 两臂 QP01 闭式例程 | **替换+收窄** |
| S13 | 候选水合 | 重排前取正文(`:9598-9606`) | QP03 带见证全有全无+证据组 | **新增** |
| S14 | 融合 | RRF k=60;cap 默认关 | 同函数经 seam,git 零改动 | **保留** |
| S15 | 重排 | cross_encoder 可关降 RRF | 强制 cross_encoder;reranking.py 被改写(fbe7f29);发送前 QP07 | **改写+新增** |
| S16 | 预算选择 | select_facts_within_budget | 同函数经 seam,git 零改动 | **保留** |
| S17 | 输出复核 | 无 | final_check QP07+输出授权+G 末帧复核 | **新增** |
| S18 | 事后回取 | entities 默认开;chunks/source_facts 可开 | 全链拒绝,投影恒 null | **关闭** |
| S19 | 响应投影 | 开放形状直构 | 闭投影+字段剥离+G 二次校验 | **替换** |
| S20 | 审计/计量 | 异步落 audit_log 表+llm_requests | 本地同步 fsync 段;cash_cost 恒 UNKNOWN | **替换** |

---

## 4. 六处关键刀口(对照+核心代码)

**S2 认证**
```python
# 产品 control/envelope.py:93,155-162
"jti": secrets.token_hex(16)     # 请求 ID,一次性,防重放
issued <= now < deadline         # 死线 ≤15s;cookie 身份再下取到 expires_at
```
上游:API key 认完即走。产品:先登录,再由 G 拿一次性签名信封"代办",H 逐字段验签。

**S5 准入**(两核查员矛盾,按三重证据裁决:上游有 lane,产品整建制重写)
```python
# 上游 api/admission.py:257-275            # 产品 gateway/admission.py:188-199
"recall": LaneConfig(16/核, 30s)            _global/_tenant/_subject/_action 四层桶
# 超时 503+Retry-After · 断连 499          # +有界队列+状态预留槽 · 并入 10s · 无 499
```

**S7 超长查询**
```python
# 上游 http.py:6026-6035(REST 400)/ memory_engine.py:8605-8609(引擎截断)
# 产品 contracts/query_http.py:134 + native/query.py:480
query = text_input(value["query"], 8192, empty=False)   # 超→400
require(0 < len(spec["query"].encode()) <= 8192)        # 引擎断言再拒
```

**S12/S13 检索与水合**
```sql
-- 上游 sql/postgresql.py:324(semantic 臂动态拼装)+ 463-464(BM25 OR 拼接)
1 - (embedding <=> $1::vector)   …  'qb' | '9'
-- 产品 native_query_sql.py:264-274(闭式例程,guard 内联)
1-(embedding OPERATOR({ext}.<=>) qvector) AS score
qts:=plainto_tsquery('pg_catalog.english'::regconfig, p_request->>'query_text');  -- AND
```
上游重排前也全量取正文(`:9598-9606`)但只是取文本;产品是带见证的全有全无水合+证据组。

**S17 资格复核**(上游完全没有)
```python
# 产品 native/query.py:443-461(before_send)与 548-562(final_check)
receipt = await self.pg.qualify_read(..., refs, witness)   # QP07 逐组资格
# 失败→整答案抑制→503 QUERY_CHECK_CLEANUP_UNKNOWN/UNKNOWN(:577-583)
```

**S20 审计**
```python
# 上游 engine/audit.py:209-213 —— 异步 INSERT audit_log(可关,零开销直通)
# 产品 control/audit.py:233-244 —— {record,length,sha256} 帧逐条 fsync(硬前置)
#   失败:当次 QUERY_AUDIT_UNAVAILABLE;后续锁 Unit QUERY_UNIT_NOT_READY(audit.py:90-92)
```

---

## 5. 贯穿案例:一次召回的 18 步(代码路径)

bank=`qb-bank`,主体 alice(有 read Grant),body `{"query":"青桥周报本周进展","types":["world"],"max_tokens":4096}`。代码路径走查,非运行声明。

| 步 | 处置 | 证据 |
|---|---|---|
| 1 | POST+路径匹配;10s 总预算起表(双钟) | `s1.py:203-219`;`qhttp:166-169` |
| 2 | 身份:Bearer RPC(无缓存) | `gq:168-171`;`s1.py:151-157` |
| 3 | 四元组授权(无 Grant 则在此 403,后续防线不执行) | `snapshots.py:318-326`;`gq:189-190` |
| 4 | governor 4 层桶放行 | `admission.py:182-199` |
| 5 | 读体≤65536B;闭 parse(query≤8192B 等) | `gq:175-179`;`qhttp:122-154` |
| 6 | 重身份+重授权;cookie 死线收紧 | `gq:180-183`;`qhttp:175-185` |
| 7 | 签 18 字段信封→禁重试转发→H | `gq:126-156,133-139` |
| 8 | H 验封(HMAC+jti 防重放+15s 窗) | `handler/app.py:123-124`;`env:129-196` |
| 9 | begin:重 parse+invocation=UUID(jti)+deadline=min(4 项) | `auth:414-448,419-427` |
| 10 | Meta 快照+ModelBinding 恰一;max_tokens≤激活上限 | `auth:450-518,464-467` |
| 11 | PRE_FACTORY_PG 预检(失败 409)→audit START(jti 重放第二道拒绝) | `facade:271-277`;`audit.py:256-257` |
| 12 | host 白名单→engine.recall_async→移交 host.recall | `host:463-528`;`memory_engine.py:8569-8571` |
| 13 | encode_query→L embed(维度/空间复核) | `host:631-632`;`models/native.py:65-76` |
| 14 | QP01 两臂(中文 keyword 失配 UNKNOWN;semantic 无地板) | `host:634-638`;`native_query_sql.py:261,264-274` |
| 15 | QP03 带见证水合→组登记 | `host:640-649,245-259` |
| 16 | cap→RRF→trim(原码)→CrossEncoder(发送前 QP07)→打分→预算 | `host:652-673,443-461` |
| 17 | final_check(失败→整答案抑制→503 UNKNOWN) | `host:544-585,577-583` |
| 18 | 闭投影→G 末帧复核→形状校验→回传;audit 落 TERMINAL | `facade:128-159`;`gq:241-247`;`audit.py:146-299` |

三个对照断点:(a) 无 Grant→第 3 步 403;(b) `include_chunks:true`→第 5 步 404(`qhttp:132-133`,是 404 不是 400);(c) 缺 rerank 绑定→第 10 步 403,与 (a) 对外不可区分。

---

## 6. 改造到什么程度:三层结论

| 层 | 结论 | 依据 |
|---|---|---|
| 算法/代码层:基本齐 | 全链代码在位;融合/预算 vendor 原码,重排改写加血缘 | git:fbe7f29/32a0bcc;fusion/fact_budget 零改动 |
| 装配层:缺生产组合根 | G 端 forwarder 默认 None;H 端 QueryFacade 仅测试构造——**今天没有生产入口能走到这条链** | `s1.py:78`;`tests/stage1/query/test_host_authority.py:224` |
| 验证层:STALE+全 NOT_RUN | 指纹 `4cb8c00a→ea23b4e4` 待重绑;真实 DataPG/L/端到端未运行 | `STAGED §9.1`;`MODULE-PROGRESS §5` |

---

## 7. 缺什么

**篮子 A|S1 要求、产品关闭**:图/时间臂(见 §2.6:图臂还有写侧前置)、observation 读取、chunk/原文回查、中文样例、Pages 检索组合(D7)、Reflect 的 MM 层。
**篮子 B|版本差**:#5388 截断(被硬拒中和)、MCP 紧凑 JSON(无关)、#5432(被中和)、#5358 retain 断连(留意)、#5063 指标(留意)。
**篮子 C|验证缺口**:T4a 全部退出项、T6 第一链、J1 双租户、B1/B2 原生对照——全 NOT_RUN。
**两个无主决定**:①中文检索路线(oracle 要中文 vs keyword english 钉死,无包负责);②G/H/L deadline(10s vs 15/60s PROPOSED,无具名归属)。
**一个计划结构观察**:图臂的写侧前置(实体解析解封+边生产)在当前计划包里没有显式承载,D8 只覆盖读侧。

---

## 8. 下一步(依 NEXT-STAGE-PLAN 原文)

**最短关键路径**:D1+D3+D4 并行 → T1/T2/T3/T4a → T6 第一条真实链(不等目录/浏览器,:114)。

| 顺序 | 包 | recall 相关最小内容 | 验证 |
|---|---|---|---|
| 1 | D1+D3+D4 | D4:activation 重绑 manifest `ea23b4e4…`;H24/H40/H41+H38/H39 | T1/T2/T3/T4a |
| 2 | T6 | 来源 gold;65536/65537B;末帧;无授权对照;不关闭 J1/J2/J3 | T6 |
| 3 | D2/D4 | 目录 provider(绑实际 forwarder);UI18 wire 差额 | T4b/T5 |
| 4 | D5→D6→D7 | 人工页→归纳/MM→Pages 检索组合 | T7/T9/T10 |
| 5 | D8/T11 | 图/时间/原文/文件/管理逐入口(**图臂需先补写侧前置,见 §2.6**) | T11 |
| 6 | D9→T12→T8 | 多实例/部署/接手;T8 最后,当前 NOT_ACCEPTED | T12/T8 |

---

## 9. 交叉验证记录(1+1 落痕 v3)

| 初稿说法 | 复核结果 | 处置 |
|---|---|---|
| "IDF 选词会选中 QB-9 这类低频词" | 证伪:IDF 选词是 >16 token 的长查询截断器,短查询直接透传(`bm25_term_selection.py:98-99,153-158`) | 改写案例1 |
| "上游时间过滤很干净" | 修正:窗口只管本臂入选,排序按向量距离,融合结果不受窗口约束 | 改写案例3 |
| ""N月第M周"可解析" | 证伪:中文规则无此模式,"10月第一周"最可能解析成整月 | 改写案例3 |
| "小库产品也会漏召 B" | 修正:semantic 无地板,小库 B 必进 top-k;漏召是规模效应 | 改写案例2 |
| "ASCII token 在中文文本里正常切分" | 修正:无空格时"项目qb"粘成一个词元;AND 语义下 qb+9 全命中才匹配 | 改写案例1 |
| "中文检索弱是产品引入" | 修正:默认部署下继承上游(上游默认也是 english);但上游可配置,产品锁死 | 改写案例4 |
| "图臂缺数据=表没建" | 修正:表和部分索引建了,但 retain 不写 entities、实体解析被合同关闭——是**写侧不产数** | 改写 §2.3 原因1 |
| "产品没有融合" | 修正:两臂 RRF 照跑(与上游降级配置同形状);不等价在 keyword AND/OR、语料范围、能力面 | 改写 §2.2 |
| "上游 semantic 与产品同门槛" | 新发现:上游地板 0.3,产品无地板(semantic_min=-1) | 补入 §2.2/后果4 |
| v2 全部记录(裁决准入矛盾、entities 默认开、validate_recall=1 等) | 保留有效 | §9 v2 |

---

## 10. 一段话总结

上游四臂是一个递进的信息网:向量找"意思像的",关键词找"字面同的"(OR,宽容),图扩展找"和已找到的说了同一件事的"(靠实体和边),时间臂给带日期的问题兜底。产品第一刀先交了前两个,还各收紧了一档(AND、无地板)——这不是做错了,是把第一版验收面压到最小的主动裁剪;但要说清楚:图臂补回来之前,得先让 retain 开始"种米"(实体解析和边),这一步在当前计划里还没有自己的包;时间臂的米是现成的,缺的只是窗口这件小零件。对使用者来说,今天这两臂意味着:编号查询要看写事实时留没留空格,关联事实在大库里会漏,时间问题既排不准也滤不掉,纯中文全押给向量模型。先拍板中文路线和 deadline 两个无主决定,再走 D1/D3/D4→T6——这就是全部下一步。
