# 叙事与报告结构

输入的资料先需要可寻址保存，才可交给异步worker；worker的成功只有在图、向量和元数据状态一致时才有业务意义；多份状态及租户路由共同决定横向扩容边界。

报告内容：结论及版本边界；Skill评价；当前输入/构建/读取时序；存储与隔离矩阵；具体失效场景及证据；目标架构（复用优先）；幂等/租约/版本发布/补偿协议；拓扑与后端取舍；商业平台补齐项；可独立验收的实施顺序；故障注入和容量模型；未决问题。

原理来源限第一方：FastAPI background tasks、Python asyncio gather、PostgreSQL SELECT SKIP LOCKED、AWS transactional outbox/saga/idempotency、Temporal Activity定义、Kubernetes volume及probe、Neo4j官方数据库管理。当前仓库行为仍以本地代码为准。
