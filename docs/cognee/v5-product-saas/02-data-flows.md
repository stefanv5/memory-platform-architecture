# 02：开户、上传、查询、更新与删除的端到端流程

本章用同一个例子贯穿：甲公司有财务组和研发组；财务组使用 Space F，研发组使用 Space R；两者可同处 Cell C1，但不能混用图、证据或会话。所有状态和平台组件均是建议实现。

## 1. 开户与空间开通先于数据处理

1. 登录身份通过组织目录创建 Tenant，选择允许区域、隔离等级、用量预算；不把数据库连接串作为客户参数。
2. 用户建立 Group 并邀请成员；管理员创建 Space，指定初始 grants 和产品能力画像。
3. Placement 按区域、Provider 能力、Cell 健康与容量选择资源组，创建 provisioning 记录。资源不足保持等待或失败，不偷偷跨区域。
4. 控制器幂等创建原生 Dataset/bank、必要图/向量空间、运行时凭据、原件前缀和会话映射。
5. 验证连接、权限与版本后将绑定置 ready，再开放上传；半开通资源由 RepairJob 清理或补齐。
6. 记录管理操作审计。资源创建账号仅在控制器使用，普通请求不会临时获取建库管理员密码。

Cognee 当前存在请求内懒创建路径，首版需把开通职责前移，或至少用受控的幂等开通状态机包围它；不能把多副本“同时首次访问”当作资源控制方案。[当前资源开通入口](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/utils/get_or_create_dataset_database.py#L97)。

## 2. 图 3：上传资料后，实际写入发生在哪里

~~~mermaid
sequenceDiagram
  participant U as 用户或连接器
  participant A as 平台 API与权限
  participant O as 原件对象存储
  participant P as 平台SQL目录与Job
  participant W as Cell固定执行owner
  participant C as Cognee流水线
  participant S as 图与向量及原生SQL
  U->>A: 申请上传到Tenant A / Space F
  A->>P: 权限与配额检查，创建UploadSession
  A-->>U: 固定对象键和版本的受限上传能力
  U->>O: 上传不可变原件
  U->>A: finalize + 幂等键 + 校验信息
  A->>O: 核验完整对象、版本、大小与完整性
  A->>P: 同一事务保存SourceVersion、Job、可选Outbox
  A-->>U: 202 accepted + operation_id
  W->>P: claim Job，恢复scope与binding并重新授权
  W->>P: 关闭Space查询准入，等待已发读请求结束
  W->>O: 按受控manifest读取原件
  W->>C: add / cognify，等待实际完成
  C->>S: 保存原生元数据、图、向量与来源关联
  C-->>W: pipeline结果
  W->>S: 核验产物和来源完整性
  W->>P: CAS完成当前Job并按gate版本恢复查询准入
  U->>A: 查询operation或Space状态
  A-->>U: searchable，或repair_pending及原因
~~~

此图对应首版 blocked_in_place 更新模式。执行 owner 是部署实例，不是 Dataset.owner_id 的用户归属字段；远程图模式下，写 Worker 承担同一修改协调职责。

平台原件与 Cognee 内部 Data/Document 并非重复身份：平台持有稳定资料 ID 和版本，Provider 记录其原生资料/块/事实映射。替换引擎不改客户 source_id。

上传与数据库不是一个事务。应先登记 UploadSession 暂存保护，再上传并校验；finalize 与 GC 对同一登记使用互斥或 CAS。否则长暂停后恢复的请求可能接受一个已被 GC 清掉的对象。失败上传可回收，已接受 Job 的对象必须有持久引用。

PDF/网页/附件属于外部输入，解析任务需有文件类型、体积、超时、内存和出站访问边界；受控暂存后再处理。它们不是让公共 API 任意读取本机路径或内网 URL 的通行证。

首版可由 Worker 直接轮询 PostgreSQL Job，不要求同时部署 RabbitMQ、Kafka、Redis、Temporal。若已有 broker，则事务内 Outbox 记录投递意图，投递允许重复，Job 仍是权威状态。该模式解决提交与通知双写缺口，不解决图、向量和对象之间的原子事务。[Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)

## 3. 接受、执行、可见是三种不同状态

| 层 | 必要状态 | 为什么独立 |
|---|---|---|
| 业务操作 | accepted、queued、running、validating、succeeded、repair_pending、failed、cancelled | 模型调用结束不等于所有存储完成；取消请求不等于回滚 |
| 空间可见性 | empty、serving、updating_blocked、recovering_blocked、deleting_blocked、deleted | 原地部分写失败时必须保持不可查 |
| 执行 owner | ready、draining、suspect、fencing、recovering | 失联不证明已停止，不能立刻启动第二个文件写者 |

幂等键在调用前持久化，与 tenant+space 绑定；同键不同 request_digest 拒绝。同一资料的 revision、一次操作 ID、attempt 重试号分别建模。较旧 revision 或 tombstone 前的重放不允许覆盖新状态。

原地更新恢复准入也必须 CAS：匹配当前 mutation/attempt、owner epoch、binding 和 gate revision，并确认没有取消、删除或修复阻断。若匹配失败，旧Job不得重新开放Space，交由当前协调者处理。不能只在generation发布时验证版本，却允许原地任务结束后无条件改回ready。

Worker 开始时重验最新权限和取消状态，固定 binding/model profile。恢复时先核对原生副作用，不能只看 Job failed 就重新调用模型。现有 PipelineRun 和进度是审计材料，不是持久队列/完整检查点；Dataset 锁明确仅进程内有效。[锁的边界](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/locks/dataset_lock.py#L18-L34)

## 4. 图 4：查询、模型上下文和证据都必须在授权范围内

~~~mermaid
sequenceDiagram
  participant U as 用户或Agent
  participant A as 平台查询入口
  participant P as Policy与绑定目录
  participant Q as 查询协调
  participant R as Cognee运行时或固定owner
  participant D as 图与向量
  participant L as 模型服务
  participant E as 来源与会话服务
  U->>A: query + 明确Space范围 + mode
  A->>P: 验证成员、key scope、Space授权
  P-->>A: 已授权范围、policy revision、固定binding
  A->>E: 校验SessionGrant及历史QA、trace、来源的当前权限
  E-->>A: 仅返回仍授权的历史上下文
  A->>Q: 查询预算与可信上下文
  Q->>R: 获取Space读permit并执行检索
  R->>D: 只访问该binding的图和向量
  D-->>R: 候选、关联和来源
  R-->>Q: 授权证据或原生回答
  opt 跨空间统一回答策略
    Q->>L: 仅发送各已授权空间的证据
    L-->>Q: 答案
  end
  Q->>P: 按合同复核权限/删除状态
  Q->>E: 记录私有会话与证据引用
  A-->>U: 答案或证据集，含来源版本
  A->>Q: 响应或流结束后完成输出fence并释放剩余permit
  U->>A: 打开证据/下载原件
  A->>P: 重新检查source.read/download
  A->>E: 解析固定source版本
  E-->>U: 授权内容或受限下载能力
~~~

图中的两类回答策略不同时默认执行。单空间可使用 Cognee 原生回答；跨空间统一回答需要平台先获取证据再生成。当前多 Dataset 路径会分别执行检索/回答，不等于已经有跨空间统一重排和一次生成，需新增明确策略。

每次查询只解析一次 binding，图和向量来自同一份资源清单。首版读 permit 的获取必须与写者关闭准入互斥；“查到 ready 后再调用引擎”之间仍有竞争，不能代替准入协议。

如果 Cell C1 的检索不可用，不应把请求随机重试到 C2；只有目录确认 C2 有相同已发布数据和授权绑定时才可路由。多空间查询失败默认报告失败或有标识的部分结果，不能悄悄放宽范围。

模型服务接受租户批准的 profile 与预算；模型端点、数据出境规则、Embedding 版本来自目录，客户不能用 query options 改进程全局配置。更换 Embedding 模型不只是修改模型名，即使维度相同也需要验证或重建索引。

会话保存和自动反馈仍保留产品记忆功能。会话存储使用独立授权 scope；触发 improve 的图写操作进入同一个 Space 写协调，不能在持读 permit 时同步等待写 permit而死锁，也不能绕过屏障。普通只读凭证默认不授予用户可控的写型feedback/improve；若产品启用自动派生维护，必须由独立受限维护主体执行，仅处理已授权记忆的既定维护操作，并受撤权/删除/预算约束，不能转化成任意source.write。影子评测关闭写回副作用，不通过关闭整个 CACHING 冒充性能改进。

数据读permit与输出fence分别追踪：前者覆盖引擎实际读取，后者持续到响应/流最后字节或取消确认。首版可保守地都持有到响应结束；慢客户端必须有超时和取消收敛。撤权/删除声称对所有在途输出生效时，必须等输出fence完成，不能在拼完答案时提前释放就确认撤权。

## 5. 更新：先选一致性模式，再承诺服务行为

| 模式 | 修改期间能否读取 | 实现方法 | 成本与适用 |
|---|---|---|---|
| 首版 blocked_in_place | 该 Space 暂停，其他 Space 正常 | 持久关闭准入、排空读者、修改、核验后恢复；失败保持 blocked | 改造少、可验证；不适合承诺持续读可用的套餐 |
| 后续 isolated_build_publish | 普通新增/更新可读旧版本 | 新 Dataset/成套图向量构建，验证后 CAS 切 active binding | 双份存储与重建成本；要新增完整发布机制 |
| Provider 原生可见性 | 依据其实际能力 | 如 Hindsight operation 与派生刷新状态 | 不能强行声明和上述两种相同，需能力画像验收 |

isolated_build_publish 的独立版本必须覆盖图、向量、来源归属和完成标记，不能只给向量换 collection。初期按完整 Space 重建估算；新版本 sealed 后不得由自动维护偷偷改写。发布同时检查 expected binding、写 epoch、来源清单、取消和 tombstone；旧读者退出后再回收旧版本。

该机制属于平台新建能力，不是 Cognee 配置开关。提高可用性的收益，要与全量再抽取、模型费用、数据库数量和回收复杂度一起评估。

## 6. 图 5：删除的完成点在哪里

~~~mermaid
flowchart LR
  Request["授权删除请求"]:::platform --> Mark["同事务保存tombstone和DeletionJob<br/>阻止旧版本再次发布"]:::platform
  Mark --> Gate["关闭受影响Space查询准入<br/>取消或排空在途读取"]:::platform
  Gate --> Clean["删除或重建干净的<br/>图、向量、来源、派生记忆"]:::cognee
  Clean --> Verify{"完整性及不可检索验证"}:::platform
  Verify -->|失败| Repair["repair_pending<br/>保持隔离并重试"]:::platform
  Repair --> Clean
  Verify -->|通过| Online["先失效或隔离相关会话历史<br/>QA向量、上下文与答案缓存"]:::platform
  Online --> Serve["按当前gate版本开放干净版本<br/>所有在线读取路径不再使用已删内容"]:::platform
  Serve --> Physical["已不可访问原件和历史数据的物理清理<br/>按策略处理备份"]:::platform
  Physical --> Receipt["逐资源删除回执<br/>说明保留例外与到期时间"]:::platform
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

只移除 source citation 不够：旧图的摘要、实体或关系仍可能携带被删事实。首版在干净版本或原地清理验证完成前阻断受影响 Space 的新查询；已发布给用户的答案无法撤回。

区分三项承诺：删除已接受、对在线查询已生效、物理清理按政策完成。保留期备份不能谎称即时擦除；从备份恢复时先应用持久 tombstone 和撤权记录，再对外开放。会话有相关资料的历史答案时，应按产品保留政策清理或隔离，不能只清文件。

租户退订也用同一机制：停入口/凭证→排空或取消任务→逐 Space 隔离和清理→原件/会话/密钥处理→资源回收→最小审计记录保留。禁止对共享实例执行全局 prune 来代替租户删除。

## 7. 图 9：Cell 内部展开——Cognee 承担哪些处理

主产品图把 Cognee 合并为运行时方框；下面展开它的职责，避免把首版误读为平台重新实现整个引擎。橙色是新增的范围/执行控制，蓝色是复用的 Cognee 处理，绿色是外部存储。普通文档链与会话链不同，不是每次 remember 都完整走分块抽图。

~~~mermaid
flowchart TB
  Scope["平台已批准命令<br/>tenant、actor、Space、binding、profile"]:::platform
  Guard["Provider ScopeGuard + 查询/写准入<br/>恢复固定Dataset、原件位置和运行身份"]:::platform
  Scope --> Guard

  subgraph Runtime["Cognee 完整运行时"]
    Add["add：输入与摄取Task"]:::cognee
    Load["Loader：解析原件<br/>保存文本与资料元数据"]:::cognee
    Build["cognify：分类、分块"]:::cognee
    Extract["图抽取、摘要<br/>DataPoint与来源关联"]:::cognee
    Index["Embedding与add_data_points<br/>图节点/关系、向量索引"]:::cognee
    Search["search / recall<br/>选择Retriever、取证据与图上下文"]:::cognee
    Answer["Completion / 回答<br/>原生模型调用"]:::cognee
    Session["SessionManager<br/>QA、trace、会话记忆"]:::cognee
    Maintain["improve / memify<br/>派生维护任务"]:::cognee
    Handler["Dataset Handler与Adapter<br/>解析固定图/向量资源"]:::cognee
    Add --> Load --> Build --> Extract --> Index --> Handler
    Search --> Handler
    Search --> Answer --> Session
    Maintain --> Index
  end

  Guard --> Add
  Guard --> Search
  Guard --> Maintain
  Handler --> Graph[("该Dataset的Ladybug图文件")]:::oss
  Handler --> Vector[("该Dataset向量schema")]:::oss
  Load --> SQL[("Cell原生SQL与受控原件")]:::oss
  Session --> Cache[("范围化会话库及QA索引")]:::oss
  Session -.需要写图的后续任务.-> Job["平台维护Job<br/>独立写授权与Space协调"]:::platform
  Job --> Guard
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

这里绿色的图存储代表纯开源 Ladybug 路径，远程图部署另见存储章节。所有读取经过同一空间绑定，所有修改经过同一写协调；会话的原生写入也必须经过范围化改造。Handler 仍需接入平台固定凭据/资源，而不是默认全局管理员连接。

复用入口：[默认 cognify 任务](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/cognify/cognify.py#L581)、[图与向量写入](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/tasks/storage/add_data_points.py#L317)。流程图表达职责和调用方向，不意味着 SQL、图、向量已经有共同事务。
