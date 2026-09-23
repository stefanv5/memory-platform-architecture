# 部署、迁移和运维边界

流水线与存储的可插拔性使应用能搬上云，但容器启动配置仍需与作业所有权、持久化范围相配合。

- entrypoint.sh:57-64：dev/local与生产均gunicorn -w 1；默认超时30000秒不能替代持久任务队列。Docker compose是本地开发拓扑，包含源目录挂载、ENV=local、默认用户密码及默认SQLite，并非HA证明。
- docker-compose.yml:7-9、cognee/api/client.py:82-88,149-173：stop grace默认15秒，background drain默认8秒，随后关闭数据库engine。终止预算必须协同，已存在优雅关闭基础，但需execution模块核实哪些task实际注册。
- cognee/api/client.py:98-107,129-136：启动迁移、stale run恢复、解析认证secret。多副本启动产生业务动作，不能当成纯无状态boot。
- cognee/modules/migrations/runner.py:86-109,149-164：已有PG session advisory lock，跨主机互斥；SQLite FileLock作用为同机共享文件。不能说迁移完全缺少分布式保护。
- cognee/modules/migrations/startup.py:289-324：关系迁移和graph/vector数据链在同一全局migration lock内依次运行。:386-451支持ENABLE_AUTO_MIGRATIONS=false及CLI显式upgrade，迁移失败按dataset阻断写入。迁移锁只锁迁移调用；不能从此推导它与活跃业务写入互斥。建议生产发布独立迁移Job及版本准入，必要时停止写入，保留现有锁。
- cognee/api/v1/health/routers/get_health_router.py:13-34：/health调用后端探测，失败返回503。health.py:250起对SQL/图/向量/文件系统检查；文件存储检查会写删测试对象，detailed还检查LLM/embedding。建议拆轻量liveness与有超时、缓存的readiness；依赖短暂失败不应造成全体Pod重启。
- cognee/shared/rate_limiting.py:17-33,64-83：LLM/embedding AsyncLimiter为进程内全局；多个Pod使用同一个provider key时每个独立限流。需按provider credential+tenant全局预算，并与本地并发上限分层。
- cognee/modules/observability/metrics.py:29-49,154-210：已有OpenTelemetry memory指标与OTLP接缝；缺SDK/未setup时no-op。不能把源代码有metric等同部署已采集。扩展job_age/lease_conflicts/retry/deadletter/ready_version差距/tenant成本指标。
- cognee-mcp/src/server.py:1031-1078,1098-1100：API模式可复用远端API并跳过本地迁移；cognee_client.py:127-146支持Bearer或X-Api-Key。推荐MCP作为受认证边缘适配层连API；现有单client配置不足以自动给多租户共享MCP提供每请求身份，需要明确部署和身份传递。
- distributed/deploy/modal_app.py：共享volume挂/data并允许并发，安装PyPI包未固定当前commit；没有指定远程graph/queue协议。Fly模板volume保留、可缩至0。Railway模板仅PG关系+向量，图未覆盖；Render磁盘挂/data，而根Dockerfile默认/cognee-storage，需显式核对路径。这些是托管起步模板，不能作完整集群实现证据。

建议拓扑先实现网络化状态与独立worker，再增加API副本；专属实例保留嵌入式后端时，明确一份database单写拥有者、PVC调度、冷恢复和备份约束，不能多个replica直接共享目录。Kubernetes只管理Pod/卷身份，不给应用多存储事务或租户配额。

阅读范围：entrypoint.sh、Dockerfile、docker-compose.yml、distributed/deploy/{modal_app.py,fly.toml,railway-template.json,render.yaml}及health router完整读取；client.py:80-183及关键位置检索；health.py、migration startup/runner、OTel metrics为选段研究，不宣称整个运维模块已读完。
