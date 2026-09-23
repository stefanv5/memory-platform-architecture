# V5：从 Cognee 到可运营的多租户记忆产品

本版回答：客户看到什么、用户组在哪里授权、请求如何到达引擎、数据实际写在哪里、哪里隔离、为何如此，以及代码需要改什么。它延续 V4 的 Provider 边界，补齐产品和运行合同。

**建议首版：统一产品 API + 平台权限/空间目录 + 分组运行单元 Cell + Cognee 完整运行时。以空间作为相同资料读取权限的边界，固定执行实例承载嵌入式图的所有读写，用持久作业和查询准入屏障实现可靠更新。** 需要持续读可用时，再增加独立版本构建发布；需要查询独立扩容时，可选择远程图方案。不能将两种部署路径混在一张图里假装无差别。

## 阅读路径

| 文档 | 回答的问题 |
|---|---|
| [01 产品与身份](01-product-and-identity.md) | 对外 UI/API/MCP 是什么；企业、多组成员、空间、服务账号如何授权 |
| [02 端到端数据流](02-data-flows.md) | 开户、上传、查询、会话、更新、删除如何走完；失败与可见性如何定义 |
| [03 物理存储与部署](03-storage-and-cells.md) | Cell 内外的实际库、schema、图文件、对象、会话、凭据如何放置 |
| [04 交付与演进](04-delivery-and-evolution.md) | 哪些代码改造、先后顺序、Hindsight 切换、恢复与验收 |

各文档的图应按路径一起阅读：产品逻辑边界、执行顺序和物理拓扑不能由一张图兼任。

## 四个容易混淆的概念

| 概念 | 例子 | 用来解决什么 | 不代表什么 |
|---|---|---|---|
| Tenant 企业租户 | 甲公司、乙公司 | 客户身份、归属、计费、数据驻留和隔离等级 | 企业成员自动可读全部资料 |
| Group 用户组 | 甲公司财务组、研发组 | 给一组主体授权，减少逐人管理 | 一个物理数据库或计算集群 |
| Space 知识空间 | 财务制度、研发规范 | 统一读权限集合、资料版本、引擎绑定 | 某个永远不变的 Cognee Dataset UUID |
| Cell 运行资源组 | shared-cn-01、dedicated-b-01 | 固定运行配置、存储端点、容量与故障范围 | 天然一个租户；或自动跨租户数据库权限隔离 |

“多组”同时在两个地方实现：**业务用户组在平台目录和授权服务；多组运行资源在 Placement 目录、Cell 路由和独立部署。** Group 改成员不应该搬数据库，Cell 扩容也不应该改用户权限。

## 图 1：客户入口到运行资源组

颜色：🟦 当前 Cognee 开源组件；🟩 其他开源基础组件；🟧 需要实现的平台策略/适配；🟪 可选商业组件。相同颜色不表示已经集成或通过生产验收。

~~~mermaid
flowchart TB
  Browser["企业控制台<br/>组织、用户组、空间、资料、作业、问答"]:::actor
  Client["客户服务 / Agent<br/>SDK、REST、远程 MCP"]:::actor
  Browser --> Edge["TLS 入口 / 负载均衡"]:::oss
  Client --> Edge
  Edge --> API["平台 API 多副本<br/>认证、权限、配额、幂等"]:::platform
  IdP["Keycloak（开源身份系统示例）"]:::oss --> API
  API --> Policy["组织、用户组、空间授权<br/>服务凭证、会话授权"]:::platform
  API --> Directory["资源目录<br/>Space → Provider → Cell → Binding"]:::platform
  Policy --> Catalog[("平台 PostgreSQL<br/>目录、权限、作业、来源版本")]:::oss
  Directory --> Catalog
  API --> Query["查询协调 / 可信请求上下文"]:::platform
  API --> Jobs["持久作业调度<br/>可先轮询 SQL Job"]:::platform
  Jobs --> Catalog
  Query --> Router["Cell 路由<br/>绑定固定、健康检查、准入"]:::platform
  Jobs --> Router

  subgraph C1["共享 Cell C1：多个租户，受信运行域"]
    Owner["固定执行实例组<br/>所有嵌入式图读写经过 owner"]:::platform
    Cognee["Cognee 完整运行时<br/>add、cognify、retrieval、answer、维护"]:::cognee
    Local[("Ladybug 图文件<br/>底层卷为部署资源")]:::oss
    SQL[("C1 PostgreSQL<br/>Cognee 元数据、PGVector、独立会话库")]:::oss
    Object[("C1 对象存储<br/>原件、版本、备份清单")]:::oss
    Owner --> Cognee
    Cognee --> Local
    Cognee --> SQL
    Cognee --> Object
  end

  subgraph C2["专属 Cell C2：乙公司"]
    Dedicated["独立运行配置、身份与数据凭据<br/>独立存储和网络边界"]:::platform
  end
  Router --> Owner
  Router --> Dedicated
  Controller["资源控制器<br/>开通、排空、迁移、恢复、退役"]:::platform --> Directory
  Controller --> Owner
  Controller --> Dedicated
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
  classDef actor fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

控制台和 SDK 只认识企业、空间、资料、操作及证据，不暴露数据库地址、原生 Dataset/bank 或管理凭据。Cognee API/SDK 位于受控数据面，不是另一套可绕过产品授权的公网入口。

平台控制面保存产品目录和策略；Cell 执行记忆计算。两者是职责边界，首版可在少量服务内实现，不要求把每项职责拆成微服务。每 Cell 先固定 SQL/cache/object/model 配置，以部署边界解决当前 Cognee 全局配置依赖。

## 多租隔离落在什么地方

| 实施层 | 具体约束 | 为什么及好处 | 代价/边界 |
|---|---|---|---|
| 外部身份 | 每请求验证 principal，校验所选 tenant 和凭证 scope | 用户不能通过改 header 或工具参数切入他人企业 | 身份系统不能替代资料授权 |
| 业务授权 | Group/用户/服务主体对 Space 的 action grants | 权限独立于 Cognee，可替换引擎；企业内可分财务和研发 | 权限变更需撤缓存、处理排队任务 |
| 知识处理 | 不同读权限资料分 Space/原生 Dataset | 限制图遍历、摘要与模型上下文，避免先推理后过滤的泄漏 | 实体重复、空间数与跨空间查询增加 |
| 作业与运行时 | 固定 tenant、binding、版本和执行 owner；调度按租户预算 | 防错路由、旧作业覆盖、资源独占 | 需要持久状态及失败恢复 |
| 物理存储 | Dataset 文件/schema/database；对象 scope；专用会话库 | 降低数据混用和清理影响，便于迁移/备份 | 命名空间不等于最小权限账号 |
| 资源组 | Cell 独立配置、凭据、网络策略和容量上限 | 限制故障范围，允许扩容或客户专属部署 | 共享 Cell 内仍需应用与存储授权 |
| 产品输出 | 答案、证据下载、session、trace、缓存均按授权释放 | 堵住图存储之外的旁路 | 下载凭据有效期与在途请求要有明确合同 |

不把 namespace、目录前缀或 schema 命名误当万能安全沙箱。[AWS 租户隔离](https://docs.aws.amazon.com/whitepapers/latest/saas-architecture-fundamentals/tenant-isolation.html)与[Kubernetes 多租户](https://kubernetes.io/docs/concepts/security/multi-tenancy/)分别说明应用资源边界、网络及配额的重要性；其机制需要和产品授权组合使用。

## 基线与证据

Cognee SHA：663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e；Hindsight SHA：12f2d54f643baddacb98cd547c89b1a50c5c3dcc。当前源码已有 Dataset ACL/路由，不应从零重写；[官网也明确 Tenant 与 Dataset 数据边界不同](https://docs.cognee.ai/core-concepts/multi-user-mode/multi-user-mode-overview)。

本轮重点源码核查：[入口与授权](../evidence/12-product-auth.md)、[存储隔离](../evidence/12-storage-isolation.md)、[执行与恢复](../evidence/12-execution-lifecycle.md)。它们列出实际阅读区间及未覆盖范围。以下方案是待实施设计，不是已部署的 SaaS；未运行隔离攻击、模型调用、故障注入或性能测试。
