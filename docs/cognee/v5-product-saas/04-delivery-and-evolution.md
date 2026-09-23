# 04：改造工作包、恢复、引擎迁移与上线验收

本版的产品闭环不以“画出 Kubernetes”作为完成条件，而以真实身份、真实后端和故障测试验证。以下工作包尚未编码实施。

## 1. 改造应该落在哪些模块

平台模块名为拟建逻辑职责，可先放在一个 API 服务和一个控制/作业服务内；不要求各建微服务。

| 工作包 | 平台新增位置 | Cognee 接点/现状 | 交付结果 |
|---|---|---|---|
| 身份与用户组 | OrganizationDirectory、CredentialService、Policy | [User/tenant选择](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/tenants/methods/select_tenant.py#L32-L65)与Principal ACL | 请求级tenant、组到Space授权、范围化key、单一权威与原生投影 |
| Provider范围 | ScopeGuard、IdentityMap、BindingResolver | [上下文绑定](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L255-L348) | 固定actor/tenant/native scope，完整恢复，不按owner当前选择重算 |
| 上传与来源 | UploadSession、SourceService、SourceVersion | [add任务入口](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/add/add.py#L263-L270) | 不可变原件、幂等finalize、对象/资料/原生ID映射 |
| 持久作业 | Job/Attempt、Dispatcher、RepairJob | [当前后台执行](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/layers/pipeline_execution_mode.py#L104-L127)为进程内Task | 等待完整pipeline，重复投递不重复发布，失败可修复 |
| 执行owner与准入 | OwnerDirectory、SpaceGate、OwnerController | [当前本地锁](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/locks/dataset_lock.py#L18-L34) | 嵌入式所有读写单owner，更新屏障和交接协议 |
| 恢复与子任务收敛 | RecoveryCoordinator、受控补偿 | [runner](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/run_tasks.py#L210-L350)、[当前age恢复](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/cognify/recovery.py#L48-L124) | 先停止并await子任务再补偿；恢复先取得唯一所有权，不因超一小时误回滚他人任务 |
| Cell与最小权限 | Placement、Provisioner、自定义handler、SecretsResolver | [shared vector handler](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/pgvector/PGVectorSharedDatasetDatabaseHandler.py#L39-L87) | cell固定配置，分离DDL和运行权限，受控collection初始化 |
| 会话与清理 | SessionACL、scope映射、ScopedPurge | [session可见性](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/session_lifecycle/metrics.py#L334-L376)、[forget](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/forget/forget.py#L191-L218) | 共享知识不自动共享对话，清理不影响别租户 |
| 连续可读发布 | GenerationBuilder、Validator、Publisher、GC | 原生Dataset与图/向量接点 | 新Dataset成套构建，CAS切binding，旧读者退出后回收 |
| 第二引擎 | HindsightProvider、能力画像、迁移器 | [V4能力映射](../v4-engine-plugins/architecture.md) | 稳定API下真实第二后端，不按函数名机械替换 |

不为商业化改造长期复制整个 Cognee。优先外部薄适配与现有扩展点；必要的runner、上下文和安全清理修改保持小补丁、独立测试及上游升级记录。对每个Provider固定SDK/API版本，升级先跑契约和恢复测试。

## 2. 实施顺序和退出条件

| 阶段 | 做什么 | 未通过时不能承诺什么 |
|---|---|---|
| P0：产品边界 | Tenant/Group/Space、scope key、私有会话、入口封闭与权限权威 | 企业多用户服务；不能仅凭原生demo登录上线 |
| P1：一个Cell闭环 | 平台SQL、原件、持久Job、owner路由、准入屏障、范围化删除、最小权限初始化 | API扩副本后的可靠多租生产；不能保留管理员账号再宣传硬隔离 |
| P2：运营与多Cell | 公平调度、资源目录、容量预算、备份恢复、drain与fence、审计用量 | 专属套餐、弹性迁移、故障恢复SLA |
| P3：持续读取 | 隔离generation构建发布，或实际验证的Provider可见性协议 | 更新期间零中断或跨图/向量原子可见 |
| P4：替代引擎 | 真实HindsightProvider，新空间验证，再迁移一个旧空间 | 配置热切换旧数据、无损迁移或性能提升 |

P0/P1 不是只写文档；退出需跑下文的两个租户、多个组、多个空间和故障用例。先把一个闭环做完整，再扩大实例数量。

## 3. 故障恢复如何操作

| 事件 | 应对流程 | 保持的边界 |
|---|---|---|
| API在提交后断开 | 客户端用相同幂等键读取原operation | 不重新创建一份作业 |
| 图已写，向量失败 | Space保持blocked，记录部分副作用，取得所有权后修复 | failed不等于清理完成 |
| Worker失联 | suspect、停止新投递；判断旧执行环境是否仍活 | lease超时不等于停止 |
| 嵌入式owner节点分区 | 终止旧实例或验证存储IO隔离，再启动接管；无法证明则维持blocked | 一个图文件不产生两个写者 |
| 发布后响应丢失 | 读取权威Job/binding确认，不能盲目删除可能已发布版本 | 已发布结果不被当失败产物回收 |
| SQL/对象或凭据不可用 | 停止相关读写、告警，按当前绑定恢复 | 不降级成默认用户或任意其他Cell |
| 租户退订或资料删除中断 | tombstone保留，逐资源补清，禁止旧版本重新发布 | 不复活旧资料 |
| 备份恢复 | 隔离恢复→核对manifest→重放删除/撤权→验收→开放 | 备份存在不等于恢复后授权仍正确 |

固定 owner + PVC 并非自动高可用。Pod被驱逐、SQL owner字段改变、网络撤流都不能证明旧进程已停止写文件。只有确认进程/节点终止或存储层隔离旧IO后才能安全接管同一文件。备份间隔、原件重建时间与模型费用决定实际RPO/RTO，不能直接写“零丢失”。

恢复策略还需处理旧任务对共享SQL/会话的副作用：仅阻止切换active binding不够。新generation隔离输出之后，也要拒绝旧epoch修改平台权威状态，并排空或隔离旧环境。

## 4. 图 8：切换引擎是持久迁移，不是修改名称

~~~mermaid
flowchart LR
  Start["原Space继续使用Cognee"]:::cognee --> Prepare["创建Hindsight目标binding<br/>平台权限和source ID保持"]:::platform
  Prepare --> Replay["重放原件、业务时间、纠正<br/>同步删除记录与新版本"]:::platform
  Replay --> Check["影子检索/回答评测<br/>不写反馈或会话"]:::platform
  Check --> Gate{"能力、隔离、来源<br/>删除、效果通过"}:::platform
  Gate -->|未通过| Keep["旧绑定继续服务<br/>修复或终止迁移"]:::platform
  Gate -->|通过| Drain["排空或隔离旧写作业<br/>追平最后变更"]:::platform
  Drain --> Switch["CAS切换binding revision"]:::platform
  Switch --> H["Hindsight API与原生Worker"]:::oss
  H --> Window["保留有条件回退窗口<br/>双端变更与tombstone对账"]:::platform
  Window --> Retire["满足保留策略后退役旧资源"]:::platform
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

回退不是指针随时指回旧库。如果旧端没有新写入和删除记录，回切会丢数据或复活已删除内容。原件可帮助重建，不能恢复所有未单独记录的人工图编辑、反馈学习状态和原生派生记忆。

Hindsight自身的operation、bank和transfer能力在适配器内转换，外部客户不改API。只有已验收的新空间选择可以接近“改配置”；旧空间必须走迁移。

## 5. 成熟产品的验收矩阵

所有项目都是待实施测试，不是本轮已通过：

| 测试 | 必须观察到的结果 |
|---|---|
| 两租户同名组/空间/文档/session；同账号并发访问 | UUID和scope定位正确，无活动tenant相互覆盖 |
| 组成员移除、只读key上传、MCP伪造tenant/clientInfo | 权限拒绝；客户端名称不能扩大访问 |
| 共享空间但不共享会话 | 他人QA/trace不可读，原文下载单独授权 |
| 文档ACL不同及跨空间问答 | 检索/图扩展前分域；没有只在引用尾部过滤 |
| 同幂等键并发及响应丢失 | 一个业务operation；不同摘要冲突；副作用可对账 |
| 图成功后向量失败、补偿失败 | blocked + repair_pending；不返回半成品回答 |
| 长任务时新副本启动 | 不被按任务年龄误清理 |
| owner lease到期而旧进程仍活 | 不出现同文件第二写者，不能仅刷新SQL owner接管 |
| 新generation发布与取消/删除竞争 | tombstone和版本检查优先，旧结果不再次可见 |
| 删除与流式读取竞争 | 按明确完成点取消/排空；不承诺撤回已发送内容 |
| 池膨胀、单租户大量任务 | 连接、并发和预算受控，其他租户获得调度机会 |
| 租户删除、全局prune防护 | 只影响目标范围，修复与备份政策可追踪 |
| 凭据限权后首次摄取/模型切换 | collection/索引初始化可控，不偷用管理凭据 |
| 备份恢复及Cell迁移 | 图/向量/来源版本一致；撤权和删除不回滚 |
| Hindsight真实适配 | 通用契约通过；特有能力缺失显式拒绝，不静默丢会话语义 |

观测维度应覆盖tenant、space、cell、operation、binding和model profile；日志不默认采集完整正文。容量关注队列年龄、活跃Dataset、连接总数、图规模、检索fan-out、模型预算、修复积压和恢复时间。SLA数值需要在目标硬件与真实语料压测后确定，本版不虚构吞吐/时延。

用户账单按已定义计价事件去重；供应商因重试产生的模型成本仍要记在成本账中，不能用“operation幂等”假定下游模型从未被重复调用。

## 6. 本轮验证与边界

本轮进行了代码定向阅读、三个专项合并及关键路径抽查；重点确认active tenant修改、session可见性、shared凭据、全局cache prune和本地锁的边界。没有部署产品、运行模型或迁移客户数据。

本仓库保存了V4归档和V5设计，业务源码仍位于原Cognee仓库且未修改。后续实现应以本版退出条件验收，不将静态图或单用户add/recall成功当成完整产品完成。
