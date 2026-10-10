# Recall 流程庖丁解牛 v2:上游原样 → 产品现状,逐阶段对照

> 生成:2026-10-10(v2 细化版) · 方法:本人先读 doc-06 原始 md 与 `NEXT-STAGE-PLAN.md` 全文成稿 → 3 个核查 subagent 交叉审查(上游 18 阶段 / 产品 18 阶段+20 项评级 / 下一步 9 条)→ 人工裁决 1 处核查员互相矛盾。
> 论断标注:`[一手]` = 亲读;`[复核]` = 独立核查证实;`[改判]` = 初稿被复核修正。行号基于 agentstratum HEAD `abea5ed`;vendor = 0.10.2(`eb021da`);doc-06 = 上游 0.10.3(`6d8b09678`)。

---

## 0. 口径对齐

| 对象 | 版本/基线 | 说明 |
|---|---|---|
| doc-06 | 上游 hindsight-api-slim **v0.10.3**,基线 `6d8b09678`(2026-10-08) | 原始 md:`hindsight-源码庖丁解牛/06-api-层与服务形态.md` |
| 产品内核 | vendor 锁 `eb021da` = **v0.10.2**(2026-09-29) | doc-06 行号普遍漂移约 150-200 行;引用 vendor 一律按 0.10.2 实测行号 |
| 关键陷阱 | vendor 不是纯上游 | 含产品 seam:`activate_product_kernel` + fail-closed 守卫(`engine/schema.py:20-41`) |

---

## 1. 全景图:两条召回路径

### 上游(doc-06 / vendor 0.10.2,单体进程,最多四臂)

```
Client ── HTTP :8888 ──► 单体 FastAPI(REST + MCP + 内嵌 poller 同进程,99 条路由)
 │ 别名重写(失败静默吞) → API key(get_request_context)
 │ → _authenticate_tenant(租户 schema ContextVar,请求内缓存)
 │ → precheck_for(recall,计费 8 路由之一,body 未读即可拒)
 │ → admission lane:16/核 · 排队≤30s ── 超时 503+Retry-After · 排队中断连 499
 │ → api_recall:>500 token → HTTP 400(0.10.3 起统一截断 #5388)
 │ → engine.recall_async:内部入口截断 → sanitize → _require_bank_exists(404)
 │ → 四臂并行[semantic | BM25 | graph | temporal](bank 级三开关可关;无窗口则时间臂跳过)
 │ → cap_per_source(全局配置,默认关) → RRF(k=60) → trim → 全量取回正文 → 重排
 │     cross_encoder(默认;bank 可关降级 RRF)——重排派发后不可取消(协作式取消硬边界)
 │ → token 预算选择(只计 text) → entities 默认回取;chunks/source_facts 默认关
 ▼ → 开放响应(audit_log 异步落表;llm_requests 记 LLM 调用;on_recall_complete 计量回调)
```

### 产品(contextdb,五角色,两臂 + 双重资格复核)

```
Client ── cookie/bearer ──► G 网关(仅 2 条路由 · 无 Engine/PG/密钥)
 │ QueryDeadline.ingress:10s 逻辑总预算(wall+mono 双钟)
 │ → Session RPC(MetaPG,每请求,无缓存) → 四元组 Grant 授权 → governor(4 层令牌桶+队列+状态槽)
 │ → 闭 schema parse(query≤8192B · types⊆{world,experience} · 未知键→404)
 │ → cookie 死线下取到会话 expires_at(≤15s 窗) → HMAC 信封(jti 一次性) → H 验封+授权重查
 │ → Authority:当前 Meta 快照 + ModelBinding(recall+embed+rerank 恰一个) + PRE_FACTORY_PG 预检
 │ → host 白名单 [QP01,QP03,QP07,recall,done] → L.embed → QP01 两臂候选
 │ → QP03 全量水合(带见证) → 证据组登记(gate 串行) → cap/RRF/trim(vendor 原码)
 │ → CrossEncoder(L.rerank,reranking.py 已改写:血缘挂钩)——发送前逐组 QP07
 │ → 打分 → token 预算 → final_check(输出前再跑 QP07+输出授权)
 │ → project_public(闭投影,null 字段) → audit 逐帧 fsync
 ◄── G 末帧死线复核(过期改判 503 UNKNOWN) ── 无 499;传输后异常一律 UPSTREAM_UNKNOWN
```

一句话:**上游是一台开架自助机,产品是一条带三道安检的流水线**。门从 99 条收到 2 条;臂从四条砍到两条;上游只管"查到就给",产品还要给每条证据发两次"资格证"。

---

## 2. 逐阶段对照大表(S1—S20)

改造程度图例:**保留**=经 seam 沿用 vendor 原码(git 证实零改动) · **适配**=沿用构造换绑定 · **替换**=新机制取代上游机制 · **改写**=在 vendor 源文件内修改 · **新增**=上游没有 · **关闭**=能力移除

| # | 阶段 | 上游做什么 | 产品做什么 | 改造程度 |
|---|---|---|---|---|
| S1 | 入口路由 | 99 条业务路由;`_register_routes` `http.py:5275`;bank 别名路由层重写、失败静默吞(`unknown_params.py:122-158`) | 全库只认 2 条 `POST /v1/default/banks/{bank}/memories/{recall\|reflect}`;`bank_id` 原样提取,无别名 | **替换(收窄)** |
| S2 | 认证 | `Authorization` 头 API key(`http.py:5281`) | 登录会话(MetaPG 三表,bearer/cookie+CSRF)+G→H HMAC 信封(jti 一次性,`envelope.py:65-100,129-196`) | **替换** |
| S3 | 租户定位 | `_authenticate_tenant` 设 schema ContextVar,请求内缓存(`memory_engine.py:3249-3300`) | Meta 注册表快照+四元组 Grant(`snapshots.py:318-326`);快照 digest 钉死,漂移=409 | **替换** |
| S4 | 操作/授权校验 | `validate_recall` 仅 1 个调用点(`:8653`);precheck 挂 8 条计费路由,body 未读即拒(`http.py:5376-5450`) | H 验封重查+`authority.begin` 重 parse 重授权(`auth:414-448`)+SQL 内联守卫(`native_sql.py:137-167`)——**三层重查编排** | **新增** |
| S5 | 准入 | recall lane 16/核、排队≤30s→503+Retry-After/断连 499(`config.py:1563,1572`;`http.py:5363-5372`) | governor:4 层令牌桶+_队列+并发闸+状态预留槽(`admission.py:132-244`),并入 10s 总账,无 499 | **替换**(双方均有准入,机制整建制重写) |
| S6 | 请求解析 | Pydantic 开放模型(`http.py:413-518`);`types` 默认 None→展开**全部三种 fact type 含 observation**(`:6040`) | 闭式 parse,白名单 6 键,`types` 默认/强制 `{world,experience}`(`qhttp:122-154`) | **替换** |
| S7 | 超长查询 | HTTP 入口 >500 token→400(`:6026-6035`);引擎入口截断(`:8605-8609`) | 65536B 体上限+字节预留→413;query≤8192B→400;引擎断言——**无截断**(`qhttp:13,134`;`host:480`) | **替换**(注:上游已有长度上限) |
| S8 | 转发通道 | 进程内直调,无网关 | HMAC 信封+禁重试 aiohttp+响应≤1MB 增量读+闭式校验(`gq:126-156,58-111`) | **新增** |
| S9 | 引擎构造 | 进程单例 MemoryEngine(`server.py:70-87`) | QueryFacade 每实例一套 pg/host/factory/engine+Closed ports(`facade:209-224,71-117`);**仓内无生产装配点,仅测试构造** | **适配**(+守卫为新增) |
| S10 | 模型授权 | 配置/环境选 provider(`config.py:592,1273-1277`) | ModelBinding 五元组+purpose/ports 过滤恰一个(`auth:474-481`);L 端口白名单(`models/client.py:179-182`) | **替换** |
| S11 | 向量编码 | 多 provider 嵌入栈(`engine/embeddings.py`) | vendor provider 栈整体绕过;产品 `ScopedNativeModels._embed` 直发 L+维度/空间复核(`models/native.py:65-76`);vendor 只剩批处理壳(`native_models.py:598-599`) | **替换**(嵌入机制)/适配(批处理链) |
| S12 | 候选检索 | 四臂:semantic+BM25 单连接 UNION(`postgres.py:140-157`)、graph 播种扩展(`:179-190`)、时间窗(`:158-171`);bank 级三开关(`:8701-8703`) | 两臂 QP01:`native_query_candidates_v1`(semantic `<=>` 余弦+keyword `pg_catalog.english` ts_rank_cd,`native_query_sql.py:264-278`);图/时间/实体全关 | **替换+收窄** |
| S13 | 候选水合 | 重排前全量取回正文(`hydrate_results :9598-9606`)——只取文本 | QP03 **带见证的全有全无水合**+证据组登记(witness 绑定/冲突/重用拒绝,`host:245-259,640-649`) | **新增**(上游有无见证取回,无资格语义) |
| S14 | 融合 | RRF k=60(`fusion.py:29`);cap_per_source 全局配置默认 0=关(`config.py:1332`);trim(`:9550-9567`) | 同库同函数,经 seam hash 钉死加载(`host:610-619`);git 证实 `fusion.py/recall_boost.py/fact_budget.py` 自导入零改动 | **保留(经 seam)** |
| S15 | 重排 | cross_encoder 默认可关降 RRF(`:1642-1651`);执行体 Step4(`:9535`) | 强制 cross_encoder;**`reranking.py` 被产品改写**(seam 标记+`query_policy` 形参+`record_pair` 血缘挂钩,提交 fbe7f29/32a0bcc);发送前逐组 QP07 | **改写+新增(QP07)** |
| S16 | 预算选择 | `select_facts_within_budget`(`fact_budget.py:43`,调用 `:10083`);max_tokens 只计 text | 同函数经 seam;git 证实零改动 | **保留(经 seam)** |
| S17 | 输出复核 | 无 | `final_check`:QP07 逐组+输出授权,不合格抑制整个答案(`host:544-585`);G 末帧死线复核(`gq:241-247`) | **新增** |
| S18 | 事后回取 | entities **默认开**(`http.py:398-402,6053-6054`);chunks/source_facts 默认关可开(`:404-410`;`:9827-9876`,`:10177-10353`) | 全链拒绝(`host:622-625`);投影恒 null(`facade:137-143`) | **关闭** |
| S19 | 响应投影 | `RecallResultModel` 直构开放形状(`:10571-10578`;`http.py:768-827`) | `project_native` 闭投影+字段剥离(`facade:128-159`);G 二次形状校验(`gq:58-99`) | **替换** |
| S20 | 审计/计量 | `@audited` 异步写 `audit_log` 表(`engine/audit.py:183-231`);`llm_requests` 表(`engine/llm_trace.py:598,699`);`on_recall_complete` 计量回调(`operation_validator.py:789-804`) | 本地同步 fsync 审计(0700 目录+dev/inode+逐帧 sha256,`control/audit.py:146-299`);usage 仅 observed_calls、`cash_cost` 恒 UNKNOWN | **替换** |

**改造程度汇总**:替换 10 · 新增 4 · 保留(经 seam)2 · 适配 1 · 改写 1 · 关闭 1(S11 双标计替换/适配)。

---

## 3. 贯穿案例:一次召回的 18 步(代码路径叙述)

例:bank=`qb-bank`(属租户 T-B),主体 alice(租户 T-A,有 read Grant),body `{"query":"青桥周报本周进展","types":["world"],"max_tokens":4096}`。**这是代码路径走查,不是运行声明**——凡涉真实 PG/模型行为标 UNKNOWN。

| 步 | 代码处置 | 证据 |
|---|---|---|
| 1 | POST+路径匹配→QueryRoute("qb-bank","recall");10s 总预算起表 | `s1.py:203-219`;`qhttp:166-169` |
| 2 | 身份:Bearer RPC(无缓存);Host 单值校验 | `gq:168-171`;`s1.py:151-157` |
| 3 | 四元组授权:alice(T-A)对 qb-bank(T-B) 无 Grant→**403 AUTH_DENIED**,后三道防线不会再被执行 | `snapshots.py:318-326`;`gq:189-190` |
| 4 | governor:4 层桶+队列放行 | `admission.py:182-199` |
| 5 | 读体≤65536B;闭 parse:query≤8192B ✓、types ✓、无未知键 ✓ | `gq:175-179`;`qhttp:122-154` |
| 6 | 重身份+重授权(`gq:180-183`);cookie 身份死线下取到 expires_at | `qhttp:175-185` |
| 7 | 签 18 字段信封(jti=请求 ID)→禁重试转发→H | `gq:126-156,133-139` |
| 8 | H 验封:HMAC+jti 防重放+method/path/bank/action/body 绑定+15s 窗 | `handler/app.py:123-124`;`env:129-196` |
| 9 | authority.begin:重 parse 重核 route/bank/mode(`auth:419-427`)、invocation=UUID(jti)、deadline=min(4 项) | `auth:414-448` |
| 10 | 当前 Meta 快照+ModelBinding 恰一个(recall:ports {embed,rerank});max_tokens≤激活上限(`auth:464-467`) | `auth:450-518` |
| 11 | PRE_FACTORY_PG 预检(pair/READY,失败 409 BANK_NOT_READY)→audit START(jti 重放在此第二道拒绝 `audit.py:256-257`) | `facade:271-277`;`audit.py:256-257` |
| 12 | host 白名单核查→`engine.recall_async`→vendor 检测 `_native_query_host` 移交 `host.recall` | `host:463-528`;`memory_engine.py:8569-8571` |
| 13 | `encode_query(["青桥周报本周进展"])`→L embed(input_type=query+space_id,维度复核) | `host:631-632`;`models/native.py:65-76` |
| 14 | QP01 两臂:semantic 余弦+keyword `plainto_tsquery('pg_catalog.english',中文)`——**不报错,命中与否 UNKNOWN**(分词未实测);各臂 LIMIT+witness | `host:634-638`;`native_query_sql.py:261,264-274` |
| 15 | QP03 全有全无水合→组登记(witness 一致性核验) | `host:640-649,245-259` |
| 16 | cap→RRF→trim(vendor 原码)→CrossEncoder(L rerank,发送前逐组 QP07)→打分→预算选择 | `host:652-673,443-461` |
| 17 | final_check:源复检+记录校验+output 授权+逐组 QP07;**失败→整答案抑制→503 QUERY_CHECK_CLEANUP_UNKNOWN/UNKNOWN**(因已有模型发送,gate.effect≠NONE) | `host:544-585,577-583`;`facade:40-41,61` |
| 18 | project_native 闭投影(null 字段+剥离)→G 末帧死线复核(过期改判 503 UNKNOWN)→G 形状校验→回传;audit 落 TERMINAL 帧 | `facade:128-159`;`gq:241-247,58-99`;`audit.py:146-299` |

**三个对照断点**:(a) alice 换成无 Grant 主体→第 3 步 403,SQL/模型零执行;(b) body 加 `include_chunks:true`→第 5 步 404 CAPABILITY_NOT_ENABLED(未知键,`qhttp:132-133`,注意是 404 不是 400);(c) ModelBinding 缺 rerank→第 10 步 403,与 (a) 对外不可区分。

---

## 4. 核心代码对照(五组)

**①准入:上游 1 层 lane vs 产品 4 层桶**
```python
# 上游 api/admission.py:257-275 —— 每操作一条 lane,两个数
"recall": LaneConfig(config.admission_recall_max_in_flight,   # 16/核
                     config.admission_recall_max_wait_seconds) # 30s → 503+Retry-After / 499
# 产品 gateway/admission.py:188-199 —— 四层令牌桶 + 有界队列 + 状态预留槽,无 499
_global/_tenant/_subject/_action[principal, lane]             # 并入 G 端 10s 总账
```

**②超长查询:上游按入口分 vs 产品两级硬拒**
```python
# 上游 http.py:6026-6035(REST) / memory_engine.py:8605-8609(引擎入口截断)
if count_tokens(request.query) > get_config().recall_max_query_tokens:   # 默认 500
    raise HTTPException(400, "Query too long: ...")
query = _truncate_query_to_token_limit(query, ...)                        # 内部调用方截断
# 产品 contracts/query_http.py:134 + native/query.py:480 —— 没有截断这回事
query = text_input(value["query"], 8192, empty=False)                     # 超限→400
require(0 < len(spec["query"].encode()) <= 8192)                          # 引擎断言再拒
```

**③候选 SQL:上游四臂动态拼装 vs 产品闭式两臂例程**
```sql
-- 上游 engine/sql/postgresql.py:324(semantic 臂,动态拼装)+ 413-419(BM25 臂)
1 - (embedding <=> $1::vector)                      -- + 图扩展 + 时间窗臂
-- 产品 storage/native_query_sql.py:264-274(编译期固定的例程,guard 内联)
1-(embedding OPERATOR({ext}.<=>) qvector) AS score  -- 仅 semantic+keyword 两臂
qts:=plainto_tsquery('pg_catalog.english'::regconfig, p_request->>'query_text');
```

**④资格复核:上游没有 vs 产品发送前+输出前各一次**
```python
# 产品 native/query.py:443-461(before_send)与 548-562(final_check)
receipt = await self.pg.qualify_read(..., refs, witness)   # QP07 逐组资格
# 上游对应位置:hydrate_results(:9598-9606)只取正文,无逐组资格/见证语义
```

**⑤审计:上游异步落表 vs 产品同步 fsync 本地段**
```python
# 上游 engine/audit.py:183-231 —— fire-and-forget INSERT audit_log(可开关,零开销直通)
# 产品 control/audit.py:233-244 —— {record,length,sha256} 帧逐条 fsync,失败锁 Unit:
#   当次 QUERY_AUDIT_UNAVAILABLE;后续请求 QUERY_UNIT_NOT_READY(audit.py:90-92)
```

---

## 5. 改造到什么程度:三层结论

| 层 | 结论 | 依据 |
|---|---|---|
| **算法/代码层:基本齐** | G→H→SQL→seam 全链代码在位;融合/预算沿用 vendor 原码,重排改写加血缘,检索例程编译期固定 | §2 大表;git 提交核查(fbe7f29/32a0bcc;fusion/fact_budget 零改动) |
| **装配层:缺生产组合根** | G 端 `query_forwarder` 默认 None、src 无装配点;H 端 QueryFacade **仅测试构造**;整条 recall 链今天"没有任何生产入口能走到" | `s1.py:78`;`facade` 构造仅见于 `tests/stage1/query/test_host_authority.py:224` |
| **验证层:STALE + 全 NOT_RUN** | native 指纹 `4cb8c00a→ea23b4e4` 待重绑 activation;真实 DataPG/L/端到端全未运行 | `STAGED-CAPABILITIES §9.1`;`MODULE-PROGRESS §5` |

---

## 6. 缺什么(三个篮子 + 两个无主决定)

**篮子 A|S1 要求、产品关闭**(MP-03/MP-08,`FIRST-RELEASE-SCOPE.json` 未后置):图/时间臂、observation 读取、chunk/原文回查、中文样例(见无主决定①)、Pages 检索组合(D7)、Reflect 的 MM 层。

**篮子 B|版本差(0.10.3 已改、产品内核 0.10.2 没有)**:#5388 统一截断(被产品 8192B 硬拒中和)、recall MCP 紧凑 JSON(产品无 MCP,无关)、#5432 bank-gone store 层下沉(被注册表语义中和)、#5358 同步 retain 断连取消(**留意**)、#5063 路由模板指标(**留意**)。

**篮子 C|验证缺口**:T4a 全部退出项(正常 Recall 带来源、read/model 拒绝、Retain final/hold 改 stamp 后 send/output 拒绝、H24/H40/H41 上限/末帧、L 错误/usage 关联)、T6 第一链、J1 双租户、B1/B2 原生对照——全部 NOT_RUN。

**两个无主决定(文档里没有任何包负责,必须先拍板)**:
1. **中文检索路线**:TEST-PLAN 中文 oracle(Q1/Q2/Q3、AT05c)+ keyword 臂 `pg_catalog.english` 钉死 + MP-03 中文样例验收,三者并置,而 NEXT-STAGE-PLAN/D4/D8 文本均无语言决定落点(五组文档全检证实)。
2. **G/H/L 绝对 deadline**:10s vs 15/60s 是 PROPOSED 未接受;STAGED 只说"下一包须在运行前固定"(`§3.6:267`),未具名负责包——真实运行包登记义务里有"预算与deadline"(`NEXT-STAGE-PLAN:180`),但归属未定。

---

## 7. 下一步(全部依 NEXT-STAGE-PLAN 原文)

**最短关键路径**(mermaid 唯一源头节点):**D1(Session 生产组合)+D3(Retain 新资源 H/W 提供方)+D4(base Query 重绑定)并行 → T1/T2/T3/T4a → T6 第一条真实链**。第一条 API 链不等目录/浏览器(`:114` "base HTTP不等待D2";STAGED `§3.6`)。

| 顺序 | 包 | recall 相关的最小内容 | 验证 |
|---|---|---|---|
| 1 | D1+D3+D4 | D4:QueryActivation 重绑到 manifest `ea23b4e4…`(继承未变 10 核心/22 路径);补 H24/H40/H41(正常/65536/末帧)与 H38/H39(SessionStore→G 窗口) | T1/T2/T3/T4a |
| 2 | T6 | 同 raw/root/document/facts/ref 独立来源 gold;65536/65537B 拒绝;末帧发布顺序;无授权对照;**本包不关闭 J1/J2/J3** | T6 |
| 3 | D2/D4 | Bank DTO/目录 provider(须绑实际同一 forwarder,"仅返回 NOT_ENABLED 不足以完成本包");UI18 只修 wire 差额 | T4b/T5 |
| 4 | D5→D6→D7 | 人工页提供方(D5)是归纳/MM(D6)与 Pages 检索组合(D7)的固定输入;未审前 Host 继续拒绝 PagesStore | T7/T9/T10 |
| 5 | D8/T11 | 图/时间/原文/文件/管理逐入口("已有实现仅待验证/缺提供方/缺接线/后续关闭") | T11 |
| 6 | D9→T12→T8 | 多实例/部署/接手;T8 最后合流,**当前 NOT_ACCEPTED** | T12/T8 |

并行条款(`:70`):D5 可在 D3/D4 接口固定后并行;D9 准备不等 T6;真实 L/OBS 未就绪时可先交 Session 真实 PG 证明,但不得宣称完整业务链完成。

---

## 8. 交叉验证记录(1+1 落痕 v2)

| 初稿说法 | 复核结果 | 处置 |
|---|---|---|
| "validate_recall 有 22 个调用点" | 上游核查员证伪:全引擎恰 1 个(`:8653`);22 是 doc-06 所述 0.10.3 `validate_bank_read` 数 | 删除该说法 |
| "上游 entities 默认关" | 上游核查员证伪:entities 默认开(`http.py:398-402`),chunks/source_facts 才默认关 | 改写 S18 |
| "S5 准入=适配" | 产品核查员称"上游无准入",与上游核查员+doc-06+前轮 V2 三重证据矛盾 | **人工裁决:改判"替换"**(上游有 lane,产品整建制重写) |
| "上游 types 默认与产品一致" | 新发现:上游默认展开全部三种含 observation(`:6040`),产品默认/强制 {world,experience} | 补入 S6 |
| "融合/重排/预算=vendor 原码" | git 核实:fusion/recall_boost/fact_budget 零改动;**reranking.py 被改写**(fbe7f29/32a0bcc) | S14/S16 保留,S15 改"改写" |
| "QueryFacade 生产可用" | 证实仓内无装配点,仅测试构造;运行期生命周期 UNKNOWN | 补入 §5 装配层 |
| "audit 失败锁 Unit=QUERY_AUDIT_UNAVAILABLE" | 拆分:当次该码,后续锁 Unit 是 QUERY_UNIT_NOT_READY(`audit.py:90-92`) | 改写 S20/案例17 |
| "下一步第 2 层=D2" | NEXT-STAGE-PLAN:43 原文为 **D2/D4** 两包 | 改写 §7 |
| "deadline 统一归 D4/T4a" | 文档无具名归属;PROPOSED 标签出自 TEST-PLAN:140 | 改为"无主决定②" |
| (v1)"中文会被 keyword 臂拒绝" | 不拒绝,静默空转;且"应在 D4/D8 前解决"是建议非文档原义 | 案例步 14 标 UNKNOWN;§6 分开表述 |

---

## 9. 一段话总结

上游的 recall 是一台开架自助机:单体进程、最多四臂、开放 schema、API key 进出、30 秒排队和 499,查到就给。产品把它整建制改造成三道安检的流水线:门收到 2 条,臂砍到 2 条,认证换成会话+一次性信封,隔离下沉到 SQL 本体,并新增了上游完全没有的资格体系(带见证水合+发送前/输出前两次 QP07+逐帧 fsync 审计)。20 个阶段里:10 个替换、4 个新增、2 个原码保留、1 个适配、1 个源内改写、1 个关闭——**算法层基本齐,装配层缺生产组合根,验证层 STALE+全 NOT_RUN**。下一步不在写代码:先拍板两个无主决定(中文检索路线、G/H/L deadline),再走 D1/D3/D4→T6 第一条真实链。
