# 03：Cell、实际存储、权限与两种部署形态

逻辑隔离需要落到数据库、文件、对象和凭据。以下选择将当前能复用的接点与必须新增的能力分开；并不宣称配置后已经达到目标负载或 SLA。

## 1. Cell 如何工作，为什么先按部署隔离配置

Cell 是一组固定的运行时、网络访问、关系库、图/向量资源、会话和对象端点。平台保存 SpacePlacement，将每个空间路由到一个明确 Cell。新建空间按健康、容量、区域和 Provider 能力放置；旧空间不因扩容自动改哈希路由。

当前 Cognee 只有部分 graph/vector 配置在 Dataset ContextVar 中，SQL/cache/S3 和 handler 凭据仍有全局依赖。首版每 Cell 用独立部署与固定配置，避免一个进程临时切环境变量服务多个存储组。未来若要同进程连接多 Cell，必须一起改这些工厂和缓存键，不能只改图库 URL。

好处：可复用多数原生工厂，单 Cell 故障影响可界定，企业专属部署沿用相同接口。代价：更多部署与连接池，跨 Cell 迁移需要受控流程。

## 2. 图 6：首版全开源存储布局

示例中 D_F 和 D_R 是内部 Dataset UUID 的简写；ds_DF 等 schema 也是说明性别名，实际命名由 handler 规范化 UUID 决定。对象前缀是平台拟新增规则，非当前 Cognee 默认路径。

~~~mermaid
flowchart TB
  Catalog["平台目录<br/>甲/F → C1 / Owner01 / D_F<br/>甲/R → C1 / Owner02 / D_R<br/>乙/X → 专属C2"]:::platform
  Catalog --> PGPlatform[("平台 PostgreSQL<br/>Tenant、Group、Space、Grant<br/>SourceVersion、Binding、Job")]:::oss

  subgraph C1["Cell C1：甲公司等共享租户"]
    O1["Owner01：固定Cognee执行实例<br/>F的查询、导入、删除与维护"]:::platform
    O2["Owner02：固定Cognee执行实例<br/>R的查询、导入、删除与维护"]:::platform
    G1[("Ladybug 图文件 D_F.lbug<br/>存于部署卷1")]:::oss
    G2[("Ladybug 图文件 D_R.lbug<br/>存于部署卷2")]:::oss
    PGC[("pg-c1 / cognee_c1<br/>共享原生用户、ACL、Dataset元数据<br/>ds_DF：F向量；ds_DR：R向量")]:::oss
    Session[("pg-c1 / sessions_c1<br/>独立会话库<br/>平台租户/用户/空间范围映射")]:::oss
    Obj[("对象服务 / memory-c1<br/>tenants/甲/spaces/F/source/文档/版本<br/>tenants/甲/spaces/R/source/文档/版本")]:::oss
    O1 --> G1
    O2 --> G2
    O1 --> PGC
    O2 --> PGC
    O1 --> Session
    O2 --> Session
    O1 --> Obj
    O2 --> Obj
  end
  Catalog --> O1
  Catalog --> O2
  Catalog --> C2["专属Cell C2<br/>乙的独立实例配置、凭据、库/卷和对象范围"]:::platform
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
~~~

“共享 Cell”不等于所有企业共用一个图；图按 Dataset 分文件，向量按 Dataset 分 schema。一个 owner 可管理多个空间；为了控制故障范围应限制活跃空间数、引擎缓存和连接。此方案的水平扩展单位是更多完整空间/owner，不是同一个图文件的多个写副本。

## 3. 数据到底存什么、谁拥有它

| 数据 | 实际位置示例 | 所有者与权限要求 |
|---|---|---|
| 企业、组、空间授权 | 平台 PostgreSQL 平台表 | 平台 Policy 唯一权威；tenant 复合约束，可加入 RLS |
| 作业、绑定、原件目录、删除记录 | 同平台库的 scoped tables | 原件目录与 Job 同事务；状态机及 CAS 更新 |
| Cognee 用户/ACL/Dataset/来源元数据 | C1 PostgreSQL 的 cognee_c1 | 引擎原生表，权限投影与平台映射；不能声称已有 tenant RLS |
| 图 | Owner01 持久卷中 owner_uuid/D_F.lbug | 仅受控 owner 打开；原生 Dataset.owner_id 与执行 owner 不同 |
| 向量 | pg-c1/cognee_c1 内 ds_<dataset_uuid> | pgvector_shared；需改 handler 才能使用有范围的运行账号 |
| 原件与版本 | memory-c1/tenants/<t>/spaces/<s>/source/<d>/<rev>/ | 平台稳定版本目录；IAM和签名能力限制，前缀本身不是授权 |
| 派生中间产物 | .../builds/<attempt>/ 或 generations/<g>/ | 处理 profile 和来源 manifest；失败产物可回收 |
| 会话/trace | sessions_c1 专用库及对应 QA 向量 | 平台 scope 映射与会话授权覆盖所有路径；不能只处理 cache 表 |
| 答案缓存 | 带 tenant/principal-policy/space-binding/model/strategy 的键 | 首版不做跨租户内容缓存复用；撤权和删除失效 |
| 审计/用量 | tenant scoped 元数据账本 | 不记录密钥，正文与 trace 不默认进入公共运维日志 |
| 备份 | 对象存储中的受限备份与恢复 manifest | 图 checkpoint、向量/SQL版本、原件和tombstone共同对账 |

当前 pgvector_shared 使用关系库配置，不是 VECTOR endpoint 指向的任意独立 PostgreSQL。若要向量单独数据库服务，应使用另一受支持 handler 或定制绑定；不能只改环境变量名期待所有路径生效。[shared handler](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/pgvector/PGVectorSharedDatasetDatabaseHandler.py#L39-L87)。

当前 raw root 依赖 owner 当前 tenant。平台要将 object URI 固化在 SourceVersion/manifest 中，并适配文件上下文，避免用户切企业或离职改变原始资料定位。[当前上下文](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/context_global_variables.py#L255-L348)。

对象服务绿色表示可采用开源 S3 兼容实现，例如 Ceph RGW 候选；本轮未测试兼容性，须验证 Cognee 所用上传、列举、删除及 IAM 语义。若选择云对象服务或托管卷，应另按商业服务计费和能力评估，不能将 S3 协议或部署卷本身当成开源产品。

## 4. 数据库权限如何真正限制访问

首版应将资源开通与运行时账号分离，但这是一项实际改造，不是命名几个账号就完成：

| 凭据 | 可做什么 | 不应该出现在哪里 |
|---|---|---|
| Provisioner | 建库/schema、受控迁移、建 collection/index、分配角色 | 公网 API、普通查询、作业消息内容 |
| Runtime reader | 读取已批准图/向量与必要元数据 | 系统库管理和建删其他空间 |
| Runtime writer | 在限定资源写入、删除、维护 | 未授权 Cell/租户资源和平台权限表 |
| Session service | 范围化会话读写和清理 | 图数据库管理；共享实例全局 prune |
| Object workload role | 指定 Cell/租户对象范围 | 任意 bucket、用户可修改的 endpoint |
| Break-glass 运维 | 有期限的维修或恢复操作 | 常驻业务凭据；无审计全局访问 |

Neo4j 默认 handler 用全局 graph 凭据建 database，又将相同凭据交普通 adapter；PGVector shared 也用关系账号建 schema并执行运行时访问。没有自动完成每租户 GRANT/REVOKE。[Neo4j 解析](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/neo4j_driver/Neo4jDatasetDatabaseHandler.py#L53-L95)、[PG schema 创建](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/postgres/admin.py#L138-L172)。

新的 handler 从可信 Binding 解析 runtime secret_ref；建资源走管理身份。PGVector collection 首次创建仍涉及 DDL，因此要预建所需表/索引，或新增受控 DDL 服务；直接撤 CREATE 可能使首次摄取失败。[collection 初始化](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py#L322-L369)。

新增平台表可用 tenant RLS 加固：每事务从已认证上下文设置 tenant，事务外缺范围默认拒绝；运行角色不是 owner/superuser/BYPASSRLS，迁移角色分离，连接归还后不能残留租户上下文。这样减少遗漏 WHERE 的风险；共享服务若已被攻陷并可随意选择 tenant，RLS 不自动成为进程之间的硬边界。[PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)

有权限的数据库账号可以访问不同 schema，search_path 不是权限系统。[PostgreSQL Schema 权限](https://www.postgresql.org/docs/current/ddl-schemas.html)。因此隔离等级必须诚实标明：

- **共享标准模式**：平台 ACL、Dataset 分隔、范围化数据账号和 Cell 网络限制；平台及 Cell 服务是受信系统，不承诺被攻陷的共享运行时完全不能影响同 Cell 客户。
- **专属数据面模式**：一个租户使用独立 Cell、SQL/会话库、图/卷、对象凭据与运行池；若承诺主机/网络层独占，还需专属节点/网络/账号。平台控制面仍可能共享，不能宣传整个系统完全物理独占。
- 尚未完成 runtime 最小权限改造的环境，只能按受限试运行环境评估，不把现有管理员连接方式包装成成熟数据库租户隔离。

## 5. 图 7：需要独立扩容查询时的远程图方案

~~~mermaid
flowchart TB
  Route["已授权Space Binding"]:::platform --> Query["查询 Runtime 副本<br/>Cognee Retriever / Answer"]:::cognee
  Route --> Jobs["持久Job与Space写协调"]:::platform
  Jobs --> Ingest["导入 Runtime<br/>Cognee Pipeline"]:::cognee
  Query --> Graph[("Neo4j Enterprise<br/>按Dataset的database")]:::commercial
  Ingest --> Graph
  Query --> Vector[("PostgreSQL + PGVector<br/>按Dataset的schema")]:::oss
  Ingest --> Vector
  Query --> Gate["共享查询准入/发布协议<br/>跨副本permit或固定协调者"]:::platform
  Ingest --> Gate
  Provision["资源开通与DDL控制器"]:::platform --> Graph
  Provision --> Vector
  classDef cognee fill:#DBEAFE,stroke:#2563EB,color:#172554;
  classDef oss fill:#DCFCE7,stroke:#15803D,color:#14532D;
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef commercial fill:#F3E8FF,stroke:#9333EA,color:#581C87;
~~~

这条路径不用各查询 Pod 打开同一嵌入式文件，允许读取计算独立扩容。跨图/向量的一致性和同空间写协调仍需平台实现。blocked_in_place 模式的准入必须覆盖所有查询副本，不能复制每进程本地读写锁后宣称全局屏障；可以先将每空间准入固定到唯一协调者，或实现持久分布式读 permit 与失败处置。

读副本失联或 permit TTL 到期不证明读取已结束。写者进入前须确认旧查询停止、隔离其执行环境，或使用已经验证的快照读取及输出拒绝协议；无法证明时保持 blocked。固定协调者重启也必须恢复未决 permit，不能清空内存计数后当作所有读者已经退出。输出 fence 还需覆盖最后一个响应字节，不能只保护数据库查询阶段。

这是可选商业路线，不是给用户默认采购许可。适用前提是目标 Neo4j 产品/版本允许当前 handler 所需的 CREATE DATABASE；不能笼统说所有 Aura 方案均兼容。当前另有 neo4j_community handler，但实现是每 Dataset 一个 Docker 容器、volume及本机端口，不是同一 Community 实例多库，也不是即插即用的 Kubernetes 管理服务。[Community handler](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/graph/neo4j_driver/Neo4jCommunityDatasetDatabaseHandler.py#L68-L158)

不将 PostgreSQL graph demo 作为已成熟的默认图方案；若要求完全开源、远程图库、同等图能力和高可用同时成立，需要单独做适配与运行验收。

## 6. 多个资源组如何扩容和迁移

| 动作 | 具体步骤 | 不能做什么 |
|---|---|---|
| API 扩容 | 增副本，所有请求仍经同一 Policy/Binding；连接限额保持 | 以 API 副本数推断图写吞吐提高 |
| 增 owner | 注册容量，优先放新 Space | 让多个实例同时打开原图文件 |
| 增 Cell | 建独立配置/端点/凭据和健康状态，Placement放新空间 | 用重算哈希立即移动所有存量请求 |
| 搬迁 Space | drain→停写/隔离→复制或重建→核验→原子切binding→旧资源回收 | 只改 graph endpoint、不改向量/来源/会话清单 |
| 热 Space | 限制预算与队列，增加单实例资源或迁远程后端 | 声称整个 Dataset 已自动跨 Cell 分图 |
| 专属升级 | 开通专属 Cell，迁移数据、会话、权限映射与在途作业 | 只给专属Pod但继续共享不受限数据库账号 |

首版每 Cell 一个 Kubernetes namespace 可用于部署管理，网络默认拒绝后按服务放行，设置 ResourceQuota/requests/limits。它限制 Cell 资源与通信，不替代企业 Group/Space 权限；也不解决共享 Cell 内的租户公平调度。后者需 Job 调度器按 tenant 并发、token预算、队列年龄分配容量。

## 7. 会话、缓存与池大小是实际容量的一部分

当前 SQL cache 核心键是 user_id+session_id，没有独立 tenant/space 字段。平台需把复合 scope 映射成稳定内部身份，并覆盖所有历史读取、trace、清理及 SessionQAVector 路径。不能只迁 SQL cache，却让 QA 向量继续指向旧空间。

SqlCacheAdapter.prune 无条件删除五类 cache 表数据；Redis prune 使用 FLUSHDB，均不是租户级删除。[SQL prune](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/cache/sql/SqlCacheAdapter.py#L1196-L1215)。独立 session 数据库降低误操作影响，仍需范围化清理。

pgvector_shared 每个活跃 Dataset 可有自己的 engine/pool。容量按实际连接的所有进程与 schema 汇总：

连接上界预算 ≈ Σ每个进程活跃 engine 的(pool_size + max_overflow)，再加元数据、会话和管理连接。

原生默认值不是租户容量承诺。应限制活跃 Dataset、连接池、fan-out、引擎缓存与作业并发，并测冷启动与驱逐。共享数据库并不意味着只有一个连接池。

## 8. Hindsight 放进物理架构时改变什么

平台外部 API、Group/Space、来源版本、作业目录仍保留；绑定切到另一个 Hindsight Cell：

- 企业可映射租户 schema，Space 映射 schema 内 bank；平台仍负责 bank 级权限。
- 事实、实体、关系、向量与全文主要在 Hindsight PostgreSQL 内，不继续经过 Cognee Graph/Vector Adapter。
- API 与原生 Worker 分开部署，平台跟踪原生 operation，不重复调度其内部任务。
- 默认租户扩展无认证；已有 StaticKeys/Supabase扩展需正确打包和映射企业多成员共享。不能仅把 bank_id 放进 URL 就宣布完成隔离。
- 同样要验证运行角色、schema边界、派生记忆删除与恢复，不因用另一引擎免除这些责任。

固定 SHA 的原生实现与限制见 [Hindsight 存储/Worker 核查](../evidence/11-hindsight-runtime.md)。平台可变的是 Binding 和 Provider 适配；数据库不是以同一种图表结构直接互换。
