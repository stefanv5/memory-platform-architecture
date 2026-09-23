# 13：共享运行池能否承载临时租户请求，以及 serverless 的边界

结论：**可以把多个租户的请求分配给同一组受控 Cognee 运行实例，但应按固定运行配置建立池，在每次操作中显式绑定授权 dataset；不能把当前完整运行时视作任意唤起、任意退出、无状态的函数。** ContextVar 已提供按 async context 选择数据库与部分模型配置的机制，但它不是完整租户上下文、不是凭证验证器，也没有把所有全局配置与缓存变成请求局部状态。

本章固定 Cognee SHA `663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`，复用09、12证据，限定运行时身份/上下文/查询副作用和启动入口；不重复深入存储 adapter、worker 或 recovery 内部。以下明确区分现有代码、推断与新增上线要求。

## 1. 现有代码能切换什么

| 范围 | 现有事实与代码 | 对 pooled runtime 的含义 |
|---|---|---|
| Dataset 身份 | current_dataset_id、graph_db_config、vector_db_config、session_user、LLM/Embedding 都有 ContextVar 定义。[变量](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L29-L45) | 同一进程可在不同操作上下文中选不同 dataset，并不要求每个 tenant 一套完整程序 |
| Dataset 路由 | scope 接受 UUID；数据库与文件存储按 dataset 真实 owner 解析，而不是将 caller 当物理 owner。permission_type 只有显式传入才在此再次检查。[绑定入口](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L177-L204)、[授权与owner](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L238-L265) | 先执行产品授权，再从可信 binding 得到 dataset；不能允许用户传 UUID 后直接进入 scope 并假定已经授权 |
| LLM/Embedding 覆盖 | 调用者显式提供的配置被 set 到 context；async-with exit 恢复这两个覆盖和 current_dataset_id；阶段 pipeline_stage 也 finally reset。[覆盖](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L193-L204)、[退出](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L380-L400)、[阶段恢复](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/llm/pipeline_stage.py#L12-L26) | 可承载经过平台验证的调用级 profile，但不是自动读取某 tenant 的凭证与模型策略 |
| Graph/Vector/File scope 退出 | 这三类 ContextVar 被明确设计为退出后保留，兼容 pipeline 后在 scope 外访问；session_user setter 也只 set 没有返回 reset token。[保留行为](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L341-L348)、[session setter](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L38-L45) | 不能把 async-with 结束解释为全部清理。复用同一 async task 顺序运行不同 job，或从污染的父任务创建下一任务，需要完整入口/退出边界 |
| 全局设置 | save_llm_config 修改缓存全局对象并可写 os.environ；LLMGateway 框架分支读取全局 get_llm_config。[settings](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/settings/save_llm_config.py#L14-L24)、[Gateway](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/llm/LLMGateway.py#L120-L154) | 不得用每租户请求改 settings/env 的方式共享进程；运行池的框架、全局SQL/cache基础配置应固定 |
| 活跃组织 | select_tenant 持久修改 User.tenant_id。[实现](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/tenants/methods/select_tenant.py#L32-L65) | 同一外部用户在两组织并发时不能依赖先切tenant再执行；按(org, external principal)映射固定 tenant 的原生身份，或实现真正请求级tenant |

**推断的准确边界**：保留的 ContextVar 不等于两个正常独立 async task 天然共用同一个变量值，不能据此直接宣称“任意并发一定串租”。风险来自错误的任务创建/复用、scope 外调用、漏绑 session user 或错误 fallback；共享配置对象和 adapter 缓存则是另一类进程级问题。必须分别测试，不能靠单次顺序 happy path 排除。

## 2. 一次可安全复用运行实例的调用应该怎样执行

以下为**建议新增 ScopeGuard / ProviderAdapter 边界**：

1. 从可信 Job/RequestContext 验证 actor、org、credential scopes、action、space、policy/binding revision；从服务端目录解析 native dataset 与固定 tenant 的原生 User。拒绝用户指定存储地址、native owner 或任意全局配置。
2. 为本次操作建立已审查的干净执行 context，显式绑定 graph/vector/file、dataset、session user、LLM/Embedding profile 和阶段标签。可以采用真正新建且不继承脏 scope 的执行任务，或外层统一快照/恢复所需 ContextVars；“调用 create_task”本身不代表干净，因为默认可继承父上下文。
3. 使用 async-with 而非已废弃的裸 await context API，确保 dataset slot 按期释放；域授权不能因 scope 绑定而省略。正常返回、异常、取消和 __aenter__ 初始化失败都由最外层 finally 恢复，不能只依赖内部 __aexit__。内部 apply 在成功后才标记已应用，初始化过程若失败需要外层统一保护。[兼容API与生命周期](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L354-L400)
4. 等待纳入业务承诺的 session/history/反馈提交；需要异步派生维护时，转换成可追踪操作，携带最小 scope 和独立维护主体。禁止让请求结束后仍存在不受跟踪的租户任务。
5. 输出前复核授权/删除条件，释放实例借用；context 清理并不等于必须关闭全部可缓存引擎。连接与引擎缓存可复用，但缓存键、租户凭证归属、在用引用和退役规则必须正确。

这里的“清理”不能通过遍历关闭全局 engine cache 来实现，否则会伤害同进程其他正在执行的租户。对需要不同全局 provider/framework/SQL/cache 配置的请求应路由到不同 Cell/pool，而非请求中切换进程环境。

Embedding 还有独立限制：vector engine 缓存键只包括数据库连接/名称/schema等配置，不含 embedding profile；创建 adapter 时才获取 embedder。对同一数据库临时切不同模型不能只靠 ContextVar 保证正确。[缓存键](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/create_vector_engine.py#L100-L137)、[创建时绑定](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/create_vector_engine.py#L167)。建议 dataset/binding 固定 embedding profile 与 generation；换模型走新索引发布，不把同维度等同同语义空间。这里只核实 cache 配置边界，数据库细节见存储证据。

## 3. 查询节点也不是纯只读、纯无状态

**现有**：session-aware 查询取得 session_user，加载 session snapshot，并可能并行执行反馈分析和答案生成；生成后 commit_turn；即使仅返回确认语也有 add_qa 路径。[查询turn](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/retrieval/session_aware_completion.py#L305-L399)。高层 recall 还记录 operation、search history。[recall history](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/recall/recall.py#L850-L862)。具体 SearchType、only_context 和配置改变副作用集合，不能声称每个查询一定写图，也不能承诺所有查询都完全无写。

**上线含义**：查询副本需要 session/history 写通路和幂等计量；同session跨实例并发需要保持turn顺序/反馈归属，不能将进程内协调直接当分布式保证。仅在HTTP答案发送后冻结/销毁函数，可能截断未完成的保存或维护。只读API key是否允许触发受控自适应反馈应在产品契约明确：若允许，后台使用有范围的维护主体；若不允许，禁写型反馈而保留允许的session功能，不能默认用关闭整个 CACHING 来模拟可互换的“无状态查询”。

会话权限也独立于租户路由：当前 dataset read 可以带来会话 QA/trace 访问，企业私有session需要12章所述适配；缓存键不能只含外部 session_id。跨后端或跨pool复用历史前重新校验 tenant、owner、space scope、来源授权版本。

## 4. 启动不是完全无副作用：serverless 冷启动要前置控制

**现有入口事实**：FastAPI lifespan 执行 migrations；失败分支尝试 create_database 再迁移；随后执行 recover_stale_pipeline_runs_on_startup，再解析认证 secrets。[lifespan](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/client.py#L92-L136)。这已经足以说明启动不只是创建无状态路由。scope 绑定还会调用 get_or_create_dataset_database，存在资源开通路径。[绑定中的开通入口](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L259-L265)

**限定推断**：大量冷启动实例若共用控制数据库，都会进入这些启动动作，必须检查迁移、开通和恢复的执行身份、互斥与范围。仅凭此入口不能断言 recovery 已经或必然破坏其他 worker，本章不读其内部；具体扫描/修复条件由执行证据负责。建议将schema migration、资源provisioning及恢复控制统一到受控启动/控制器流程；应用实例只做兼容性检查与必要的局部初始化。不得让每个按需函数都拥有全库迁移/全局恢复权限。

## 5. 三种“serverless”承诺要分开

| 产品说法 | 可实现范围 | 不可跳过条件 |
|---|---|---|
| 客户不用管理服务器 | 平台托管、按请求/用量计费；后端仍可有常驻 owner pool | 隔离、配额、调度与故障恢复完成；不需要宣称运行时无状态 |
| API 层弹性/缩到零 | 平台入口与路由尽量外置状态，冷启动重新取目录 | Auth/context/profile由持久目录恢复，冷启动不执行无范围迁移/恢复 |
| 任意计算实例都可执行任意租户数据操作 | 仅当后端存储访问、运行配置、协议和query副作用均支持该模式后采用 | 嵌入式图 owner 约束、文件访问及跨实例session协调由存储/执行方案满足；不能靠ContextVar就取消owner |

因此推荐先使用 **pooled 多租户控制面/API + 按固定配置划分的运行池 + 平台目录选择 Space binding**。嵌入式图方案仍路由到持有该数据访问权的固定 owner；将运行池包装成serverless产品是交付模式，不改变物理生命周期。选择远程图等路线后，也只是降低文件owner约束，不自动移除session/history、缓存、模型profile和后台任务状态。

## 6. 可执行上线前条件

- **身份矩阵通过**：两个tenant、多个用户与服务key同时/顺序命中同一实例；数据、session、模型凭证和trace无串用；同外部用户双组织并发不调用持久 select_tenant。
- **上下文异常矩阵通过**：初始化失败、LLM失败、取消、超时、嵌套scope后下一请求无残留；对task继承、scope外调用和遗漏session_user做负向测试。
- **全局配置禁止请求写入**：settings/env、loader/retriever全局registry只在pool启动/发布阶段更新；需不同全局profile时路由不同pool。
- **模型与缓存一致**：dataset embedding profile固定；缓存命中时验证所用embedder/数据库属于本binding；凭证轮换有明确cache退役流程。
- **查询副作用持久化**：跨实例session顺序、重试幂等、反馈授权与durable派生任务有验收；请求返回后不依赖未跟踪内存任务完成承诺。
- **启动权限收敛**：迁移/provision/recovery职责从按需应用实例移出或证明正确串行与scope；应用启动不修改其他正在执行实例的作业状态。
- **排空与输出协议通过**：回收实例前拒绝新租户操作、排空或取消在途工作并记录状态；权限撤销/删除的输出屏障覆盖实际响应发送，不能仅在模型返回时释放。

以上是待实施上线门槛，非本轮已运行的测试结果。没有完成这些条件，准确表述应是“具备多租户scope路由基础，正在改造共享运行池”，而不是“已支持无状态serverless集群”。

## 阅读范围

本轮重读/补读：context_global_variables.py 24–51、159–207、233–267、341–402；pipeline_stage.py 1–26；session_aware_completion.py 305–401；create_vector_engine.py 100–137；client.py 92–141；save_llm_config.py 1–24。LLMGateway全局getter、recall history及startup名通过定位并复用09/06已读证据；User.tenant_id及会话授权复用12。未重复读worker/recovery/数据库adapter内部，未测试外部数据库/模型，不作全文件或全仓覆盖声明。未修改Cognee源码，未提交。
