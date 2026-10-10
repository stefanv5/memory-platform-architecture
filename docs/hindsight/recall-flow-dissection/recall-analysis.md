# Recall 流程庖丁解牛 v4:每一步都标出"开源改了什么、影响是什么"

> 生成:2026-10-10(v4) · 方法:8 个核查 subagent 五轮交叉审查(版本差/差异/案例/四臂机制/影响/能力分解/差异全清单),人工裁决 1 处矛盾,累计修正初稿 20+ 处。行号基于 agentstratum HEAD `abea5ed`;vendor=v0.10.2;doc-06=上游 v0.10.3。
> 标记:`[一手]` 亲读 · `[复核]` 独立核查 · `[推断]` PG 文档推演 · `UNKNOWN` 运行时未知。

---

## 0. 口径对齐

| 对象 | 版本/基线 | 说明 |
|---|---|---|
| doc-06 | 上游 v0.10.3,基线 `6d8b09678` | 《06 · API 层与服务形态》 |
| 产品内核 | vendor 锁 `eb021da` = v0.10.2 | vendor 引用按 0.10.2 实测行号 |
| 关键陷阱 | vendor 不是纯上游 | 含产品 seam(`engine/schema.py:20-41` fail-closed 守卫) |

---

## 1. 全景图:每个节点标出差异

图例:【替】=新机制取代上游 【新】=上游没有 【留】=vendor 原码零改动(git 证实) 【改】=vendor 源文件内修改 【闭】=能力移除

### 上游(开源基线)

```
Client ── HTTP :8888 ──► 单体 FastAPI(REST+MCP+poller 同进程,99 条路由)
 │ 别名重写(失败静默吞) → API key → 租户 schema ContextVar
 │ → precheck(计费 8 路由) → admission lane(16/核,排队≤30s → 503/499)
 │ → api_recall:>500 token → 400;types 默认全部三种(含 observation)
 │ → 四臂并行[semantic(地板0.3) | BM25(OR,语言可配) | graph(种子扩展) | temporal(有窗才跑)]
 │ → cap(默认关) → RRF(k=60) → trim → 取正文 → 重排(cross_encoder,可关降 RRF)
 ▼ → 开放响应(entities 默认回取;audit 可关;on_recall_complete 计量)
```

### 产品(每个节点 = 差异 + 影响)

```
Client ── cookie/bearer ──► ①G 网关:仅 2 条路由【替】
 │     影响:99 条管理端点全不可达;raw/文档无任何读入口
 │ ②会话认证,每请求 2 次 RPC【替】
 │     影响:各含一次 MetaPG 事务;并发帽 8/超时 1s,打满 503
 │ ③四元组 Grant+快照指纹【替】
 │     影响:G 快照启动装入不热更;H 每请求真读但须等于钉死版本——漂移=每请求 409
 │ ④governor 4 层令牌桶【替】
 │     影响:进程本地、冷启动 0 令牌(重启后第一波易吃 429);配额不随副本叠加
 │ ⑤闭 parse:未知键→404【替】
 │     影响:多传一个参数从"被忽略"变"整请求被拒"
 │ ⑥HMAC 信封(jti 一次性)→ H 验封【新】
 │     影响:多一跳网络+签名设施;G↔H 必须同机房(10s 总账内)
 │ ⑦Authority:ModelBinding+PRE_FACTORY_PG 预检【新】
 │     影响:换模型=快照+activation+profile 一次完整控制面变更
 │ ⑧host 白名单 [QP01,QP03,QP07,recall,done]【新】
 │     影响:能力面编译期封存;加能力=新 pair 注册
 │ ⑨两臂 QP01(semantic 无地板+keyword AND·english)【替+收窄】
 │     影响:见 §3 四案例(编号/关联/时间/中文)
 │ ⑩QP03 带见证水合+证据组【新】
 │     影响:单次召回约 12 次 PG 往返起步,reflect 按轮倍增;换可审计证据链;帽 8 组/200 条
 │ ⑪cap/RRF/trim(vendor 原码)【留】 影响:无
 │ ⑫CrossEncoder(L rerank,源已改写)【改】
 │     影响:每次重排多一轮发送前检查+输入记血缘;超时不再降级 RRF 而是报错
 │ ⑬final_check(输出前 QP07)+G 末帧复核【新】
 │     影响:迟到撤权→整个答案被抑制成 503(已花的模型调用作废,计次保留)
 ▼ ⑭闭投影+审计逐帧 fsync【替+闭】
       影响:键都在值全空,scores 从不出现;审计盘故障=锁 Unit 拒服务;cash_cost 恒 UNKNOWN
```

一句话:上游是一台开架自助机,产品是一条带三道安检的流水线——**安检每加一道,都对应一条可指认的代码差异和一项可感知的代价**。

---

## 2. 差异全清单:除了两臂还有什么

经核查的 31 条用户可见差异,按六组归置(D 编号为核查编号)。

### 第一组|进门方式变了

| # | 差异 | 大白话影响 |
|---|---|---|
| D1 | API key 直连 → 登录会话(bearer/cookie+CSRF)+G→H 信封 | 客户端不能拿一个长期 key 直连;要先换会话,浏览器要管 CSRF |
| D19 | 授权=MetaPG 四元组 Grant;无 Grant→403 且与"不存在"**不可区分** | 排错时分不清"拼错 bank"还是"没授权"(上游可分辨 404) |
| D21 | bank 别名:上游路由层重写;产品仅展示,拿别名寻址→**404** | 别名只能当备注 |

### 第二组|门变窄了

| # | 差异 | 大白话影响 |
|---|---|---|
| D2 | ~99 路由 → 2 条查询路由(+retain/状态);bank 管理无任何路由 | 建 bank、改配置、翻记忆列表——**根本没有入口** |
| D3 | 请求参数闭集:recall 恰 6 键,未知键 404(上游忽略并报告) | SDK 必须按白名单裁参数 |
| D4 | 产品发明了上游没有的 `reranking` 请求键且强制 `cross_encoder`(上游重排是 bank 级开关,请求里无此参数) | **照抄上游文档会 404** |
| D13 | 9 键 settings(per_arm_limit/temperature 等)由 activation 控制,请求不可传 | 调召回条数/预算=改部署,不是改请求 |

### 第三组|能查的范围变小了

| # | 差异 | 大白话影响 |
|---|---|---|
| D5 | types:上游默认三型(含 observation);产品强制 {world,experience} | 上游自动归纳的综合知识,产品查不到 |
| D12 | 上游 bank 级检索开关+每请求 min_scores/tags/时间参数;产品全无 | 按租户关某条臂、按请求过滤标签——全部消失 |
| D14 | keyword:上游 OR+可配语言(还可换 pgroonga 等后端);产品 AND+english 钉死 | 多词查询"全词都要中",查不到的明显变多 |
| D15 | semantic:上游 0.3 地板;产品无地板 | 低相关结果更多,精度换覆盖,靠重排兜 |
| D16 | graph/temporal 两臂:产品无 | 实体扩散与时间捞取结构性缺失(详见 §3) |

### 第四组|回应变少了

| # | 差异 | 大白话影响 |
|---|---|---|
| D7 | 响应 6 键中 5 键恒 null;fact 白名单剥离;scores **从不出现**(死分支) | 靠 trace/实体/chunk/分数做下游功能的一方全部失效 |
| D8 | usage:recall 的 200 两边都没有;产品差异在**错误负载**(nullable+cash_cost 恒 UNKNOWN) | 失败响应给的是"部分调用、金额未知"的账,不能精确计费 |
| — | 响应帽:产品 ≤1MiB+结果恒 ≤200 条,超 413 | 大结果要分页式多查(上游无承诺) |

### 第五组|规矩变严了

| # | 差异 | 大白话影响 |
|---|---|---|
| D6 | 超长:上游 500 token(可配可关)→400;产品 8192 **字节**→**413** | 中文按字节算更易触顶;报"太大"不报"参数错" |
| D9 | 错误:闭集+retryable 恒 false+无 499;429/503 OVERLOADED 才带 Retry-After(≤60s,OVERLOADED 恒 1s) | 重试逻辑要按"宁可 503 不慢慢等"重写 |
| D10 | 时限:上游排队 30s+执行无总账;产品全程 10s 逻辑账+信封 15s | 慢查询直接 503,慢路径必须重新设计 |
| D17 | 审计:上游 fire-and-forget 且默认关;产品同步 fsync **硬前置不可关** | 审计盘一抖,上游少条日志,产品直接拒绝回答 |
| — | Host 头必须精确等于公网 origin | 反代/直连 IP 访问会被 403——部署新坑 |
| — | Content-Type 只收 application/json | curl 默认表单会踩 400 |

### 第六组|多出来的(产品独有)

| # | 差异 | 大白话影响 |
|---|---|---|
| D18 | 证据链:每条候选带 witness,发送前+输出前两次 QP07;失败**抑制整个答案** | 撤权后上游最多少召回,产品可能把算好的答案整个吞掉变 503 |
| — | 关联 ID 响应头(X-Request-ID/X-A01-Request-Id/Invocation-Id 等) | 出问题能拿 id 对账(上游无) |
| — | 真 429 限流(四层桶) | 打太快时上游说"挤",产品说"你被限流了,X 秒后再来" |
| — | 断开语义:transport 后断开→503 UNKNOWN(上游 499+取消计算) | 用户关页面,上游省算力,产品照样把账算成"结果未知" |

**对 API 使用者冲击前五**(核查员排序):①检索面收窄(D5+D12+D14+D15+D16)——同一句话两边查出的东西集和顺序根本不同;②端点与参数闭包(D2+D3+D13)——SDK 是重写调用面不是改参数;③时限与错误语义(D9+D10);④认证授权语义(D1+D19);⑤响应信息量(D7+D8+D18)。

---

## 3. 专章:四臂为什么变两臂

### 3.1 四个臂是干什么的(故事:A"青桥项目QB-9于10月8日完成围挡施工"、B"青桥项目的监理单位是华北监理公司"、C"青桥项目10月1日进场"、D"海上项目QZ-1完成沉桩")

1. **semantic(向量)**——找"意思像的"。上游地板 0.3,产品无地板。
2. **keyword(关键词)**——找"字面同的"。上游 OR+语言可配(还可换 pgroonga 后端);产品 AND+english 三重钉死。
3. **graph(图扩展)**——找"和已找到的说了同一件事的"。**不是独立召回**:从 semantic 高分命中(≥0.3 前 20)拿种子,做一轮三路扩展:实体路(unit_entities 共享实体)+语义 kNN 边+因果边(`ops_postgresql.py:941-1037`)。
4. **temporal(时间)**——有窗才跑;窗口来自调用方或 CPU 日期抽取;过滤 occurred/mentioned 三列;**排序仍按向量距离,窗口不约束其他臂**。

### 3.2 为什么变两臂:四个原因

1. **最硬:图臂的"米"没种——写侧不产数。**SQL 写面其实已就绪(catalog 7 张表 worker_insert=True,ENTITY/EDGE 可 release),真正缺的是**计算件**:产品 W 的 `compute_native_partition` 只做抽取+嵌入,无实体解析器;实体解析被三处合同关闭(`native_retain.py:269-270`、`native_retain_http.py:255-256`、`native_processor.py:170-192`);上游语义 kNN 边有 retain 期+graph_maintenance 维护期两个产点,产品 W 连维护任务循环都不存在;TEST-PLAN 里**实体用例数为零**。
2. **时间臂缺"窗口"零件**:四列数据在 QP01 现成可用,缺窗口来源(上游可传可抽,产品拒收且无抽取件)。
3. **治理成本**:每动一个 SQL 例程=baseline_sha256 重写+catalog seal+DTO digest+**新 pair 注册激活**;上游是 bank 级布尔开关。
4. **设计意图**:base 链先行(首发计划把 Retain→base Recall 定位为中间检查点),图/时间排到 D8/T11。

### 3.3 定性

对当前合同:明示边界,不是 bug。对 S1 全量:未完成中间态(MP-03 要求图/时间,未后置)。两处有意识简化(AND、无地板)要认账;english 是继承上游默认+额外锁死可配置面。**计划结构观察**:图臂的写侧前置在当前计划里没有显式承载包。

### 3.4 四个案例(两臂的后果)

| 案例 | 上游 | 产品 | 大白话 |
|---|---|---|---|
| "QB-9" | 预分词 qb、9,**OR** 拼接;文档侧"项目qb"粘成一词元`[推断]`,但 "9" 独立→靠 9 命中 | **AND** 要 qb+9 全中→qb 粘死→**可能整条漏** | 编号两边留空格两边都能中`[推断]` |
| "青桥项目的相关方有哪些" | graph 臂沿共享实体把 B 拉进候选 | 无图臂;小库 B 靠无地板进 top-k 排尾;**大库 top-k 填满才真漏** | 漏召是规模效应:库越大越吃亏 |
| "10月第一周的施工进展" | 中文规则**没有"N月第M周"**→最可能整月;窗口只管本臂;排序按向量 | 候选层**零日期谓词**,时间传不进来;C 靠文本相似,D 可能混入 | 两边都不干净;产品连通道都没有 |
| "围挡施工完成了吗" | 纯中文 keyword 失配(english)`[推断]` | 同样失配;但上游可换配置/后端,产品锁死 | 纯中文两边都全押向量模型 |

### 3.5 后果七条

①关联事实大库漏召;②时间查询排序差+无过滤;③keyword OR→AND 缩水;④候选更脏更全、重排压力变大;⑤纯中文失去换配置退路;⑥**正面收益**:一次例程调用、验收面最小;⑦S1 验收过不了,两臂态只是 T6 中间检查点。

---

## 4. 逐阶段对照:S1—S20(改了什么+影响)

| # | 阶段 | 改造 | 影响(大白话) |
|---|---|---|---|
| S1 | 入口路由 99→2 | **替** | 管理端点全不可达;raw/文档无读入口;只读用户在 /a01/banks 看到**空列表**(目录只列 ingest/status Grant)但知道 id 仍可查 |
| S2 | 认证 | **替** | 每请求固定 **2 次**会话 RPC(各含一次 MetaPG 事务,不复用连接);并发帽 8/超时 1s,打满 503 AUTHORITY_UNAVAILABLE |
| S3 | 租户定位 | **替** | G 快照启动装入不热更(每请求只对指纹);H 每请求真读当前 Meta 但须等于钉死版本——**漂移=每请求 409 直到换快照重启** |
| S4 | 三层重查 | **新** | 无 Grant 在 G 层即 403,SQL/模型零执行 |
| S5 | 准入 | **替** | 10s 总账;Retry-After 仅 429/503 OVERLOADED 且 ≤60s 才带(OVERLOADED 恒 1s;限流>60s 反而没头) |
| S6 | 请求解析 | **替** | 未知参数 404;observation 不可查 |
| S7 | 超长查询 | **替** | 8192 字节→413(上游 500 token 可配→400);同一句中文可能一边过一边拒 |
| S8 | 转发通道 | **新** | 多一跳网络+签名设施;G↔H 必须同机房 |
| S9 | 引擎构造 | **适配** | **无生产装配点——今天整链不可部署**(一切影响的前置事实) |
| S10 | 模型授权 | **替** | 换模型=快照+activation+profile 一次完整控制面变更 |
| S11 | 向量编码 | **替** | 换嵌入模型=新空间,旧向量全部不可达,必须重嵌 |
| S12 | 两臂 | **替+收窄** | 见 §3 四案例 |
| S13 | 带见证水合 | **新** | 单次召回约 **12 次 PG 往返**起步(Meta 读+preflight+embed 检查 2+search/hydrate 2+rerank 检查 3+终检 3),reflect 按轮倍增;换可审计证据链;帽 8 组/200 条 |
| S14 | 融合 | **留** | 无(直调 vendor 原函数) |
| S15 | 重排 | **改** | 每次重排多一轮发送前检查+输入记血缘;**超时从"降级出结果"变成"报错"**(`reranking.py:434-439`) |
| S16 | 预算 | **留** | 无 |
| S17 | 输出复核 | **新** | 迟到撤权抑制整个答案(已花的模型调用作废,计次保留) |
| S18 | 事后回取 | **闭** | entities/chunks/source_facts 恒 null,**且全产品无替代读入口** |
| S19 | 响应投影 | **替** | 键都在值全空;scores 从不出现(排序理由不可见);usage 仅错误帧 |
| S20 | 审计 | **替** | 审计盘故障=锁 Unit 拒服务;上游可关,产品不可关;cash_cost 恒 UNKNOWN |

汇总:替换 10 · 新增 4 · 保留 2 · 适配 1 · 改写 1 · 关闭 1。

---

## 5. 关键刀口核心代码(六组)

```python
# ①认证 产品 control/envelope.py:93,155-162
"jti": secrets.token_hex(16)      # 一次性请求 ID,防重放
issued <= now < deadline          # 死线 ≤15s;cookie 身份再下取到 expires_at

# ②准入 上游 api/admission.py:257-275 vs 产品 gateway/admission.py:188-199
"recall": LaneConfig(16/核, 30s)   # 上游:排队超时 503+Retry-After / 断连 499
_global/_tenant/_subject/_action  # 产品:四层桶+状态槽;并入 10s;无 499

# ③超长 上游 400+引擎截断 vs 产品两级硬拒
query = text_input(value["query"], 8192, empty=False)   # 产品 qhttp:134,超→413
require(0 < len(spec["query"].encode()) <= 8192)        # 产品引擎断言 query.py:480

# ④检索 SQL:上游动态四臂 OR vs 产品闭式两臂 AND
1 - (embedding <=> $1::vector)                                 # 上游 sql/postgresql.py:324
'qb' | '9'                                                     # 上游 OR 拼接 :463-464
qts:=plainto_tsquery('pg_catalog.english'::regconfig, ...)     # 产品 AND :261

# ⑤资格复核 产品 native/query.py:443-461,548-562(上游无)
receipt = await self.pg.qualify_read(..., refs, witness)  # 发送前+输出前各一轮

# ⑥审计 上游 engine/audit.py:209-213 异步可关 vs 产品 control/audit.py:233-244 同步 fsync
#   产品失败:当次 QUERY_AUDIT_UNAVAILABLE;后续锁 Unit QUERY_UNIT_NOT_READY(audit.py:90-92)
```

---

## 6. 贯穿案例:一次召回的 18 步(代码路径)

bank=`qb-bank`,alice(有 read Grant),body `{"query":"青桥周报本周进展","types":["world"],"max_tokens":4096}`。

| 步 | 处置 | 证据 |
|---|---|---|
| 1 | 路由匹配;10s 总预算起表(双钟) | `s1.py:203-219`;`qhttp:166-169` |
| 2 | 身份 RPC(第 1/2 次) | `gq:168-171`;`s1.py:151-157`;`session_client.py:41-73` |
| 3 | 四元组授权(无 Grant→403,后续不执行) | `snapshots.py:318-326`;`gq:189-190` |
| 4 | governor 4 层桶 | `admission.py:182-199` |
| 5 | 读体≤65536B;闭 parse | `gq:175-179`;`qhttp:122-154` |
| 6 | 重身份(第 2 次 RPC)+重授权;cookie 死线收紧 | `gq:180-183`;`qhttp:175-185` |
| 7 | 签信封→禁重试转发→H | `gq:126-156,133-139` |
| 8 | H 验封(HMAC+jti 防重放+15s 窗) | `handler/app.py:123-124`;`env:129-196` |
| 9 | begin:重 parse+deadline=min(4 项) | `auth:414-448,419-427` |
| 10 | Meta 快照(第 1 次 PG 读)+ModelBinding 恰一 | `auth:450-518` |
| 11 | PRE_FACTORY preflight(第 2 次 PG)→audit START | `facade:271-277`;`audit.py:256-257` |
| 12 | host 白名单→移交 host.recall | `host:463-528`;`memory_engine.py:8569-8571` |
| 13 | embed 检查(Meta+preflight,第 3/4 次 PG)→L embed | `native_models.py:390-394`;`models/native.py:65-76` |
| 14 | QP01 search+hydrate(第 5/6 次 PG)→组登记 | `host:634-649` |
| 15 | rerank 发送检查(Meta+preflight+QP07,第 7-9 次)→L rerank | `native_models.py:396-424`;`host:652-673` |
| 16 | 打分→预算→缓冲结果 | `host:669-690` |
| 17 | final_check(Meta+preflight+QP07,第 10-12 次);失败→抑制→503 UNKNOWN | `host:544-585,577-583` |
| 18 | 闭投影→G 末帧复核→回传;audit 落 TERMINAL | `facade:128-159`;`gq:241-247`;`audit.py:146-299` |

断点:(a) 无 Grant→步 3 即 403;(b) `include_chunks:true`→步 5 的 404;(c) 缺 rerank 绑定→步 10 的 403,与 (a) 对外不可区分。

---

## 7. 改造到什么程度:三层结论

| 层 | 结论 |
|---|---|
| 算法/代码层:基本齐 | 全链在位;融合/预算 vendor 原码,重排改写加血缘 |
| 装配层:缺生产组合根 | 全仓只有 3 个监听点(旧 G loopback、L×2);**G/H/W 生产启动入口一个不存在**;D2 的 bank_catalog 两个文件还没建 |
| 验证层:STALE+全 NOT_RUN | 指纹待重绑;真实 DataPG/L/端到端未运行 |

---

## 8. 能力补齐:需要什么→要做什么→怎么算完成

### C1 temporal 时间臂(读侧为主,最近)

**需要什么**:时间列已在 QP01 数据通路现成(`native_sql.py:427`、`native_query_sql.py:101-103`);缺窗口来源。无写侧前置。
**要做什么**:①合同决定窗口来源二选一:调用方必传 `temporal_window`(移植面≈0),或移植上游抽取件(带 dateparser 依赖+中文规则);②解封参数三处:`query_http.py:128`(键集)+`query.py:622-625`+`native_query.py:226-240`(DTO 加字段);③SQL 例程加窗谓词(照上游 `retrieval.py:568-591`:occurred 区间相交 OR mentioned_at BETWEEN;**注意** created_after/before 上游过滤的是 updated_at,语义要在合同定死);④**最小版成立**:窗口过滤+向量排序即可——spreading 依赖 memory_links(产品无人写,必然空转),上游自己也有"无 spreading 降级"先例(`retrieval.py:687-691`);⑤全家桶(例程+DTO digest+catalog seal+新 pair)+c-query 三份合同文档改口径。
**怎么算完成**:T11 图/时间入口独立 gold(现有 Q1/Q2 明言不证时态,需新时间 gold)。

### C2 graph 图臂(写侧前置+读侧,最远)

**需要什么**:写侧先产数——实体解析、entities/unit_entities 行、语义 kNN 边;**验收 oracle 也要新造**(TEST-PLAN 实体用例数为零)。
**要做什么**:①解封实体解析四处合同:`native_retain.py:269-270`、`native_retain_http.py:255-256`、vendor `native_processor.py:170-192`(`native_sql.py:205-209` 只是形状校验不是关闭点);②W 计算件产出实体行(SQL 写面 7 表已就绪、行键计算已覆盖,缺的是"谁算出这些行"——上游实体链在 `link_utils.py:282,406`+`orchestrator.py:596-769`);③语义 kNN 边:上游两个产点(retain 期 `orchestrator.py:634,769`;维护期 graph_maintenance 任务 `memory_engine.py:4084-4100`→`graph_maintenance.py:204+`)——产品 W 无维护循环,**最小版应在 retain 期内联产边**;④补 unit_entities 实体优先索引(现 8 条索引无此,加索引=改 compile_plan+新 pair);⑤读侧移植三路 CTE(`ops_postgresql.py:941-1037`,可先做单实体路)接入逐臂融合;⑥全家桶+合同文档。
**怎么算完成**:写侧新实体 oracle(现空白,可挂 AT11a 对照)+T11 图入口;不做维护循环须在合同写明"边仅 retain 期产出"。

### C3 中文 keyword(三方案,先拍板)

**需要什么**:无数据前置;是纯 keyword 臂替换/叠加,向量不动。
**要做什么**:①**换 regconfig**:改生成列表达式(`native_catalog.py:105-110`)+例程 regconfig(`native_query_sql.py:261`)——**不需要重嵌**(embedding 列独立),但 generated 列变更=catalog digest→seal→新 pair,存量数据从 OBS 重投影;②**引入 CJK 后端(pgroonga)**:上游路径在 `sql/postgresql.py:381-398`(TokenBigram),产品要新写例程分支+换/加索引+pair 声明扩展依赖——改动面最大;③**维持 english+声明限制**:改合同文档与 TEST-PLAN 口径,接受 AT05c 缺口,语义臂兜底。
**怎么算完成**:三方案统一=AT05c/AT11c 中文 gold 通过+口径入合同;方案 1/2 另需新 pair 安装证据。

### C4 observation 读取(读侧+一个隐藏的 release 缺口)

**需要什么**:读侧解封+**写侧=D6**;另有一个语法级缺口(见下)。
**要做什么**:①读侧解封三处:`native_query_sql.py:125,228`+`query.py:622-627`+`query_http.py:140`,再加 G 响应 type 白名单(`gateway/query.py:98`);prefer_observations 是检索后按 provenance 去重(Python 侧,`memory_engine.py:8528-8533`),不进 SQL;②based_on 桶加 observation(`query.py:731`);③**隐藏缺口**:可见性视图已为 observation 预留(`native_sql.py:449-453`),但派生 observation 要求 FACT map 行 support_kind='BASE_FACT_SET'(`native_sql.py:438-448`),而 release 例程把 FACT 硬绑 RAW_COMPLETE(`native_projection_sql.py:278`,另有 `:161-162` 派生闸)——**D6 必须改 sealed 的 release 语法出新 pair**,是"改合同语法"不是"放开关"。
**怎么算完成**:产出=T9(关 DERIVED 不算正常通过);读取=T11。

### C5 chunk/原文回查

**需要什么**:无新数据(行都在,`native_visible_chunks/documents` 视图已建好 `native_sql.py:469-479`);前置是 D8 入口清单与权限面。现状:**全产品无任何 document/raw/chunk 读路由**(只有 ingest/status/query)。
**要做什么**:①读例程(QP04 或新编号;照 QP07 的 guard/stamp/baseline 模式);②HTTP 入口+G 转发(`query_http.py:43` 键集扩展+`handler/app.py:112-116` dispatch);③权限:按当前 read Grant,OBS 凭据留在 H 侧不下放。
**怎么算完成**:D8/T11(AT08b"非搜索直读 doc/chunk/raw"已有用例)。

### C6 MM 进 Reflect

**需要什么**:MM 存储已在产品 catalog(`native_catalog.py:333` mental_models 含 embedding;pages 又加了 prepared 列,`native_pages_sql.py:274-312` 已能写 MM);缺工具位+检索例程+D6 刷新 producer。
**要做什么**:①解封 reflect 五工具位中的 MM 两位(`query.py:725` 的 closed()→真实检索;签名对应 `reflect/agent.py:565-576`);MM 有 embedding,可走 QP01 同型向量臂;②observations 位依赖 C4/D6,expand 位依赖 C2/C4——可只先解 MM 两位;③生成侧=D6(pages DERIVED 合同)。
**怎么算完成**:T9(AT06c 五步链已是现成 gold)。

### C7 Pages 检索组合

**需要什么**:D5/T7 页合同+D6/T9 派生输出;组合 store 类已存在(`native_pages.py:29-40`,方法全继承)。
**要做什么**:**唯一硬拒点**是 `native_query_authority.py:186` 的精确类型检查——按 D7 合同加显式组合分支(不是删检查);`handler/app.py:101-107` 的 kernel 检查天然通过;retain 权威层同型检查(`native_retain_authority.py:239,325`)若组合资源收 retain 需同改;D2 目录须把组合 profile 列为支持。
**怎么算完成**:T10(两个 pure 测试不足以关闭)。

### C8 生产组合根(装配)

**需要什么**:D1-D4 各自生产构造物+D9 部署面。代码零件齐,装配点**一个都不存在**。
**要做什么**(距可启动组合的装配点清单):①D1:Session 真实 pool/首装/启动停止入口+生命周期收据;②D2:`contracts/bank_catalog.py`+`gateway/bank_catalog.py` 两个**尚不存在**的文件+s1 显式 route/provider;③D3:H/W 生产组合 config+显式进程入口(新启动器);④D4:真实 base Unit 的 approved QueryActivation/profile 生产构造(现仅测试);⑤总装:把 QueryForwarder+NativeRetainForwarder 传入 `create_shared_gateway`(`s1.py:100-120`)并拉起 H/W 的入口(现无);⑥D9:目标部署配置/health/启停脚本。
**怎么算完成**:T1/T2(D1)、T4b(D2)、T3/T6(D3)、T4a/T5(D4)、T12(D9);中间检查点=T6。

**两个最易被低估的前置**(核查员):①C2 图臂——表面是"解封一个开关",实际要新造模型侧实体抽取、边产出口径、索引、catalog 变更和**全新 oracle**;②C4 observation——可见性视图已预留易让人以为只差放行,实际要动 sealed 的 release 语法出新 pair。

---

## 9. 下一步(依 NEXT-STAGE-PLAN 原文)

最短关键路径:**D1+D3+D4 并行 → T1/T2/T3/T4a → T6**(第一条真实链,不等目录/浏览器)。

| 顺序 | 包 | 内容 | 验证 |
|---|---|---|---|
| 1 | D1+D3+D4 | Session 生产组合;Retain 新资源 H/W 提供方;QueryActivation 重绑(`ea23b4e4…`)+H24/H40/H41+H38/H39 | T1/T2/T3/T4a |
| 2 | T6 | 来源 gold;65536/65537B;末帧;无授权对照;不关闭 J1/J2/J3 | T6 |
| 3 | D2/D4 | 目录 provider(绑实际 forwarder);UI18 wire 差额 | T4b/T5 |
| 4 | D5→D6→D7 | 人工页→归纳/MM(含 C4 release 语法变更)→Pages 检索组合 | T7/T9/T10 |
| 5 | D8/T11 | 图/时间/原文/文件/管理逐入口(图臂需先补 C2 写侧前置) | T11 |
| 6 | D9→T12→T8 | 多实例/部署/接手;T8 最后,当前 NOT_ACCEPTED | T12/T8 |

---

## 10. 交叉验证记录(1+1 落痕 v4)

| 初稿说法 | 复核结果 | 处置 |
|---|---|---|
| "每请求 1 次会话 RPC" | 证伪:每数据请求 **2 次**(认证+发送前复核) | 改 S2 |
| "召回 DB 往返=QP03+QP07×组数" | 低估:单组约 **12 次**起步(Meta 读×4+preflight×3+QP 数据+QP07×2) | 改 S13/案例 |
| "上游有 reranking 请求参数" | 证伪:上游无此参数(bank 级开关);产品发明请求键并强制值——**照抄上游文档会 404** | 补 D4 |
| "产品超长=400" | 修正:是 **413**(400 只给形状错) | 改 D6/S7 |
| "G 快照每请求重建" | 修正:启动装入不热更,每请求只对指纹;H 真读但须等于钉死版本 | 改 S3 |
| "重排超时会降级" | 证伪:受控模式超时**直接报错**(`reranking.py:434-439`) | 改 S15 |
| "scores 可透出" | 修正:原生 body 无 scores,投影透出是**死分支** | 改 S19 |
| "写别名=403" | 修正:无此路由,是 **404** | 改 D21 |
| "用户面错误码=6 个闭集" | 补正:用户面还有 401/429 | 改 D9 |
| "recall 200 有 usage 差异" | 修正:200 两边都无 usage;差异在错误负载 | 改 D8 |
| "retain 只写 4 表" | 修正:SQL 写面 7 表就绪;缺的是**计算件**(谁算出实体/边) | 改 §3.2/C2 |
| "observation 只差读侧放行" | 修正:还有 release 语法缺口(FACT 硬绑 RAW_COMPLETE)——**改 sealed 语法出新 pair** | 改 C4 |
| "图臂数据表没建" | 修正:表/部分写面就绪;缺计算件+实体 oracle(测试用例为零) | 改 C2 |
| 新发现 3 条 | 只读用户 /a01/banks 空列表;G 冷启动 0 令牌重启后易 429;ingest UNKNOWN 写冷却 1-10s | 补 §2/§4 |
| v2/v3 全部记录 | 保留有效 | — |

---

## 11. 一段话总结

把开源 Hindsight 改造成 agentstratum,不是"换了个皮",而是**在 20 个环节里动了 18 个**:10 处整建制替换、4 处从无到有、1 处改了上游源码、1 处直接砍掉,只有融合和预算两段原封不动地经 hash 钉死沿用。两臂只是最显眼的那处收窄——同一段话在两边的世界里查出的东西集和顺序根本不同。每一刀都有代价:每请求两次会话 RPC、单次召回约 12 次 PG 往返、10 秒总账、答案可能被制度性吞成 503;每一刀也都有回报:每条证据可审计、越权在 SQL 层就死、出问题有 id 可对账。能力补齐的真东西不在"把开关打开":时间臂缺一个窗口零件,图臂要先把 retain 的"米"种出来(实体解析+边+全新验收),observation 藏着一个要改 sealed 语法的缺口,而这一切之前,先得把今天只存在于测试里的生产装配点补上——D1/D3/D4→T6,仍是全部下一步的起点。
