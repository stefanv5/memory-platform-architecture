# 01：对外产品、企业用户组与可信身份

产品首先要回答“谁可以在什么空间做什么”，再决定请求去哪个引擎。以下产品 API、模型和服务名都是建议新增；不是 Cognee 当前路由的重命名清单。

## 1. 客户实际看到的产品

| 产品页面/入口 | 客户可以做什么 | 背后责任 |
|---|---|---|
| 企业与成员 | 创建组织、邀请成员、同步身份、停用成员、建用户组 | Identity/OrganizationDirectory；成员关系不依赖引擎 |
| 知识空间 | 创建空间、给组授权、查看容量和状态 | SpaceService + Policy；绑定 Provider 与隔离等级 |
| 资料与连接器 | 上传、导入、更新、查看处理进度/来源、删除 | SourceService + 持久 Job；连接器使用范围受限服务主体 |
| 检索与问答 | 选择已授权空间，获得答案和可访问引用 | QueryCoordinator + Provider；显式区分检索与回答 |
| 个人/共享会话 | 查看自己的历史、显式分享、撤销和清除 | SessionService，独立会话授权 |
| 凭证与集成 | 创建只读空间 key、轮换、接入 SDK/MCP | CredentialService，权限只能比所属主体更窄 |
| 用量与账单 | 查看配额、模型调用和存储用量 | 幂等用量账本；租户管理员不因看账单自动能读正文 |
| 审计与运维 | 企业内审计；平台人员处理资源与修复 | 审计元数据与正文分权；支持人员临时授权可追踪 |

用户不需要知道 graph database 名称、Worker 身份或 generation 路由。这些进入平台运维界面，产品只展示“处理中、可查询、更新维护、删除清理中”等有意义状态。

建议对外统一资源 API：

| 示例 | 语义及关键检查 |
|---|---|
| POST /v1/tenants/{t}/spaces | space.create；空间进入 provisioning，就绪后方可写入 |
| PUT /v1/tenants/{t}/spaces/{s}/grants/{subject} | space.manage_access；目标主体属于同企业，禁止默认跨企业分享 |
| POST /v1/tenants/{t}/spaces/{s}/uploads | source.write；签发固定对象/版本的上传意图 |
| POST /v1/tenants/{t}/spaces/{s}/uploads/{u}/finalize | 再验范围、对象与幂等键；返回 202 + operation |
| POST /v1/tenants/{t}/query | 明确 mode=retrieve/answer、space_ids、预算；越权显式范围拒绝 |
| GET /v1/tenants/{t}/operations/{op} | op 所属 tenant、space 和查看权限；不能只凭不可猜 UUID |
| DELETE /v1/tenants/{t}/spaces/{s}/sources/{id} | source.delete；返回持久删除操作及清理状态 |
| GET /v1/tenants/{t}/spaces/{s}/sources/{id}/versions/{v}/download | source.download；授权后才发短期下载能力 |
| /v1/tenants/{t}/sessions/... | session owner/显式共享权限，不能用 space.read 替代 |
| /mcp | 将工具调用转换成相同产品命令与 Policy，不提供旁路 |

客户端携带的 t、s 只是请求意图。系统验证凭证和成员关系之后才形成可信上下文。401 表示未通过认证，403 表示未获授权；资源存在性敏感时统一采用不暴露存在性的响应策略。写入 202 不承诺已可查询；更新屏障返回明确的暂不可用状态、operation 和重试提示。

## 2. 用户组在哪实现，与空间、租户如何关联

~~~mermaid
flowchart LR
  T["Tenant 甲公司"]:::platform --> M["成员 Alice / Bob"]:::platform
  T --> G1["Group 财务组"]:::platform
  T --> G2["Group 研发组"]:::platform
  M --> GM["GroupMembership"]:::platform
  GM --> G1
  GM --> G2
  G1 --> Grant1["SpaceGrant<br/>read / write / manage_access"]:::platform
  G2 --> Grant2["SpaceGrant"]:::platform
  Grant1 --> F["Space 财务制度"]:::platform
  Grant2 --> R["Space 研发规范"]:::platform
  Bot["服务主体：财务助手"]:::platform --> Key["凭证 scope<br/>仅财务空间 read"]:::platform
  Key --> Grant1
  F --> CF["Cognee Dataset F<br/>独立图与向量范围"]:::cognee
  R --> CR["Cognee Dataset R<br/>独立图与向量范围"]:::cognee
  F -.迁移后新绑定.-> HF["Hindsight bank F<br/>保留平台 Space ID"]:::oss
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

Group 是平台组织目录对象，不是存储分片。成员可加入多个组，组可获多个空间权限；空间可同时授权多个组。建议首期使用可审计的 allow grants，避免过早引入复杂 deny 优先级和任意嵌套组。停用成员撤销其访问和未开始用户作业，而非删除全组资料。

权限示例：Alice 属于财务组，Bob 属于研发组。Alice 只能查询财务空间，不能因属于甲公司自动读取研发资料。租户管理员默认管理成员/资源，不默认获得全部正文；需要读取时另给 grant 并留审计。

Cognee 已有 User/Tenant/Role 的 Principal ACL 基础，可作为首版权限投影。它不是没有“组式授权”；平台 Group 可以映射 tenant 内专用 Role，具体权限映射要版本化。现有权限主要是 read/write/share/delete；产品若细分 download、session、billing 等 action，不能仅靠投影完成全部策略。[ACL 模型](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/models/ACL.py#L8-L22)、[Role 模型](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/models/Role.py#L8-L28)。

## 3. 请求上下文在哪里建立、在哪里复核

| 环节 | 必须执行 | 防止什么 |
|---|---|---|
| 身份入口 | 验证人类登录或服务凭证，取得真实 principal | 自报用户、租户、MCP 名称冒充身份 |
| Policy | 检查 membership、资源归属、action、凭证 scope、请求空间 | 只读 key 继承用户的全部可写权限 |
| SpaceService | 从目录解析批准的 binding/Cell | 客户端提供别人的 Dataset ID 或数据库地址 |
| 调度提交 | 固定 actor、tenant、space、policy revision、binding、输入版本 | 等待期间改变当前组织或默认配置导致错路由 |
| Worker 开始 | 从可信 Job 重取资源，按当前权限重验，检查取消/删除/版本 | 旧权限长期变成后台通行证 |
| Provider 入口 | 校验绑定所属范围，恢复完整上下文，映射原生身份 | 适配器错用全局用户、残留 ContextVar |
| 回答/证据/下载 | 校验输出范围及必要的最新权限，引用到固定资料版本 | 缓存、trace、下载成为数据旁路 |

内部 Context 至少包含 actor_id、tenant_id、credential_id、action、space_ids、policy_revision、binding_revision、request/trace_id。只由受信服务构建，通过认证的内部通道传递；下游不接受用户追加的同名 header 覆盖它。系统维护任务使用明确的服务主体，不能伪装成排队用户永久拥有权限。

当前 select_tenant 会更新持久化 User.tenant_id。这是用户级当前组织，不适合一个账号同时在甲乙两个企业发起并发请求。首版可将同一外部用户映射成每租户一个 Cognee 内部身份，固定其 tenant，避免每请求调用 select_tenant；代价是维护映射和审计关联。以后再改造成彻底的请求级上下文。[select_tenant](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/users/tenants/methods/select_tenant.py#L32-L65)。

## 4. 权限权威和原生 ACL 的关系

V5 新产品建议以平台 Policy 为产品权限权威，Cognee ACL 是能力受限的执行投影。所有产品请求先过平台，原生 API 不接受外部直达，避免投影延迟扩大访问。

新增 grant：原生投影成功后才将产品 grant 标 ready；撤权：平台先拒绝后续访问并失效缓存，再移除原生 ACL。失败进入 acl_sync_pending，不允许另一套管理界面独立修改原生授权。原生权限比产品粗的部分由平台继续限制；不能简单以租户级超级用户绕过所有 Cognee 检查。

现有 Cognee 安装迁入产品时，先导入/对账其 ACL，再切换权威。V4 所说“暂代理现有 ACL”是迁移阶段；不是两套系统长期各自接受 grant/revoke。

如要求“撤权完成后无旧查询继续输出”，需要关闭相关查询准入、取消或排空在途请求、完成撤权及缓存失效后才确认完成。流式已返回的 token 无法撤回；短期下载 URL 在过期前可能仍有效。需要更强即时下载撤权时，使用每次重新授权的下载代理，不把一次签名 URL 当永久可撤销权限。

## 5. 为什么首版空间内资料必须同读权限

当前主要 ACL 是 Dataset 粒度。知识抽取会合并实体、生成关系和摘要；某条答案可能没有直接引用受限文档，却已使用其事实。对最终引用做过滤无法撤销先前的图推理或 LLM 上下文。

因此首版规则是：**一个 Space 内的资料具有相同读取主体集合。** 写/删除权限仍可分角色；但不提供没有实现底层约束的“同空间私有文档”开关。连接器发现源文档 ACL 不同，应归入不同安全空间；跨空间搜索先分别授权，再检索和融合可访问证据。

好处是复用 Dataset 现有图/向量边界，权限模型可解释。代价是相同实体可能在多个空间重复，权限组合多时空间数量增长。应对接源系统权限并限制分区数量，不能为降低数量悄悄将私有文档归入公共图。

后续资料级权限需要覆盖 chunk/fact/node/edge/summary/observation 的来源和权限传播，以及候选检索、图扩展、上下文、缓存和删除。这是独立能力工程，不是给向量 payload 加 tenant/document_id 即完成。

## 6. 会话与 MCP 必须有自己的约束

Session 键建议平台生成并映射为 tenant + owner principal + space scope + session UUID；多空间会话保存显式范围。会话默认私有，共享要有独立 SessionGrant。空间 read 不自动授权别人的问题、工具 trace 或私人消息。

反方向也成立：SessionGrant 不能扩大来源权限。每次将历史QA/摘要/trace重新放入模型上下文、展示或分享前，应复核其来源Space和资料版本的当前权限。撤去财务空间权限后，不能从旧的多空间会话带回财务事实。历史派生内容若缺少足够来源标记，应保守隔离该条或该段会话，而非假定它不含受限信息；原始用户消息按会话授权与产品保留策略另行处理。

源码当前允许 dataset 可读者查询相关 session 行；详情可读取会话 owner 的 QA/trace，和“默认个人私有会话”产品承诺不同，需要调整。[session 可见性](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/session_lifecycle/metrics.py#L334-L376)。

Cognee MCP 的 clientInfo.name 用于默认 dataset 命名，启动时可共用一个后端 client/key。这不能区分企业用户身份。远程 MCP 需验证每个用户/服务主体，再调用同一平台 Policy；未完成时只能作为同一可信主体的入口。[MCP 默认空间](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee-mcp/src/server.py#L928-L953)。

公共产品不透传 forget everything。该路径存在无用户范围的 cache prune，共享后端时会影响他人缓存，应改成范围明确的删除作业。[forget 清理路径](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/v1/forget/forget.py#L191-L218)。

## 7. 最小平台数据模型

以下为概念模型，尚非已实施迁移：

| 对象 | 核心键/约束 | 不可忽略的规则 |
|---|---|---|
| Tenant、Membership | tenant_id；tenant+principal 唯一 | 成员停用和租户停用均进入授权判断 |
| Group、GroupMember | tenant+group；tenant+group+principal | 复合外键保证成员、组来自同租户 |
| Space、SpaceGrant | tenant+space；subject+action+space | 不用可重名的 display_name 当身份 |
| Credential | id、secret digest、principal、tenant、scopes、expires/status | key 只能缩小权限；明文仅创建时展示 |
| Binding | tenant+space+revision、provider、cell、native_scope、secret_ref | 客户端不能任意选择 native_scope |
| SourceVersion | tenant+space+source+revision、hash、object version、tombstone | 不同租户相同文件 hash 不形成共享授权 |
| Job/Attempt | scope+idempotency_key；request digest；固定 binding | 重试次数与一次业务操作身份分开 |
| Session/SessionGrant | tenant+owner+session、scope、policy revision | 会话、trace、QA 向量清理一致 |
| Usage/Audit | 唯一事件 ID、tenant、operation/attempt、模型 profile | 客户计费策略与供应商重复调用成本分别记账 |

所有以 tenant 为范围的父子关系都使用包含 tenant 的约束，避免“有 tenant 列但关联到了别租户空间”。身份全局表不机械套同一 tenant 过滤；授予跨租户运维权限必须显式、可审计。
