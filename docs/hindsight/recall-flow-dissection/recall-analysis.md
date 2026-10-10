# Recall 流程庖丁解牛 v5:三步看懂开源与 agentstratum 的差异

> 生成:2026-10-10(v5) · 方法:8 个核查 subagent 五轮交叉审查,人工裁决 1 处矛盾,累计修正初稿 20+ 处。行号基于 agentstratum HEAD `abea5ed`;vendor=v0.10.2(`eb021da`);doc-06=上游 v0.10.3。
> 标记:`[一手]` 亲读 · `[复核]` 独立核查 · `[推断]` PG 文档推演 · `UNKNOWN` 运行时未知。

---

## 0. 口径对齐

| 对象 | 版本/基线 | 说明 |
|---|---|---|
| doc-06 | 上游 v0.10.3,基线 `6d8b09678` | 《06 · API 层与服务形态》 |
| 产品内核 | vendor 锁 `eb021da` = v0.10.2 | vendor 引用按 0.10.2 实测行号 |
| 关键陷阱 | vendor 不是纯上游 | 含产品 seam(`engine/schema.py:20-41` fail-closed 守卫) |

**徽章图例**:[替]=新机制取代上游 [新]=上游没有 [留]=vendor 原码零改动(git 证实) [改]=vendor 源内修改 [闭]=能力移除 [同]=两边一致

---

## 1. 图一:开源 Hindsight 的完整流程(24 步,一步不漏)

```
【入口与中间件】
 1 Client 发起 POST /v1/default/banks/{bank}/memories/recall
 2 MCPMiddleware(/mcp 前缀拦截,REST 透传)
 3 HttpObservabilityMiddleware(记指标;旧正则折叠法打标签)
 4 ClientDisconnectCancellationMiddleware(挂断连令牌,仅 recall/reflect)
 5 GZip 中间件
 6 UnknownParamsRoute:bank 别名重写(失败静默吞)+未知参数收集(X-Ignored-Params)
【依赖解析(FastAPI Depends,body 未读前)】
 7 get_request_context:从 Authorization 头取 API key(Bearer/裸 key)
 8 precheck_for①:_authenticate_tenant → 租户 schema 写入 _current_schema ContextVar(请求内缓存)
 9 precheck_for②:OperationValidator.precheck(计费 8 路由之一,可 402/429 拒)
10 admit_for:recall lane(16/核,排队≤30s)→ 超时 503+Retry-After;排队中断连 499
【handler 与引擎入口】
11 body 解析 RecallRequest(pydantic 13 参数;空 query→422)
12 api_recall(@audited 注册):query > 500 token → HTTP 400
13 engine.recall_async:sanitize_text 清洗孤立代理对
14 引擎入口截断 _truncate_query_to_token_limit(内部调用方降级路径)
15 _require_bank_exists:bank 不存在 → 404
16 bank 级三开关解析(enable_text_search/graph/temporal,默认全开)
17 fact_type 展开(types=None → world+experience+observation 全部)
【检索编排(PostgresMemories.recall_unified)】
18 semantic+BM25 臂:单连接 UNION SQL —— 向量 1-(embedding<=>q),关键词 ts_rank_cd(OR 拼接,语言可配)
19 graph 臂:从 semantic 高分命中(≥0.3 前 20)拿种子 → 一轮三路扩展(实体路 unit_entities | 语义kNN 边 | 因果边)
20 temporal 臂:有窗才跑(occurred 区间相交 OR mentioned_at 落窗;排序仍按向量;沿边 spreading)
【融合与重排】
21 逐 fact_type 汇总各臂 → cap_per_source(默认关) → RRF(k=60) → trim
22 hydrate_results 全量取回正文(重排前最后一个取消检查点)
23 cross_encoder 重排(worker 线程,派发后不可取消)→ apply_combined_scoring
24 prefer_observations 去重 → select_facts_within_budget(只计 text)
【事后回取与响应】
25 entities 回取(默认开);chunks/source_facts(默认关,可开)
26 RecallResult → RecallResponse 组装(6 键开放填充)
27 响应旁路:@audited → audit_log 异步落表(可关);llm_requests 记 LLM 调用;on_recall_complete 计量回调
```

## 2. 图二:当前代码(agentstratum)的完整流程(24 步)

```
【G 网关】
 1 Client 发起 POST /v1/default/banks/{bank}/memories/recall(cookie/bearer)
 2 query_route 闭集匹配:全库只认 recall|reflect 两条,其余 404【替】
 3 QueryDeadline.ingress:10 秒逻辑总预算(wall+mono 双钟)【新】
 4 身份 RPC 第 1 次(Session→MetaPG,无缓存/无连接复用)【替】
 5 四元组 Grant 授权(tenant,subtenant,bank,action);无 Grant→403【替】
 6 governor.admit:global/tenant/subject/action 四层令牌桶+有界队列【替】
 7 读体 ≤65536B(字节预留+超时)
 8 闭 parse:恰 6 键白名单;未知键→404;query≤8192B→413;trace/prefer_observations=false;reranking=cross_encoder【替】
 9 重身份 RPC 第 2 次+重授权;cookie 死线收紧到 expires_at(≤15s 窗)【新】
【转发】
10 签 18 字段 HMAC 信封(jti=请求 ID,一次性)【新】
11 转发(aiohttp 禁重试)→ H【新】
【H 在线服务】
12 verify_bound:验签+jti 防重放+method/path/bank/action/body 绑定+authorize 重查【新】
13 authority.begin:admission.check+原始字节重 parse+invocation=UUID(jti)+deadline=min(4 项)【新】
14 当前 Meta 读(PG#1)+ModelBinding 五元组过滤恰一(recall:embed+rerank)【新】
15 PRE_FACTORY preflight(PG#2)→ audit START(jti 重放第二道拒绝)【新】
16 host 白名单:capabilities=[QP01,QP03,QP07,recall,done]+settings 9 键+space/dimension 对 binding【新】
17 engine.recall_async → vendor 检测 _native_query_host → 整段移交 host.recall【同】
【检索(两臂)】
18 embed 发送检查(Meta+preflight,PG#3,4)→ L.embed(input_type=query,维度/空间复核)【替】
19 QP01 search(PG#5,guard 内联):semantic(无地板)+keyword(plainto AND·english);QP03 全有全无水合(PG#6)→ 证据组登记【替+新】
【融合与重排】
20 cap/RRF/trim:直调 vendor 原码(git 零改动)【留】
21 重排发送检查(Meta+preflight+QP07,PG#7-9)→ L.rerank(强制 cross_encoder;输入记血缘;超时报错不降级)【改】
22 combined scoring → token 预算选择(vendor 原码)【留】
【复核与响应】
23 final_check:output 授权+preflight+逐组 QP07(PG#10-12);失败→抑制整个答案→503 UNKNOWN【新】
24 闭投影(5 键恒 null,scores 从不出现)→ audit 逐帧 fsync → G 响应校验 → G 末帧死线复核 → 回传【替+闭】
```

## 3. 图三:大差异对比(逐行对齐)+ 三张拆分小图

### 3.1 大对比图(14 行对齐)

| # | 环节 | 开源上游 | agentstratum | 差异 |
|---|---|---|---|---|
| 1 | 入口路由 | 99 条;别名路由层重写 | 2 条闭集;无别名 | [替] |
| 2 | 认证 | API key,1 步 | 会话 RPC×2+信封+验封 | [替][新] |
| 3 | 租户/授权 | schema ContextVar+validator 1 处 | 四元组 Grant+G/H/SQL 三层重查 | [替][新] |
| 4 | 准入 | lane 16/核,排队 30s→503/499 | governor 4 层桶,并入 10s 总账 | [替] |
| 5 | 请求解析 | pydantic 开放 13 参数,未知忽略 | 6 键白名单,未知 404 | [替] |
| 6 | 超长 | 500 token(可配)→400/截断 | 8192 字节→413 硬拒 | [替] |
| 7 | 检索臂 | 四臂(含 graph/temporal) | 两臂(semantic+keyword) | [替+收窄] |
| 8 | 候选水合 | 取正文(无资格语义) | 带见证全有全无+证据组 | [新] |
| 9 | 融合 | RRF k=60 | **相同**(vendor 原码) | [留] |
| 10 | 重排 | cross_encoder 可关,超时降级 RRF | 强制 CE+血缘+超时报错 | [改] |
| 11 | 预算选择 | select_facts_within_budget | **相同**(vendor 原码) | [留] |
| 12 | 输出复核 | 无 | final_check+G 末帧 | [新] |
| 13 | 事后回取 | entities 默认开;chunks 可开 | 恒 null,无替代读入口 | [闭] |
| 14 | 审计 | 异步落表,默认关可关 | 同步 fsync 硬前置,不可关 | [替] |

### 3.2 小图 A|数据源不一致(查的根本不是同一批数据)

```
开源上游读:                          agentstratum 读:
┌────────────────────────────┐      ┌────────────────────────────┐
│ memory_units                │      │ native_base_facts 视图      │
│  fact_type=world ✓          │      │  fact_type=world    ✓      │
│  fact_type=experience ✓     │      │  fact_type=experience ✓    │
│  fact_type=observation ✓ ←──┼─差───┤  observation        ✗ 不放行│
│ entities / unit_entities ✓←─┼─差───┤  entities/unit_entities ✗ 空表│
│ memory_links(语义kNN/因果)✓←┼─差───┤  memory_links         ✗ 不读│
│ search_vector(语言可配)  ✓←─┼─差───┤  search_vector(english钉死)│
│ embedding(地板 0.3)      ✓←─┼─差───┤  embedding(无地板)          │
└────────────────────────────┘      └────────────────────────────┘
结论:observation 事实、实体关联、图边——产品今天查不到,不是查得差,是数据面就没通。
```

### 3.3 小图 B|流程不一致(控制流:认证/授权/准入/失败)

```
           开源(1 层,进程内)                agentstratum(3 层,跨进程)
认证   Authorization 头 API key ──同──  会话 RPC×2 → HMAC 信封 → H 验封(jti 一次性)
授权   引擎内 validate_recall(1 处)──同── G 四元组 → H 重查 → SQL 内联 guard(三层)
准入   lane 16/核·排队 30s·503/499 ──同── governor 四层桶 · 10s 总账 · 无 499
取消   断连→499+阶段边界取消计算   ──同── transport 后不取消;断开→503 UNKNOWN
失败   查到什么给什么              ──同── 撤权/漂移→抑制整个答案(503 UNKNOWN)
```

### 3.4 小图 C|接口/能力不一致(合同面)

```
参数:   13 个开放参数(未知忽略)      →  6 键白名单(未知 404)
强制值: 无 reranking 请求参数          →  发明 reranking 键且必须 =cross_encoder
配置:   bank 级臂开关+每请求 min_scores →  全锁进 activation(9 键 settings),请求不可传
响应:   6 键按需填充(entities 默认开) →  5 键恒 null+scores 从不出现
限额:   query 500 token(可配)        →  query 8192B+体 65536B+响应 1MiB+200 条(全硬帽)
错误:   422 {detail} + 499           →  400 {error:{code,effect,…}} + 无 499 + retryable 恒 false
MCP:    /mcp 39 工具(recall 可用)    →  无 MCP 面
```

---

## 4. 差异全清单:31 条用户可见差异(六组)

**第一组|进门方式**:D1 API key→会话+信封;D19 无 Grant=403 与"不存在"不可区分;D21 别名寻址→404。
**第二组|门变窄**:D2 99→2 路由(bank 管理无入口);D3 参数闭集未知键 404;D4 发明 reranking 键强制 cross_encoder(照抄上游文档会 404);D13 settings 锁进 activation。
**第三组|能查范围**:D5 observation 不可查;D12 bank 级检索开关+请求级 min_scores/tags/时间全消失;D14 keyword OR→AND;D15 semantic 0.3 地板→无地板;D16 graph/temporal 缺失。
**第四组|回应变少**:D7 响应 5 键恒 null+scores 从不出现;D8 usage 仅错误负载+cash_cost 恒 UNKNOWN;响应帽 1MiB/200 条。
**第五组|规矩变严**:D6 超长 8192B→413;D9 错误闭集+无 499+Retry-After 仅 429/503 OVERLOADED(≤60s,恒 1s);D10 10s 总账;D17 审计 fsync 硬前置不可关;Host 头精确匹配;Content-Type 只收 JSON。
**第六组|多出来**:D18 证据链(撤权→答案抑制 503);关联 ID 响应头;真 429 四层限流;断开→UNKNOWN 不取消。

**冲击前五**:①检索面收窄 ②端点参数闭包 ③时限错误语义 ④认证授权语义 ⑤响应信息量。

---

## 5. 专章:四臂为什么变两臂

**四个原因**(按证据强度):①图臂的"米"没种——SQL 写面 7 表就绪但 W 计算件只做抽取+嵌入,实体解析被三处合同关闭,语义 kNN 边无人产,TEST-PLAN 实体用例为零;②时间臂缺"窗口"零件(数据列现成,窗口来源没有);③治理成本(加臂=例程+baseline+seal+digest+新 pair,不是开关);④设计意图(base 链先行,图/时间排 D8/T11)。
**定性**:对当前合同是明示边界,不是 bug;对 S1 全量是未完成中间态。**计划结构观察**:图臂写侧前置没有显式承载包。
**四案例**:QB-9(AND 粘词可能漏`[推断]`);关联事实(小库排尾、大库漏);10月第一周(上游也无"N月第M周"规则,产品连通道都没有);纯中文(两边 keyword 都失配,上游可换配置产品锁死)。
**后果七条**:关联事实大库漏召/时间排序差无过滤/keyword 缩水/候选更脏更全/失去换配置退路/**正面:一次例程调用验收面最小**/过不了 S1 验收。

---

## 6. 逐阶段对照:S1—S20(改造+影响)

| # | 阶段 | 改造 | 影响 |
|---|---|---|---|
| S1 | 路由 99→2 | [替] | 管理端点全不可达;raw/文档无读入口;只读用户目录空列表 |
| S2 | 认证 | [替] | 每请求 2 次会话 RPC;帽 8/超时 1s,打满 503 |
| S3 | 租户定位 | [替] | G 快照不热更;H 真读须等于钉死版本——漂移=每请求 409 |
| S4 | 三层重查 | [新] | 无 Grant 在 G 层即 403,SQL/模型零执行 |
| S5 | 准入 | [替] | 10s 总账;Retry-After 仅 429/503 OVERLOADED ≤60s(恒 1s) |
| S6 | 请求解析 | [替] | 未知参数 404;observation 不可查 |
| S7 | 超长 | [替] | 8192B→413;中文更易触顶 |
| S8 | 转发 | [新] | 多一跳网络+签名设施;须同机房 |
| S9 | 引擎构造 | [适配] | **无生产装配点,今天整链不可部署** |
| S10 | 模型授权 | [替] | 换模型=快照+activation+profile 完整变更 |
| S11 | 向量编码 | [替] | 换嵌入模型=新空间,旧向量不可达须重嵌 |
| S12 | 两臂 | [替+收窄] | 见 §5 四案例 |
| S13 | 见证水合 | [新] | 单次召回约 12 次 PG 往返起步;帽 8 组/200 条 |
| S14 | 融合 | [留] | 无 |
| S15 | 重排 | [改] | 多一轮发送前检查+血缘;超时报错不降级 |
| S16 | 预算 | [留] | 无 |
| S17 | 输出复核 | [新] | 迟到撤权抑制整个答案 |
| S18 | 事后回取 | [闭] | 恒 null 且无替代读入口 |
| S19 | 响应投影 | [替] | 键在值全空;scores 从不出现;usage 仅错误帧 |
| S20 | 审计 | [替] | 审计盘故障=锁 Unit;不可关;cash_cost 恒 UNKNOWN |

---

## 7. 关键刀口核心代码(六组)

```python
# ①认证 产品 control/envelope.py:93,155-162
"jti": secrets.token_hex(16)      # 一次性请求 ID
issued <= now < deadline          # 死线 ≤15s;cookie 身份再下取到 expires_at

# ②准入 上游 api/admission.py:257-275 vs 产品 gateway/admission.py:188-199
"recall": LaneConfig(16/核, 30s)   # 上游:503+Retry-After / 499
_global/_tenant/_subject/_action  # 产品:四层桶;并入 10s;无 499

# ③超长 上游 400+截断 vs 产品两级硬拒
query = text_input(value["query"], 8192, empty=False)   # 产品 qhttp:134,超→413
require(0 < len(spec["query"].encode()) <= 8192)        # 产品引擎断言 query.py:480

# ④检索 SQL:上游动态四臂 OR vs 产品闭式两臂 AND
1 - (embedding <=> $1::vector)                                 # 上游 sql/postgresql.py:324
'qb' | '9'                                                     # 上游 OR :463-464
qts:=plainto_tsquery('pg_catalog.english'::regconfig, ...)     # 产品 AND :261

# ⑤资格复核 产品 native/query.py:443-461,548-562(上游无)
receipt = await self.pg.qualify_read(..., refs, witness)  # 发送前+输出前各一轮

# ⑥审计 上游 engine/audit.py:209-213 异步可关 vs 产品 control/audit.py:233-244 同步 fsync
```

---

## 8. 贯穿案例:一次召回 18 步(含 PG 往返计数)

1 路由匹配+10s 起表 → 2 身份 RPC#1 → 3 四元组授权(无 Grant 即 403)→ 4 governor → 5 读体+闭 parse → 6 重身份 RPC#2+重授权 → 7 签信封转发 → 8 H 验封 → 9 begin 重 parse → 10 Meta 读(PG#1)+ModelBinding → 11 preflight(PG#2)+audit START → 12 host 白名单→移交 → 13 embed 检查(PG#3,4)→L.embed → 14 QP01(PG#5)+QP03(PG#6)+组登记 → 15 重排检查(PG#7-9)→L.rerank → 16 打分+预算 → 17 final_check(PG#10-12);失败抑制 → 18 闭投影→G 末帧复核→回传+audit TERMINAL。
证据:`s1.py:203-219`、`qhttp:166-169,122-154`、`gq:126-186,241-247`、`env:129-196`、`auth:414-518`、`facade:271-277,128-159`、`host:463-690,544-585`、`native_models.py:390-424`、`audit.py:146-299`。
断点:(a) 无 Grant→步 3 的 403;(b) include_chunks→步 5 的 404;(c) 缺 rerank 绑定→步 10 的 403,与 (a) 不可区分。

---

## 9. 改造到什么程度:三层结论

算法/代码层基本齐(全链在位,融合/预算 vendor 原码);**装配层缺生产组合根**(全仓仅 3 个监听点,G/H/W 生产启动入口一个不存在,D2 的 bank_catalog 两文件未建);验证层 STALE+全 NOT_RUN。

---

## 10. 能力补齐:需要什么→要做什么→怎么算完成

**C1 temporal 时间臂**(读侧,最近):需要=时间列现成,缺窗口来源。要做=①窗口二选一(调用方必传,移植面≈0;或移植上游抽取件)②解封三处(`query_http.py:128`/`query.py:622-625`/`native_query.py:226-240`)③例程加窗谓词(照 `retrieval.py:568-591`)④最小版=窗口过滤+向量排序(spreading 产品无人写,必然空转;上游有无 spreading 降级先例 `retrieval.py:687-691`)⑤全家桶+合同口径。完成=T11 新时间 gold。

**C2 graph 图臂**(写侧前置,最远):需要=实体解析+实体行+kNN 边+**全新验收 oracle(实体用例数为零)**。要做=①解封四处合同(`native_retain.py:269-270`/`native_retain_http.py:255-256`/vendor `native_processor.py:170-192`;`native_sql.py:205-209` 只是形状校验)②W 计算件产出实体行(SQL 写面 7 表就绪,缺"谁算出这些行";上游实体链 `link_utils.py:282,406`+`orchestrator.py:596-769`)③kNN 边最小版 retain 期内联产(上游另有 graph_maintenance 维护期产点,产品 W 无维护循环)④补 unit_entities 实体优先索引⑤读侧移植三路 CTE(`ops_postgresql.py:941-1037`,可先单实体路)⑥全家桶。完成=实体 oracle+T11;不做维护循环须合同写明"边仅 retain 期产出"。

**C3 中文 keyword**(三方案先拍板):①换 regconfig——不重嵌(embedding 独立),但 generated 列变更=新 pair,存量从 OBS 重投影;②pgroonga 后端——上游路径 `sql/postgresql.py:381-398`,产品新写分支+索引+扩展依赖,改动面最大;③维持 english+声明限制。完成=AT05c/AT11c 中文 gold+口径入合同。

**C4 observation 读取**(读侧+隐藏缺口):读侧解封三处(`native_query_sql.py:125,228`/`query.py:622-627`/`query_http.py:140`)+G type 白名单(`gateway/query.py:98`)+based_on 加桶(`query.py:731`);**隐藏缺口**:release 例程把 FACT 硬绑 RAW_COMPLETE(`native_projection_sql.py:278`),而派生 observation 要 FACT+BASE_FACT_SET(`native_sql.py:438-448`)——D6 必须**改 sealed 的 release 语法出新 pair**。完成=产出 T9+读取 T11。

**C5 chunk/原文回查**:无新数据(视图已建 `native_sql.py:469-479`);要读例程(照 QP07 guard 模式)+HTTP 入口+G 转发+OBS 凭据留 H 侧。完成=D8/T11(AT08b 已有用例)。

**C6 MM 进 Reflect**:MM 存储已在(`native_catalog.py:333`);解封五工具位中的 MM 两位(`query.py:725`;MM 有 embedding 可走 QP01 同型臂);生成侧=D6。完成=T9(AT06c 现成 gold)。

**C7 Pages 检索组合**:组合 store 类已存在(`native_pages.py:29-40`);**唯一硬拒点**=`native_query_authority.py:186` 精确类型检查——按 D7 合同加显式组合分支(不是删检查);retain 权威层同型检查(`native_retain_authority.py:239,325`)视需要同改。完成=T10。

**C8 生产组合根**:装配点一个不存在(全仓仅 3 监听点)。要做=D1 Session 启动入口、D2 两个 bank_catalog 新文件、D3 H/W 生产启动器、D4 生产 activation 构造、总装(把 forwarder 传入 `create_shared_gateway` 并拉起 H/W)、D9 部署配置。完成=T1/T2/T3/T4a/T4b/T12;中间检查点=T6。

**两个最易低估**:①C2 表面"解封开关",实际新造模型侧抽取+边口径+索引+catalog 变更+oracle;②C4 看似放行,实际动 sealed release 语法出新 pair。

---

## 11. 下一步(依 NEXT-STAGE-PLAN 原文)

最短关键路径:**D1+D3+D4 并行 → T1/T2/T3/T4a → T6**。顺序:1) D1/D3/D4(Session 生产组合;Retain 新资源 H/W;QueryActivation 重绑 `ea23b4e4…`+H24/H40/H41+H38/H39)→ 2) T6(来源 gold/65536/65537/末帧/无授权对照)→ 3) D2/D4 目录+UI → 4) D5→D6→D7(人工页→归纳/MM 含 C4 release 变更→Pages 组合)→ 5) D8/T11(图/时间/原文/文件/管理;图臂先补 C2 写侧)→ 6) D9→T12→T8(当前 NOT_ACCEPTED)。

---

## 12. 交叉验证记录(1+1 落痕,累计)

五轮累计修正 20+ 处,最近一轮:每请求 2 次会话 RPC(非 1);单次召回约 12 次 PG 往返(非 2-3);上游无 reranking 请求参数(产品发明并强制);超长是 413 非 400;G 快照不热更;重排超时报错不降级;scores 死分支;写别名 404 非 403;用户面还有 401/429;recall 200 无 usage(差异在错误负载);retain SQL 写面 7 表就绪(缺计算件);observation 有 release 语法缺口;实体测试用例为零;新发现:只读用户目录空列表/G 冷启动 0 令牌/ingest UNKNOWN 写冷却。完整落痕见 v4 §10 与前版。

---

## 13. 一段话总结

把开源 Hindsight 改造成 agentstratum,是在 24 步流程里动了 18 个环节:10 处整建制替换、4 处从无到有、1 处改上游源码、1 处砍掉,只有融合和预算两段原封不动沿用。两臂只是最显眼的收窄——**数据源上 observation/实体/图边今天就不通,流程上认证授权从 1 层变 3 层,接口上从开放 13 参数变成 6 键闭集**。每刀有代价(2 次会话 RPC、12 次 PG 往返、10 秒总账、答案可能被吞成 503),也有回报(证据可审计、越权 SQL 层就死、有 id 可对账)。能力补齐的真东西不在开开关:时间臂缺窗口零件,图臂要先种"米"再加全新验收,observation 藏着 sealed 语法缺口——而这一切之前,先把只存在于测试里的生产装配点补上:D1/D3/D4→T6。
