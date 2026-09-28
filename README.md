# Memory Platform Architecture

基于源码的 AI 记忆平台架构研究：Cognee 云化、多租户、集群、持久化与可替换引擎设计。

- [竞品全景与 ContextDB 架构（最新）](docs/competitive-landscape/README.md)
- [Cognee 与 Hindsight：当前架构、代码实现及演进对比](docs/competitive-landscape/05-code-architecture-cognee-vs-hindsight.md)
- [Cognee 分析目录](docs/cognee/README.md)
- [初学者图解：两份文档怎样用到3种APOC、7种GDS，AGE怎么替代](docs/cognee/apache-age-apoc-gds-walkthrough.md)
- [Cognee 图存储与 PostgreSQL 替换分析](docs/cognee/graph-storage-postgres-analysis.md)
- [Cognee PG 图能力：数据模型、改造位置与工作量](docs/cognee/postgres-graph-implementation-plan.md)
- [Cognee × AGE：版本缺口、PG/华为云条件、逐项改造与工时（最新）](docs/cognee/apache-age-compatibility-matrix.md)
- [Cognee 接入 Apache AGE：版本、图能力、性能机制与华为云边界](docs/cognee/apache-age-feasibility-analysis.md)
- [V6：共享集群与 Serverless（最新）](docs/cognee/v6-cluster-serverless/architecture.md)
- [V5：多租户产品端到端落地](docs/cognee/v5-product-saas/README.md)
- [V4：Cognee / Hindsight 插件边界](docs/cognee/v4-engine-plugins/architecture.md)

区分源码事实、建议设计与待验证假设。本文档仓库不包含生产实现，不以静态分析替代运行、隔离或性能验收。
