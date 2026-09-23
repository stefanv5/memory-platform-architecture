# V6：现有部署模板与自动扩缩容依据

本轮补查目的：区分能被Serverless平台托管、可伸缩应用集群，以及数据库/任务一致性。

## 当前仓库已有入口

- [Modal模板](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/distributed/deploy/modal_app.py#L22-L58)：创建ASGI应用，挂载共享Modal Volume到/data，配置请求超时、空闲回收和单容器并发。它安装未固定版本的发布包，不能把模板中的pip安装结果当作本次SHA。
- [部署说明](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/distributed/deploy/README.md#L21-L40)：已有serverless部署定位，生产建议Postgres+PGVector；这仍不解决默认嵌入式图的多容器写协议。
- [Fly配置](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/distributed/deploy/fly.toml#L23-L29)：auto stop/start、min_machines_running=0；说明可配置闲置停止，不是已验证的跨节点任务恢复。

静态结论：不能说Cognee完全没有Serverless部署支持；也不能由这些模板推导已经具备多租共享文件并发写、持久Job和安全故障接管。本轮未部署模板，未验证当前Modal SDK参数兼容性。

## 官方基础设施语义

- [Modal Volume](https://modal.com/docs/guide/volumes)：文档描述commit/reload可见性，并要求避免多个容器并发修改同一文件；不能将挂载该卷视作嵌入式数据库分布式锁。
- [Knative Request Flow](https://knative.dev/docs/serving/request-flow/)：共享Activator在容量为零/不足时接住请求并等待Pod就绪；这解释谁负责从零唤醒HTTP服务。Activator不是持久业务任务队列。
- [Knative scale-to-zero](https://knative.dev/docs/serving/autoscaling/scale-to-zero/)：KPA相关配置支持缩零；产品可为时延保留最小热副本。缩零不是客户不管理服务器的唯一条件。
- [KEDA PostgreSQL scaler](https://keda.sh/docs/2.20/scalers/postgresql/)：可用返回单个数值的SQL查询生成伸缩指标。读取任务数量不等于领取任务；claim、公平性与幂等由应用实现。
- [KEDA长任务注意事项](https://keda.sh/docs/2.20/concepts/scaling-deployments/#long-running-executions)：Deployment缩容可能终止正在处理长任务的Pod；应实现生命周期处理或采用Jobs。
- [KEDA ScaledJob](https://keda.sh/docs/2.20/concepts/scaling-jobs/)：可以按事件创建处理完成后退出的Job；故障、重试及副作用仍需应用协议。

实际阅读：modal_app.py全文；distributed/deploy/README.md前48行；fly.toml全文；其他部署文件仅rg定位。没有泛称覆盖所有分布式实现，也没有用部署目录名字证明产品集群能力。
