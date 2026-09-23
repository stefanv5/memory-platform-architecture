# 一手资料与适用边界

外部资料访问日期2026-09-23。均用于设计原理或供应商能力边界，不用于代替commit 663a2dc的实现证据。

1. FastAPI后台任务：https://fastapi.tiangolo.com/tutorial/background-tasks/ 。进程内任务与多服务器worker职责不同。
2. Python asyncio：https://docs.python.org/3/library/asyncio-task.html#running-tasks-concurrently 。默认gather首次异常传播，其他awaitable仍可继续；用于构建失败与补偿并发的风险推导，尚未故障复现。
3. AWS transactional outbox：https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html 。SQL业务记录与投递意图同事务，消费者仍需去重。
4. AWS幂等API：https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/ 。明确请求意图的idempotency key，区别内容相同和同一次操作重试。
5. AWS saga：https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/saga-orchestration.html 。跨资源采用局部提交+补偿，不伪称全局ACID。
6. PostgreSQL SELECT：https://www.postgresql.org/docs/current/sql-select.html 。SKIP LOCKED适用于多消费者队列表，不能当通用一致性快照；claim事务需短且提交后有持久lease。
7. Temporal Activity：https://docs.temporal.io/activity-definition 。耐久执行并不免除活动幂等；写成功但未确认仍会重试。
8. Kubernetes volume：https://kubernetes.io/docs/concepts/storage/persistent-volumes/ 。RWX为访问能力，不自动赋予数据库并发安全。
9. Kubernetes probes：https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/ 。区分进程活性、可接流量、启动等待。
10. Neo4j database create：https://neo4j.com/docs/operations-manual/current/database-administration/standard-databases/create-databases/ 。文档注明Enterprise及Aura限制；具体托管套餐与仓库5.x驱动兼容必须实测，不泛化为任意Aura支持CREATE DATABASE。
11. Cognee架构介绍：https://www.cognee.ai/how-cognee-builds-ai-memory 。作为图/向量/关系协作的产品定位参考。
12. Cognee Cloud官方API文档：https://docs.cognee.ai/api-reference/introduction 。托管平台宣称的可扩展性不等于开源checkout包含对应control plane。

项目场景：企业文档/代码/会话需要可持续积累、图关联和混合检索；抽取、检索、改进共用存储和任务约定是它的复用价值。商业化问题不只“能查”，还要保证任务不丢、租户不串、删除可验证、成本可归因。

本轮不做市场竞争排名、未经证实的组织动机推测或云报价。用户关注代码及集群落地，产品背景保持简短。
