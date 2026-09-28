# Cognee 商业化架构研究

本轮基线：Cognee `663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`；Hindsight `12f2d54f643baddacb98cd547c89b1a50c5c3dcc`。

- [初学者先读：用两份项目文档拆解APOC、GDS与AGE替代](apache-age-apoc-gds-walkthrough.md)：从张三重复入图、集合合并和关系写入，到计算副本、连通分量、最短距离和聚类系数，逐个解释3种APOC与7种GDS；包含图示和输入/输出。
- [Apache AGE 兼容矩阵与逐项改造（最新）](apache-age-compatibility-matrix.md)：按 AGE＋PG 发布包列明功能与缺口；详解UUID/业务边唯一性、3种APOC和7种GDS调用缺失后的具体后果、替代成本与工时归属；盘点47个图接口，按全Agent开发列出并行依赖、Agent小时及3～6自然日的初始基础交付预算。
- [图存储、模块数据流与 PostgreSQL 替换分析](graph-storage-postgres-analysis.md)：图的模块用途、节点与关系模型、向量和图的交互、PG 适配机制、功能边界及华为云 RDS 兼容条件。
- [PG 图能力的具体实现、改造清单与工作量](postgres-graph-implementation-plan.md)：节点/边/来源如何落表，反馈、truth、局部更新和时间检索的接口契约，生产化问题、华为云落地与工作范围；历史人工估算不适用于本项目当前Agent交付方式。
- [Apache AGE 真实执行机制与性能背景](apache-age-feasibility-analysis.md)：固定跳 JOIN、VLE 缓存、图存储机制与验证方案；完整版本兼容和工时结论以最新矩阵为准。
- [与 Hindsight 的当前架构及代码实现对比](../competitive-landscape/05-code-architecture-cognee-vs-hindsight.md)：接入、存储、检索、事务、后台演进、性能成本与扩展取舍；先独立介绍两边再比较。

| 版本 | 内容 | 状态 |
|---|---|---|
| [V6](v6-cluster-serverless/architecture.md) | 多租户共享计算、远程存储、临时写所有权、集群与 Serverless | 最新运行架构；修订默认部署路线 |
| [V5](v5-product-saas/README.md) | 产品入口、用户组、端到端数据流、存储隔离、资源组与交付验收 | 产品合同保留；默认运行拓扑由 V6 修订 |
| [V4](v4-engine-plugins/architecture.md) | 可持续演进、MemoryProvider、业务工作流与 Hindsight 替换 | 本次归档基线 |
| [V3](history/v3-components.md) | 组件来源标色与替换接点 | 主架构由 V4 修订 |
| [V2](history/v2-multitenancy.md) | 当前数据流、多租户和集群风险 | 保留源码依据 |
| [V1](history/v1-analysis.md) | 初始全景与风险研究 | 历史材料 |

[evidence](evidence/) 保存各轮定向分析笔记及阅读边界。历史笔记可能保留当时的待核查判断；设计结论以最新版本为准。源码链接固定到分析时的提交。
