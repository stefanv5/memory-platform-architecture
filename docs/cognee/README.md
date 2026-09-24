# Cognee 商业化架构研究

本轮基线：Cognee `663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`；Hindsight `12f2d54f643baddacb98cd547c89b1a50c5c3dcc`。

- [图存储、模块数据流与 PostgreSQL 替换分析](graph-storage-postgres-analysis.md)：图的模块用途、节点与关系模型、向量和图的交互、PG 适配机制、功能边界及华为云 RDS 兼容条件。
- [PG 图能力的具体实现、改造清单与工作量](postgres-graph-implementation-plan.md)：节点/边/来源如何落表，反馈、truth、局部更新和时间检索的接口契约，生产化问题、华为云落地与分档人日估算。
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
